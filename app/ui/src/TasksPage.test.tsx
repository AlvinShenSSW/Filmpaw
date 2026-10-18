import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import {
  type ArchiveTask,
  getOperationLogs,
  getTransfers,
  type OperationEvent,
  stopTransfer,
  type TransferList,
} from "./archiveApi";
import { TaskCenter } from "./TasksPage";
import { TransferProvider } from "./TransferProvider";
import { theme } from "./theme";

vi.mock("./archiveApi", async (original) => ({
  ...(await original<typeof import("./archiveApi")>()),
  getTransfers: vi.fn(),
  getOperationLogs: vi.fn(),
  stopTransfer: vi.fn(),
}));
const task = (name: string, overrides: Partial<ArchiveTask> = {}): ArchiveTask => ({
  id: name,
  request_id: `req-${name}`,
  context_id: `ctx-${name}`,
  local_dir: "D:\\batch",
  subdir: name,
  performer_id: name,
  performer_name: name,
  source_label: "NAS",
  has_thumb: false,
  source_path: `D:\\batch\\${name}`,
  target_path: `\\\\nas\\library\\${name}`,
  status: "running",
  phase: "copying",
  current_name: `MOV-${name}`,
  progress_bytes: 20,
  total_bytes: 100,
  verified_bytes: 0,
  completed_count: 0,
  total_count: 1,
  conflicts: [],
  message: null,
  stop_requested: false,
  created_at: "2026-10-18",
  updated_at: "2026-10-18",
  items: [],
  ...overrides,
});
const snapshot = (
  active: ArchiveTask[] = [],
  history: ArchiveTask[] = [],
  next_cursor: number | null = null,
): TransferList => ({
  active,
  history,
  next_cursor,
  busy: active.length > 0,
  blocked_reason: null,
  concurrency: 2,
});
const event = (id: number, outcome: OperationEvent["outcome"]): OperationEvent => ({
  id,
  event_key: `event-${id}`,
  operation_id: "paired-operation",
  task_id: "cleanup-1",
  created_at: "2026-10-18",
  kind: "cleanup",
  action: "delete_empty_actor",
  outcome,
  performer_name: "演员甲",
  item_name: "演员甲",
  source_path: "D:\\batch\\演员甲",
  target_path: null,
  message: null,
});
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <ThemeProvider theme={theme}>
          <TransferProvider>
            <TaskCenter />
          </TransferProvider>
        </ThemeProvider>
      </QueryClientProvider>,
    ),
  };
}
beforeEach(() => {
  sessionStorage.clear();
  vi.clearAllMocks();
  vi.mocked(getTransfers).mockResolvedValue(snapshot());
  vi.mocked(getOperationLogs).mockResolvedValue({ items: [], next_cursor: null });
});

it("shows each actor's independent progress and can stop B while A's stop response is pending", async () => {
  const first = task("A"),
    second = task("B", { progress_bytes: 70 }),
    queued = task("C", { status: "queued", phase: "preflight" });
  vi.mocked(getTransfers).mockResolvedValue(snapshot([first, second, queued]));
  let finish!: (value: ArchiveTask) => void;
  vi.mocked(stopTransfer).mockImplementation((id) =>
    id === "A"
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : Promise.resolve({ ...second, stop_requested: true }),
  );
  mount();
  const a = await screen.findByRole("article", { name: "归档任务 A" });
  const b = screen.getByRole("article", { name: "归档任务 B" });
  expect(within(a).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "20");
  expect(within(b).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "70");
  expect(screen.getByRole("article", { name: "归档任务 C" })).toHaveTextContent("等待执行");
  await userEvent.click(within(a).getByRole("button", { name: "停止传输" }));
  await userEvent.click(
    within(screen.getByRole("dialog")).getByRole("button", { name: "停止队列" }),
  );
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(within(a).getByRole("button", { name: "停止传输" })).toBeDisabled();
  expect(within(b).getByRole("button", { name: "停止传输" })).toBeEnabled();
  await userEvent.click(within(b).getByRole("button", { name: "停止传输" }));
  await userEvent.click(
    within(screen.getByRole("dialog")).getByRole("button", { name: "停止队列" }),
  );
  expect(stopTransfer).toHaveBeenNthCalledWith(1, "A");
  expect(stopTransfer).toHaveBeenNthCalledWith(2, "B");
  await act(async () => finish({ ...first, stop_requested: true }));
});

