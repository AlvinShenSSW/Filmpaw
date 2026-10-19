import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  tauri: true,
  invoke: vi.fn(),
  listen: vi.fn(),
  unlisten: vi.fn(),
  latest: vi.fn(),
  stop: vi.fn(),
  transfers: vi.fn(),
  stopAll: vi.fn(),
  cleanup: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({ isTauri: () => mocks.tauri, invoke: mocks.invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: mocks.listen }));
vi.mock("./archiveApi", async (original) => ({
  ...(await original<typeof import("./archiveApi")>()),
  getLatestTransfer: mocks.latest,
  stopTransfer: mocks.stop,
  getTransfers: mocks.transfers,
  stopTransfers: mocks.stopAll,
}));
vi.mock("./cleanupApi", () => ({
  getCleanup: mocks.cleanup,
  isCleanupActive: (task: { status: string } | null | undefined) =>
    task?.status === "queued" || task?.status === "running",
}));

import { ExitCoordinator } from "./ExitCoordinator";

let closeRequested: (event: {
  payload: {
    task_id: string | null;
    detail: string | null;
    operation?: "cleanup" | "transfer" | null;
  };
}) => void;

beforeEach(() => {
  vi.resetAllMocks();
  mocks.tauri = true;
  mocks.listen.mockImplementation(async (_event, callback) => {
    closeRequested = callback;
    return mocks.unlisten;
  });
  mocks.latest.mockResolvedValue({ id: "task-1", status: "running", current_name: "Movie A" });
  mocks.stop.mockResolvedValue({ id: "task-1", status: "running", stop_requested: true });
  mocks.transfers.mockResolvedValue(transferList(["task-1", "task-2"]));
  mocks.stopAll.mockImplementation(async (task_ids: string[]) => ({ task_ids }));
  mocks.cleanup.mockResolvedValue({ id: "cleanup-1", status: "running", current_name: "Actor A" });
  mocks.invoke.mockResolvedValue({ safe: true, task_id: null });
});

afterEach(() => vi.useRealTimers());

function transferList(ids: string[], busy = ids.length > 0) {
  return {
    active: ids.map((id) => ({
      id,
      status: "running",
      performer_name: id,
      current_name: `Movie ${id}`,
    })),
    history: [],
    next_cursor: null,
    busy,
    blocked_reason: null,
    concurrency: 2,
  };
}

async function requestClose(operation?: "cleanup" | "transfer" | null) {
  await waitFor(() => expect(mocks.listen).toHaveBeenCalled());
  act(() =>
    closeRequested({
      payload: {
        task_id: operation === "cleanup" ? "cleanup-1" : "task-1",
        detail: null,
        operation,
      },
    }),
  );
  await screen.findByRole("dialog");
}

