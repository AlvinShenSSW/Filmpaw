import CloseIcon from "@mui/icons-material/Close";
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
import { serverBase } from "./api";
import { archiveError } from "./archiveApi";
import {
  type CleanupContext,
  type CleanupTask,
  createCleanup,
  getLatestCleanup,
  isCleanupActive,
  readCleanupContext,
} from "./cleanupApi";
import { directoryRetryDelay, retryDirectoryRead } from "./directoryReads";
import { tokens } from "./theme";

const TASK_KEY = ["archive-cleanup-latest"];
type Submission = { context: CleanupContext; body: { context_id: string; request_id: string } };
type Binding = { path: string | null; epoch: number };
type Applied = Binding & { names: string[]; taskId: string; eventId: string };
const storageKey = () => `filmpaw-cleanup-pending:${serverBase()}`;

function readSubmission(): Submission | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey()) ?? "null");
    return value &&
      typeof value.context?.id === "string" &&
      typeof value.context?.batch_path === "string" &&
      typeof value.context?.batch_revision === "string" &&
      typeof value.body?.request_id === "string" &&
      value.body.context_id === value.context.id
      ? value
      : null;
  } catch {
    return null;
  }
}

function saveSubmission(value: Submission | null) {
  try {
    if (value) sessionStorage.setItem(storageKey(), JSON.stringify(value));
    else sessionStorage.removeItem(storageKey());
  } catch {
    /* The running provider still retains the exact submission. */
  }
}

