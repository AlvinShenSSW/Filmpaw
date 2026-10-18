import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import ArrowDownwardIcon from "@mui/icons-material/ArrowDownward";
import ArrowForwardIcon from "@mui/icons-material/ArrowForward";
import ArrowUpwardIcon from "@mui/icons-material/ArrowUpward";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import FolderIcon from "@mui/icons-material/Folder";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import RefreshIcon from "@mui/icons-material/Refresh";
import SwapHorizIcon from "@mui/icons-material/SwapHoriz";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import Snackbar from "@mui/material/Snackbar";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { useMutation } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { serverBase } from "./api";
import { type ArchiveFolder, archiveError } from "./archiveApi";
import { useCleanup } from "./CleanupProvider";
import type { PerformerOut } from "./client";
import { DIRECTORY_RETRIES, useArchiveContext } from "./directoryReads";
import { openArchivePair } from "./openTarget";
import { formatBytes, useTransfer } from "./TransferProvider";
import { tokens } from "./theme";

const nameOrder = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });

function FolderPane({
  source,
  path,
  items,
  error,
  selected,
  disabled,
  busy,
  onToggle,
  onSelectAll,
  onRefresh,
}: {
  source: boolean;
  path: string;
  items: ArchiveFolder[];
  error: string | null;
  selected: Set<string>;
  disabled: boolean;
  busy: boolean;
  onToggle: (name: string) => void;
  onSelectAll: () => void;
  onRefresh: () => void;
}) {
  const [descending, setDescending] = useState(false);
  const [fullName, setFullName] = useState<string | null>(null);
  const [copyMessage, setCopyMessage] = useState("");
  const sorted = [...items].sort(
    (a, b) => nameOrder.compare(a.name, b.name) * (descending ? -1 : 1),
  );
  const eligible = items.filter((item) => !item.error);
  const selectedCount = eligible.filter((item) => selected.has(item.name)).length;
  return (
    <Box
      component="section"
      aria-label={source ? "本地影片" : "NAS 片库"}
      sx={{
        display: "flex",
        flexDirection: "column",
        minHeight: 0,
        minWidth: 0,
        border: `1px solid ${tokens.lineStrong}`,
        borderRadius: "10px",
        overflow: "hidden",
      }}
    >
      <Box
        sx={{
          px: 1.5,
          py: 1,
          bgcolor: "background.paper",
          borderBottom: `1px solid ${tokens.line}`,
          flexShrink: 0,
        }}
      >
        <Box sx={{ display: "flex", alignItems: "center", gap: 0.75 }}>
          <FolderIcon fontSize="small" sx={{ color: tokens.orangeDeep }} />
          <Typography component="h3" variant="body2" sx={{ fontWeight: 600 }}>
            {source ? "本地 · 待整理" : "NAS · 已归档"}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {error ? "—" : items.length}
          </Typography>
          <Tooltip title={source ? "刷新本地影片" : "刷新 NAS 影片"}>
            <span style={{ marginLeft: "auto" }}>
              <IconButton
                size="small"
                aria-label={source ? "刷新本地影片" : "刷新 NAS 影片"}
                disabled={busy}
                onClick={onRefresh}
              >
                <RefreshIcon fontSize="small" />
              </IconButton>
            </span>
          </Tooltip>
        </Box>
        <Typography
          component="div"
          variant="caption"
          noWrap
          title={path}
          sx={{ fontFamily: tokens.mono, color: "text.secondary", fontSize: 10 }}
        >
          {path}
        </Typography>
      </Box>
      <Box
        sx={{
          display: "flex",
          alignItems: "center",
          minHeight: 38,
          px: 1.25,
          borderBottom: `1px solid ${tokens.line}`,
          flexShrink: 0,
        }}
      >
        {source ? (
          <Box
            component="label"
            sx={{ display: "flex", alignItems: "center", fontSize: 12, cursor: "pointer" }}
          >
            <Checkbox
              size="small"
              slotProps={{ input: { "aria-label": "全选本地影片" } }}
              checked={eligible.length > 0 && selectedCount === eligible.length}
              indeterminate={selectedCount > 0 && selectedCount < eligible.length}
              disabled={disabled || !eligible.length}
              onChange={onSelectAll}
              sx={{ p: 0.5, mr: 0.5 }}
            />
            全选
          </Box>
        ) : (
          <Typography variant="caption" color="text.secondary">
            影片文件夹
          </Typography>
        )}
        <Button
          size="small"
          color="inherit"
          onClick={() => setDescending((value) => !value)}
          aria-label={`${source ? "本地" : "NAS"}名称${descending ? "降序" : "升序"}，点击切换`}
          endIcon={descending ? <ArrowDownwardIcon /> : <ArrowUpwardIcon />}
          sx={{ ml: "auto", fontSize: 11, minWidth: 0, color: "text.secondary" }}
        >
          名称{descending ? "倒序" : "正序"}
        </Button>
      </Box>
      <Box sx={{ flex: 1, minHeight: 0, overflowY: "auto", overscrollBehavior: "contain" }}>
        {error && (
          <Alert severity="warning" sx={{ m: 1 }}>
            {error}
            {items.length > 0 ? "；下方保留上次读取结果" : ""}
          </Alert>
        )}
        {sorted.length === 0
          ? !error && (
              <Box sx={{ px: 2, py: 5, textAlign: "center", color: "text.secondary" }}>
                <FolderIcon sx={{ fontSize: 30, color: "text.disabled" }} />
                <Typography variant="body2">
                  {source ? "没有待整理的影片文件夹" : "这个演员目录还没有影片文件夹"}
                </Typography>
              </Box>
            )
          : sorted.map((item) => (
              <Box
                key={item.name}
                sx={{
                  display: "flex",
                  alignItems: "center",
                  gap: 1,
                  minHeight: 58,
                  px: 1.5,
                  py: 1,
                  borderBottom: `1px solid ${tokens.line}`,
                  cursor: source && !disabled ? "pointer" : "default",
                  bgcolor: source && selected.has(item.name) ? tokens.orangeSoft : "transparent",
                  "&:hover": {
                    bgcolor: source && selected.has(item.name) ? tokens.orangeSoft : tokens.hoverBg,
                  },
                }}
              >
                <Box
                  component={source ? "label" : "div"}
                  sx={{
                    display: "flex",
                    alignItems: "center",
                    gap: 1,
                    flex: 1,
                    minWidth: 0,
                    cursor: source && !disabled ? "pointer" : "default",
                  }}
                >
                  {source && (
                    <Checkbox
                      size="small"
                      checked={selected.has(item.name)}
                      slotProps={{ input: { "aria-label": `选择 ${item.name}` } }}
                      disabled={disabled || !!item.error}
                      onChange={() => onToggle(item.name)}
                      sx={{ p: 0.25 }}
                    />
                  )}
                  <FolderIcon sx={{ fontSize: 20, flexShrink: 0, color: tokens.orangeDeep }} />
                  <Box sx={{ minWidth: 0, flex: 1 }}>
                    <Typography
                      component="div"
                      variant="body2"
                      noWrap
                      title={item.name}
                      sx={{ fontSize: 12, fontWeight: 500 }}
                    >
                      {item.name}
                    </Typography>
                    <Typography
                      component="div"
                      variant="caption"
                      color={item.error ? "error" : "text.secondary"}
                      sx={{ fontSize: 10 }}
                    >
                      {item.error ??
                        (item.files == null
                          ? "整文件夹 · 内容数量未统计"
                          : `${item.files} 个文件 · 整文件夹`)}
                    </Typography>
                  </Box>
                  <Typography
                    variant="caption"
                    color="text.secondary"
                    sx={{ fontFamily: tokens.mono, fontSize: 10, flexShrink: 0 }}
                  >
                    {formatBytes(item.size_bytes)}
                  </Typography>
                </Box>
                <Tooltip title="查看完整名称">
                  <IconButton
                    size="small"
                    aria-label={`查看完整名称：${item.name}`}
                    onClick={() => {
                      setCopyMessage("");
                      setFullName(item.name);
                    }}
                    sx={{ flexShrink: 0, p: 0.25 }}
                  >
                    <InfoOutlinedIcon sx={{ fontSize: 16 }} />
                  </IconButton>
                </Tooltip>
              </Box>
            ))}
      </Box>
      <Dialog open={fullName !== null} onClose={() => setFullName(null)} maxWidth="sm" fullWidth>
        <DialogTitle>影片文件夹完整名称</DialogTitle>
        <DialogContent>
          <Typography sx={{ overflowWrap: "anywhere", userSelect: "all", fontFamily: tokens.mono }}>
            {fullName}
          </Typography>
          <Typography
            variant="caption"
            component="p"
            color="text.secondary"
            sx={{ overflowWrap: "anywhere", mt: 1 }}
          >
            所在目录：{path}
          </Typography>
          {copyMessage && (
            <Typography variant="caption" color="text.secondary">
              {copyMessage}
            </Typography>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setFullName(null)}>关闭</Button>
          <Button
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(fullName ?? "");
                setCopyMessage("名称已复制");
              } catch {
                setCopyMessage("请选中上方名称并按 Ctrl+C 复制");
              }
            }}
          >
            复制名称
          </Button>
        </DialogActions>
      </Dialog>
      <Box
        sx={{
          flexShrink: 0,
          px: 1.5,
          py: 0.75,
          borderTop: `1px solid ${tokens.line}`,
          bgcolor: "background.paper",
        }}
      >
        <Typography variant="caption" color="text.secondary" sx={{ fontSize: 10 }}>
          {source
            ? `已选 ${selectedCount} / ${items.length} 部${error ? " · 目录未刷新" : ""}`
            : error
              ? "读取未完成 · 等待恢复连接"
              : `${items.length} 部影片文件夹 · 仅查看`}
        </Typography>
      </Box>
    </Box>
  );
}

