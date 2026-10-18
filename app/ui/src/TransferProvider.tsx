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
import { createContext, type ReactNode, useContext, useState } from "react";
import {
  type ArchiveTask,
  archiveError,
  createTransfer,
  getLatestTransfer,
  isTransferActive,
  stopTransfer,
} from "./archiveApi";
import { tokens } from "./theme";

const TASK_KEY = ["archive-transfer-latest"];
const PHASES = {
  preflight: "正在预检",
  copying: "正在复制",
  verifying: "正在校验",
  committing: "正在保存到片库",
  cleaning: "正在清理本地",
  finished: "已结束",
};

export function transferLabel(task: ArchiveTask): string {
  if (task.stop_requested && isTransferActive(task)) return "正在安全停止";
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

function useController() {
  const client = useQueryClient();
  const [uncertain, setUncertain] = useState(false);
  const [dismissedId, setDismissedId] = useState<string | null>(null);
  const [requestedTask, setRequestedTask] = useState<ArchiveTask | null>(null);
  const latest = useQuery({
    queryKey: TASK_KEY,
    queryFn: ({ signal }) => getLatestTransfer(signal),
    // Browser Internet reachability does not determine whether the local
    // 127.0.0.1 sidecar can report or safely stop an active queue.
    networkMode: "always",
    retry: false,
    refetchInterval: (query) => (isTransferActive(query.state.data) || uncertain ? 1000 : 5000),
    refetchIntervalInBackground: true,
  });
  const apply = async (task: ArchiveTask) => {
    await client.cancelQueries({ queryKey: TASK_KEY });
    client.setQueryData(TASK_KEY, task);
    setDismissedId(null);
  };
  const start = useMutation({
    mutationFn: (body: Parameters<typeof createTransfer>[0]) => createTransfer(body),
    networkMode: "always",
    retry: false,
    onSuccess: async (task) => {
      setUncertain(false);
      await apply(task);
    },
    onError: (error) => {
      // Network/server failures may occur after the server accepted the job.
      // Never automatically POST it again or allow an unrelated new queue.
      const status = (error as { status?: number })?.status;
      setUncertain(!status || status >= 500);
      client.invalidateQueries({ queryKey: TASK_KEY });
    },
  });
  const stop = useMutation({
    mutationFn: (id: string) => stopTransfer(id),
    networkMode: "always",
    retry: false,
    onSuccess: apply,
    onError: () => client.invalidateQueries({ queryKey: TASK_KEY }),
  });
  const task = latest.data ?? null;
  // A confirmed task with the submitted id resolves a lost POST response.
  const submissionRecovered = uncertain && task?.request_id === start.variables?.request_id;
  const unresolved = uncertain && !submissionRecovered;
  return {
    task,
    active: isTransferActive(task),
    unavailable: latest.isError || latest.isPending || unresolved,
    unresolved,
    loading: latest.isPending,
    start,
    stop,
    refresh: latest.refetch,
    dismissed: task?.id === dismissedId && !isTransferActive(task),
    dismiss: () => setDismissedId(task?.id ?? null),
    requestedTask,
    requestTaskView: () => setRequestedTask(task),
    clearTaskView: () => setRequestedTask(null),
    retrySubmission: () => {
      // Explicitly recover the already-authorized queue with the exact same
      // identity. A delayed first POST cannot create a second queue.
      if (start.variables && !start.isPending) start.mutate(start.variables);
    },
  };
}

const TransferContext = createContext<ReturnType<typeof useController> | null>(null);

export function TransferProvider({ children }: { children: ReactNode }) {
  const controller = useController();
  return <TransferContext.Provider value={controller}>{children}</TransferContext.Provider>;
}

export function useTransfer() {
  const value = useContext(TransferContext);
  if (!value) throw new Error("TransferProvider is required");
  return value;
}

export function TransferProgress({ task }: { task: ArchiveTask }) {
  const known = task.total_bytes > 0 && task.phase !== "preflight";
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

function itemLabel(status: string): string {
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

export function TransferStatusBar({ onViewArchive }: { onViewArchive?: () => void }) {
  const transfer = useTransfer();
  const [details, setDetails] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const { task } = transfer;
  const recoveryAction = transfer.unresolved ? (
    <Button disabled={transfer.start.isPending} onClick={transfer.retrySubmission}>
      重试确认本次传输
    </Button>
  ) : (
    <Button onClick={() => transfer.refresh()}>重试查询</Button>
  );
  if (!task || transfer.dismissed) {
    return transfer.unavailable && !transfer.loading ? (
      <Alert severity="warning" action={recoveryAction}>
        {transfer.unresolved
          ? "任务提交结果暂不可确认。可以重试确认本次传输，不会另建队列。"
          : "归档任务状态暂不可确认，新传输暂不可用。"}
      </Alert>
    ) : null;
  }
  return (
    <>
      <Box
        role="status"
        aria-label="归档任务"
        sx={{
          flexShrink: 0,
          borderTop: `1px solid ${tokens.lineStrong}`,
          bgcolor: "background.paper",
          px: 2.5,
          py: 1,
          display: "flex",
          gap: 2,
          alignItems: "center",
          minWidth: 0,
        }}
      >
        <Box sx={{ width: 180, minWidth: 0 }}>
          <Typography variant="body2" noWrap sx={{ fontWeight: 600 }}>
            {task.performer_name}
          </Typography>
          <Typography
            variant="caption"
            noWrap
            component="div"
            title={task.target_path}
            color="text.secondary"
          >
            {task.target_path}
          </Typography>
        </Box>
        <TransferProgress task={task} />
        {onViewArchive && (
          <Button
            size="small"
            sx={{ whiteSpace: "nowrap" }}
            onClick={() => {
              transfer.requestTaskView();
              onViewArchive();
            }}
          >
            查看目录
          </Button>
        )}
        <Button size="small" sx={{ whiteSpace: "nowrap" }} onClick={() => setDetails(true)}>
          任务详情
        </Button>
        {transfer.active ? (
          <Button
            size="small"
            color="inherit"
            startIcon={<StopCircleOutlinedIcon />}
            onClick={() => setConfirmStop(true)}
            disabled={task.stop_requested || transfer.stop.isPending}
            sx={{ whiteSpace: "nowrap" }}
          >
            {task.stop_requested ? "停止中…" : "停止传输"}
          </Button>
        ) : (
          <IconButton size="small" aria-label="收起已结束任务" onClick={transfer.dismiss}>
            <CloseIcon fontSize="small" />
          </IconButton>
        )}
      </Box>
      {transfer.unavailable && (
        <Alert severity="warning" action={recoveryAction}>
          {transfer.unresolved
            ? "新任务提交结果暂不可确认。可以重试确认本次传输，不会另建队列。"
            : "任务状态暂不可确认。服务恢复前请勿关闭应用或重新提交。"}
        </Alert>
      )}
      {transfer.stop.isError && (
        <Alert severity="error">{archiveError(transfer.stop.error, "停止请求失败，请重试")}</Alert>
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