function useController() {
  const client = useQueryClient();
  const [submission, setSubmission] = useState<Submission | null>(readSubmission);
  const [uncertain, setUncertain] = useState(() => readSubmission() !== null);
  const [preparing, setPreparing] = useState(false);
  const [preparingPath, setPreparingPath] = useState<string | null>(null);
  const [readAttempt, setReadAttempt] = useState(0);
  const [error, setError] = useState("");
  const [dismissedId, setDismissedId] = useState<string | null>(null);
  const [binding, setBinding] = useState<Binding>({ path: null, epoch: 0 });
  const bindingRef = useRef(binding);
  const preparingRef = useRef<AbortController | null>(null);
  const preparingKey = useRef<readonly unknown[] | null>(null);
  const sending = useRef(false);
  const origin = useRef<(Submission & Binding) | null>(null);
  const [applied, setApplied] = useState<Applied | null>(null);
  const observed = useRef({ id: "", names: new Set<string>(), terminal: false });

  const latest = useQuery({
    queryKey: TASK_KEY,
    queryFn: ({ signal }) => getLatestCleanup(signal),
    networkMode: "always",
    retry: false,
    refetchInterval: (query) => (isCleanupActive(query.state.data) || uncertain ? 1000 : 5000),
    refetchIntervalInBackground: true,
  });
  const applyTask = async (task: CleanupTask) => {
    await client.cancelQueries({ queryKey: TASK_KEY });
    client.setQueryData(TASK_KEY, task);
    setDismissedId(null);
  };
  const submit = useMutation({
    mutationFn: createCleanup,
    networkMode: "always",
    retry: false,
    onSuccess: async (task) => {
      setUncertain(false);
      saveSubmission(null);
      await applyTask(task);
    },
    onError: (reason) => {
      const status = (reason as { status?: number })?.status;
      const unknown = !status || status >= 500;
      setUncertain(unknown);
      if (!unknown) saveSubmission(null);
      setError(archiveError(reason, "清理请求失败"));
      void client.invalidateQueries({ queryKey: TASK_KEY });
    },
  });
  const task = latest.data ?? null;
  const recovered = uncertain && task?.request_id === submission?.body.request_id;
  const unresolved = uncertain && !recovered;
  const unavailable = latest.isPending || latest.isError || unresolved;
  const active = isCleanupActive(task);
  const blocked = preparing || submit.isPending || active || unavailable;

  useEffect(() => {
    if (!recovered) return;
    setUncertain(false);
    setError("");
    saveSubmission(null);
  }, [recovered]);

  const cancelPreparation = useCallback(() => {
    preparingRef.current?.abort();
    if (preparingKey.current)
      void client.cancelQueries({ queryKey: preparingKey.current, exact: true });
  }, [client]);
  const bindBatch = useCallback(
    (path: string, switched = false) => {
      if (bindingRef.current.path === path && !switched) return;
      cancelPreparation();
      const next = { path, epoch: bindingRef.current.epoch + 1 };
      bindingRef.current = next;
      setBinding(next);
    },
    [cancelPreparation],
  );

  useEffect(() => cancelPreparation, [cancelPreparation]);

  useEffect(() => {
    if (!task) return;
    if (observed.current.id !== task.id)
      observed.current = { id: task.id, names: new Set(), terminal: false };
    const confirmed = task.items
      .filter((item) => item.status === "deleted")
      .map((item) => item.name);
    const fresh = confirmed.filter((name) => !observed.current.names.has(name));
    const terminal = !isCleanupActive(task);
    if (!fresh.length && (!terminal || observed.current.terminal)) return;
    for (const name of fresh) observed.current.names.add(name);
    observed.current.terminal = terminal;
    const captured = origin.current;
    const current = bindingRef.current;
    const sameOrigin =
      captured?.body.request_id === task.request_id &&
      captured.context.batch_revision === task.batch_revision &&
      captured.path === task.batch_path &&
      current.path === task.batch_path &&
      captured.epoch === current.epoch;
    // Restored results have no trustworthy UI generation. Re-read the current
    // directory, never use old deletion names to prune newly recreated folders.
    if (!sameOrigin) {
      if (!captured && current.epoch === 1 && current.path === task.batch_path) {
        void client.invalidateQueries({
          queryKey: ["local-subdirs", task.batch_path],
          exact: true,
        });
        void client.invalidateQueries({ queryKey: ["archive-context", task.batch_path] });
      }
      return;
    }
    setApplied({ ...current, names: fresh, taskId: task.id, eventId: crypto.randomUUID() });
    const localKey = ["local-subdirs", task.batch_path];
    void client.cancelQueries({ queryKey: localKey, exact: true }).then(() => {
      if (bindingRef.current.path !== current.path || bindingRef.current.epoch !== current.epoch)
        return;
      client.setQueryData<{ path: string; subdirs: string[] }>(localKey, (previous) =>
        previous
          ? { ...previous, subdirs: previous.subdirs.filter((name) => !fresh.includes(name)) }
          : previous,
      );
      void client.invalidateQueries({ queryKey: localKey, exact: true });
      void client.invalidateQueries({ queryKey: ["archive-context", task.batch_path] });
    });
  }, [task, client]);

  const begin = async (path: string) => {
    if (blocked || sending.current || bindingRef.current.path !== path) return;
    sending.current = true;
    const captured = { ...bindingRef.current };
    const controller = new AbortController();
    preparingRef.current = controller;
    setPreparing(true);
    setPreparingPath(path);
    setReadAttempt(0);
    setError("");
    try {
      const queryKey = ["cleanup-context", path, captured.epoch, crypto.randomUUID()];
      preparingKey.current = queryKey;
      const context = await client.fetchQuery({
        queryKey,
        queryFn: ({ signal }) =>
          readCleanupContext(path, AbortSignal.any([signal, controller.signal])),
        networkMode: "always",
        retry: (count, reason) => {
          const retry = !controller.signal.aborted && retryDirectoryRead(count, reason);
          if (retry) setReadAttempt(count + 1);
          return retry;
        },
        retryDelay: directoryRetryDelay,
        gcTime: 0,
      });
      controller.signal.throwIfAborted();
      if (bindingRef.current.epoch !== captured.epoch || context.batch_path !== path) return;
      const next = { context, body: { context_id: context.id, request_id: crypto.randomUUID() } };
      origin.current = { ...next, ...captured };
      setSubmission(next);
      saveSubmission(next);
      setPreparing(false);
      preparingKey.current = null;
      await submit.mutateAsync(next.body);
    } catch (reason) {
      if (!controller.signal.aborted) setError(archiveError(reason, "无法读取清理范围"));
    } finally {
      if (preparingRef.current === controller) {
        preparingRef.current = null;
        preparingKey.current = null;
      }
      setPreparing(false);
      sending.current = false;
    }
  };

  return {
    task,
    active,
    blocked,
    unavailable,
    unresolved,
    preparing,
    pendingPath: preparing ? preparingPath : submission?.context.batch_path,
    readAttempt,
    error,
    binding,
    bindBatch,
    applied,
    begin,
    submit,
    loading: latest.isPending,
    refresh: latest.refetch,
    dismissed: task?.id === dismissedId && !active,
    dismiss: () => {
      setDismissedId(task?.id ?? null);
      setError("");
    },
    retrySubmission: () => {
      if (!submission || submit.isPending || !unresolved) return;
      setError("");
      submit.mutate(submission.body);
    },
  };
}

const CleanupStateContext = createContext<ReturnType<typeof useController> | null>(null);
export function CleanupProvider({ children }: { children: ReactNode }) {
  const controller = useController();
  return <CleanupStateContext.Provider value={controller}>{children}</CleanupStateContext.Provider>;
}
export function useCleanup() {
  const value = useContext(CleanupStateContext);
  if (!value) throw new Error("CleanupProvider is required");
  return value;
}

