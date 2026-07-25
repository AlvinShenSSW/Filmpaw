import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import FolderOpenIcon from "@mui/icons-material/FolderOpen";
import RefreshIcon from "@mui/icons-material/Refresh";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import IconButton from "@mui/material/IconButton";
import MenuItem from "@mui/material/MenuItem";
import Snackbar from "@mui/material/Snackbar";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { serverBase } from "./api";
import type { PerformerOut } from "./client";
import {
  addAliasApiPerformersPerformerIdAliasesPost,
  deleteAliasApiAliasesAliasIdDelete,
  deletePerformerApiPerformersPerformerIdDelete,
  listPerformersApiPerformersGet,
  listSourcesApiSourcesGet,
  openPerformerApiPerformersPerformerIdOpenPost,
  purgeMissingApiPerformersPurgeMissingPost,
  scanAllApiScanAllPost,
} from "./client";
import { tokens } from "./theme";

/** Poster sizes (issue #27 — the old 34×48 / 52×74 were unreadable).
 * 2:3 poster ratio; `grid` is the library tile, `card` the archive match. */
export const POSTER = {
  grid: { w: 110, h: 156, font: 40 },
  card: { w: 156, h: 222, font: 56 },
} as const;

export function PosterAvatar({
  performer,
  size = "grid",
}: {
  performer: Pick<PerformerOut, "id" | "name" | "has_thumb" | "is_missing">;
  size?: keyof typeof POSTER;
}) {
  const { w, h, font } = POSTER[size];
  const common = {
    width: w,
    height: h,
    borderRadius: "10px",
    flexShrink: 0,
    opacity: performer.is_missing ? 0.45 : 1,
  } as const;
  if (performer.has_thumb) {
    return (
      <Box
        component="img"
        src={`${serverBase()}/api/performers/${performer.id}/thumb`}
        alt={`${performer.name} 头像`}
        sx={{ ...common, objectFit: "cover", objectPosition: "center top" }}
      />
    );
  }
  return (
    <Box
      aria-label={`${performer.name} 首字头像`}
      sx={{
        ...common,
        bgcolor: tokens.orangeSoft,
        color: tokens.orangeText, // 6.79:1 on soft; orangeDeep was only 4.27:1
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontWeight: 700,
        fontSize: font,
      }}
    >
      {performer.name.charAt(0)}
    </Box>
  );
}

function detailOf(error: unknown, fallback: string): string {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  return typeof detail === "string" ? detail : fallback;
}

