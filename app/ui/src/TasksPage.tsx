import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import MenuItem from "@mui/material/MenuItem";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { archiveError, getOperationLogs, getTransfers, type LogFilters } from "./archiveApi";
import { TransferRecovery, TransferTaskCard, useTransfer } from "./TransferProvider";
import { tokens } from "./theme";

const outcomes = {
  started: "准备（结果未确认）",
  succeeded: "已确认成功",
  failed: "失败",
  skipped: "跳过／保留",
  stopped: "已停止",
  needs_review: "待核对",
};
const actions: Record<string, string> = {
  transfer_accepted: "接受归档任务",
  transfer_started: "开始归档",
  stop_requested: "请求停止",
  transfer_finished: "归档结束",
  transfer_conflict: "同名冲突",
  transfer_recovered: "重启核对",
  copy_movie: "复制影片",
  verify_movie: "校验影片",
  commit_movie: "提交影片到片库",
  delete_source_file: "删除来源文件",
  delete_source_directory: "删除来源目录",
  cleanup_accepted: "接受空目录清理",
  cleanup_started: "开始检查空目录",
  delete_empty_actor: "清理空演员目录",
  cleanup_finished: "空目录清理结束",
};

export function TaskCenter({ onViewArchive }: { onViewArchive?: () => void }) {
  const transfer = useTransfer();
  const [tab, setTab] = useState(0);
  const [draft, setDraft] = useState<LogFilters>({});
  const [filters, setFilters] = useState<LogFilters>({});
  // History pages are independent of the authoritative, frequently polled
  // active snapshot. A slow old page cannot replace the current active list.
  const history = useInfiniteQuery({
    queryKey: ["archive-transfer-history"],
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam, signal }) => getTransfers(signal, { before_id: pageParam }),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: tab === 0,
    networkMode: "always",
    retry: false,
  });
  const logs = useInfiniteQuery({
    queryKey: ["archive-operation-logs", filters],
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam, signal }) =>
      getOperationLogs({ ...filters, before_id: pageParam }, signal),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: tab === 1,
    networkMode: "always",
    retry: false,
  });
  const seen = new Set(transfer.activeTasks.map((task) => task.id));
  const past = [
    ...transfer.history,
    ...(history.data?.pages.flatMap((page) => page.history) ?? []),
  ].filter((task) => {
    if (seen.has(task.id)) return false;
    seen.add(task.id);
    return true;
  });
  const events = logs.data?.pages.flatMap((page) => page.items) ?? [];
  const showLogs = (taskId: string) => {
    const next = { task_id: taskId };
    setFilters(next);
    setDraft(next);
    setTab(1);
  };
  return (
    <Box
      sx={{
        height: "100%",
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
        p: { xs: 2, lg: 3 },
        gap: 1.5,
      }}
    >
      <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
        <Typography component="h1" variant="h6" sx={{ flex: 1 }}>
          任务与日志
        </Typography>
        <Button
          onClick={() =>
            tab === 0 ? Promise.all([transfer.refresh(), history.refetch()]) : logs.refetch()
          }
        >
          刷新{tab === 0 ? "任务" : "日志"}
        </Button>
      </Box>
      <Tabs
        value={tab}
        onChange={(_event, value: number) => setTab(value)}
        aria-label="任务中心页签"
      >
        <Tab label="归档任务" />
        <Tab label="操作日志" />
      </Tabs>
      <Box
        sx={{
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          gap: 1.5,
        }}
      >
        {tab === 0 ? (
          <>
            <TransferRecovery />
            <Typography variant="body2" color="text.secondary">
              {transfer.activeTasks.length} 个活动任务 · 同时执行上限 {transfer.concurrency}
            </Typography>
            {transfer.active && !transfer.activeTasks.length && (
              <Alert severity="info">服务正在恢复或安全收尾，请等待状态确认。</Alert>
            )}
            {transfer.pendingRequests
              .filter((request) => request.state === "sending")
              .map((request) => (
                <Alert key={request.body.request_id} severity="info">
                  {request.binding?.performer_name ?? "归档任务"} · 正在提交
                </Alert>
              ))}
            {transfer.activeTasks.map((task) => (
              <Box key={task.id}>
                <TransferTaskCard
                  task={task}
                  onViewArchive={onViewArchive}
                  onViewLogs={() => showLogs(task.id)}
                />
                {task.status !== "queued" && task.status !== "running" && (
                  <Typography variant="caption">正在完成收尾，请稍候。</Typography>
                )}
              </Box>
            ))}
            <Typography component="h2" variant="subtitle1">
              已结束任务
            </Typography>
            {past.map((task) => (
              <TransferTaskCard
                key={task.id}
                task={task}
                onViewArchive={onViewArchive}
                onViewLogs={() => showLogs(task.id)}
              />
            ))}
            {!past.length && history.isSuccess && (
              <Typography color="text.secondary">暂无已结束任务。</Typography>
            )}
            {history.isError && (
              <Alert
                severity="warning"
                action={
                  <Button
                    onClick={() =>
                      history.isFetchNextPageError ? history.fetchNextPage() : history.refetch()
                    }
                  >
                    重试
                  </Button>
                }
              >
                {archiveError(history.error, "历史任务读取失败")}
              </Alert>
            )}
            {history.hasNextPage && (
              <Button disabled={history.isFetching} onClick={() => history.fetchNextPage()}>
                加载更早任务
              </Button>
            )}
          </>
        ) : (
          <>
            <Box
              component="form"
              onSubmit={(event) => {
                event.preventDefault();
                setFilters(draft);
              }}
              sx={{ display: "flex", gap: 1, flexWrap: "wrap" }}
            >
              {filters.task_id && (
                <Typography variant="body2" sx={{ alignSelf: "center" }}>
                  仅查看所选任务
                </Typography>
              )}
              <TextField
                size="small"
                select
                label="操作"
                value={draft.action ?? ""}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, action: event.target.value }))
                }
                sx={{ minWidth: 180 }}
              >
                <MenuItem value="">全部操作</MenuItem>
                {Object.entries(actions).map(([value, label]) => (
                  <MenuItem value={value} key={value}>
                    {label}
                  </MenuItem>
                ))}
              </TextField>
              <TextField
                size="small"
                select
                label="结果"
                value={draft.outcome ?? ""}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, outcome: event.target.value }))
                }
                sx={{ minWidth: 180 }}
              >
                <MenuItem value="">全部结果</MenuItem>
                {Object.entries(outcomes).map(([value, label]) => (
                  <MenuItem value={value} key={value}>
                    {label}
                  </MenuItem>
                ))}
              </TextField>
              <Button type="submit">筛选</Button>
              <Button
                onClick={() => {
                  setDraft({});
                  setFilters({});
                }}
              >
                清除筛选
              </Button>
            </Box>
            <Typography variant="caption" color="text.secondary">
              记录本应用的归档与空目录清理。准备记录表示结果尚未确认；只有成功回执确认对应操作已完成。
            </Typography>
            {logs.isPending && <Typography>正在读取日志…</Typography>}
            {logs.isError && (
              <Alert
                severity="warning"
                action={
                  <Button
                    onClick={() =>
                      logs.isFetchNextPageError ? logs.fetchNextPage() : logs.refetch()
                    }
                  >
                    重试
                  </Button>
                }
              >
                {archiveError(logs.error, "日志读取失败")}
              </Alert>
            )}
            {!events.length && logs.isSuccess && (
              <Typography color="text.secondary">没有符合筛选条件的日志。</Typography>
            )}
            {events.map((event) => (
              <Box
                component="article"
                aria-label={`日志 ${event.id}`}
                data-operation-id={event.operation_id}
                key={event.id}
                sx={{ border: `1px solid ${tokens.line}`, borderRadius: 2, p: 1.5 }}
              >
                <Typography variant="body2" sx={{ fontWeight: 600 }}>
                  {event.item_name ??
                    event.performer_name ??
                    (event.kind === "cleanup" ? "空目录清理" : "归档任务")}{" "}
                  · {outcomes[event.outcome]}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {event.created_at} · {actions[event.action] ?? event.action}
                </Typography>
                {event.message && (
                  <Typography variant="body2" sx={{ overflowWrap: "anywhere" }}>
                    {event.message}
                  </Typography>
                )}
                {event.source_path && (
                  <Typography variant="caption" component="div" sx={{ overflowWrap: "anywhere" }}>
                    来源：{event.source_path}
                  </Typography>
                )}
                {event.target_path && (
                  <Typography variant="caption" component="div" sx={{ overflowWrap: "anywhere" }}>
                    目标：{event.target_path}
                  </Typography>
                )}
                {!filters.task_id && (
                  <Button size="small" onClick={() => showLogs(event.task_id)}>
                    只看此任务
                  </Button>
                )}
              </Box>
            ))}
            {logs.hasNextPage && (
              <Button disabled={logs.isFetching} onClick={() => logs.fetchNextPage()}>
                加载更早日志
              </Button>
            )}
          </>
        )}
      </Box>
    </Box>
  );
}

export function TasksPage() {
  const navigate = useNavigate();
  return (
    <TaskCenter
      onViewArchive={() => {
        void navigate({ to: "/archive" });
      }}
    />
  );
}
