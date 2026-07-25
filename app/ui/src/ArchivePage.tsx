import FolderIcon from "@mui/icons-material/Folder";
import SwapHorizIcon from "@mui/icons-material/SwapHoriz";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Snackbar from "@mui/material/Snackbar";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import {
  getSettingsApiSettingsGet,
  listPerformersApiPerformersGet,
  localSubdirsApiLocalSubdirsGet,
  openPairApiOpenPairPost,
  putSettingsApiSettingsPut,
} from "./client";
import { PosterAvatar } from "./PerformersPage";

function detailOf(error: unknown, fallback: string): string {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  return typeof detail === "string" ? detail : fallback;
}

/** Tauri native dir picker; dev fallback = prompt for a path. */
async function pickDirectory(): Promise<string | null> {
  const { isTauri } = await import("@tauri-apps/api/core");
  if (isTauri()) {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const r = await open({ directory: true, title: "选择本地下载目录" });
    return typeof r === "string" ? r : null;
  }
  return window.prompt("输入本地目录路径 (dev 模式)");
}

export function ArchivePage() {
  const queryClient = useQueryClient();
  const [localDir, setLocalDir] = useState<string | null>(null);
  const [dirLoaded, setDirLoaded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  const [toast, setToast] = useState("");
  const [flashId, setFlashId] = useState<string | null>(null);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    return () => {
      if (flashTimer.current) clearTimeout(flashTimer.current);
    };
  }, []);

  // Restore the remembered directory once on mount.
  useEffect(() => {
    (async () => {
      const r = await getSettingsApiSettingsGet();
      if (r.data?.last_local_dir) setLocalDir(r.data.last_local_dir);
      setDirLoaded(true);
    })();
  }, []);

  useEffect(() => {
    const t = setTimeout(() => setQ(search), 300);
    return () => clearTimeout(t);
  }, [search]);

  const subdirs = useQuery({
    queryKey: ["local-subdirs", localDir],
    enabled: localDir !== null,
    retry: false,
    queryFn: async () => {
      const r = await localSubdirsApiLocalSubdirsGet({ query: { path: localDir ?? "" } });
      if (r.error || !r.data) throw r.error ?? new Error("subdirs");
      return r.data;
    },
  });

  const results = useQuery({
    queryKey: ["archive-match", q],
    enabled: q.trim() !== "",
    queryFn: async () => {
      const r = await listPerformersApiPerformersGet({
        query: { q, include_missing: true, page_size: 50 },
      });
      if (r.error || !r.data) throw r.error ?? new Error("match");
      return r.data;
    },
  });

  const chooseDir = async () => {
    const raw = await pickDirectory();
    if (!raw) return;
    // Defensive normalization: some pickers/platforms hand back forward
    // slashes or a trailing separator; keep the stored anchor canonical
    // (Windows backslashes, no trailing slash) — EXCEPT a drive root like
    // "C:\\" must keep its separator, else "C:" is drive-relative and the
    // server would mis-report "must be absolute" instead of rejecting a root.
    const back = raw.replace(/\//g, "\\");
    // Drive root (any number of trailing separators, e.g. C:\ or C://) keeps
    // a single separator; everything else drops trailing separators.
    const dir = /^[A-Za-z]:\\+$/.test(back) ? `${back[0]}:\\` : back.replace(/\\+$/, "");
    // Validate before persisting: a bad pick must not poison the saved dir.
    const probe = await localSubdirsApiLocalSubdirsGet({ query: { path: dir } });
    if (probe.error || !probe.data) {
      // Surface the REAL reason instead of always claiming "not found" —
      // a probe can fail from a transient/connection error too (#18).
      setToast(`无法读取目录: ${detailOf(probe.error, "server 未响应")} — 未保存`);
      return;
    }
    // Persist and only apply local state after the server ACCEPTS the anchor
    // (its validation is the containment boundary); revert on rejection.
    const saved = await putSettingsApiSettingsPut({ body: { last_local_dir: dir } });
    if (saved.error || !saved.data) {
      setToast(`保存目录失败: ${detailOf(saved.error, "server 拒绝")}`);
      return;
    }
    setLocalDir(saved.data.last_local_dir ?? dir);
    setSelected(null);
    queryClient.invalidateQueries({ queryKey: ["local-subdirs"] });
  };

  const openPair = useMutation({
    mutationFn: async (performerId: string) => {
      if (!localDir || !selected) throw new Error("no-selection");
      // The server joins local_dir + subdir itself — no client-side
      // separator knowledge (Kimi R4).
      const r = await openPairApiOpenPairPost({
        body: { local_dir: localDir, subdir: selected, performer_id: performerId },
      });
      if (r.error) throw r.error;
      return performerId;
    },
    onSuccess: (performerId) => {
      setFlashId(performerId);
      if (flashTimer.current) clearTimeout(flashTimer.current);
      flashTimer.current = setTimeout(() => setFlashId(null), 900);
    },
    onError: (e) => {
      setToast(detailOf(e, "双开失败"));
      // 400 = local dir vanished/outside approved; force a re-pick prompt state
      if (detailOf(e, "").includes("本地目录不存在")) {
        queryClient.invalidateQueries({ queryKey: ["local-subdirs"] });
      }
      // 404/409 = record gone or missing; refresh match results
      queryClient.invalidateQueries({ queryKey: ["archive-match"] });
    },
  });

  const pickFolder = (name: string) => {
    setSelected(name);
    setSearch(name); // auto-fill; manual edits later do NOT clear `selected`
  };

  const items = results.data?.items ?? [];

  return (
    <Box sx={{ display: "flex", height: "100%" }}>
      <Box
        sx={{
          width: 260,
          flexShrink: 0,
          borderRight: "1px solid #ECEAE4",
          p: 2,
          display: "flex",
          flexDirection: "column",
          gap: 1,
          overflowY: "auto",
        }}
      >
        <Button
          variant="outlined"
          color="inherit"
          startIcon={<FolderIcon />}
          onClick={chooseDir}
          sx={{
            justifyContent: "flex-start",
            textTransform: "none",
            color: "text.secondary",
            borderColor: "#DDD9D1",
            fontFamily: "Consolas, monospace",
            fontSize: 12,
          }}
        >
          <Typography variant="caption" noWrap sx={{ maxWidth: 180 }}>
            {localDir ?? "选择本地目录…"}
          </Typography>
        </Button>

        {subdirs.isError && localDir && (
          <Typography variant="caption" sx={{ color: "#A32D2D" }} role="alert">
            读取失败: {detailOf(subdirs.error, "server 未响应")} — 请重新选择
          </Typography>
        )}
        {dirLoaded && !localDir && (
          <Typography variant="caption" color="text.secondary">
            选择你新下载电影所在的目录, 列出其中按表演者命名的文件夹
          </Typography>
        )}
        {subdirs.data?.subdirs.map((name) => (
          <Button
            key={name}
            onClick={() => pickFolder(name)}
            sx={{
              justifyContent: "flex-start",
              textTransform: "none",
              borderRadius: "9px",
              px: 1.5,
              color: selected === name ? "#7A4A0C" : "text.primary",
              bgcolor: selected === name ? "#FDF3E3" : "transparent",
              fontWeight: selected === name ? 600 : 400,
              "&:hover": { bgcolor: selected === name ? "#FDF3E3" : "#F7F5F1" },
            }}
            startIcon={<FolderIcon sx={{ color: "#B45E14" }} />}
          >
            {name}
          </Button>
        ))}
        {subdirs.data && subdirs.data.subdirs.length === 0 && (
          <Typography variant="caption" color="text.secondary">
            该目录下没有子文件夹
          </Typography>
        )}
      </Box>

      <Box sx={{ flex: 1, minWidth: 0, p: 2, display: "flex", flexDirection: "column" }}>
        <TextField
          fullWidth
          size="small"
          placeholder="点左侧文件夹自动匹配, 或手动输入名字/别名"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          slotProps={{ htmlInput: { "aria-label": "匹配搜索" } }}
        />
        {results.isError && (
          <Box sx={{ my: 1 }} role="alert">
            <Typography variant="caption" sx={{ color: "#A32D2D", mr: 1 }}>
              匹配加载失败 — 请确认 server 正在运行
            </Typography>
            <Button size="small" onClick={() => results.refetch()}>
              重试
            </Button>
          </Box>
        )}
        {q.trim() !== "" && !results.isError && (
          <Typography variant="caption" color="text.secondary" sx={{ my: 1 }}>
            {items.length} 条匹配(名字/别名 · 含繁简)
            {(results.data?.total ?? 0) > items.length
              ? ` · 共 ${results.data?.total} 条, 仅显示前 ${items.length} — 请细化搜索词`
              : ""}
          </Typography>
        )}

        <Box sx={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
          {q.trim() !== "" &&
            items.map((p) => {
              const pairDisabled = p.is_missing || !selected;
              const pairTooltip = p.is_missing
                ? "文件夹已失效"
                : !selected
                  ? "先选择左侧本地文件夹"
                  : "";
              return (
                <Box
                  key={p.id}
                  sx={{
                    border: "1px solid",
                    borderColor: flashId === p.id ? "#EF9F27" : "#ECEAE4",
                    boxShadow: flashId === p.id ? "0 0 0 3px #EF9F2733" : "none",
                    borderRadius: "11px",
                    p: "12px 14px",
                    mb: 1,
                    display: "flex",
                    alignItems: "center",
                    gap: 1.5,
                    opacity: p.is_missing ? 0.55 : 1,
                    transition: "border-color .3s, box-shadow .3s",
                  }}
                >
                  <PosterAvatar performer={p} big />
                  <Box sx={{ minWidth: 0, flex: 1 }}>
                    <Typography variant="body2" sx={{ fontWeight: 600 }}>
                      {p.name}
                      {p.aliases.length > 0 && (
                        <Typography
                          component="span"
                          variant="caption"
                          color="text.secondary"
                          sx={{ ml: 1 }}
                        >
                          {p.aliases.map((a) => a.alias).join(" · ")}
                        </Typography>
                      )}
                    </Typography>
                    <Typography
                      variant="caption"
                      noWrap
                      sx={{
                        fontFamily: "Consolas, monospace",
                        color: "text.secondary",
                        display: "block",
                      }}
                    >
                      {p.unc_path}
                    </Typography>
                  </Box>
                  <Tooltip title={pairTooltip}>
                    <span>
                      <Button
                        variant="contained"
                        disableElevation
                        size="small"
                        startIcon={<SwapHorizIcon />}
                        disabled={pairDisabled || openPair.isPending}
                        onClick={() => openPair.mutate(p.id)}
                        sx={{ fontWeight: 600, whiteSpace: "nowrap" }}
                      >
                        双开
                      </Button>
                    </span>
                  </Tooltip>
                </Box>
              );
            })}

          {q.trim() === "" && (
            <Box sx={{ textAlign: "center", py: 8, color: "text.secondary" }}>
              <SwapHorizIcon sx={{ fontSize: 36, color: "#B3AFA6" }} />
              <Typography variant="body2" sx={{ mt: 1 }}>
                选择左侧文件夹开始匹配, 或在上方手动输入
              </Typography>
            </Box>
          )}
          {q.trim() !== "" && results.data && items.length === 0 && (
            <Box sx={{ textAlign: "center", py: 8, color: "text.secondary" }}>
              <Typography variant="body2" sx={{ fontWeight: 600, color: "text.primary" }}>
                库中没有「{q}」
              </Typography>
              <Typography variant="caption">
                可能用了别的艺名 — 改搜索词试试, 或者这是还没入库的新人
              </Typography>
            </Box>
          )}
        </Box>
      </Box>

      <Snackbar
        open={toast !== ""}
        autoHideDuration={4000}
        onClose={() => setToast("")}
        message={toast}
      />
    </Box>
  );
}