describe("desktop transfer exit coordination", () => {
  it("keeps the scanner refusal visible and never treats it as an idle exit", async () => {
    const detail = "扫描尚未安全结束，已保留窗口，请稍后重试";
    mocks.transfers.mockResolvedValue(transferList([]));
    mocks.invoke.mockRejectedValue({ status: 409, detail });
    render(<ExitCoordinator />);
    await waitFor(() => expect(mocks.listen).toHaveBeenCalled());
    act(() => closeRequested({ payload: { task_id: null, detail } }));
    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    fireEvent.click(screen.getByRole("button", { name: "完成后退出" }));
    await waitFor(() => expect(mocks.invoke).toHaveBeenCalledWith("close_if_idle"));
    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(mocks.stopAll).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "继续运行" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("does not install a desktop listener in a standalone browser", () => {
    mocks.tauri = false;
    render(<ExitCoordinator />);
    expect(mocks.listen).not.toHaveBeenCalled();
    expect(mocks.latest).not.toHaveBeenCalled();
    expect(mocks.transfers).not.toHaveBeenCalled();
  });

  it("keeps the app running when the user dismisses the close request", async () => {
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(screen.getByRole("button", { name: "继续运行" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mocks.stop).not.toHaveBeenCalled();
    expect(mocks.stopAll).not.toHaveBeenCalled();
    expect(mocks.invoke).not.toHaveBeenCalled();
  });

  it("stops the queue and waits for a terminal state before asking the shell to exit", async () => {
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(screen.getByRole("button", { name: "停止全部后退出" }));
    await waitFor(() => expect(mocks.stopAll).toHaveBeenCalledWith(["task-1", "task-2"]));
    expect(mocks.invoke).not.toHaveBeenCalled();
    mocks.transfers.mockResolvedValue(transferList([]));
    await waitFor(() => expect(mocks.invoke).toHaveBeenCalledWith("close_if_idle"), {
      timeout: 2500,
    });
  });

  it("waits without stopping and still uses the atomic shell exit check", async () => {
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(screen.getByRole("button", { name: "完成后退出" }));
    expect(mocks.invoke).not.toHaveBeenCalled();
    mocks.transfers.mockResolvedValue(transferList([]));
    await waitFor(() => expect(mocks.invoke).toHaveBeenCalledWith("close_if_idle"), {
      timeout: 2500,
    });
    expect(mocks.stop).not.toHaveBeenCalled();
    expect(mocks.stopAll).not.toHaveBeenCalled();
  });

  it("keeps the window open when transfer status is unknown", async () => {
    mocks.transfers.mockRejectedValue(new Error("NAS status unavailable"));
    render(<ExitCoordinator />);
    await requestClose();
    await screen.findByText("NAS status unavailable");
    expect(mocks.invoke).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("reports a failed stop without claiming it is safe to exit", async () => {
    mocks.stopAll.mockRejectedValue(new Error("stop failed"));
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(screen.getByRole("button", { name: "停止全部后退出" }));
    await screen.findByText("stop failed");
    expect(mocks.invoke).not.toHaveBeenCalled();
  });

  it("cancels a pending wait without closing after a late status response", async () => {
    render(<ExitCoordinator />);
    await requestClose();
    let resolveLatest: (task: unknown) => void = () => {};
    mocks.transfers.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveLatest = resolve;
        }),
    );
    fireEvent.click(screen.getByRole("button", { name: "完成后退出" }));
    fireEvent.click(screen.getByRole("button", { name: "取消等待" }));
    await act(async () => resolveLatest(transferList([])));
    expect(mocks.invoke).not.toHaveBeenCalled();
  });

  it("does not exit on an error from the shell's final safety check", async () => {
    mocks.invoke.mockRejectedValue({ detail: "无法确认安全退出" });
    render(<ExitCoordinator />);
    await requestClose();
    mocks.transfers.mockResolvedValue(transferList([]));
    fireEvent.click(screen.getByRole("button", { name: "完成后退出" }));
    await screen.findByText("无法确认安全退出");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续运行" })).toBeEnabled();
  });

  it("unsubscribes when the global component unmounts", async () => {
    const view = render(<ExitCoordinator />);
    await waitFor(() =>
      expect(mocks.listen).toHaveBeenCalledWith("filmpaw-close-requested", expect.any(Function)),
    );
    await act(async () => {});
    view.unmount();
    expect(mocks.unlisten).toHaveBeenCalledTimes(1);
  });
});

describe("desktop cleanup exit coordination", () => {
  it("waits for the exact cleanup task without offering or calling transfer stop", async () => {
    render(<ExitCoordinator />);
    await requestClose("cleanup");
    await waitFor(() =>
      expect(mocks.cleanup).toHaveBeenCalledWith("cleanup-1", expect.any(AbortSignal)),
    );
    expect(mocks.latest).not.toHaveBeenCalled();
    expect(mocks.transfers).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "停止全部后退出" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成后退出" }));
    await screen.findByText(/正在等待清理完成/);
    expect(mocks.invoke).not.toHaveBeenCalled();
    mocks.cleanup.mockResolvedValue({ id: "cleanup-1", status: "completed" });
    await waitFor(() => expect(mocks.invoke).toHaveBeenCalledWith("close_if_idle"), {
      timeout: 2500,
    });
    expect(mocks.stop).not.toHaveBeenCalled();
  });

  it("keeps the window and cleanup identity when its status cannot be read", async () => {
    mocks.cleanup.mockRejectedValue(new Error("cleanup status unavailable"));
    render(<ExitCoordinator />);
    await requestClose("cleanup");
    await screen.findByText("cleanup status unavailable");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(mocks.transfers).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "停止全部后退出" })).not.toBeInTheDocument();
    expect(mocks.invoke).not.toHaveBeenCalled();
    expect(mocks.latest).not.toHaveBeenCalled();
    expect(mocks.stop).not.toHaveBeenCalled();
  });

  it("can cancel waiting for cleanup without exiting after a late response", async () => {
    render(<ExitCoordinator />);
    await requestClose("cleanup");
    let resolveCleanup: (task: unknown) => void = () => {};
    mocks.cleanup.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCleanup = resolve;
        }),
    );
    fireEvent.click(screen.getByRole("button", { name: "完成后退出" }));
    await waitFor(() => expect(mocks.cleanup).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "取消等待" }));
    await act(async () => resolveCleanup({ id: "cleanup-1", status: "completed" }));
    expect(mocks.invoke).not.toHaveBeenCalled();
    expect(mocks.stop).not.toHaveBeenCalled();
  });

  it("switches to cleanup if the atomic exit check finds a newly admitted cleanup", async () => {
    mocks.transfers.mockResolvedValue(transferList([]));
    mocks.invoke.mockResolvedValue({ safe: false, task_id: "cleanup-1", operation: "cleanup" });
    render(<ExitCoordinator />);
    await requestClose("transfer");
    fireEvent.click(screen.getByRole("button", { name: "完成后退出" }));
    await waitFor(() =>
      expect(mocks.cleanup).toHaveBeenCalledWith("cleanup-1", expect.any(AbortSignal)),
    );
    await screen.findByText(/正在等待清理完成/);
    expect(mocks.stop).not.toHaveBeenCalled();
  });
});

