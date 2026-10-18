import CloseIcon from "@mui/icons-material/Close";
import StopCircleOutlinedIcon from "@mui/icons-material/StopCircleOutlined";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import LinearProgress from "@mui/material/LinearProgress";
import Typography from "@mui/material/Typography";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  type ArchiveContext,
  type ArchiveTask,
  archiveError,
  createTransfer,
  getTransferByRequest,
  getTransfers,
  isTransferActive,
  stopTransfer,
  type TransferList,
} from "./archiveApi";
import { tokens } from "./theme";

const TASK_KEY = ["archive-transfers"];
const PENDING_KEY = "filmpaw-transfer-requests";
type Binding = Pick<
  ArchiveContext,
  "local_dir" | "subdir" | "performer_id" | "performer_name" | "source_path" | "target_path"
>;
type SubmitBody = Parameters<typeof createTransfer>[0];
type Submission = SubmitBody & { binding?: Binding };
type PendingRequest = { body: SubmitBody; binding?: Binding; state: "sending" | "unknown" };

function restoreRequests(): PendingRequest[] {
  try {
    const saved: PendingRequest[] = JSON.parse(sessionStorage.getItem(PENDING_KEY) ?? "[]");
    return saved
      .filter(
        (item) => item?.body?.request_id && item.body.context_id && Array.isArray(item.body.names),
      )
      .map((item) => ({ ...item, state: "unknown" }));
  } catch {
    return [];
  }
}

function overlaps(a: Binding, b: Binding): boolean {
  const normalize = (path: string) => path.replaceAll("/", "\\").replace(/\\+$/, "").toLowerCase();
  const paths = (binding: Binding) =>
    [binding.source_path, binding.target_path].filter(Boolean).map(normalize);
  if (a.performer_id === b.performer_id) return true;
  return paths(a).some((left) =>
    paths(b).some(
      (right) => left === right || left.startsWith(`${right}\\`) || right.startsWith(`${left}\\`),
    ),
  );
}
const PHASES = {
  queued: "等待执行",
  preflight: "正在预检",
  copying: "正在复制",
  verifying: "正在校验",
  committing: "正在保存到片库",
  cleaning: "正在清理本地",
  finished: "已结束",
};

export function transferLabel(task: ArchiveTask): string {
  if (task.stop_requested && isTransferActive(task)) return "正在安全停止";
  if (task.status === "queued") return "等待执行";
  if (isTransferActive(task)) return PHASES[task.phase];
  if (task.conflicts.length) return "同名冲突 · 队列已停止";
  if (task.items.some((item) => item.status === "cleanup_pending"))
    return "已复制 · 本地清理未完成";
  return {
    completed: "归档完成",
    stopped: "任务已停止",
    failed: "归档未完成",
    needs_review: "任务中断 · 待检查",
    queued: "正在预检",
    running: "正在传输",
  }[task.status];
}

