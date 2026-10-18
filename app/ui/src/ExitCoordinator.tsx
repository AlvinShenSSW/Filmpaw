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

type ClosePrompt = { task_id: string | null; detail: string | null };
type ExitStatus = { safe: boolean; task_id: string | null };

/** Lives outside routes so changing pages never drops an exit wait. The shell
 * remains the authority: a terminal UI snapshot alone can never kill a worker. */
export function ExitCoordinator() {
  const [prompt, setPrompt] = useState<ClosePrompt | null>(null);
  const [task, setTask] = useState<ArchiveTask | null>(null);
  const [mode, setMode] = useState<"choice" | "wait" | "stop">("choice");
  const [error, setError] = useState("");
  const [stopping, setStopping] = useState(false);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    if (!isTauri()) return;
    let disposed = false;
    let unsubscribe: (() => void) | undefined;
    void listen<ClosePrompt>("filmpaw-close-requested", ({ payload }) => {
      if (disposed) return;
      setPrompt(payload);
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
        const latest = await getLatestTransfer(controller.signal);
        if (disposed) return;
        setTask(latest);
        if (mode !== "choice" && !isTransferActive(latest)) {
          setChecking(true);
          const status = await invoke<ExitStatus>("close_if_idle");
          if (disposed) return;
          setChecking(false);
          if (status.safe) return; // The shell destroys the window itself.
        }
      } catch (e) {
        if (disposed) return;
        setChecking(false);
        setError(archiveError(e, "无法确认传输状态，窗口已保留，请重试"));
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
  };
  const stopAndWait = async () => {
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
        {task?.status === "needs_review" && (
          <Alert severity="warning" sx={{ mb: 2 }}>
            任务已停止，仍有项目需要人工检查。任务记录会保留。
          </Alert>
        )}
        {mode === "choice" ? (
          <Typography>
            {isTransferActive(task) || prompt?.task_id
              ? "归档传输仍在进行。可以继续运行，等待整个队列完成后退出，或停止队列并等待安全停止后退出。"
              : "退出前会再次确认传输状态，确认安全后才关闭应用。"}
          </Typography>
        ) : (
          <>
            <Typography role="status" sx={{ mb: 1.5 }}>
              {mode === "stop" ? "正在等待队列安全停止……" : "正在等待传输完成……"}
              {task?.current_name ? ` ${task.current_name}` : ""}
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
            <Button
              variant="contained"
              disabled={stopping || checking || !(task?.id ?? prompt?.task_id)}
              onClick={() => void stopAndWait()}
            >
              停止队列后退出
            </Button>
          </>
        )}
      </DialogActions>
    </Dialog>
  );
}
