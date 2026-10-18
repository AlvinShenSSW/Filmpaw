import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import LinearProgress from "@mui/material/LinearProgress";
import Typography from "@mui/material/Typography";
import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useRef, useState } from "react";
import { archiveError, getTransfers, stopTransfers } from "./archiveApi";
import { type CleanupTask, getCleanup, isCleanupActive } from "./cleanupApi";

type Operation = "cleanup" | "transfer" | null;
type ClosePrompt = { task_id: string | null; detail: string | null; operation?: Operation };
type ExitStatus = { safe: boolean; task_id: string | null; operation?: Operation };
type TransferList = Awaited<ReturnType<typeof getTransfers>>;
const NEW_TASK_NOTICE = "出现新的任务，尚未停止。请重新选择退出方式。";

/** Lives outside routes so changing pages never drops an exit wait. The shell
 * remains the authority: a terminal UI snapshot alone can never kill a worker. */
export function ExitCoordinator() {
  const [prompt, setPrompt] = useState<ClosePrompt | null>(null);
  const [transfers, setTransfers] = useState<TransferList | null>(null);
  const [cleanup, setCleanup] = useState<CleanupTask | null>(null);
  const [mode, setMode] = useState<"choice" | "wait" | "stop">("choice");
  const [error, setError] = useState("");
  const [stopping, setStopping] = useState(false);
  const [checking, setChecking] = useState(false);
  // Retain exactly the user's stop selection across an uncertain response.
  const [stopRequest, setStopRequest] = useState<string[] | null>(null);
  const [notice, setNotice] = useState("");
  const generation = useRef(0);
  const cleaning = prompt?.operation === "cleanup";

  useEffect(() => {
    if (!isTauri()) return;
    let disposed = false;
    let unsubscribe: (() => void) | undefined;
    void listen<ClosePrompt>("filmpaw-close-requested", ({ payload }) => {
      if (disposed) return;
      generation.current += 1;
      setPrompt(payload);
      setTransfers(null);
      setCleanup(null);
      setMode("choice");
      setStopRequest(null);
      setNotice("");
      setStopping(false);
      setChecking(false);
      setError(payload.detail ?? "");
    }).then((unlisten) => {
      if (disposed) unlisten();
      else unsubscribe = unlisten;
    });
    return () => {
      disposed = true;
      generation.current += 1;
      unsubscribe?.();
    };
  }, []);

  useEffect(() => {
    if (!prompt) return;
    const controller = new AbortController();
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        let active: boolean;
        if (prompt.operation === "cleanup") {
          if (!prompt.task_id) throw new Error("无法确认清理任务，窗口已保留，请重试");
          const latest = await getCleanup(prompt.task_id, controller.signal);
          if (disposed) return;
          setCleanup(latest);
          active = isCleanupActive(latest);
        } else {
          const latest = await getTransfers(controller.signal);
          if (disposed) return;
          setTransfers(latest);
          active = latest.busy || latest.active.length > 0;
          if (mode === "stop" && latest.active.some((task) => !stopRequest?.includes(task.id))) {
            setNotice(NEW_TASK_NOTICE);
            setStopRequest(null);
            setMode("choice");
            return;
          }
        }
        if (mode !== "choice" && !active) {
          setChecking(true);
          const status = await invoke<ExitStatus>("close_if_idle");
          if (disposed) return;
          setChecking(false);
          if (status.safe) return; // The shell destroys the window itself.
          if (
            mode === "stop" &&
            (status.operation === "cleanup" ||
              !status.task_id ||
              !stopRequest?.includes(status.task_id))
          ) {
            setNotice(NEW_TASK_NOTICE);
            setStopRequest(null);
            setMode("choice");
          }
          // Another operation may have started between the status read and
          // the atomic check. Follow its kind; never send cleanup IDs to stop.
          if (
            status.task_id !== prompt.task_id ||
            (status.operation ?? "transfer") !== (prompt.operation ?? "transfer")
          ) {
            setTransfers(null);
            setCleanup(null);
            setPrompt({ task_id: status.task_id, operation: status.operation, detail: null });
          }
        }
      } catch (e) {
        if (disposed) return;
        setChecking(false);
        setTransfers(null);
        setError(archiveError(e, "无法确认任务状态，窗口已保留，请重试"));
        setMode("choice");
      }
      if (!disposed) timer = setTimeout(poll, 1000);
    };
    void poll();
    return () => {
      disposed = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [prompt, mode, stopRequest]);

  const keepRunning = () => {
    generation.current += 1;
    setPrompt(null);
    setMode("choice");
    setError("");
    setTransfers(null);
    setCleanup(null);
    setStopRequest(null);
    setNotice("");
  };
  const stopAndWait = async () => {
    if (cleaning || stopping || checking) return;
    const ids = stopRequest ?? transfers?.active.map((task) => task.id);
    if (!ids?.length) return;
    const currentGeneration = generation.current;
    setStopRequest(ids);
    setStopping(true);
    setError("");
    setNotice("");
    try {
      const response = await stopTransfers(ids);
      if (generation.current !== currentGeneration) return;
      if (
        response.task_ids.length !== ids.length ||
        ids.some((id) => !response.task_ids.includes(id))
      ) {
        throw new Error("无法确认全部停止请求，窗口已保留，请重试");
      }
      setMode("stop");
    } catch (e) {
      if (generation.current !== currentGeneration) return;
      setError(archiveError(e, "停止请求失败，窗口已保留，请重试"));
    } finally {
      if (generation.current === currentGeneration) setStopping(false);
    }
  };

  return (
    <Dialog
      open={prompt !== null}
      onClose={checking || stopping ? undefined : keepRunning}
      maxWidth="sm"
      fullWidth
    >
      <DialogTitle>退出 Filmpaw</DialogTitle>
      <DialogContent>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}
        {notice && (
          <Alert severity="info" sx={{ mb: 2 }}>
            {notice}
          </Alert>
        )}
        {!cleaning && transfers?.blocked_reason && (
          <Alert severity="warning" sx={{ mb: 2 }}>
            {transfers.blocked_reason}
          </Alert>
        )}
        {!cleaning &&
          [...(transfers?.active ?? []), ...(transfers?.history ?? [])].some(
            (task) =>
              task.status === "needs_review" &&
              (task.id === prompt?.task_id || stopRequest?.includes(task.id)),
          ) && (
            <Alert severity="warning" sx={{ mb: 2 }}>
              任务已停止，仍有项目需要人工检查。任务记录会保留。
            </Alert>
          )}
        {mode === "choice" ? (
          <Typography>
            {cleaning
              ? "空演员目录清理仍在进行。可以继续运行，或等待清理完成后退出。"
              : transfers?.busy || prompt?.task_id
                ? "归档传输仍在进行。可以继续运行，等待全部任务完成后退出，或停止当前全部任务并等待安全停止后退出。"
                : "退出前会再次确认任务状态，确认安全后才关闭应用。"}
          </Typography>
        ) : (
          <>
            <Typography role="status" sx={{ mb: 1.5 }}>
              {cleaning
                ? "正在等待清理完成……"
                : mode === "stop"
                  ? "正在等待所选任务安全停止……"
                  : "正在等待全部传输完成……"}
              {cleaning && cleanup?.current_name ? ` ${cleanup.current_name}` : ""}
            </Typography>
            <LinearProgress aria-label="等待安全退出" />
          </>
        )}
        {!cleaning && !!transfers?.active.length && (
          <Typography component="ul" variant="body2" sx={{ pl: 2.5, overflowWrap: "anywhere" }}>
            {transfers.active.map((task) => (
              <li key={task.id}>
                {task.performer_name || task.id}
                {task.current_name ? ` · ${task.current_name}` : ""}
              </li>
            ))}
          </Typography>
        )}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={keepRunning} disabled={checking || stopping}>
          {mode === "choice" ? "继续运行" : "取消等待"}
        </Button>
        {mode === "choice" && (
          <>
            <Button
              disabled={stopping || checking}
              onClick={() => {
                setError("");
                setNotice("");
                setStopRequest(null);
                setMode("wait");
              }}
            >
              完成后退出
            </Button>
            {!cleaning && (
              <Button
                variant="contained"
                disabled={
                  stopping || checking || !(stopRequest?.length || transfers?.active.length)
                }
                onClick={() => void stopAndWait()}
              >
                {stopRequest ? "重试停止原任务" : "停止全部后退出"}
              </Button>
            )}
          </>
        )}
      </DialogActions>
    </Dialog>
  );
}