export function formatBytes(value: number | null | undefined): string {
  if (value == null) return "未统计";
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

function useController(submissionsBlocked: boolean) {
  const client = useQueryClient();
  const [pendingRequests, setPendingRequests] = useState<PendingRequest[]>(restoreRequests);
  const requestsRef = useRef(pendingRequests);
  const [dismissedId, setDismissedId] = useState<string | null>(null);
  const [requestedTask, setRequestedTask] = useState<ArchiveTask | null>(null);
  const [stoppingIds, setStoppingIds] = useState<string[]>([]);
  const [stopErrors, setStopErrors] = useState<Record<string, string>>({});
  const changeRequests = useCallback((change: (current: PendingRequest[]) => PendingRequest[]) => {
    const next = change(requestsRef.current);
    requestsRef.current = next;
    setPendingRequests(next);
    try {
      sessionStorage.setItem(PENDING_KEY, JSON.stringify(next));
    } catch {
      /* Memory state still protects this session. */
    }
  }, []);
  const latest = useQuery({
    queryKey: TASK_KEY,
    queryFn: ({ signal }) => getTransfers(signal),
    // Browser Internet reachability does not determine whether the local
    // 127.0.0.1 sidecar can report or safely stop an active queue.
    networkMode: "always",
    retry: false,
    refetchInterval: (query) => (query.state.data?.busy || pendingRequests.length ? 1000 : 5000),
    refetchIntervalInBackground: true,
  });
  const apply = useCallback(
    async (task: ArchiveTask) => {
      await client.cancelQueries({ queryKey: TASK_KEY });
      client.setQueryData<TransferList>(TASK_KEY, (current) => {
        const existing = [...(current?.active ?? []), ...(current?.history ?? [])].find(
          (item) => item.id === task.id,
        );
        // An accepted POST or recovery read can arrive after a newer poll.
        // A task never goes backwards from terminal to running, or running to queued.
        if (
          existing &&
          (existing.updated_at > task.updated_at ||
            (!isTransferActive(existing) && isTransferActive(task)) ||
            (existing.status === "running" && task.status === "queued"))
        )
          return current;
        const active = current?.active.filter((item) => item.id !== task.id) ?? [];
        const history = current?.history.filter((item) => item.id !== task.id) ?? [];
        if (isTransferActive(task) || current?.active.some((item) => item.id === task.id))
          active.unshift(task);
        else history.unshift(task);
        return {
          active,
          history,
          next_cursor: current?.next_cursor ?? null,
          busy: !!current?.busy || active.length > 0,
          blocked_reason: current?.blocked_reason ?? null,
          concurrency: current?.concurrency ?? 3,
        };
      });
      setDismissedId(null);
    },
    [client],
  );
  // Recover each exact request independently. null is inconclusive: the original
  // POST may still be in flight, and must never be replaced with a fresh ID.
  const unknownIds = pendingRequests
    .filter((item) => item.state === "unknown")
    .map((item) => item.body.request_id);
  const recovery = useQuery({
    queryKey: ["archive-transfer-recovery", unknownIds],
    enabled: unknownIds.length > 0,
    queryFn: async ({ signal }) =>
      Promise.all(unknownIds.map((id) => getTransferByRequest(id, signal))),
    networkMode: "always",
    retry: false,
    refetchInterval: 1000,
    refetchIntervalInBackground: true,
  });
  useEffect(() => {
    const recovered = recovery.data?.filter((task): task is ArchiveTask => task !== null) ?? [];
    if (!recovered.length) return;
    void Promise.all(recovered.map(apply)).then(() => {
      const ids = new Set(recovered.map((task) => task.request_id));
      changeRequests((current) => current.filter((item) => !ids.has(item.body.request_id)));
    });
  }, [recovery.data, apply, changeRequests]);
  useEffect(() => {
    const ids = new Set(
      [...(latest.data?.active ?? []), ...(latest.data?.history ?? [])].map(
        (task) => task.request_id,
      ),
    );
    if (requestsRef.current.some((item) => ids.has(item.body.request_id))) {
      changeRequests((current) => current.filter((item) => !ids.has(item.body.request_id)));
    }
  }, [latest.data, changeRequests]);
  const start = useMutation({
    mutationFn: ({ binding: _binding, ...body }: Submission) => {
      if (submissionsBlocked)
        throw Object.assign(new Error("清理进行中或状态待确认，请稍后提交。"), { status: 409 });
      return createTransfer(body);
    },
    networkMode: "always",
    retry: false,
    onMutate: ({ binding, ...body }) => {
      const savedBinding = binding
        ? {
            local_dir: binding.local_dir,
            subdir: binding.subdir,
            performer_id: binding.performer_id,
            performer_name: binding.performer_name,
            source_path: binding.source_path,
            target_path: binding.target_path,
          }
        : undefined;
      const wasUnknown = requestsRef.current.some(
        (item) => item.body.request_id === body.request_id && item.state === "unknown",
      );
      changeRequests((current) => [
        ...current.filter((item) => item.body.request_id !== body.request_id),
        { body, binding: savedBinding, state: "sending" },
      ]);
      return { wasUnknown };
    },
    onSuccess: async (task) => {
      await apply(task);
      changeRequests((current) =>
        current.filter((item) => item.body.request_id !== task.request_id),
      );
    },
    onError: (error, variables, mutationContext) => {
      // Network/server failures may occur after the server accepted the job.
      // Never automatically POST it again. Independent bindings remain usable.
      const status = (error as { status?: number })?.status;
      changeRequests((current) =>
        mutationContext?.wasUnknown || !status || status >= 500
          ? current.map((item) =>
              item.body.request_id === variables.request_id ? { ...item, state: "unknown" } : item,
            )
          : current.filter((item) => item.body.request_id !== variables.request_id),
      );
      client.invalidateQueries({ queryKey: TASK_KEY });
    },
  });
  const stop = useMutation({
    mutationFn: (id: string) => stopTransfer(id),
    networkMode: "always",
    retry: false,
    onMutate: (id) => {
      setStoppingIds((ids) => [...ids, id]);
      setStopErrors((errors) => ({ ...errors, [id]: "" }));
    },
    onSuccess: apply,
    onError: (error, id) => {
      setStopErrors((errors) => ({ ...errors, [id]: archiveError(error, "停止请求失败，请重试") }));
      void client.invalidateQueries({ queryKey: TASK_KEY });
    },
    onSettled: (_data, _error, id) => setStoppingIds((ids) => ids.filter((value) => value !== id)),
  });
  const activeTasks = latest.data?.active ?? [];
  const history = latest.data?.history ?? [];
  const tasks = [...activeTasks, ...history];
  const task = tasks[0] ?? null;
  const unresolved = pendingRequests.some((item) => item.state === "unknown");
  const hasPending = pendingRequests.some((item) => item.state === "sending");
  const unavailable = latest.isError || latest.isPending || !!latest.data?.blocked_reason;
  const isBindingBusy = (binding: Binding) =>
    activeTasks.some((task) => overlaps(task, binding)) ||
    pendingRequests.some((item) => !item.binding || overlaps(item.binding, binding));
  return {
    task,
    tasks,
    activeTasks,
    history,
    pendingRequests,
    hasPending,
    stoppingIds,
    stopErrors,
    active: !!latest.data?.busy || activeTasks.length > 0,
    unavailable,
    submissionsBlocked,
    blockedReason: latest.data?.blocked_reason,
    concurrency: latest.data?.concurrency ?? 3,
    nextCursor: latest.data?.next_cursor ?? null,
    cleanupBlocked:
      unavailable || !!latest.data?.busy || activeTasks.length > 0 || pendingRequests.length > 0,
    isBindingBusy,
    taskForBinding: (binding: Binding) =>
      tasks.find(
        (task) =>
          task.local_dir === binding.local_dir &&
          task.subdir === binding.subdir &&
          task.performer_id === binding.performer_id,
      ) ?? null,
    bindingPending: (binding: Binding) =>
      pendingRequests.some(
        (item) => item.state === "sending" && (!item.binding || overlaps(item.binding, binding)),
      ),
    unresolved,
    loading: latest.isPending,
    start,
    stop,
    refresh: latest.refetch,
    dismissed: task?.id === dismissedId && !isTransferActive(task),
    dismiss: () => setDismissedId(task?.id ?? null),
    requestedTask,
    requestTaskView: (requested = task) => setRequestedTask(requested),
    clearTaskView: () => setRequestedTask(null),
    retrySubmission: (requestId?: string) => {
      if (submissionsBlocked || unavailable) return;
      // Explicitly recover the already-authorized queue with the exact same
      // identity. A delayed first POST cannot create a second queue.
      const request = requestsRef.current.find(
        (item) => item.state === "unknown" && (!requestId || item.body.request_id === requestId),
      );
      if (request) start.mutate({ ...request.body, binding: request.binding });
    },
  };
}

const TransferContext = createContext<ReturnType<typeof useController> | null>(null);

export function TransferProvider({
  children,
  submissionsBlocked = false,
}: {
  children: ReactNode;
  submissionsBlocked?: boolean;
}) {
  const controller = useController(submissionsBlocked);
  return <TransferContext.Provider value={controller}>{children}</TransferContext.Provider>;
}

export function useTransfer() {
  const value = useContext(TransferContext);
  if (!value) throw new Error("TransferProvider is required");
  return value;
}

export function TransferProgress({ task }: { task: ArchiveTask }) {
  const known = task.total_bytes > 0 && task.phase !== "preflight" && task.phase !== "queued";
  const done = task.phase === "verifying" ? task.verified_bytes : task.progress_bytes;
  const percent = known ? Math.min(100, (done / task.total_bytes) * 100) : 0;
  return (
    <Box sx={{ minWidth: 0, flex: 1 }}>
      <Typography variant="body2" sx={{ fontWeight: 600 }} noWrap>
        {transferLabel(task)}
        {task.current_name ? ` · ${task.current_name}` : ""}
      </Typography>
      {isTransferActive(task) && (
        <LinearProgress
          aria-label={PHASES[task.phase]}
          variant={known ? "determinate" : "indeterminate"}
          value={percent}
          sx={{ my: 0.75, height: 5, borderRadius: 4 }}
        />
      )}
      <Typography variant="caption" color="text.secondary">
        已完成 {task.completed_count} / {task.total_count} 部
        {known ? ` · ${formatBytes(done)} / ${formatBytes(task.total_bytes)}` : ""}
      </Typography>
    </Box>
  );
}

export function itemLabel(status: string): string {
  return (
    {
      pending: "待处理",
      queued: "待处理",
      copying: "复制中",
      verifying: "校验中",
      committing: "保存中",
      cleaning: "清理本地中",
      completed: "已完成",
      moved: "已完成",
      stopped: "已停止",
      skipped: "未处理",
      failed: "失败",
      conflict: "同名冲突",
      cleanup_pending: "副本已保存，本地清理未完成",
      needs_review: "待检查",
    }[status] ?? status
  );
}

export function TransferTaskCard({
  task,
  onViewArchive,
  onViewLogs,
}: {
  task: ArchiveTask;
  onViewArchive?: () => void;
  onViewLogs?: () => void;
}) {
  const transfer = useTransfer();
  const [details, setDetails] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const active = isTransferActive(task);
  return (
    <>
      <Box
        role="article"
        aria-label={`归档任务 ${task.performer_name}`}
        sx={{
          flexShrink: 0,
          border: `1px solid ${tokens.lineStrong}`,
          borderRadius: 2,
          bgcolor: "background.paper",
          px: 2.5,
          py: 1,
          display: "flex",
          gap: 2,
          alignItems: "center",
          flexWrap: "wrap",
          minWidth: 0,
        }}
      >
        <Box sx={{ width: 180, minWidth: 0 }}>
          <Typography variant="body2" noWrap sx={{ fontWeight: 600 }}>
            {task.performer_name}
          </Typography>
          <Typography
            variant="caption"
            component="div"
            noWrap
            title={task.source_path}
            color="text.secondary"
          >
            从 {task.source_path}
          </Typography>
          <Typography
            variant="caption"
            noWrap
            component="div"
            title={task.target_path}
            color="text.secondary"
          >
            到 {task.target_path}
          </Typography>
        </Box>
        <TransferProgress task={task} />
        {onViewArchive && (
          <Button
            size="small"
            sx={{ whiteSpace: "nowrap" }}
            onClick={() => {
              transfer.requestTaskView(task);
              onViewArchive();
            }}
          >
            查看目录
          </Button>
        )}
        <Button size="small" sx={{ whiteSpace: "nowrap" }} onClick={() => setDetails(true)}>
          任务详情
        </Button>
        {onViewLogs && (
          <Button size="small" onClick={onViewLogs}>
            操作日志
          </Button>
        )}
        {active && (
          <Button
            size="small"
            color="inherit"
            startIcon={<StopCircleOutlinedIcon />}
            onClick={() => setConfirmStop(true)}
            disabled={task.stop_requested || transfer.stoppingIds.includes(task.id)}
            sx={{ whiteSpace: "nowrap" }}
          >
            {task.stop_requested ? "停止中…" : "停止传输"}
          </Button>
        )}
      </Box>
      {transfer.stopErrors[task.id] && (
        <Alert severity="error">{transfer.stopErrors[task.id]}</Alert>
      )}
      <Dialog open={details} onClose={() => setDetails(false)} maxWidth="md" fullWidth>
        <DialogTitle>归档任务 · {transferLabel(task)}</DialogTitle>
        <DialogContent dividers>
          <Typography variant="body2">
            {task.performer_name} · {task.source_label ?? "NAS 片库"}
          </Typography>
          <Typography
            variant="caption"
            component="p"
            sx={{ overflowWrap: "anywhere", fontFamily: tokens.mono }}
          >
            从 {task.source_path}
          </Typography>
          <Typography
            variant="caption"
            component="p"
            sx={{ overflowWrap: "anywhere", fontFamily: tokens.mono }}
          >
            到 {task.target_path}
          </Typography>
          {task.message && (
            <Alert severity={task.status === "completed" ? "success" : "warning"} sx={{ my: 1 }}>
              {task.message}
            </Alert>
          )}
          {task.conflicts.length > 0 && (
            <Alert severity="error" sx={{ my: 1 }}>
              同名冲突：{task.conflicts.join("、")}。处理冲突后，请刷新目录再重新选择。
            </Alert>
          )}
          <Box component="ul" sx={{ pl: 2.5, m: 0 }}>
            {task.items.map((item) => (
              <Box component="li" key={item.name} sx={{ py: 1, overflowWrap: "anywhere" }}>
                <Typography variant="body2" sx={{ fontWeight: 600 }}>
                  {item.name}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {itemLabel(item.status)}
                  {item.message ? ` · ${item.message}` : ""}
                </Typography>
              </Box>
            ))}
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDetails(false)}>关闭</Button>
        </DialogActions>
      </Dialog>
      <Dialog open={confirmStop} onClose={() => setConfirmStop(false)} maxWidth="xs" fullWidth>
        <DialogTitle>停止当前传输？</DialogTitle>
        <DialogContent>
          任务会在安全位置停止，不再处理后续目录。已完成的影片保留在
          NAS，未完成项目会在任务详情中列出。
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmStop(false)}>继续传输</Button>
          <Button
            color="error"
            onClick={() => {
              transfer.stop.mutate(task.id);
              setConfirmStop(false);
            }}
          >
            停止队列
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
}

