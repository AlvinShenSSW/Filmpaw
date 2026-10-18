import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  tauri: true,
  invoke: vi.fn(),
  listen: vi.fn(),
  unlisten: vi.fn(),
  latest: vi.fn(),
  stop: vi.fn(),
  cleanup: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({ isTauri: () => mocks.tauri, invoke: mocks.invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: mocks.listen }));
vi.mock("./archiveApi", async (original) => ({
  ...(await original<typeof import("./archiveApi")>()),
  getLatestTransfer: mocks.latest,
  stopTransfer: mocks.stop,
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
  vi.clearAllMocks();
  mocks.tauri = true;
  mocks.listen.mockImplementation(async (_event, callback) => {
    closeRequested = callback;
    return mocks.unlisten;
  });
  mocks.latest.mockResolvedValue({ id: "task-1", status: "running", current_name: "Movie A" });
  mocks.stop.mockResolvedValue({ id: "task-1", status: "running", stop_requested: true });
  mocks.cleanup.mockResolvedValue({ id: "cleanup-1", status: "running", current_name: "Actor A" });
  mocks.invoke.mockResolvedValue({ safe: true, task_id: null });
});

afterEach(() => vi.useRealTimers());

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
  it("does not install a desktop listener in a standalone browser", () => {
    mocks.tauri = false;
    render(<ExitCoordinator />);
    expect(mocks.listen).not.toHaveBeenCalled();
    expect(mocks.latest).not.toHaveBeenCalled();
  });

  it("keeps the app running when the user dismisses the close request", async () => {
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(screen.getByRole("button", { name: "继续运行" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mocks.stop).not.toHaveBeenCalled();
    expect(mocks.invoke).not.toHaveBeenCalled();
  });

  it("stops the queue and waits for a terminal state before asking the shell to exit", async () => {
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(screen.getByRole("button", { name: "停止队列后退出" }));
    await waitFor(() => expect(mocks.stop).toHaveBeenCalledWith("task-1"));
    expect(mocks.invoke).not.toHaveBeenCalled();
    mocks.latest.mockResolvedValue({ id: "task-1", status: "stopped" });
    await waitFor(() => expect(mocks.invoke).toHaveBeenCalledWith("close_if_idle"), {
      timeout: 2500,
    });
  });

  it("waits without stopping and still uses the atomic shell exit check", async () => {
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(screen.getByRole("button", { name: "完成后退出" }));
    expect(mocks.invoke).not.toHaveBeenCalled();
    mocks.latest.mockResolvedValue({ id: "task-1", status: "completed" });
    await waitFor(() => expect(mocks.invoke).toHaveBeenCalledWith("close_if_idle"), {
      timeout: 2500,
    });
    expect(mocks.stop).not.toHaveBeenCalled();
  });

  it("keeps the window open when transfer status is unknown", async () => {
    mocks.latest.mockRejectedValue(new Error("NAS status unavailable"));
    render(<ExitCoordinator />);
    await requestClose();
    await screen.findByText("NAS status unavailable");
    expect(mocks.invoke).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("reports a failed stop without claiming it is safe to exit", async () => {
    mocks.stop.mockRejectedValue(new Error("stop failed"));
    render(<ExitCoordinator />);
    await requestClose();
    fireEvent.click(screen.getByRole("button", { name: "停止队列后退出" }));
    await screen.findByText("stop failed");
    expect(mocks.invoke).not.toHaveBeenCalled();
  });

  it("cancels a pending wait without closing after a late status response", async () => {
    render(<ExitCoordinator />);
    await requestClose();
    let resolveLatest: (task: unknown) => void = () => {};
    mocks.latest.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveLatest = resolve;
        }),
    );
    fireEvent.click(screen.getByRole("button", { name: "完成后退出" }));
    fireEvent.click(screen.getByRole("button", { name: "取消等待" }));
    await act(async () => resolveLatest({ id: "task-1", status: "completed" }));
    expect(mocks.invoke).not.toHaveBeenCalled();
  });

  it("does not exit on an error from the shell's final safety check", async () => {
    mocks.invoke.mockRejectedValue({ detail: "无法确认安全退出" });
    render(<ExitCoordinator />);
    await requestClose();
    mocks.latest.mockResolvedValue({ id: "task-1", status: "completed" });
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
    expect(screen.queryByRole("button", { name: "停止队列后退出" })).not.toBeInTheDocument();
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
    expect(screen.queryByRole("button", { name: "停止队列后退出" })).not.toBeInTheDocument();
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
    mocks.latest.mockResolvedValue({ id: "task-1", status: "completed" });
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
