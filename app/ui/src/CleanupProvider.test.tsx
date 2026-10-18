import { ThemeProvider } from "@mui/material/styles";
import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./client", () => ({
  getSettingsApiSettingsGet: vi.fn(),
  putSettingsApiSettingsPut: vi.fn(),
  localSubdirsApiLocalSubdirsGet: vi.fn(),
  listPerformersApiPerformersGet: vi.fn(),
}));
vi.mock("./api", () => ({ serverBase: () => "http://127.0.0.1:8720" }));
vi.mock("./pickDirectory", () => ({ pickDirectory: vi.fn() }));
vi.mock("./openTarget", () => ({ openArchivePair: vi.fn() }));
vi.mock("./archiveApi", () => {
  const getLatestTransfer = vi.fn();
  return {
    createArchiveContext: vi.fn(),
    createTransfer: vi.fn(),
    getLatestTransfer,
    getTransfers: vi.fn(async () => {
      const task = await getLatestTransfer();
      const active = task && (task.status === "queued" || task.status === "running");
      return {
        active: active ? [task] : [],
        history: task && !active ? [task] : [],
        next_cursor: null,
        busy: !!active,
        blocked_reason: null,
        concurrency: 3,
      };
    }),
    getTransferByRequest: vi.fn().mockResolvedValue(null),
    getOperationLogs: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
    stopTransfer: vi.fn(),
    archiveError: (error: { detail?: string; message?: string }, fallback: string) =>
      error?.detail ?? error?.message ?? fallback,
    isTransferActive: (task: { status: string } | null) =>
      task?.status === "queued" || task?.status === "running",
  };
});
vi.mock("./cleanupApi", () => ({
  readCleanupContext: vi.fn(),
  createCleanup: vi.fn(),
  getLatestCleanup: vi.fn(),
  isCleanupActive: (task: { status: string } | null) =>
    task?.status === "queued" || task?.status === "running",
}));

import { ArchivePage } from "./ArchivePage";
import { createArchiveContext, createTransfer, getLatestTransfer } from "./archiveApi";
import { CleanupProvider, CleanupStatusBar } from "./CleanupProvider";
import {
  type CleanupTask,
  createCleanup,
  getLatestCleanup,
  readCleanupContext,
} from "./cleanupApi";
import {
  getSettingsApiSettingsGet,
  listPerformersApiPerformersGet,
  localSubdirsApiLocalSubdirsGet,
  putSettingsApiSettingsPut,
} from "./client";
import { pickDirectory } from "./pickDirectory";
import { TransferProvider } from "./TransferProvider";
import { theme } from "./theme";