export function TransferRecovery() {
  const transfer = useTransfer();
  return (
    <>
      {transfer.unavailable && !transfer.loading && (
        <Alert
          severity="warning"
          action={<Button onClick={() => transfer.refresh()}>重试查询</Button>}
        >
          {transfer.blockedReason || "归档任务状态暂不可确认，新传输暂不可用。"}
        </Alert>
      )}
      {transfer.pendingRequests
        .filter((request) => request.state === "unknown")
        .map((request) => (
          <Alert
            severity="warning"
            key={request.body.request_id}
            action={
              <Button
                disabled={transfer.submissionsBlocked || transfer.unavailable}
                onClick={() => transfer.retrySubmission(request.body.request_id)}
              >
                重试确认本次传输
              </Button>
            }
          >
            {request.binding?.performer_name ?? "归档任务"}
            ：提交结果暂不可确认。确认使用原请求，不会另建队列。
          </Alert>
        ))}
    </>
  );
}

export function TransferStatusBar({ onViewTasks }: { onViewTasks?: () => void }) {
  const transfer = useTransfer();
  if (
    !transfer.active &&
    !transfer.pendingRequests.length &&
    !transfer.unavailable &&
    (!transfer.task || transfer.dismissed)
  )
    return null;
  if (transfer.loading) return null;
  const running = transfer.activeTasks.filter((task) => task.status === "running").length;
  const queued = transfer.activeTasks.filter((task) => task.status === "queued").length;
  return (
    <Box
      role="status"
      aria-label="归档任务"
      sx={{
        flexShrink: 0,
        borderTop: `1px solid ${tokens.lineStrong}`,
        bgcolor: "background.paper",
        px: 2,
        py: 0.5,
        display: "flex",
        alignItems: "center",
        gap: 1,
        minWidth: 0,
      }}
    >
      <Typography variant="body2" noWrap sx={{ flex: 1 }}>
        {transfer.unavailable ? "任务状态待确认" : `${running} 个传输中 · ${queued} 个等待`}
        {transfer.unresolved ? " · 有提交待确认" : transfer.hasPending ? " · 正在提交" : ""}
        {transfer.active && !running && !queued ? " · 正在安全收尾" : ""}
      </Typography>
      {onViewTasks && (
        <Button size="small" onClick={onViewTasks}>
          任务与日志
        </Button>
      )}
      {!transfer.active && !transfer.pendingRequests.length && !transfer.unavailable && (
        <IconButton size="small" aria-label="收起已结束任务" onClick={transfer.dismiss}>
          <CloseIcon fontSize="small" />
        </IconButton>
      )}
    </Box>
  );
}