it("keeps prepared and confirmed deletion events distinct across cursor pages and resets the cursor on filtering", async () => {
  vi.mocked(getOperationLogs).mockImplementation(async (options) => {
    if (options?.task_id === "cleanup-1") return { items: [event(9, "failed")], next_cursor: null };
    return options?.before_id === 20
      ? { items: [event(19, "started")], next_cursor: null }
      : { items: [event(20, "succeeded")], next_cursor: 20 };
  });
  mount();
  await userEvent.click(screen.getByRole("tab", { name: "操作日志" }));
  expect(await screen.findByRole("article", { name: "日志 20" })).toHaveTextContent("已确认成功");
  await userEvent.click(screen.getByRole("button", { name: "加载更早日志" }));
  const prepared = await screen.findByRole("article", { name: "日志 19" });
  expect(prepared).toHaveTextContent("准备（结果未确认）");
  expect(prepared).not.toHaveTextContent("已确认成功");
  expect(getOperationLogs).toHaveBeenCalledWith(
    expect.objectContaining({ before_id: 20 }),
    expect.anything(),
  );
  await userEvent.click(within(prepared).getByRole("button", { name: "只看此任务" }));
  expect(await screen.findByRole("article", { name: "日志 9" })).toHaveTextContent("失败");
  expect(screen.queryByRole("article", { name: "日志 19" })).not.toBeInTheDocument();
  expect(getOperationLogs).toHaveBeenLastCalledWith(
    { task_id: "cleanup-1", before_id: undefined },
    expect.anything(),
  );
});

it("never replaces current active tasks with an older history page's active snapshot", async () => {
  const a = task("A"),
    b = task("B"),
    old = task("Old", { status: "completed" });
  let resolvePage!: (value: TransferList) => void;
  vi.mocked(getTransfers).mockImplementation((_signal, options) =>
    options?.before_id === 50
      ? new Promise((resolve) => {
          resolvePage = resolve;
        })
      : Promise.resolve(snapshot([a], [], 50)),
  );
  const { client } = mount();
  await screen.findByRole("article", { name: "归档任务 A" });
  await userEvent.click(await screen.findByRole("button", { name: "加载更早任务" }));
  act(() => client.setQueryData(["archive-transfers"], snapshot([b])));
  expect(await screen.findByRole("article", { name: "归档任务 B" })).toBeInTheDocument();
  await act(async () => resolvePage(snapshot([a], [old])));
  expect(await screen.findByRole("article", { name: "归档任务 Old" })).toBeInTheDocument();
  expect(screen.getByRole("article", { name: "归档任务 B" })).toBeInTheDocument();
  expect(screen.queryByRole("article", { name: "归档任务 A" })).not.toBeInTheDocument();
});

it("does not present a failed log query as an empty history", async () => {
  vi.mocked(getOperationLogs).mockRejectedValue(new Error("日志服务暂不可用"));
  mount();
  await userEvent.click(screen.getByRole("tab", { name: "操作日志" }));
  expect(await screen.findByText("日志服务暂不可用")).toBeInTheDocument();
  expect(screen.queryByText("没有符合筛选条件的日志。")).not.toBeInTheDocument();
  vi.mocked(getOperationLogs).mockResolvedValue({
    items: [event(1, "needs_review")],
    next_cursor: null,
  });
  await userEvent.click(screen.getByRole("button", { name: "重试" }));
  await waitFor(() =>
    expect(screen.getByRole("article", { name: "日志 1" })).toHaveTextContent("待核对"),
  );
});