const batch = "D:\\batch";
const actor = {
  id: "actor-1",
  name: "Actor A",
  source_id: 1,
  source_label: "NAS",
  unc_path: "\\\\nas\\library\\Actor A",
  is_missing: false,
  has_thumb: false,
  aliases: [],
};
const context = { id: "cleanup-context", batch_path: batch, batch_revision: "revision-a" };
const task: CleanupTask = {
  ...context,
  id: "cleanup-task",
  context_id: context.id,
  request_id: "request-1",
  status: "running",
  current_name: "Actor A",
  checked_count: 0,
  total_count: 2,
  deleted_count: 0,
  kept_count: 0,
  skipped_count: 0,
  failed_count: 0,
  message: null,
  items: [],
};
const listing = (path = batch) => ({ data: { path, subdirs: ["Actor A", "Actor B"] } });
const done = (name = "Actor A"): CleanupTask => ({
  ...task,
  status: "completed",
  current_name: null,
  checked_count: 2,
  deleted_count: 1,
  kept_count: 1,
  items: [{ name, status: "deleted", message: null }],
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const content = (show = true) => (
    <QueryClientProvider client={client}>
      <ThemeProvider theme={theme}>
        <CleanupProvider>
          <TransferProvider>
            {show ? <ArchivePage /> : <div>其他页面</div>}
            <CleanupStatusBar />
          </TransferProvider>
        </CleanupProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
  return { ...render(content()), content, client };
}
async function selectTarget() {
  await userEvent.click(await screen.findByRole("button", { name: /^Actor A$/ }));
  await userEvent.click(await screen.findByRole("button", { name: /选择此目录/ }));
  await screen.findByRole("heading", { name: "影片文件夹" });
}

beforeEach(() => {
  sessionStorage.clear();
  vi.clearAllMocks();
  sessionStorage.clear();
  vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({
    data: { last_local_dir: batch, db_path: "test" },
  } as never);
  vi.mocked(localSubdirsApiLocalSubdirsGet)
    .mockReset()
    .mockResolvedValue(listing() as never);
  vi.mocked(listPerformersApiPerformersGet).mockResolvedValue({
    data: { items: [actor], total: 1 },
  } as never);
  vi.mocked(getLatestTransfer).mockReset().mockResolvedValue(null);
  vi.mocked(getLatestCleanup).mockReset().mockResolvedValue(null);
  vi.mocked(readCleanupContext).mockReset().mockResolvedValue(context);
  vi.mocked(createCleanup)
    .mockReset()
    .mockImplementation(async (body) => {
      const accepted = { ...task, ...body };
      vi.mocked(getLatestCleanup).mockResolvedValue(accepted);
      return accepted;
    });
  vi.mocked(createArchiveContext)
    .mockReset()
    .mockResolvedValue({
      id: "transfer-context",
      local_dir: batch,
      subdir: "Actor A",
      performer_id: actor.id,
      performer_name: actor.name,
      source_label: "NAS",
      has_thumb: false,
      source_path: `${batch}\\Actor A`,
      target_path: actor.unc_path,
      source_items: [{ name: "Film A", size_bytes: 1, files: 1, error: null }],
      target_items: [],
      source_error: null,
      target_error: null,
    });
  vi.mocked(putSettingsApiSettingsPut).mockImplementation(
    async (options) => ({ data: { last_local_dir: options?.body.last_local_dir } }) as never,
  );
});
afterEach(() => {
  act(() => onlineManager.setOnline(true));
  vi.useRealTimers();
});

describe("batch empty actor cleanup", () => {
  it("starts from the explicit batch button, submits only server context identity, and shows progress across pages", async () => {
    const view = renderPage();
    const button = await screen.findByRole("button", { name: "清理空演员目录" });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    await waitFor(() => expect(createCleanup).toHaveBeenCalledTimes(1));
    expect(readCleanupContext).toHaveBeenCalledWith(batch, expect.any(AbortSignal));
    expect(createCleanup).toHaveBeenCalledWith(
      { context_id: context.id, request_id: expect.any(String) },
      expect.anything(),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    view.rerender(view.content(false));
    expect(screen.getByRole("status", { name: "空演员目录清理" })).toHaveTextContent(batch);
    expect(screen.getByRole("status", { name: "空演员目录清理" })).toHaveTextContent(
      "已检查 0 / 2",
    );
  });

  it("prunes confirmed deletions and clears target even when the complete list refresh fails", async () => {
    renderPage();
    await selectTarget();
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue({
      error: { detail: "访问被拒绝" },
      response: { status: 422 },
    } as never);
    vi.mocked(createCleanup).mockImplementation(async (body) => ({ ...done(), ...body }));
    await userEvent.click(screen.getByRole("button", { name: "清理空演员目录" }));
    await screen.findByText(/清理结果已保留，目录刷新失败/);
    expect(screen.queryByRole("button", { name: /^Actor A$/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Actor B$/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "影片文件夹" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("匹配搜索")).toHaveValue("Actor A");
    await userEvent.click(screen.getByRole("button", { name: "清理详情" }));
    expect(within(screen.getByRole("dialog")).getByText("Actor A")).toBeInTheDocument();
  });

  it("preserves the current target when another actor was deleted", async () => {
    renderPage();
    await selectTarget();
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue({
      data: { path: batch, subdirs: ["Actor A"] },
    } as never);
    vi.mocked(createCleanup).mockImplementation(async (body) => ({ ...done("Actor B"), ...body }));
    await userEvent.click(screen.getByRole("button", { name: "清理空演员目录" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /^Actor B$/ })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("heading", { name: "影片文件夹" })).toBeInTheDocument();
  });

  it("does not apply an old A cleanup response after switching A to B and back to A", async () => {
    renderPage();
    const pending = deferred<CleanupTask>();
    vi.mocked(createCleanup).mockReturnValue(pending.promise);
    await userEvent.click(await screen.findByRole("button", { name: "清理空演员目录" }));
    await waitFor(() => expect(createCleanup).toHaveBeenCalledTimes(1));
    for (const path of ["D:\\other-batch", batch]) {
      vi.mocked(pickDirectory).mockResolvedValue(path);
      await userEvent.click(
        screen.getByRole("button", { name: path === batch ? "D:\\other-batch" : batch }),
      );
      await screen.findByRole("button", { name: path });
    }
    await selectTarget();
    const original = vi.mocked(createCleanup).mock.calls[0][0];
    await act(async () => pending.resolve({ ...done(), ...original }));
    expect(screen.getByRole("button", { name: /^Actor A$/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "影片文件夹" })).toBeInTheDocument();
  });

  it("cancels a delayed context read after switching batches without submitting a deletion", async () => {
    const pending = deferred<typeof context>();
    vi.mocked(readCleanupContext).mockReturnValue(pending.promise);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "清理空演员目录" }));
    await waitFor(() => expect(readCleanupContext).toHaveBeenCalledTimes(1));
    const signal = vi.mocked(readCleanupContext).mock.calls[0][1];
    vi.mocked(pickDirectory).mockResolvedValue("D:\\other-batch");
    await userEvent.click(screen.getByRole("button", { name: batch }));
    await screen.findByRole("button", { name: "D:\\other-batch" });
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve(context));
    expect(createCleanup).not.toHaveBeenCalled();
  });

  it("blocks transfer during the cleanup POST and running task", async () => {
    renderPage();
    await selectTarget();
    await userEvent.click(screen.getByRole("checkbox", { name: "选择 Film A" }));
    const pending = deferred<CleanupTask>();
    vi.mocked(createCleanup).mockReturnValue(pending.promise);
    await userEvent.click(screen.getByRole("button", { name: "清理空演员目录" }));
    await waitFor(() => expect(createCleanup).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: /传输到 NAS/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "清理空演员目录" })).toBeDisabled();
    await act(async () => pending.resolve(task));
    expect(screen.getByRole("button", { name: /传输到 NAS/ })).toBeDisabled();
    expect(createTransfer).not.toHaveBeenCalled();
  });

  it("blocks cleanup while a transfer is being submitted", async () => {
    renderPage();
    await selectTarget();
    await userEvent.click(screen.getByRole("checkbox", { name: "选择 Film A" }));
    vi.mocked(createTransfer).mockReturnValue(new Promise(() => {}));
    await userEvent.click(screen.getByRole("button", { name: /传输到 NAS/ }));
    expect(screen.getByRole("button", { name: "清理空演员目录" })).toBeDisabled();
    expect(createCleanup).not.toHaveBeenCalled();
  });

  it("restores session task details without starting another cleanup or pruning recreated folders", async () => {
    vi.mocked(getLatestCleanup).mockResolvedValue(done());
    renderPage();
    await screen.findByText("空演员目录清理完成");
    expect(await screen.findByRole("button", { name: /^Actor A$/ })).toBeInTheDocument();
    expect(createCleanup).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "清理详情" }));
    expect(within(screen.getByRole("dialog")).getByText("Actor A")).toBeInTheDocument();
  });

  it("allows selecting a recreated actor after refreshing the same batch", async () => {
    renderPage();
    await selectTarget();
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue({
      data: { path: batch, subdirs: ["Actor B"] },
    } as never);
    vi.mocked(createCleanup).mockImplementation(async (body) => ({ ...done(), ...body }));
    await userEvent.click(screen.getByRole("button", { name: "清理空演员目录" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /^Actor A$/ })).not.toBeInTheDocument(),
    );
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue(listing() as never);
    await userEvent.click(screen.getByRole("button", { name: "刷新本地目录" }));
    await selectTarget();
    expect(screen.getByRole("heading", { name: "影片文件夹" })).toBeInTheDocument();
  });

  it("does not prune an already reported name again when another deletion arrives", async () => {
    const view = renderPage();
    await selectTarget();
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue({
      data: { path: batch, subdirs: ["Actor B"] },
    } as never);
    vi.mocked(createCleanup).mockImplementation(async (body) => ({
      ...done(),
      ...body,
      status: "running",
    }));
    await userEvent.click(screen.getByRole("button", { name: "清理空演员目录" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /^Actor A$/ })).not.toBeInTheDocument(),
    );
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue(listing() as never);
    await userEvent.click(screen.getByRole("button", { name: "刷新本地目录" }));
    await selectTarget();
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue({
      error: { detail: "refresh failed" },
      response: { status: 422 },
    } as never);
    const body = vi.mocked(createCleanup).mock.calls[0][0];
    act(() =>
      view.client.setQueryData(["archive-cleanup-latest"], {
        ...done(),
        ...body,
        deleted_count: 2,
        items: [
          { name: "Actor A", status: "deleted", message: null },
          { name: "Actor B", status: "deleted", message: null },
        ],
      }),
    );
    await screen.findByText(/清理结果已保留，目录刷新失败/);
    expect(screen.getByRole("button", { name: /^Actor A$/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Actor B$/ })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "影片文件夹" })).toBeInTheDocument();
  });

  it("shows only deleted names in the result dialog while retaining global failure status and counts", async () => {
    vi.mocked(getLatestCleanup).mockResolvedValue({
      ...done(),
      status: "failed",
      current_name: null,
      checked_count: 5,
      total_count: 5,
      deleted_count: 2,
      failed_count: 1,
      kept_count: 1,
      skipped_count: 1,
      message: "部分目录无法处理",
      items: [
        { name: "Actor Z", status: "deleted", message: "已完成删除" },
        { name: "Has poster", status: "not_empty", message: "包含 folder.jpg" },
        { name: "Actor A", status: "deleted", message: null },
        { name: "Link", status: "skipped", message: "链接目录保留" },
        { name: "Denied", status: "failed", message: "拒绝访问" },
      ],
    });
    renderPage();
    const details = await screen.findByRole("button", { name: "清理详情" });
    const status = screen.getByRole("status", { name: "空演员目录清理" });
    expect(status).toHaveTextContent("清理未完成");
    expect(status).toHaveTextContent("已检查 5 / 5 · 已删除 2 · 非空保留 1 · 跳过 1 · 失败 1");
    await userEvent.click(details);
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Actor Z",
      "Actor A",
    ]);
    for (const text of [
      batch,
      "Has poster",
      "Link",
      "Denied",
      "部分目录无法处理",
      "已完成删除",
      "包含 folder.jpg",
      "链接目录保留",
      "拒绝访问",
      "结果保留在当前应用会话；重启后不会自动继续清理。",
    ]) {
      expect(dialog.queryByText(text)).not.toBeInTheDocument();
    }
  });

  it.each<[string, Partial<CleanupTask>]>([
    ["zero candidates", { status: "completed", total_count: 0 }],
    [
      "all retained",
      {
        status: "completed",
        total_count: 1,
        kept_count: 1,
        items: [{ name: "Has poster", status: "not_empty", message: "包含 folder.jpg" }],
      },
    ],
    [
      "all skipped",
      {
        status: "completed",
        total_count: 1,
        skipped_count: 1,
        items: [{ name: "Link", status: "skipped", message: "链接目录保留" }],
      },
    ],
    [
      "failed",
      {
        status: "failed",
        total_count: 1,
        failed_count: 1,
        items: [{ name: "Denied", status: "failed", message: "拒绝访问" }],
      },
    ],
    ["still running", { status: "running", total_count: 1 }],
  ])("shows a truthful zero-deletion result for %s", async (_label, state) => {
    vi.mocked(getLatestCleanup).mockResolvedValue({
      ...task,
      current_name: null,
      ...state,
    });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "清理详情" }));
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText("暂无已清理的目录。")).toBeInTheDocument();
    expect(dialog.queryAllByRole("listitem")).toHaveLength(0);
    expect(createCleanup).not.toHaveBeenCalled();
  });

  it("ignores a settings response from the previous page after another batch is restored", async () => {
    const previous = deferred<Awaited<ReturnType<typeof getSettingsApiSettingsGet>>>();
    vi.mocked(getSettingsApiSettingsGet).mockReturnValueOnce(previous.promise as never);
    const view = renderPage();
    await waitFor(() => expect(getSettingsApiSettingsGet).toHaveBeenCalledTimes(1));
    view.rerender(view.content(false));
    vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({
      data: { last_local_dir: "D:\\new-batch" },
    } as never);
    vi.mocked(readCleanupContext).mockResolvedValue({ ...context, batch_path: "D:\\new-batch" });
    view.rerender(view.content());
    await screen.findByRole("button", { name: "D:\\new-batch" });
    await act(async () => previous.resolve({ data: { last_local_dir: batch } } as never));
    await userEvent.click(screen.getByRole("button", { name: "清理空演员目录" }));
    await waitFor(() =>
      expect(readCleanupContext).toHaveBeenCalledWith("D:\\new-batch", expect.any(AbortSignal)),
    );
  });
});

