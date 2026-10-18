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
import { useEffect, useState } from "react";
import {
  type ArchiveTask,
  archiveError,
  getLatestTransfer,
  isTransferActive,
  stopTransfer,
} from "./archiveApi";
import { type CleanupTask, getCleanup, isCleanupActive } from "./cleanupApi";

type Operation = "cleanup" | "transfer" | null;
type ClosePrompt = { task_id: string | null; detail: string | null; operation?: Operation };
type ExitStatus = { safe: boolean; task_id: string | null; operation?: Operation };

/** Lives outside routes so changing pages never drops an exit wait. The shell
 * remains the authority: a terminal UI snapshot alone can never kill a worker. */
export function ExitCoordinator() {
  const [prompt, setPrompt] = useState<ClosePrompt | null>(null);
  const [task, setTask] = useState<ArchiveTask | null>(null);
  const [cleanup, setCleanup] = useState<CleanupTask | null>(null);
  const [mode, setMode] = useState<"choice" | "wait" | "stop">("choice");
  const [error, setError] = useState("");
  const [stopping, setStopping] = useState(false);
  const [checking, setChecking] = useState(false);
  const cleaning = prompt?.operation === "cleanup";

  useEffect(() => {
    if (!isTauri()) return;
    let disposed = false;
    let unsubscribe: (() => void) | undefined;
    void listen<ClosePrompt>("filmpaw-close-requested", ({ payload }) => {
      if (disposed) return;
      setPrompt(payload);
      setTask(null);
      setCleanup(null);
      setMode("choice");
      setError(payload.detail ?? "");
    }).then((unlisten) => {
      if (disposed) unlisten();
      else unsubscribe = unlisten;
    });
    return () => {
      disposed = true;
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
          const latest = await getLatestTransfer(controller.signal);
          if (disposed) return;
          setTask(latest);
          active = isTransferActive(latest);
        }
        if (mode !== "choice" && !active) {
          setChecking(true);
          const status = await invoke<ExitStatus>("close_if_idle");
          if (disposed) return;
          setChecking(false);
          if (status.safe) return; // The shell destroys the window itself.
          // Another operation may have started between the status read and
          // the atomic check. Follow its kind; never send cleanup IDs to stop.
          if (
            status.task_id !== prompt.task_id ||
            (status.operation ?? "transfer") !== (prompt.operation ?? "transfer")
          ) {
            setTask(null);
            setCleanup(null);
            setPrompt({ task_id: status.task_id, operation: status.operation, detail: null });
          }
        }
      } catch (e) {
        if (disposed) return;
        setChecking(false);
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
  }, [prompt, mode]);

  const keepRunning = () => {
    setPrompt(null);
    setMode("choice");
    setError("");
    setTask(null);
    setCleanup(null);
  };
  const stopAndWait = async () => {
    if (cleaning) return;
    const id = task?.id ?? prompt?.task_id;
    if (!id) return;
    setStopping(true);
    setError("");
    try {
      await stopTransfer(id);
      setMode("stop");
    } catch (e) {
      setError(archiveError(e, "停止请求失败，窗口已保留，请重试"));
    } finally {
      setStopping(false);
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
        {!cleaning && task?.status === "needs_review" && (
          <Alert severity="warning" sx={{ mb: 2 }}>
            任务已停止，仍有项目需要人工检查。任务记录会保留。
          </Alert>
        )}
        {mode === "choice" ? (
          <Typography>
            {cleaning
              ? "空演员目录清理仍在进行。可以继续运行，或等待清理完成后退出。"
              : isTransferActive(task) || prompt?.task_id
                ? "归档传输仍在进行。可以继续运行，等待整个队列完成后退出，或停止队列并等待安全停止后退出。"
                : "退出前会再次确认任务状态，确认安全后才关闭应用。"}
          </Typography>
        ) : (
          <>
            <Typography role="status" sx={{ mb: 1.5 }}>
              {cleaning
                ? "正在等待清理完成……"
                : mode === "stop"
                  ? "正在等待队列安全停止……"
                  : "正在等待传输完成……"}
              {(cleaning ? cleanup : task)?.current_name
                ? ` ${(cleaning ? cleanup : task)?.current_name}`
                : ""}
            </Typography>
            <LinearProgress aria-label="等待安全退出" />
          </>
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
                setMode("wait");
              }}
            >
              完成后退出
            </Button>
            {!cleaning && (
              <Button
                variant="contained"
                disabled={stopping || checking || !(task?.id ?? prompt?.task_id)}
                onClick={() => void stopAndWait()}
              >
                停止队列后退出
              </Button>
            )}
          </>
        )}
      </DialogActions>
    </Dialog>
  );
}
