import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { theme } from "./theme";

vi.mock("./archiveApi", () => ({
  createArchiveContext: vi.fn(),
  createTransfer: vi.fn(),
  getLatestTransfer: vi.fn(),
  stopTransfer: vi.fn(),
  archiveError: (error: { detail?: string }) => error.detail ?? "请求失败",
  isTransferActive: (task: { status: string } | null) =>
    task?.status === "queued" || task?.status === "running",
}));
vi.mock("./api", () => ({ serverBase: () => "http://127.0.0.1:8720" }));
vi.mock("./openTarget", () => ({ openArchivePair: vi.fn() }));

import { ArchiveTransferView } from "./ArchiveTransferView";
import { createArchiveContext, createTransfer, getLatestTransfer } from "./archiveApi";
import { openArchivePair } from "./openTarget";
import { TransferProvider, TransferStatusBar } from "./TransferProvider";

const performer = {
  id: "performer-1",
  name: "演员甲",
  source_id: 1,
  source_label: "主片库",
  unc_path: "\\\\NAS\\library\\演员甲",
  is_missing: false,
  has_thumb: false,
  aliases: [],
};
const context = {
  id: "context-1",
  local_dir: "D:\\batch",
  subdir: "演员甲",
  performer_id: performer.id,
  performer_name: performer.name,
  source_label: "主片库",
  has_thumb: false,
  source_path: "D:\\batch\\演员甲",
  target_path: performer.unc_path,
  source_items: [
    { name: "MOV-20", size_bytes: null, files: null, error: null },
    { name: "MOV-3", size_bytes: null, files: null, error: null },
  ],
  target_items: [{ name: "MOV-1", size_bytes: null, files: null, error: null }],
  source_error: null,
  target_error: null,
};
const task = {
  ...context,
  id: "task-1",
  request_id: "request-1",
  context_id: context.id,
  status: "queued",
  phase: "preflight",
  current_name: null,
  progress_bytes: 0,
  total_bytes: 0,
  verified_bytes: 0,
  completed_count: 0,
  total_count: 1,
  conflicts: [],
  message: null,
  stop_requested: false,
  created_at: "2026-10-18T01:00:00Z",
  updated_at: "2026-10-18T01:00:00Z",
  items: [{ name: "MOV-3", status: "pending", size_bytes: 0, progress_bytes: 0, message: null }],
};

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const content = (subdir: string) => (
    <QueryClientProvider client={client}>
      <ThemeProvider theme={theme}>
        <TransferProvider>
          <ArchiveTransferView
            localDir={context.local_dir}
            subdir={subdir}
            performer={performer}
            onBack={vi.fn()}
          />
          <TransferStatusBar />
        </TransferProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
  return { ...render(content("演员甲")), content };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(createArchiveContext).mockReset().mockResolvedValue(context);
  vi.mocked(getLatestTransfer).mockReset().mockResolvedValue(null);
  vi.mocked(createTransfer)
    .mockReset()
    .mockResolvedValue(task as never);
  vi.mocked(openArchivePair).mockResolvedValue(undefined);
});