export function ArchiveTransferView({
  localDir,
  subdir,
  performer,
  onBack,
}: {
  localDir: string;
  subdir: string;
  performer: PerformerOut;
  onBack: () => void;
}) {
  const transfer = useTransfer();
  const cleanup = useCleanup();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pathOpen, setPathOpen] = useState(false);
  const [toast, setToast] = useState("");
  const [startError, setStartError] = useState("");
  const finished = useRef<string | null>(null);
  const contextKey = `${localDir}\0${subdir}\0${performer.id}`;
  const context = useArchiveContext(localDir, subdir, performer.id);
  const data = context.data;
  useEffect(() => {
    // Clear only when the actor/batch/target changes. A transient retry of the
    // same directory must preserve checked folders that still exist.
    void contextKey;
    setSelected(new Set());
    setStartError("");
  }, [contextKey]);
  useEffect(() => {
    if (!data || data.source_error) return;
    const eligible = new Set(
      data.source_items.filter((item) => !item.error).map((item) => item.name),
    );
    setSelected((current) => {
      const next = new Set([...current].filter((name) => eligible.has(name)));
      return next.size === current.size ? current : next;
    });
  }, [data]);

  const currentTask = transfer.task;
  const matchesTask =
    currentTask?.local_dir === localDir &&
    currentTask.subdir === subdir &&
    currentTask.performer_id === performer.id;
  const submissionConfirmed =
    currentTask?.request_id != null &&
    currentTask.request_id === transfer.start.variables?.request_id;
  useEffect(() => {
    if (submissionConfirmed) setStartError("");
  }, [submissionConfirmed]);
  useEffect(() => {
    if (!currentTask || transfer.active || !matchesTask || finished.current === currentTask.id)
      return;
    finished.current = currentTask.id;
    setSelected(new Set());
    void context.refetch();
  }, [currentTask, matchesTask, transfer.active, context.refetch]);

  const openPair = useMutation({
    mutationFn: () => openArchivePair(subdir, performer.id),
    onError: (error) => setToast(archiveError(error, "双开失败")),
  });
  const disabled =
    !data ||
    !!data.source_error ||
    !!data.target_error ||
    context.fetchStatus !== "idle" ||
    context.isError ||
    transfer.active ||
    transfer.unavailable ||
    cleanup.blocked ||
    transfer.start.isPending;
  const taskItems = matchesTask ? currentTask.items : [];
  const sourceItems = (data?.source_items ?? [])
    .filter(
      (item) =>
        !transfer.active ||
        !taskItems.some((done) => done.name === item.name && done.status === "completed"),
    )
    .map((item) =>
      taskItems.some((done) => done.name === item.name && done.status === "cleanup_pending")
        ? { ...item, error: "副本已保存，本地清理未完成；请核对后手动处理" }
        : item,
    );
  const targetItems = [...(data?.target_items ?? [])];
  if (transfer.active) {
    for (const item of taskItems) {
      if (
        item.status === "completed" &&
        !targetItems.some((existing) => existing.name === item.name)
      ) {
        targetItems.push({
          name: item.name,
          size_bytes: item.size_bytes,
          files: null,
          error: null,
        });
      }
    }
  }
  const toggle = (name: string) =>
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  const selectAll = () => {
    const names = sourceItems.filter((item) => !item.error).map((item) => item.name);
    setSelected((current) =>
      names.every((name) => current.has(name)) ? new Set() : new Set(names),
    );
  };
  const submit = async () => {
    if (!data || disabled || selected.size === 0) return;
    setStartError("");
    try {
      await transfer.start.mutateAsync({
        context_id: data.id,
        names: [...selected].sort(nameOrder.compare),
        request_id: crypto.randomUUID(),
      });
      setSelected(new Set());
    } catch (error) {
      setStartError(archiveError(error, "启动失败，请查看任务状态"));
      // In particular, a stale context is not refreshed and re-submitted
      // automatically: the user must inspect the new directory snapshot.
      setSelected(new Set());
    }
  };
  const targetPath = data?.target_path ?? performer.unc_path;
  const refresh = () => {
    setStartError("");
    void context.refetch();
  };
  return (
    <Box
      sx={{
        flex: 1,
        minWidth: 0,
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
      }}
    >
      <Box sx={{ px: { xs: 2, lg: 3 }, pt: 0.75, flexShrink: 0 }}>
        <Button
          size="small"
          color="inherit"
          startIcon={<ArrowBackIcon />}
          onClick={onBack}
          sx={{ color: "text.secondary", fontSize: 12, px: 0, minHeight: 26, mb: 0.5 }}
        >
          匹配结果
        </Button>
        <Box sx={{ display: "flex", alignItems: "center", gap: 1.75, pb: 1.25 }}>
          {performer.has_thumb ? (
            <Box
              component="img"
              src={`${serverBase()}/api/performers/${performer.id}/thumb`}
              alt={`${performer.name} 头像`}
              sx={{
                width: { xs: 38, lg: 50 },
                height: { xs: 54, lg: 72 },
                objectFit: "cover",
                objectPosition: "center top",
                borderRadius: "7px",
                flexShrink: 0,
              }}
            />
          ) : (
            <Box
              aria-label={`${performer.name} 首字头像`}
              sx={{
                width: { xs: 38, lg: 50 },
                height: { xs: 54, lg: 72 },
                borderRadius: "7px",
                flexShrink: 0,
                bgcolor: tokens.orangeSoft,
                color: tokens.orangeText,
                display: "grid",
                placeItems: "center",
                fontSize: 24,
              }}
            >
              {performer.name.charAt(0)}
            </Box>
          )}
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Box sx={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 1, mb: 0.5 }}>
              <Typography component="h2" sx={{ fontSize: 20, fontWeight: 650, lineHeight: 1.3 }}>
                {performer.name}
              </Typography>
              <Chip
                size="small"
                variant="outlined"
                label={performer.source_label ?? "NAS 片库"}
                sx={{ height: 22, fontSize: 11 }}
              />
              {data && (
                <Typography
                  variant="caption"
                  color={data.target_error ? "error" : "text.secondary"}
                >
                  {data.target_error ? "暂不可访问" : `${targetItems.length} 部已归档`}
                </Typography>
              )}
            </Box>
            <Box sx={{ display: "flex", alignItems: "center", minWidth: 0 }}>
              <Typography
                variant="caption"
                noWrap
                title={targetPath}
                sx={{ fontFamily: tokens.mono, color: "text.secondary", fontSize: 11 }}
              >
                {targetPath}
              </Typography>
              <Tooltip title="查看并复制完整路径">
                <IconButton
                  size="small"
                  aria-label="查看并复制完整路径"
                  onClick={() => setPathOpen(true)}
                  sx={{ flexShrink: 0, p: 0.5 }}
                >
                  <ContentCopyIcon sx={{ fontSize: 15 }} />
                </IconButton>
              </Tooltip>
            </Box>
          </Box>
          <Box
            sx={{
              display: "flex",
              flexDirection: "column",
              alignItems: "flex-end",
              gap: 0.25,
              flexShrink: 0,
            }}
          >
            <Button
              variant="outlined"
              color="inherit"
              size="small"
              startIcon={<SwapHorizIcon />}
              onClick={() => openPair.mutate()}
              disabled={openPair.isPending || !!data?.target_error}
              sx={{ fontSize: 12, whiteSpace: "nowrap" }}
            >
              双开文件夹
            </Button>
            <Button
              size="small"
              color="inherit"
              onClick={onBack}
              sx={{ fontSize: 11, minHeight: 22, py: 0, color: "text.secondary" }}
            >
              更换归档目标
            </Button>
          </Box>
        </Box>
      </Box>
      <Box
        sx={{
          px: { xs: 2, lg: 3 },
          flex: 1,
          minHeight: 0,
          display: "flex",
          flexDirection: "column",
        }}
      >
        <Box
          sx={{
            borderTop: `1px solid ${tokens.line}`,
            py: 1.25,
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexShrink: 0,
          }}
        >
          <Typography component="h2" variant="body2" sx={{ fontWeight: 600 }}>
            影片文件夹
          </Typography>
          <Typography
            variant="caption"
            color="text.secondary"
            sx={{ display: "flex", alignItems: "center", gap: 1 }}
          >
            本地 batch <ArrowForwardIcon sx={{ fontSize: 15, color: tokens.orangeDeep }} /> NAS 片库
          </Typography>
        </Box>
        {context.isPaused && (
          <Alert severity="info" sx={{ mb: 1 }} role="status">
            目录读取已暂停，等待连接恢复。恢复后将自动继续读取。
          </Alert>
        )}
        {context.retrying && (
          <Alert severity="info" sx={{ mb: 1 }} role="status">
            目录读取暂未完成，正在自动重试（{Math.min(context.failureCount, DIRECTORY_RETRIES)} /{" "}
            {DIRECTORY_RETRIES}）…
          </Alert>
        )}
        {context.isError && context.fetchStatus === "idle" && (
          <Alert
            severity="error"
            action={
              <Button color="inherit" onClick={refresh}>
                重试读取目录
              </Button>
            }
            sx={{ mb: 1 }}
          >
            {data?.source_error || data?.target_error
              ? "目录读取未完成，请稍后重试。"
              : archiveError(context.error, "目录读取失败")}
            {context.failureCount > DIRECTORY_RETRIES
              ? ` 已自动重试 ${DIRECTORY_RETRIES} 次。`
              : ""}
          </Alert>
        )}
        {startError && (
          <Alert
            severity="error"
            action={
              <Button color="inherit" onClick={refresh} disabled={transfer.active}>
                刷新目录
              </Button>
            }
            sx={{ mb: 1 }}
          >
            {startError}
          </Alert>
        )}
        {matchesTask &&
          !transfer.active &&
          currentTask?.status !== "completed" &&
          currentTask?.message && (
            <Alert severity="warning" sx={{ mb: 1 }}>
              {currentTask.message}
            </Alert>
          )}
        {context.isPending && !data ? (
          <Box sx={{ flex: 1, display: "grid", placeItems: "center" }}>
            <CircularProgress aria-label="读取影片目录" />
          </Box>
        ) : data ? (
          <Box
            sx={{
              flex: 1,
              minHeight: 0,
              display: "grid",
              gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
              gridTemplateRows: "minmax(0, 1fr)",
              gap: { xs: 1.25, lg: 2 },
            }}
          >
            <FolderPane
              source
              path={data.source_path}
              items={sourceItems}
              error={data.source_error}
              selected={selected}
              disabled={disabled}
              busy={context.fetchStatus !== "idle" || transfer.active}
              onToggle={toggle}
              onSelectAll={selectAll}
              onRefresh={refresh}
            />
            <FolderPane
              source={false}
              path={data.target_path}
              items={targetItems}
              error={data.target_error}
              selected={new Set()}
              disabled
              busy={context.fetchStatus !== "idle" || transfer.active}
              onToggle={() => {}}
              onSelectAll={() => {}}
              onRefresh={refresh}
            />
          </Box>
        ) : (
          <Box sx={{ flex: 1 }} />
        )}
      </Box>
      <Box
        component="footer"
        sx={{
          display: "flex",
          alignItems: "center",
          gap: 1.5,
          flexShrink: 0,
          mt: 1.5,
          px: { xs: 2, lg: 3 },
          py: 1.5,
          borderTop: `1px solid ${tokens.line}`,
          bgcolor: "background.paper",
        }}
      >
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>
            {selected.size ? `已选 ${selected.size} 部影片` : "勾选左侧影片，开始归档"}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {transfer.active
              ? "当前归档任务进行中，可在下方查看进度。"
              : cleanup.blocked
                ? "清理进行中或状态待确认，暂不能开始传输。"
                : transfer.unavailable
                  ? "任务状态暂不可确认，请等待恢复。"
                  : "完整复制并校验后，移除对应本地影片文件夹。"}
          </Typography>
        </Box>
        {selected.size > 0 && (
          <Button size="small" color="inherit" onClick={() => setSelected(new Set())}>
            取消选择
          </Button>
        )}
        <Button
          variant="contained"
          disableElevation
          endIcon={<ArrowForwardIcon />}
          onClick={submit}
          disabled={disabled || selected.size === 0}
          sx={{ whiteSpace: "nowrap", fontWeight: 600 }}
        >
          {transfer.start.isPending
            ? "正在提交…"
            : `传输到 NAS${selected.size ? `（${selected.size}）` : ""}`}
        </Button>
      </Box>
      <Dialog open={pathOpen} onClose={() => setPathOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>完整目录路径</DialogTitle>
        <DialogContent>
          <Typography sx={{ fontFamily: tokens.mono, overflowWrap: "anywhere", userSelect: "all" }}>
            {targetPath}
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setPathOpen(false)}>关闭</Button>
          <Button
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(targetPath);
                setToast("路径已复制");
              } catch {
                setToast("无法自动复制，请选中上方路径并按 Ctrl+C");
              }
            }}
          >
            复制路径
          </Button>
        </DialogActions>
      </Dialog>
      <Snackbar
        open={!!toast}
        message={toast}
        autoHideDuration={4000}
        onClose={() => setToast("")}
      />
    </Box>
  );
}