const ITEM_LABELS = {
  deleted: "已删除",
  not_empty: "非空保留",
  skipped: "已跳过",
  failed: "失败，已保留",
};
export function CleanupStatusBar() {
  const cleanup = useCleanup();
  const [details, setDetails] = useState(false);
  const task =
    cleanup.preparing || cleanup.submit.isPending || cleanup.unresolved ? null : cleanup.task;
  const displayPath = task?.batch_path ?? cleanup.pendingPath ?? cleanup.binding.path;
  const visibleTask = task && !cleanup.dismissed;
  const busy = cleanup.preparing || cleanup.submit.isPending || cleanup.active;
  if (!visibleTask && !busy && !cleanup.error && (!cleanup.unavailable || cleanup.loading))
    return null;
  return (
    <>
      {(visibleTask || busy) && (
        <Box
          role="status"
          aria-label="空演员目录清理"
          sx={{
            flexShrink: 0,
            px: 2.5,
            py: 1,
            borderTop: `1px solid ${tokens.line}`,
            display: "flex",
            alignItems: "center",
            gap: 2,
          }}
        >
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Typography variant="body2" fontWeight={600} noWrap>
              {cleanup.preparing
                ? cleanup.readAttempt
                  ? `正在重试清理范围（${cleanup.readAttempt} / 3）…`
                  : "正在读取清理范围…"
                : cleanup.submit.isPending
                  ? "正在提交清理…"
                  : cleanup.active
                    ? `正在清理空演员目录${task?.current_name ? ` · ${task.current_name}` : ""}`
                    : task?.status === "failed"
                      ? "清理未完成"
                      : "空演员目录清理完成"}
            </Typography>
            <Typography variant="caption" component="div" noWrap title={displayPath ?? ""}>
              {displayPath}
            </Typography>
            {busy && <LinearProgress aria-label="清理进行中" sx={{ my: 0.5 }} />}
            {task && (
              <Typography variant="caption" color="text.secondary">
                已检查 {task.checked_count} / {task.total_count} · 已删除 {task.deleted_count} ·
                非空保留 {task.kept_count} · 跳过 {task.skipped_count} · 失败 {task.failed_count}
              </Typography>
            )}
          </Box>
          {task && (
            <Button size="small" sx={{ whiteSpace: "nowrap" }} onClick={() => setDetails(true)}>
              清理详情
            </Button>
          )}
          {!busy && !cleanup.unresolved && (
            <IconButton size="small" aria-label="收起清理结果" onClick={cleanup.dismiss}>
              <CloseIcon fontSize="small" />
            </IconButton>
          )}
        </Box>
      )}
      {cleanup.unavailable && !cleanup.loading && (
        <Alert
          severity="warning"
          action={
            cleanup.unresolved ? (
              <Button disabled={cleanup.submit.isPending} onClick={cleanup.retrySubmission}>
                确认本次清理结果
              </Button>
            ) : (
              <Button onClick={() => cleanup.refresh()}>重试查询清理状态</Button>
            )
          }
        >
          {cleanup.unresolved
            ? "清理提交结果暂不可确认。可确认同一次请求，不会另建清理任务。"
            : "清理任务状态暂不可确认，新的清理和传输暂不可用。"}
        </Alert>
      )}
      {cleanup.error && !cleanup.unresolved && <Alert severity="error">{cleanup.error}</Alert>}
      <Dialog open={details} onClose={() => setDetails(false)} fullWidth maxWidth="sm">
        <DialogTitle>空演员目录清理结果</DialogTitle>
        <DialogContent dividers>
          <Typography variant="body2" sx={{ overflowWrap: "anywhere", fontFamily: tokens.mono }}>
            {task?.batch_path}
          </Typography>
          {task?.message && (
            <Alert
              severity={task.status === "failed" || task.failed_count ? "warning" : "info"}
              sx={{ my: 1 }}
            >
              {task.message}
            </Alert>
          )}
          {task?.status === "completed" && task.total_count === 0 && (
            <Typography sx={{ my: 1 }}>当前 batch 没有可清理的演员目录。</Typography>
          )}
          <Box component="ul" sx={{ pl: 2.5 }}>
            {task?.items.map((item) => (
              <Box component="li" key={item.name} sx={{ py: 0.75, overflowWrap: "anywhere" }}>
                <Typography variant="body2">
                  {item.name} · {ITEM_LABELS[item.status]}
                </Typography>
                {item.message && (
                  <Typography variant="caption" color="text.secondary">
                    {item.message}
                  </Typography>
                )}
              </Box>
            ))}
          </Box>
          <Typography variant="caption" color="text.secondary">
            结果保留在当前应用会话；重启后不会自动继续清理。
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDetails(false)}>关闭</Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