describe("directory read retries", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());
  const tick = async (ms: number) => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  };

  it("retries a transient NAS partial response and keeps the local list visible", async () => {
    vi.mocked(createArchiveContext)
      .mockResolvedValueOnce({
        ...context,
        target_items: [],
        target_error: "NAS 正在唤醒",
        target_retryable: true,
      } as never)
      .mockResolvedValueOnce({ ...context, id: "ready-after-retry" });
    renderView();
    await tick(1);
    expect(screen.getByText(/正在自动重试/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "选择 MOV-3" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^传输到 NAS/ })).toBeDisabled();
    expect(createArchiveContext).toHaveBeenCalledTimes(1);
    await tick(1000);
    expect(createArchiveContext).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/正在自动重试/)).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "NAS 片库" })).getByText("MOV-1"),
    ).toBeInTheDocument();
    expect(createTransfer).not.toHaveBeenCalled();
  });

  it("stops after three retries and leaves a manual retry without inventing an empty NAS", async () => {
    vi.mocked(createArchiveContext).mockResolvedValue({
      ...context,
      target_items: [],
      target_error: "NAS 忙",
      target_retryable: true,
    } as never);
    renderView();
    await tick(1);
    await tick(1000);
    expect(createArchiveContext).toHaveBeenCalledTimes(2);
    await tick(2000);
    expect(createArchiveContext).toHaveBeenCalledTimes(3);
    await tick(4000);
    expect(createArchiveContext).toHaveBeenCalledTimes(4);
    expect(screen.getByRole("button", { name: "重试读取目录" })).toBeEnabled();
    expect(screen.queryByText("这个演员目录还没有影片文件夹")).not.toBeInTheDocument();
    await tick(20_000);
    expect(createArchiveContext).toHaveBeenCalledTimes(4);
    expect(createTransfer).not.toHaveBeenCalled();
  });

  it("cancels an old context retry when the actor changes", async () => {
    vi.mocked(createArchiveContext).mockResolvedValueOnce({
      ...context,
      target_items: [],
      target_error: "NAS 忙",
      target_retryable: true,
    } as never);
    const view = renderView();
    await tick(1);
    const oldSignal = vi.mocked(createArchiveContext).mock.calls[0][1];
    vi.mocked(createArchiveContext).mockResolvedValue({
      ...context,
      id: "other-context",
      subdir: "演员乙",
    });
    view.rerender(view.content("演员乙"));
    await tick(1);
    expect(oldSignal?.aborted).toBe(true);
    expect(createArchiveContext).toHaveBeenCalledTimes(2);
    await tick(10_000);
    expect(createArchiveContext).toHaveBeenCalledTimes(2);
    expect(vi.mocked(createArchiveContext).mock.calls[1][0].subdir).toBe("演员乙");
  });

  it("ignores a delayed old response after switching actors", async () => {
    let resolveOld!: (value: typeof context) => void;
    vi.mocked(createArchiveContext).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    );
    const view = renderView();
    await tick(1);
    const oldSignal = vi.mocked(createArchiveContext).mock.calls[0][1];
    vi.mocked(createArchiveContext).mockResolvedValue({
      ...context,
      id: "new-context",
      subdir: "演员乙",
      source_items: [{ name: "NEW-1", size_bytes: null, files: null, error: null }],
    });
    view.rerender(view.content("演员乙"));
    await tick(1);
    expect(oldSignal?.aborted).toBe(true);
    expect(screen.getByRole("checkbox", { name: "选择 NEW-1" })).toBeInTheDocument();
    resolveOld(context);
    await tick(10_000);
    expect(screen.queryByRole("checkbox", { name: "选择 MOV-3" })).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "选择 NEW-1" })).toBeInTheDocument();
    expect(createArchiveContext).toHaveBeenCalledTimes(2);
  });

  it("retains checked folders and previous lists through a transient refresh failure", async () => {
    renderView();
    await tick(1);
    fireEvent.click(screen.getByRole("checkbox", { name: "选择 MOV-3" }));
    vi.mocked(createArchiveContext)
      .mockResolvedValueOnce({
        ...context,
        id: "partial",
        source_items: [],
        source_error: "本地忙",
        source_retryable: true,
      } as never)
      .mockResolvedValueOnce({ ...context, id: "refreshed" });
    fireEvent.click(screen.getByRole("button", { name: "刷新本地影片" }));
    await tick(1);
    expect(screen.getByRole("checkbox", { name: "选择 MOV-3" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "选择 MOV-3" })).toBeDisabled();
    await tick(1000);
    expect(screen.getByRole("checkbox", { name: "选择 MOV-3" })).toBeChecked();
    expect(screen.getByRole("button", { name: /^传输到 NAS/ })).toBeEnabled();
    expect(createTransfer).not.toHaveBeenCalled();
  });

  it("does not retry security refusals or a failed transfer submission", async () => {
    vi.mocked(createArchiveContext).mockRejectedValueOnce({
      status: 403,
      detail: "没有目录访问权限",
    });
    renderView();
    await tick(10_000);
    expect(createArchiveContext).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "重试读取目录" }));
    await tick(1);
    fireEvent.click(screen.getByRole("checkbox", { name: "选择 MOV-3" }));
    vi.mocked(createTransfer).mockRejectedValue({ status: 503, detail: "传输服务暂不可用" });
    fireEvent.click(screen.getByRole("button", { name: /^传输到 NAS/ }));
    await tick(10_000);
    expect(createTransfer).toHaveBeenCalledTimes(1);
  });
});