describe("cleanup read retries and uncertain submissions", () => {
  beforeEach(() => vi.useFakeTimers());
  const tick = async (ms = 0) =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });

  it("retries only the read context at 1/2/4 seconds before making one delete POST", async () => {
    vi.mocked(readCleanupContext)
      .mockRejectedValueOnce({ status: 503 })
      .mockRejectedValueOnce({ status: 503 })
      .mockRejectedValueOnce({ status: 503 })
      .mockResolvedValue(context);
    renderPage();
    await tick(20);
    fireEvent.click(screen.getByRole("button", { name: "清理空演员目录" }));
    await tick();
    expect(readCleanupContext).toHaveBeenCalledTimes(1);
    await tick(999);
    expect(readCleanupContext).toHaveBeenCalledTimes(1);
    await tick(1);
    expect(readCleanupContext).toHaveBeenCalledTimes(2);
    await tick(2000);
    expect(readCleanupContext).toHaveBeenCalledTimes(3);
    expect(createCleanup).not.toHaveBeenCalled();
    await tick(4000);
    expect(readCleanupContext).toHaveBeenCalledTimes(4);
    expect(createCleanup).toHaveBeenCalledTimes(1);
  });

  it("stops after three read retries without issuing a deletion and allows explicit retry", async () => {
    vi.mocked(readCleanupContext).mockRejectedValue({ status: 503, detail: "batch 暂不可读" });
    renderPage();
    await tick(20);
    fireEvent.click(screen.getByRole("button", { name: "清理空演员目录" }));
    await tick(7010);
    expect(readCleanupContext).toHaveBeenCalledTimes(4);
    expect(createCleanup).not.toHaveBeenCalled();
    expect(screen.getByText("batch 暂不可读")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "清理空演员目录" })).toBeEnabled();
  });

  it("cancels an old context retry timer when the batch changes", async () => {
    vi.mocked(readCleanupContext).mockRejectedValue({ status: 503 });
    renderPage();
    await tick(20);
    fireEvent.click(screen.getByRole("button", { name: "清理空演员目录" }));
    await tick(20);
    expect(readCleanupContext).toHaveBeenCalledTimes(1);
    vi.mocked(pickDirectory).mockResolvedValue("D:\\other-batch");
    fireEvent.click(screen.getByRole("button", { name: batch }));
    await tick(20);
    expect(screen.getByRole("button", { name: "D:\\other-batch" })).toBeInTheDocument();
    await tick(10000);
    expect(readCleanupContext).toHaveBeenCalledTimes(1);
    expect(createCleanup).not.toHaveBeenCalled();
  });

  it("submits to the local sidecar offline, never replays on reconnect, and explicitly recovers the same identity", async () => {
    vi.mocked(createCleanup).mockRejectedValue(new TypeError("Failed to fetch"));
    renderPage();
    await tick(20);
    act(() => onlineManager.setOnline(false));
    fireEvent.click(screen.getByRole("button", { name: "清理空演员目录" }));
    await tick(20);
    expect(createCleanup).toHaveBeenCalledTimes(1);
    const body = vi.mocked(createCleanup).mock.calls[0][0];
    expect(screen.getByText(/清理提交结果暂不可确认/)).toBeInTheDocument();
    await tick(10000);
    act(() => onlineManager.setOnline(true));
    await tick(10000);
    expect(createCleanup).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "清理空演员目录" })).toBeDisabled();
    vi.mocked(createCleanup).mockResolvedValue({ ...task, ...body });
    fireEvent.click(screen.getByRole("button", { name: "确认本次清理结果" }));
    await tick(20);
    expect(createCleanup).toHaveBeenCalledTimes(2);
    expect(vi.mocked(createCleanup).mock.calls[1][0]).toEqual(body);
    expect(readCleanupContext).toHaveBeenCalledTimes(1);
  });

  it("keeps the exact uncertain submission across a webview reload", async () => {
    vi.mocked(createCleanup).mockRejectedValue(new TypeError("response lost"));
    const view = renderPage();
    await tick(20);
    fireEvent.click(screen.getByRole("button", { name: "清理空演员目录" }));
    await tick(20);
    const body = vi.mocked(createCleanup).mock.calls[0][0];
    view.unmount();
    renderPage();
    await tick(20);
    expect(screen.getByText(/清理提交结果暂不可确认/)).toBeInTheDocument();
    expect(createCleanup).toHaveBeenCalledTimes(1);
    vi.mocked(createCleanup).mockResolvedValue({ ...task, ...body });
    fireEvent.click(screen.getByRole("button", { name: "确认本次清理结果" }));
    await tick(20);
    expect(vi.mocked(createCleanup).mock.calls[1][0]).toEqual(body);
  });
});