export function PerformersPage() {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  const [showMissing, setShowMissing] = useState(true);
  const [aliasEditFor, setAliasEditFor] = useState<string | null>(null);
  const [aliasInput, setAliasInput] = useState("");
  const [toast, setToast] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<PerformerOut | null>(null);
  const [confirmPurge, setConfirmPurge] = useState(false);
  const [sourceFilter, setSourceFilter] = useState<number | "">("");

  useEffect(() => {
    const t = setTimeout(() => setQ(search), 300);
    return () => clearTimeout(t);
  }, [search]);

  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: async () => {
      const r = await listSourcesApiSourcesGet();
      if (r.error) throw r.error;
      return r.data ?? [];
    },
  });

  // A deleted source must not linger as the active filter (its id would
  // 422 every subsequent fetch and strand the page).
  useEffect(() => {
    if (sourceFilter !== "" && sources.data && !sources.data.some((s) => s.id === sourceFilter)) {
      setSourceFilter("");
    }
  }, [sources.data, sourceFilter]);

  const performers = useInfiniteQuery({
    queryKey: ["performers", q, showMissing, sourceFilter],
    queryFn: async ({ pageParam }) => {
      const r = await listPerformersApiPerformersGet({
        query: {
          q,
          include_missing: showMissing,
          page: pageParam,
          page_size: 200,
          ...(sourceFilter !== "" ? { source_id: sourceFilter } : {}),
        },
      });
      if (r.error || !r.data) throw r.error ?? new Error("list");
      return r.data;
    },
    initialPageParam: 1,
    getNextPageParam: (last) =>
      last.page * last.page_size < last.total ? last.page + 1 : undefined,
  });
  const refetch = () => queryClient.invalidateQueries({ queryKey: ["performers"] });

  const addAlias = useMutation({
    mutationFn: async (vars: { performerId: string; alias: string }) => {
      const r = await addAliasApiPerformersPerformerIdAliasesPost({
        path: { performer_id: vars.performerId },
        body: { alias: vars.alias },
      });
      if (r.error) throw r.error;
    },
    onSuccess: () => {
      setAliasEditFor(null);
      setAliasInput("");
      refetch();
    },
    onError: (e) => setToast(detailOf(e, "添加别名失败")),
  });

  const deleteAlias = useMutation({
    mutationFn: async (aliasId: number) => {
      const r = await deleteAliasApiAliasesAliasIdDelete({ path: { alias_id: aliasId } });
      if (r.error) throw r.error;
    },
    onSuccess: refetch,
    onError: (e) => setToast(detailOf(e, "删除别名失败")),
  });

  const openFolder = useMutation({
    mutationFn: async (performerId: string) => {
      const r = await openPerformerApiPerformersPerformerIdOpenPost({
        path: { performer_id: performerId },
      });
      if (r.error) throw r.error;
    },
    onError: (e) => {
      setToast(detailOf(e, "打开失败"));
      refetch(); // a 404 flags the row missing server-side
    },
  });

  const deleteOne = useMutation({
    mutationFn: async (performerId: string) => {
      const r = await deletePerformerApiPerformersPerformerIdDelete({
        path: { performer_id: performerId },
      });
      if (r.error) throw r.error;
    },
    onSuccess: () => {
      setConfirmDelete(null);
      refetch();
    },
    onError: (e) => setToast(detailOf(e, "删除失败")), // dialog stays open
  });

  const purge = useMutation({
    mutationFn: async () => {
      const r = await purgeMissingApiPerformersPurgeMissingPost();
      if (r.error || !r.data) throw r.error ?? new Error("purge");
      return r.data;
    },
    onSuccess: (d) => {
      setConfirmPurge(false);
      setToast(`已清理 ${d.deleted} 条失效记录`);
      refetch();
    },
    onError: (e) => setToast(detailOf(e, "清理失败")), // dialog stays open
  });

  const rescanAll = useMutation({
    mutationFn: async () => {
      const r = await scanAllApiScanAllPost();
      if (r.error || !r.data) throw r.error ?? new Error("scan");
      return r.data;
    },
    onSuccess: (results) => {
      const ok = results.filter((x) => x.ok);
      const bad = results.length - ok.length;
      const added = ok.reduce((n, x) => n + (x.added ?? 0), 0);
      const refreshed = ok.reduce((n, x) => n + (x.refreshed ?? 0), 0);
      const missing = ok.reduce((n, x) => n + (x.missing ?? 0), 0);
      setToast(
        `重扫完成: 新增 ${added} · 更新 ${refreshed} · 失效 ${missing}` +
          (bad ? ` · ${bad} 个源不可达已跳过` : ""),
      );
      refetch();
      // counts/last-scan changed; a deleted source also falls out of the
      // filter dropdown via the stale-filter effect
      queryClient.invalidateQueries({ queryKey: ["sources"] });
    },
    onError: (e) => setToast(detailOf(e, "重扫失败")),
  });

  const pages = performers.data?.pages;
  const items = pages?.flatMap((p) => p.items) ?? [];
  const stats = pages?.[0];
  // GLOBAL missing count from the envelope — the purge endpoint deletes ALL
  // missing rows regardless of the current search/filter view.
  const missingTotal = stats?.missing_total ?? 0;

  return (
    <Box sx={{ p: 3, display: "flex", flexDirection: "column", height: "100%" }}>
      <Box
        sx={{
          display: "flex",
          gap: 1.5,
          alignItems: "center",
          mb: 1.5,
          flexWrap: "wrap",
          "& > .MuiButtonBase-root, & > .MuiFormControlLabel-root": { flexShrink: 0 },
        }}
      >
        <TextField
          size="small"
          placeholder="搜索名字或别名(支持繁简互搜)"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          slotProps={{ htmlInput: { "aria-label": "搜索表演者" } }}
          sx={{ flex: 1, minWidth: 200 }}
        />
        <TextField
          select
          size="small"
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value === "" ? "" : Number(e.target.value))}
          sx={{ minWidth: 150 }}
          disabled={sources.isError}
          error={sources.isError}
          helperText={sources.isError ? "来源列表加载失败" : undefined}
          slotProps={{
            htmlInput: { "aria-label": "来源筛选" },
            select: { displayEmpty: true },
          }}
        >
          <MenuItem value="">全部来源</MenuItem>
          {sources.data?.map((s) => (
            <MenuItem key={s.id} value={s.id}>
              {s.label ?? s.unc_path}
            </MenuItem>
          ))}
        </TextField>
        <FormControlLabel
          control={
            <Switch
              size="small"
              checked={showMissing}
              onChange={(e) => setShowMissing(e.target.checked)}
            />
          }
          label={<Typography variant="caption">显示失效</Typography>}
          sx={{ whiteSpace: "nowrap", mr: 0 }}
        />
        <Button
          variant="outlined"
          color="inherit"
          size="small"
          onClick={() => setConfirmPurge(true)}
          disabled={missingTotal === 0}
          sx={{ whiteSpace: "nowrap", color: "text.secondary", borderColor: tokens.lineStrong }}
        >
          清理失效
        </Button>
        <Button
          variant="contained"
          disableElevation
          size="small"
          startIcon={<RefreshIcon />}
          onClick={() => rescanAll.mutate()}
          disabled={rescanAll.isPending}
          sx={{ whiteSpace: "nowrap", fontWeight: 600 }}
        >
          全部重扫
        </Button>
      </Box>

      <Box sx={{ flex: 1, minHeight: 0, overflowY: "auto", pr: 0.5 }}>
        {performers.isError && (
          <Box sx={{ textAlign: "center", py: 6 }} role="alert">
            <Typography variant="body2" sx={{ color: tokens.bad, mb: 1 }}>
              列表加载失败 — 请确认 server 正在运行
            </Typography>
            <Button size="small" onClick={() => performers.refetch()}>
              重试
            </Button>
          </Box>
        )}
        {!performers.isError && pages && items.length === 0 && (
          <Box sx={{ textAlign: "center", py: 6, color: "text.secondary" }}>
            <Typography variant="body2">
              {q
                ? `没有匹配「${q}」的记录 — 名字与别名均未命中`
                : "还没有表演者 — 先在设置里添加 NAS 扫描源, 再点扫描建立索引"}
            </Typography>
          </Box>
        )}
        <Box
          aria-label="表演者列表"
          sx={{
            display: "grid",
            gridTemplateColumns: `repeat(auto-fill, minmax(${POSTER.grid.w + 40}px, 1fr))`,
            gap: 2,
            alignItems: "start",
          }}
        >
          {items.map((p) => (
            <Box
              key={p.id}
              sx={{
                border: `1px solid ${tokens.line}`,
                borderRadius: "12px",
                p: 1.5,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                gap: 0.75,
                opacity: p.is_missing ? 0.55 : 1,
                transition: "border-color 180ms ease",
                "&:hover": { borderColor: tokens.lineStrong },
              }}
            >
              <Tooltip title={p.unc_path} placement="top">
                <Box sx={{ position: "relative", display: "flex" }}>
                  <PosterAvatar performer={p} size="grid" />
                </Box>
              </Tooltip>

              <Typography
                variant="subtitle2"
                noWrap
                title={p.name}
                sx={{ maxWidth: "100%", textAlign: "center" }}
              >
                {p.name}
              </Typography>

              <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                <Typography
                  variant="caption"
                  sx={{ color: p.is_missing ? tokens.muted : tokens.ok }}
                >
                  {p.is_missing ? "○ 失效" : "● 在线"}
                </Typography>
                <Tooltip title={p.id}>
                  <Typography
                    variant="caption"
                    sx={{ fontFamily: tokens.mono, color: "text.secondary" }}
                  >
                    {p.id.slice(0, 4)}
                  </Typography>
                </Tooltip>
              </Box>

              <Box
                sx={{
                  display: "flex",
                  flexWrap: "wrap",
                  justifyContent: "center",
                  gap: 0.5,
                  width: "100%",
                }}
              >
                {p.aliases.map((a) => (
                  <Chip
                    key={a.id}
                    label={a.alias}
                    size="small"
                    onDelete={() => deleteAlias.mutate(a.id)}
                  />
                ))}
                {aliasEditFor === p.id ? (
                  <TextField
                    size="small"
                    autoFocus
                    value={aliasInput}
                    placeholder="别名后回车"
                    onChange={(e) => setAliasInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && aliasInput.trim()) {
                        addAlias.mutate({ performerId: p.id, alias: aliasInput.trim() });
                      }
                      if (e.key === "Escape") setAliasEditFor(null);
                    }}
                    slotProps={{ htmlInput: { "aria-label": `为 ${p.name} 添加别名` } }}
                    sx={{ width: "100%", "& input": { py: 0.4, fontSize: 12 } }}
                  />
                ) : (
                  <Button
                    size="small"
                    onClick={() => {
                      setAliasEditFor(p.id);
                      setAliasInput("");
                    }}
                    sx={{ color: tokens.orangeDeep, fontSize: 12, minWidth: 0, px: 1 }}
                  >
                    ＋ 别名
                  </Button>
                )}
              </Box>

              <Box sx={{ display: "flex", gap: 0.5 }}>
                <IconButton
                  size="small"
                  aria-label={`打开 ${p.name} 的文件夹`}
                  onClick={() => openFolder.mutate(p.id)}
                  sx={{ color: tokens.orangeDeep }}
                >
                  <FolderOpenIcon fontSize="small" />
                </IconButton>
                {p.is_missing && (
                  <IconButton
                    size="small"
                    aria-label={`删除失效记录 ${p.name}`}
                    onClick={() => setConfirmDelete(p)}
                  >
                    <DeleteOutlineIcon fontSize="small" />
                  </IconButton>
                )}
              </Box>
            </Box>
          ))}
        </Box>
      </Box>

      <Box sx={{ display: "flex", alignItems: "center", gap: 2, mt: 1 }}>
        <Typography variant="caption" color="text.secondary">
          共 {stats?.total ?? 0} 条 ·{" "}
          {sourceFilter !== ""
            ? `当前来源: ${sources.data?.find((s) => s.id === sourceFilter)?.label ?? sourceFilter}`
            : `${stats?.source_count ?? 0} 个来源`}
          {items.length < (stats?.total ?? 0) ? ` · 已显示 ${items.length}` : ""}
        </Typography>
        {performers.hasNextPage && (
          <Button
            size="small"
            onClick={() => performers.fetchNextPage()}
            disabled={performers.isFetchingNextPage}
            sx={{ color: tokens.orangeDeep, fontSize: 12 }}
          >
            加载更多
          </Button>
        )}
      </Box>

      <Dialog open={confirmDelete !== null} onClose={() => setConfirmDelete(null)}>
        <DialogTitle>删除失效记录?</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            {confirmDelete?.name} — {confirmDelete?.unc_path}
            <br />
            仅删除这条记录, 同名组的别名保留。
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmDelete(null)}>取消</Button>
          <Button color="error" onClick={() => confirmDelete && deleteOne.mutate(confirmDelete.id)}>
            删除
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={confirmPurge} onClose={() => setConfirmPurge(false)}>
        <DialogTitle>清理全部失效记录?</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            全库共 {missingTotal} 条失效记录将被删除(不受当前搜索/筛选影响), 组别名保留。
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmPurge(false)}>取消</Button>
          <Button color="error" onClick={() => purge.mutate()}>
            清理
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar
        open={toast !== ""}
        autoHideDuration={4000}
        onClose={() => setToast("")}
        message={toast}
      />
    </Box>
  );
}
