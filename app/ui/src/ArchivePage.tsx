import FolderIcon from "@mui/icons-material/Folder";
import RefreshIcon from "@mui/icons-material/Refresh";
import SwapHorizIcon from "@mui/icons-material/SwapHoriz";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import IconButton from "@mui/material/IconButton";
import Snackbar from "@mui/material/Snackbar";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ArchiveTransferView } from "./ArchiveTransferView";
import { useCleanup } from "./CleanupProvider";
import type { PerformerOut } from "./client";
import {
  getSettingsApiSettingsGet,
  listPerformersApiPerformersGet,
  localSubdirsApiLocalSubdirsGet,
  putSettingsApiSettingsPut,
} from "./client";
import {
  DIRECTORY_RETRIES,
  DirectoryReadError,
  directoryRetryDelay,
  retryDirectoryRead,
  transientDirectoryError,
} from "./directoryReads";
import { openArchivePair } from "./openTarget";
import { PosterAvatar } from "./PerformersPage";
import { pickDirectory } from "./pickDirectory";
import { useTransfer } from "./TransferProvider";
import { tokens } from "./theme";

function detailOf(error: unknown, fallback: string): string {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  return typeof detail === "string" ? detail : fallback;
}

export function ArchivePage() {
  const queryClient = useQueryClient();
  const transfer = useTransfer();
  const cleanup = useCleanup();
  const bindBatch = cleanup.bindBatch;
  const [localDir, setLocalDir] = useState<string | null>(null);
  const [dirLoaded, setDirLoaded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [target, setTarget] = useState<PerformerOut | null>(null);
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  const [toast, setToast] = useState("");
  const [flashId, setFlashId] = useState<string | null>(null);
  const [isPicking, setIsPicking] = useState(false);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    return () => {
      if (flashTimer.current) clearTimeout(flashTimer.current);
    };
  }, []);

  // Restore the remembered directory once on mount.
  useEffect(() => {
    let disposed = false;
    (async () => {
      const r = await getSettingsApiSettingsGet();
      if (disposed) return;
      if (r.data?.last_local_dir) {
        setLocalDir(r.data.last_local_dir);
        bindBatch(r.data.last_local_dir);
      }
      setDirLoaded(true);
    })();
    return () => {
      disposed = true;
    };
  }, [bindBatch]);

  useEffect(() => {
    const t = setTimeout(() => setQ(search), 300);
    return () => clearTimeout(t);
  }, [search]);

  const requestedTask = transfer.requestedTask;
  const clearTaskView = transfer.clearTaskView;
  useEffect(() => {
    if (!dirLoaded || !requestedTask) return;
    // Viewing an old task must never silently replace the approved batch.
    // The immutable task details stay available even when this anchor changed.
    if (localDir !== requestedTask.local_dir) {
      setToast(`该任务来自 ${requestedTask.local_dir}；请先选择这个 batch，再查看目录。`);
    } else {
      setSelected(requestedTask.subdir);
      setSearch(requestedTask.subdir);
      setTarget({
        id: requestedTask.performer_id,
        name: requestedTask.performer_name,
        source_id: 0,
        source_label: requestedTask.source_label,
        unc_path: requestedTask.target_path,
        has_thumb: requestedTask.has_thumb,
        is_missing: false,
        aliases: [],
      });
    }
    clearTaskView();
  }, [requestedTask, dirLoaded, localDir, clearTaskView]);

  const subdirs = useQuery({
    queryKey: ["local-subdirs", localDir],
    enabled: localDir !== null,
    retry: retryDirectoryRead,
    retryDelay: directoryRetryDelay,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    queryFn: async ({ signal }) => {
      const r = await localSubdirsApiLocalSubdirsGet({
        query: { path: localDir ?? "" },
        signal: AbortSignal.any([signal, AbortSignal.timeout(30_000)]),
      });
      if (r.error || !r.data)
        throw new DirectoryReadError(
          detailOf(r.error, "server 未响应"),
          transientDirectoryError(r.error) ||
            transientDirectoryError({ status: r.response?.status }),
        );
      return r.data;
    },
  });

  // A folder deleted after upload must not linger as the selection (pair
  // would target a gone directory). Keyed off subdirs.data, which only
  // updates on SUCCESS — a failed refresh keeps the list and the selection
  // intact so the user can retry. The search term is deliberately left
  // alone (design §7.3: left selection and right search are independent).
  // Reconcile the latest selection with a successful snapshot. Clearing the
  // actor also clears its NAS target, so a deleted actor cannot reopen details.
  useEffect(() => {
    const data = subdirs.data;
    if (!data) return;
    if (selected && !data.subdirs.includes(selected)) {
      setSelected(null);
      setTarget(null);
    }
  }, [subdirs.data, selected]);

  const appliedCleanup = cleanup.applied;
  const consumedCleanup = useRef<string | null>(null);
  useEffect(() => {
    if (
      !appliedCleanup ||
      appliedCleanup.path !== localDir ||
      appliedCleanup.epoch !== cleanup.binding.epoch ||
      consumedCleanup.current === appliedCleanup.eventId
    )
      return;
    consumedCleanup.current = appliedCleanup.eventId;
    if (selected && appliedCleanup.names.includes(selected)) {
      setSelected(null);
      setTarget(null);
    }
  }, [appliedCleanup, localDir, cleanup.binding.epoch, selected]);

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
    if (isPicking) return; // guard against concurrent dialogs / probe+save
    setIsPicking(true);
    try {
      await runChooseDir();
    } finally {
      setIsPicking(false);
    }
  };

  const runChooseDir = async () => {
    const raw = await pickDirectory("选择 batch 目录（本机或 NAS 临时目录）");
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
    bindBatch(saved.data.last_local_dir ?? dir, true);
    setSelected(null);
    setTarget(null);
    queryClient.invalidateQueries({ queryKey: ["local-subdirs"] });
  };

  const openPair = useMutation({
    mutationFn: async (performerId: string) => {
      if (!localDir || !selected) throw new Error("no-selection");
      // Only the subdir NAME travels — the server joins it onto the approved
      // anchor it already holds (Kimi R4, tightened in #31).
      await openArchivePair(selected, performerId);
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
    setTarget(null);
    setSearch(name); // auto-fill; manual edits later do NOT clear `selected`
  };

  const items = results.data?.items ?? [];

  return (
    <Box sx={{ display: "flex", height: "100%", minHeight: 0, overflow: "hidden" }}>
      <Box
        sx={{
          width: { xs: 210, lg: 250 },
          flexShrink: 0,
          borderRight: `1px solid ${tokens.line}`,
          p: 2,
          display: "flex",
          flexDirection: "column",
          gap: 1,
          overflowY: "auto",
        }}
      >
        <Box sx={{ display: "flex", gap: 0.5, alignItems: "center" }}>
          <Button
            variant="outlined"
            color="inherit"
            startIcon={<FolderIcon />}
            onClick={chooseDir}
            disabled={isPicking}
            title={localDir ?? undefined}
            sx={{
              flex: 1,
              minWidth: 0,
              justifyContent: "flex-start",
              textTransform: "none",
              color: "text.secondary",
              borderColor: tokens.lineStrong,
              fontFamily: tokens.mono,
              fontSize: 12,
            }}
          >
            <Typography variant="caption" noWrap sx={{ maxWidth: 150 }}>
              {localDir ?? "选择本地目录…"}
            </Typography>
          </Button>
          <Tooltip title="刷新本地目录">
            <span>
              <IconButton
                size="small"
                aria-label="刷新本地目录"
                onClick={() => subdirs.refetch()}
                disabled={!localDir || subdirs.isFetching}
                sx={{ color: tokens.orangeDeep, flexShrink: 0 }}
              >
                {subdirs.isFetching ? (
                  <CircularProgress size={16} color="inherit" />
                ) : (
                  <RefreshIcon fontSize="small" />
                )}
              </IconButton>
            </span>
          </Tooltip>
        </Box>

        <Tooltip title="只删除当前 batch 一级完全空的演员目录；含文件或子目录的保留">
          <span>
            <Button
              size="small"
              color="inherit"
              onClick={() => localDir && void cleanup.begin(localDir)}
              disabled={!localDir || isPicking || cleanup.blocked || transfer.cleanupBlocked}
              sx={{
                fontSize: 12,
                color: "text.secondary",
                width: "100%",
                justifyContent: "flex-start",
              }}
            >
              清理空演员目录
            </Button>
          </span>
        </Tooltip>

        {subdirs.isFetching && subdirs.failureCount > 0 && (
          <Typography
            variant="caption"
            color="text.secondary"
            role="status"
            aria-label="本地目录重试"
          >
            正在自动重试本地目录（{Math.min(subdirs.failureCount, DIRECTORY_RETRIES)} /{" "}
            {DIRECTORY_RETRIES}）…
          </Typography>
        )}
        {subdirs.isError && !subdirs.isFetching && localDir && (
          <Box>
            {appliedCleanup?.path === localDir &&
              appliedCleanup.epoch === cleanup.binding.epoch && (
                <Typography variant="caption" role="alert" color="warning.main">
                  清理结果已保留，目录刷新失败。已确认删除的目录已从列表移除。
                </Typography>
              )}
            <Typography variant="caption" sx={{ color: tokens.bad }} role="alert">
              读取失败: {detailOf(subdirs.error, "server 未响应")} —{" "}
              {transientDirectoryError(subdirs.error) ? "请稍后重试" : "请重新选择"}
            </Typography>
            <Button size="small" onClick={() => subdirs.refetch()}>
              重试读取本地目录
            </Button>
          </Box>
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
              color: selected === name ? tokens.orangeText : "text.primary",
              bgcolor: selected === name ? tokens.orangeSoft : "transparent",
              fontWeight: selected === name ? 600 : 400,
              "&:hover": { bgcolor: selected === name ? tokens.orangeSoft : tokens.hoverBg },
            }}
            startIcon={<FolderIcon sx={{ color: tokens.orangeDeep }} />}
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

      {target && localDir && selected ? (
        <ArchiveTransferView
          key={`${localDir}:${selected}:${target.id}`}
          localDir={localDir}
          subdir={selected}
          performer={target}
          onBack={() => setTarget(null)}
        />
      ) : (
        <Box
          sx={{
            flex: 1,
            minWidth: 0,
            minHeight: 0,
            p: 2,
            display: "flex",
            flexDirection: "column",
          }}
        >
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
              <Typography variant="caption" sx={{ color: tokens.bad, mr: 1 }}>
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
                      borderColor: flashId === p.id ? tokens.orange : tokens.line,
                      boxShadow: flashId === p.id ? `0 0 0 3px ${tokens.orange}33` : "none",
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
                    <PosterAvatar performer={p} size="card" />
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
                          fontFamily: tokens.mono,
                          color: "text.secondary",
                          display: "block",
                        }}
                      >
                        {p.unc_path}
                      </Typography>
                    </Box>
                    <Box sx={{ display: "flex", flexDirection: "column", gap: 1, flexShrink: 0 }}>
                      <Button
                        variant="contained"
                        disableElevation
                        size="small"
                        disabled={pairDisabled}
                        onClick={() => setTarget(p)}
                        aria-label={`选择此目录：${p.name} ${p.unc_path}`}
                        sx={{ fontWeight: 600, whiteSpace: "nowrap" }}
                      >
                        选择此目录
                      </Button>
                      <Tooltip title={pairTooltip}>
                        <span>
                          <Button
                            variant="outlined"
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
                  </Box>
                );
              })}

            {q.trim() === "" && (
              <Box sx={{ textAlign: "center", py: 8, color: "text.secondary" }}>
                <SwapHorizIcon sx={{ fontSize: 36, color: "text.disabled" }} />
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
      )}

      <Snackbar
        open={toast !== ""}
        autoHideDuration={4000}
        onClose={() => setToast("")}
        message={toast}
      />
    </Box>
  );
}