describe("multiple transfer exit coordination", () => {
  it("waits for an older running task even when latest is already completed", async () => {
    mocks.latest.mockResolvedValue({ id: "newer", status: "completed" });
    mocks.transfers.mockResolvedValue(transferList(["older"]));
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(screen.getByRole("button", { name: "完成后退出" }));
    await waitFor(() => expect(mocks.transfers).toHaveBeenCalled());
    expect(mocks.latest).not.toHaveBeenCalled();
    expect(mocks.invoke).not.toHaveBeenCalled();
    mocks.transfers.mockResolvedValue(transferList([]));
    await waitFor(() => expect(mocks.invoke).toHaveBeenCalledWith("close_if_idle"), {
      timeout: 2500,
    });
  });

  it("honors busy workers even when no active task is listed", async () => {
    mocks.transfers.mockResolvedValue(transferList([], true));
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(screen.getByRole("button", { name: "完成后退出" }));
    await waitFor(() => expect(mocks.transfers).toHaveBeenCalledTimes(2));
    expect(mocks.invoke).not.toHaveBeenCalled();
    mocks.transfers.mockResolvedValue(transferList([]));
    await waitFor(() => expect(mocks.invoke).toHaveBeenCalledWith("close_if_idle"), {
      timeout: 2500,
    });
  });

  it("stops the explicit task set and waits until every worker is idle", async () => {
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(await screen.findByRole("button", { name: "停止全部后退出" }));
    await waitFor(() => expect(mocks.stopAll).toHaveBeenCalledWith(["task-1", "task-2"]));
    expect(mocks.stop).not.toHaveBeenCalled();
    mocks.transfers.mockResolvedValue(transferList(["task-1"]));
    await waitFor(() => expect(screen.queryByText(/Movie task-2/)).not.toBeInTheDocument(), {
      timeout: 2500,
    });
    expect(mocks.invoke).not.toHaveBeenCalled();
    mocks.transfers.mockResolvedValue(transferList([]));
    await waitFor(() => expect(mocks.invoke).toHaveBeenCalledWith("close_if_idle"), {
      timeout: 2500,
    });
  });

  it("asks again if another task arrives after the requested stop set", async () => {
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(await screen.findByRole("button", { name: "停止全部后退出" }));
    await waitFor(() => expect(mocks.stopAll).toHaveBeenCalledTimes(1));
    mocks.transfers.mockResolvedValue(transferList(["task-3"]));
    await screen.findByText("出现新的任务，尚未停止。请重新选择退出方式。", {}, { timeout: 2500 });
    expect(screen.getByRole("button", { name: "继续运行" })).toBeEnabled();
    expect(mocks.stopAll).toHaveBeenCalledTimes(1);
    expect(mocks.invoke).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "停止全部后退出" }));
    await waitFor(() => expect(mocks.stopAll).toHaveBeenLastCalledWith(["task-3"]));
  });

  it("retries an uncertain stop with the original IDs instead of expanding to new tasks", async () => {
    mocks.stopAll.mockRejectedValueOnce(new Error("stop response lost"));
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(await screen.findByRole("button", { name: "停止全部后退出" }));
    await screen.findByText("stop response lost");
    mocks.transfers.mockResolvedValue(transferList(["task-1", "task-2", "task-3"]));
    await screen.findByText(/task-3/, {}, { timeout: 2500 });
    fireEvent.click(screen.getByRole("button", { name: "重试停止原任务" }));
    await waitFor(() => expect(mocks.stopAll).toHaveBeenCalledTimes(2));
    expect(mocks.stopAll.mock.calls).toEqual([[["task-1", "task-2"]], [["task-1", "task-2"]]]);
    await screen.findByText("出现新的任务，尚未停止。请重新选择退出方式。");
    expect(mocks.invoke).not.toHaveBeenCalled();
  });

  it("asks again when the final shell check discovers a task outside the stopped set", async () => {
    mocks.invoke.mockResolvedValue({ safe: false, task_id: "task-3", operation: "transfer" });
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(await screen.findByRole("button", { name: "停止全部后退出" }));
    await waitFor(() => expect(mocks.stopAll).toHaveBeenCalledTimes(1));
    mocks.transfers.mockResolvedValue(transferList([]));
    await screen.findByText("出现新的任务，尚未停止。请重新选择退出方式。", {}, { timeout: 2500 });
    expect(mocks.invoke).toHaveBeenCalledTimes(1);
    expect(mocks.stopAll).toHaveBeenCalledTimes(1);
  });

  it("keeps the exact selection retryable when a stop acknowledgement is incomplete", async () => {
    mocks.stopAll.mockResolvedValue({ task_ids: ["task-1"] });
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(await screen.findByRole("button", { name: "停止全部后退出" }));
    await screen.findByText("无法确认全部停止请求，窗口已保留，请重试");
    expect(screen.getByRole("button", { name: "重试停止原任务" })).toBeEnabled();
    expect(mocks.invoke).not.toHaveBeenCalled();
  });

  it("never offers transfer stop when a cleanup appears after stopping transfers", async () => {
    mocks.invoke.mockResolvedValue({ safe: false, task_id: "cleanup-1", operation: "cleanup" });
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(await screen.findByRole("button", { name: "停止全部后退出" }));
    await waitFor(() => expect(mocks.stopAll).toHaveBeenCalledTimes(1));
    mocks.transfers.mockResolvedValue(transferList([]));
    await screen.findByText("出现新的任务，尚未停止。请重新选择退出方式。", {}, { timeout: 2500 });
    expect(screen.queryByRole("button", { name: /停止.*退出/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续运行" })).toBeEnabled();
    await waitFor(() =>
      expect(mocks.cleanup).toHaveBeenCalledWith("cleanup-1", expect.any(AbortSignal)),
    );
    expect(mocks.stopAll).toHaveBeenCalledTimes(1);
    expect(mocks.stop).not.toHaveBeenCalled();
  });

  it("ignores a late stop reply after a newer close prompt changed to cleanup", async () => {
    let resolveStop: (value: { task_ids: string[] }) => void = () => {};
    mocks.stopAll.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveStop = resolve;
        }),
    );
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(await screen.findByRole("button", { name: "停止全部后退出" }));
    await waitFor(() => expect(mocks.stopAll).toHaveBeenCalledTimes(1));
    await requestClose("cleanup");
    await act(async () => resolveStop({ task_ids: ["task-1", "task-2"] }));
    expect(screen.getByRole("button", { name: "继续运行" })).toBeEnabled();
    expect(screen.queryByText(/正在等待所选任务安全停止/)).not.toBeInTheDocument();
    expect(mocks.invoke).not.toHaveBeenCalled();
  });
});
