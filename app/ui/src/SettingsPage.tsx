import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import DnsIcon from "@mui/icons-material/Dns";
import RefreshIcon from "@mui/icons-material/Refresh";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  addSourceApiSourcesPost,
  deleteSourceApiSourcesSourceIdDelete,
  getSettingsApiSettingsGet,
  healthApiHealthGet,
  listSourcesApiSourcesGet,
  scanOneApiSourcesSourceIdScanPost,
} from "./client";

interface ScanState {
  kind: "idle" | "scanning" | "ok" | "unreachable";
  text?: string;
}

function detailOf(error: unknown, fallback: string): string {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  return typeof detail === "string" ? detail : fallback;
}

export function SettingsPage() {
  const queryClient = useQueryClient();
  const [uncInput, setUncInput] = useState("");
  const [addError, setAddError] = useState("");
  const [scanStates, setScanStates] = useState<Record<number, ScanState>>({});
  const [confirmDelete, setConfirmDelete] = useState<{
    id: number;
    unc_path: string;
    performer_count: number;
  } | null>(null);

  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: async () => {
      const r = await listSourcesApiSourcesGet();
      if (r.error) throw r.error;
      return r.data ?? [];
    },
  });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: async () => {
      const r = await healthApiHealthGet();
      if (r.error) throw r.error;
      return r.data;
    },
    retry: 1,
  });
  const settings = useQuery({
    queryKey: ["app-settings"],
    queryFn: async () => {
      const r = await getSettingsApiSettingsGet();
      if (r.error) throw r.error;
      return r.data;
    },
    retry: 1,
  });

  const refetchSources = () => queryClient.invalidateQueries({ queryKey: ["sources"] });

  const addSource = useMutation({
    mutationFn: async (unc_path: string) => {
      const r = await addSourceApiSourcesPost({ body: { unc_path } });
      if (r.error) throw r.error;
      return r.data;
    },
    onSuccess: () => {
      setUncInput("");
      setAddError("");
      refetchSources();
    },
    onError: (e) => setAddError(detailOf(e, "添加失败 — 路径需以 \\\\ 开头的 UNC 格式并可访问")),
  });

  const scanOne = useMutation({
    mutationFn: async (sourceId: number) => {
      setScanStates((s) => ({ ...s, [sourceId]: { kind: "scanning" } }));
      const r = await scanOneApiSourcesSourceIdScanPost({
        path: { source_id: sourceId },
      });
      if (r.error || !r.data) throw r.error ?? new Error("scan");
      return { sourceId, data: r.data };
    },
    onSuccess: ({ sourceId, data }) => {
      setScanStates((s) => ({
        ...s,
        [sourceId]: {
          kind: "ok",
          text: `新增 ${data.added} · 更新 ${data.refreshed} · 失效 ${data.missing}`,
        },
      }));
      refetchSources();
    },
    // The mutation VARIABLES carry the source id — a transport-level
    // rejection has no body, and keying failure state off the error object
    // left the row stuck in "scanning" forever.
    onError: (_e: unknown, sourceId: number) => {
      setScanStates((s) => ({
        ...s,
        [sourceId]: { kind: "unreachable", text: "源不可达 — 已跳过, 记录未变动" },
      }));
    },
  });

  const deleteSource = useMutation({
    mutationFn: async (sourceId: number) => {
      const r = await deleteSourceApiSourcesSourceIdDelete({ path: { source_id: sourceId } });
      if (r.error) throw r.error;
    },
    onSuccess: () => {
      setConfirmDelete(null);
      refetchSources();
    },
  });

  return (
    <Box sx={{ p: 3, maxWidth: 860 }}>
      <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>
        设置
      </Typography>

      <Box sx={{ display: "flex", gap: 1.5 }}>
        <TextField
          fullWidth
          size="small"
          placeholder="\\NAS名\共享\目录\"
          value={uncInput}
          onChange={(e) => setUncInput(e.target.value)}
          slotProps={{ htmlInput: { "aria-label": "新扫描源路径" } }}
        />
        <Button
          variant="contained"
          disableElevation
          onClick={() => uncInput.trim() && addSource.mutate(uncInput.trim())}
          disabled={addSource.isPending}
          sx={{ whiteSpace: "nowrap", fontWeight: 600 }}
        >
          ＋ 添加源
        </Button>
      </Box>
      {addError && (
        <Typography variant="body2" sx={{ color: "#A32D2D", mt: 0.75 }} role="alert">
          {addError}
        </Typography>
      )}

      <Box sx={{ mt: 2.5, display: "flex", flexDirection: "column", gap: 1 }}>
        {sources.data?.map((s) => {
          const scan = scanStates[s.id] ?? { kind: "idle" };
          return (
            <Box
              key={s.id}
              sx={{
                border: "1px solid",
                borderColor:
                  scan.kind === "ok"
                    ? "#C4DD9D"
                    : scan.kind === "unreachable"
                      ? "#F0B8B8"
                      : "#ECEAE4",
                bgcolor:
                  scan.kind === "ok"
                    ? "#EAF3DE"
                    : scan.kind === "unreachable"
                      ? "#FCEBEB"
                      : "background.default",
                borderRadius: "11px",
                p: "11px 14px",
                display: "flex",
                alignItems: "center",
                gap: 1.5,
              }}
            >
              <DnsIcon sx={{ color: "#B45E14", fontSize: 20 }} />
              <Box sx={{ minWidth: 0, flex: 1 }}>
                <Typography
                  variant="body2"
                  sx={{ fontWeight: 600, fontFamily: "Consolas, monospace" }}
                  noWrap
                >
                  {s.unc_path}
                </Typography>
                <Typography
                  variant="caption"
                  sx={{
                    color:
                      scan.kind === "ok"
                        ? "#3B6D11"
                        : scan.kind === "unreachable"
                          ? "#A32D2D"
                          : "text.secondary",
                  }}
                >
                  {scan.kind === "scanning" ? (
                    <>
                      <CircularProgress size={10} sx={{ mr: 0.5 }} /> 扫描中…
                    </>
                  ) : scan.text ? (
                    scan.text
                  ) : (
                    `${s.performer_count} 人 · 上次扫描 ${s.last_scan_at ?? "从未"} · ${
                      s.reachable ? "在线" : "离线"
                    }`
                  )}
                </Typography>
              </Box>
              <Button
                size="small"
                variant="outlined"
                color="inherit"
                startIcon={<RefreshIcon />}
                onClick={() => scanOne.mutate(s.id)}
                disabled={scan.kind === "scanning"}
                sx={{ color: "text.secondary", borderColor: "#DDD9D1" }}
              >
                扫描
              </Button>
              <IconButton
                size="small"
                aria-label="删除源"
                onClick={() =>
                  setConfirmDelete({
                    id: s.id,
                    unc_path: s.unc_path,
                    performer_count: s.performer_count,
                  })
                }
              >
                <DeleteOutlineIcon fontSize="small" />
              </IconButton>
            </Box>
          );
        })}
        {sources.data?.length === 0 && (
          <Typography variant="body2" color="text.secondary" sx={{ py: 3, textAlign: "center" }}>
            还没有扫描源 — 在上方粘贴 NAS 目录的 UNC 路径添加
          </Typography>
        )}
      </Box>

      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 3 }}>
        数据库: {settings.data?.db_path ?? "…"} · v{health.data?.version ?? "…"}
      </Typography>

      <Dialog open={confirmDelete !== null} onClose={() => setConfirmDelete(null)}>
        <DialogTitle>删除扫描源?</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            {confirmDelete?.unc_path}
            <br />
            将同时删除该源下的 <b>{confirmDelete?.performer_count}</b> 条表演者记录 (组别名保留)。
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmDelete(null)}>取消</Button>
          <Button
            color="error"
            onClick={() => confirmDelete && deleteSource.mutate(confirmDelete.id)}
            disabled={deleteSource.isPending}
          >
            删除
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