describe("archive transfer", () => {
  it("starts with no selection, sorts names naturally, and submits only checked folders", async () => {
    renderView();
    const source = await screen.findByRole("region", { name: "本地影片" });
    await screen.findByRole("checkbox", { name: "选择 MOV-3" });
    expect(screen.getByRole("button", { name: /^传输到 NAS/ })).toBeDisabled();
    const rows = within(source).getAllByRole("checkbox").slice(1);
    expect(rows.map((row) => row.getAttribute("aria-label"))).toEqual([
      "选择 MOV-3",
      "选择 MOV-20",
    ]);
    await userEvent.click(screen.getByRole("checkbox", { name: "选择 MOV-3" }));
    await userEvent.click(screen.getByRole("button", { name: /^传输到 NAS/ }));
    await waitFor(() =>
      expect(createTransfer).toHaveBeenCalledWith({
        context_id: "context-1",
        names: ["MOV-3"],
        request_id: expect.any(String),
      }),
    );
    expect(screen.getByRole("checkbox", { name: "选择 MOV-3" })).not.toBeChecked();
    expect(await screen.findByRole("status", { name: "归档任务" })).toHaveTextContent("预检");
  });

  it("clears selection when local actor context changes", async () => {
    const view = renderView();
    await userEvent.click(await screen.findByRole("checkbox", { name: "选择 MOV-3" }));
    vi.mocked(createArchiveContext).mockResolvedValue({
      ...context,
      id: "context-2",
      subdir: "演员乙",
    });
    view.rerender(view.content("演员乙"));
    await waitFor(() =>
      expect(createArchiveContext).toHaveBeenCalledWith(
        {
          local_dir: "D:\\batch",
          subdir: "演员乙",
          performer_id: performer.id,
        },
        expect.anything(),
      ),
    );
    await waitFor(() =>
      expect(screen.getByRole("checkbox", { name: "选择 MOV-3" })).not.toBeChecked(),
    );
    expect(screen.getByRole("button", { name: /^传输到 NAS/ })).toBeDisabled();
  });

  it("keeps local list visible when NAS is offline and disables transmission", async () => {
    vi.mocked(createArchiveContext).mockResolvedValue({
      ...context,
      target_items: [],
      target_error: "NAS 不可访问",
    });
    renderView();
    expect(await screen.findByText("NAS 不可访问")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "选择 MOV-3" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^传输到 NAS/ })).toBeDisabled();
    expect(createTransfer).not.toHaveBeenCalled();
  });

  it("reuses validated pair opening and exposes the full target path", async () => {
    renderView();
    await screen.findByRole("checkbox", { name: "选择 MOV-3" });
    await userEvent.click(screen.getByRole("button", { name: "双开文件夹" }));
    expect(openArchivePair).toHaveBeenCalledWith("演员甲", performer.id);
    await userEvent.click(screen.getByRole("button", { name: "查看并复制完整路径" }));
    expect(within(screen.getByRole("dialog")).getByText(performer.unc_path)).toBeInTheDocument();
  });

  it("shows the full folder name without toggling its selection", async () => {
    renderView();
    await screen.findByRole("checkbox", { name: "选择 MOV-3" });
    await userEvent.click(screen.getByRole("button", { name: "查看完整名称：MOV-3" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("MOV-3")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "复制名称" })).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
    expect(await screen.findByRole("checkbox", { name: "选择 MOV-3" })).not.toBeChecked();
  });

  it("identifies an uncleaned source copy and excludes it from select-all", async () => {
    vi.mocked(getLatestTransfer).mockResolvedValue({
      ...task,
      status: "needs_review",
      items: [{ ...task.items[0], status: "cleanup_pending" }],
    } as never);
    renderView();
    expect(
      await screen.findByText("副本已保存，本地清理未完成；请核对后手动处理"),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("checkbox", { name: "全选本地影片" })).toBeEnabled(),
    );
    expect(screen.getByRole("checkbox", { name: "选择 MOV-3" })).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: "全选本地影片" }));
    expect(screen.getByRole("checkbox", { name: "选择 MOV-3" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "选择 MOV-20" })).toBeChecked();
  });

  it("moves a completed item between displayed lists while the next queued item is running", async () => {
    vi.mocked(getLatestTransfer).mockResolvedValue({
      ...task,
      status: "running",
      total_count: 2,
      completed_count: 1,
      current_name: "MOV-20",
      items: [
        { ...task.items[0], status: "completed" },
        { ...task.items[0], name: "MOV-20", status: "copying" },
      ],
    } as never);
    renderView();
    await screen.findByRole("checkbox", { name: "选择 MOV-20" });
    expect(screen.queryByRole("checkbox", { name: "选择 MOV-3" })).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "NAS 片库" })).getByText("MOV-3"),
    ).toBeInTheDocument();
  });

  it("shows a recovered conflict task globally without starting another transfer", async () => {
    vi.mocked(getLatestTransfer).mockResolvedValue({
      ...task,
      status: "failed",
      conflicts: ["MOV-3"],
      message: "存在同名目录，整批未启动",
    } as never);
    renderView();
    const status = await screen.findByRole("status", { name: "归档任务" });
    expect(status).toHaveTextContent("同名冲突");
    await userEvent.click(within(status).getByRole("button", { name: "任务详情" }));
    expect(within(screen.getByRole("dialog")).getByText(/同名冲突：MOV-3/)).toBeInTheDocument();
    expect(createTransfer).not.toHaveBeenCalled();
  });

  it("clears checks on refresh and never retries a rejected stale context automatically", async () => {
    vi.mocked(createTransfer).mockRejectedValue({ status: 409, detail: "目录快照已过期，请刷新" });
    renderView();
    await userEvent.click(await screen.findByRole("checkbox", { name: "选择 MOV-3" }));
    await userEvent.click(screen.getByRole("button", { name: /^传输到 NAS/ }));
    expect(await screen.findByText("目录快照已过期，请刷新")).toBeInTheDocument();
    expect(createTransfer).toHaveBeenCalledTimes(1);
    expect(createArchiveContext).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("checkbox", { name: "选择 MOV-3" })).not.toBeChecked();
    vi.mocked(createArchiveContext).mockResolvedValue({ ...context, id: "refreshed" });
    await userEvent.click(screen.getByRole("button", { name: "刷新目录" }));
    await waitFor(() => expect(createArchiveContext).toHaveBeenCalledTimes(2));
    expect(createTransfer).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /^传输到 NAS/ })).toBeDisabled();
  });

  it("sorts each list independently and selects all local folders only", async () => {
    renderView();
    await screen.findByRole("checkbox", { name: "选择 MOV-3" });
    await userEvent.click(screen.getByRole("button", { name: "本地名称升序，点击切换" }));
    const source = screen.getByRole("region", { name: "本地影片" });
    expect(
      within(source)
        .getAllByRole("checkbox")
        .slice(1)
        .map((row) => row.getAttribute("aria-label")),
    ).toEqual(["选择 MOV-20", "选择 MOV-3"]);
    expect(screen.getByRole("button", { name: "NAS名称升序，点击切换" })).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "NAS 片库" })).queryByRole("checkbox"),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: "全选本地影片" }));
    expect(screen.getByRole("checkbox", { name: "选择 MOV-3" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "选择 MOV-20" })).toBeChecked();
  });

  it("recovers a request that failed before reaching the server using the same queue identity", async () => {
    vi.mocked(createTransfer).mockRejectedValueOnce(new TypeError("Failed to fetch"));
    renderView();
    await userEvent.click(await screen.findByRole("checkbox", { name: "选择 MOV-3" }));
    await userEvent.click(screen.getByRole("button", { name: /^传输到 NAS/ }));
    expect(await screen.findByRole("button", { name: "重试确认本次传输" })).toBeEnabled();
    expect(createTransfer).toHaveBeenCalledTimes(1);
    const original = vi.mocked(createTransfer).mock.calls[0][0];
    vi.mocked(createTransfer).mockResolvedValue({
      ...task,
      request_id: original.request_id,
    } as never);
    await userEvent.click(screen.getByRole("button", { name: "重试确认本次传输" }));
    await waitFor(() => expect(createTransfer).toHaveBeenCalledTimes(2));
    expect(vi.mocked(createTransfer).mock.calls[1][0]).toEqual(original);
    expect(await screen.findByRole("status", { name: "归档任务" })).toHaveTextContent("预检");
    expect(screen.queryByRole("button", { name: "重试确认本次传输" })).not.toBeInTheDocument();
  });
});
