import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import type { ArchiveContext, ArchiveTask } from "./archiveApi";
import {
  createTransfer,
  getTransferByRequest,
  getTransfers,
  stopTransfer,
  type TransferList,
} from "./archiveApi";
import { TransferProvider, useTransfer } from "./TransferProvider";

vi.mock("./archiveApi", async (original) => ({
  ...(await original<typeof import("./archiveApi")>()),
  createTransfer: vi.fn(),
  getTransfers: vi.fn(),
  getTransferByRequest: vi.fn(),
  stopTransfer: vi.fn(),
  getLatestTransfer: vi.fn().mockResolvedValue(null),
}));

const binding = (name: string) =>
  ({
    id: `context-${name}`,
    local_dir: "D:\\batch",
    subdir: name,
    performer_id: name,
    performer_name: name,
    source_path: `D:\\batch\\${name}`,
    target_path: `\\\\nas\\library\\${name}`,
  }) as ArchiveContext;
const task = (name: string) =>
  ({
    ...binding(name),
    id: `task-${name}`,
    context_id: `context-${name}`,
    request_id: `request-${name}`,
    status: "running",
    phase: "copying",
    conflicts: [],
    items: [],
    updated_at: name,
    current_name: null,
    progress_bytes: 0,
    total_bytes: 100,
    verified_bytes: 0,
    completed_count: 0,
    total_count: 1,
    message: null,
    stop_requested: false,
    created_at: name,
  }) as ArchiveTask;
function Controls() {
  const transfer = useTransfer();
  return (
    <>
      {["A", "B"].map((name) => (
        <button
          type="button"
          key={name}
          disabled={transfer.unavailable || transfer.isBindingBusy?.(binding(name))}
          onClick={() =>
            transfer.start.mutate({
              context_id: `context-${name}`,
              names: ["MOV"],
              request_id: `request-${name}`,
              binding: binding(name),
            })
          }
        >
          Start {name}
        </button>
      ))}
      <span data-testid="unknown">
        {transfer.pendingRequests
          ?.filter((request) => request.state === "unknown")
          .map((request) => request.body.request_id)
          .join(",")}
      </span>
      <span data-testid="tasks">
        {transfer.tasks.map((task) => `${task.id}:${task.status}`).join(",")}
      </span>
      <span data-testid="cleanup">{String(transfer.cleanupBlocked)}</span>
      <button type="button" onClick={() => transfer.retrySubmission("request-A")}>
        Confirm A
      </button>
      <button type="button" onClick={() => transfer.stop.mutate("task-A")}>
        Stop A
      </button>
    </>
  );
}
function mount(submissionsBlocked = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <TransferProvider submissionsBlocked={submissionsBlocked}>
          <Controls />
        </TransferProvider>
      </QueryClientProvider>,
    ),
  };
}
beforeEach(() => {
  sessionStorage.clear();
  vi.clearAllMocks();
  vi.mocked(getTransfers).mockResolvedValue({
    active: [],
    history: [],
    next_cursor: null,
    busy: false,
    blocked_reason: null,
    concurrency: 3,
  });
  vi.mocked(getTransferByRequest).mockResolvedValue(null);
});

it("allows an independent actor while A is awaiting its POST response", async () => {
  let resolveA!: (value: ArchiveTask) => void;
  vi.mocked(createTransfer).mockImplementation((body) =>
    body.request_id === "request-A"
      ? new Promise((resolve) => {
          resolveA = resolve;
        })
      : Promise.resolve(task("B")),
  );
  mount();
  await waitFor(() => expect(screen.getByText("Start A")).toBeEnabled());
  await userEvent.click(screen.getByText("Start A"));
  expect(screen.getByText("Start A")).toBeDisabled();
  expect(screen.getByText("Start B")).toBeEnabled();
  await userEvent.click(screen.getByText("Start B"));
  expect(createTransfer).toHaveBeenCalledTimes(2);
  await act(async () => resolveA(task("A")));
});

it("keeps A's unknown result after B succeeds and explicitly retries only A's original request", async () => {
  vi.mocked(createTransfer).mockImplementation((body) =>
    body.request_id === "request-A"
      ? Promise.reject(new TypeError("connection lost"))
      : Promise.resolve(task("B")),
  );
  mount();
  await waitFor(() => expect(screen.getByText("Start A")).toBeEnabled());
  await userEvent.click(screen.getByText("Start A"));
  await waitFor(() => expect(screen.getByTestId("unknown")).toHaveTextContent("request-A"));
  await userEvent.click(screen.getByText("Start B"));
  await waitFor(() => expect(createTransfer).toHaveBeenCalledTimes(2));
  expect(screen.getByTestId("unknown")).toHaveTextContent("request-A");
  expect(screen.getByText("Start A")).toBeDisabled();
  vi.mocked(createTransfer).mockResolvedValue(task("A"));
  await userEvent.click(screen.getByText("Confirm A"));
  await waitFor(() => expect(createTransfer).toHaveBeenCalledTimes(3));
  expect(vi.mocked(createTransfer).mock.calls[2][0]).toEqual(
    vi.mocked(createTransfer).mock.calls[0][0],
  );
  await waitFor(() => expect(screen.getByTestId("unknown")).toBeEmptyDOMElement());
});

it("restores an unknown request after reload, keeps null inconclusive, and never POSTs automatically", async () => {
  sessionStorage.setItem(
    "filmpaw-transfer-requests",
    JSON.stringify([
      {
        body: { context_id: "context-A", names: ["MOV"], request_id: "request-A" },
        binding: binding("A"),
        state: "sending",
      },
    ]),
  );
  mount();
  await waitFor(() =>
    expect(getTransferByRequest).toHaveBeenCalledWith("request-A", expect.anything()),
  );
  await waitFor(() => expect(screen.getByText("Start B")).toBeEnabled());
  expect(screen.getByTestId("unknown")).toHaveTextContent("request-A");
  expect(screen.getByText("Start A")).toBeDisabled();
  expect(screen.getByText("Start B")).toBeEnabled();
  expect(createTransfer).not.toHaveBeenCalled();
});

it("does not retract the original unknown request when an explicit confirmation gets a conflict", async () => {
  sessionStorage.setItem(
    "filmpaw-transfer-requests",
    JSON.stringify([
      {
        body: { context_id: "context-A", names: ["MOV"], request_id: "request-A" },
        binding: binding("A"),
        state: "unknown",
      },
    ]),
  );
  vi.mocked(createTransfer).mockRejectedValue({ status: 409, detail: "busy" });
  mount();
  await waitFor(() => expect(screen.getByText("Start B")).toBeEnabled());
  await userEvent.click(screen.getByText("Confirm A"));
  await waitFor(() => expect(createTransfer).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByTestId("unknown")).toHaveTextContent("request-A"));
  expect(screen.getByText("Start A")).toBeDisabled();
});

it("blocks all bindings on service recovery or persistence failure", async () => {
  vi.mocked(getTransfers).mockResolvedValue({
    active: [],
    history: [],
    next_cursor: null,
    busy: true,
    blocked_reason: "正在恢复",
    concurrency: 3,
  });
  mount();
  await waitFor(() => expect(getTransfers).toHaveBeenCalled());
  expect(screen.getByText("Start A")).toBeDisabled();
  expect(screen.getByText("Start B")).toBeDisabled();
});

it("keeps the newer running snapshot when an older accepted POST arrives late", async () => {
  let resolve!: (value: ArchiveTask) => void;
  vi.mocked(createTransfer).mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const { client } = mount();
  await waitFor(() => expect(screen.getByText("Start A")).toBeEnabled());
  await userEvent.click(screen.getByText("Start A"));
  act(() =>
    client.setQueryData(["archive-transfers"], {
      active: [task("A")],
      history: [],
      next_cursor: null,
      busy: true,
      blocked_reason: null,
      concurrency: 3,
    }),
  );
  await act(async () => resolve({ ...task("A"), status: "queued", phase: "queued" }));
  expect(screen.getByTestId("tasks")).toHaveTextContent("task-A:running");
  expect(screen.getByText("Start A")).toBeDisabled();
  expect(screen.getByText("Start B")).toBeEnabled();
});

it("honors a terminal task still present in active while the server finishes cleanup", async () => {
  vi.mocked(getTransfers).mockResolvedValue({
    active: [{ ...task("A"), status: "completed" }],
    history: [],
    next_cursor: null,
    busy: true,
    blocked_reason: null,
    concurrency: 3,
  });
  mount();
  await waitFor(() => expect(screen.getByText("Start B")).toBeEnabled());
  expect(screen.getByText("Start A")).toBeDisabled();
  expect(screen.getByTestId("cleanup")).toHaveTextContent("true");
});

it("does not resend an unknown request while cleanup has the global submission lock", async () => {
  sessionStorage.setItem(
    "filmpaw-transfer-requests",
    JSON.stringify([
      {
        body: { context_id: "context-A", names: ["MOV"], request_id: "request-A" },
        binding: binding("A"),
        state: "unknown",
      },
    ]),
  );
  mount(true);
  await waitFor(() => expect(getTransferByRequest).toHaveBeenCalled());
  await userEvent.click(screen.getByText("Confirm A"));
  expect(createTransfer).not.toHaveBeenCalled();
  expect(screen.getByTestId("unknown")).toHaveTextContent("request-A");
});

const item = (name: string, status: string) => ({
  name,
  status,
  size_bytes: 1000,
  progress_bytes: 1000,
  message: null,
});
const progressing = (overrides: Partial<ArchiveTask>): ArchiveTask => ({
  ...task("A"),
  updated_at: "2026-10-18T12:00:00Z",
  current_name: "MOV-1",
  total_count: 2,
  total_bytes: 2000,
  items: [item("MOV-1", "copying"), item("MOV-2", "pending")],
  ...overrides,
});

it.each([
  {
    label: "same-movie phase and byte regression",
    current: progressing({ phase: "verifying", progress_bytes: 1000, verified_bytes: 800 }),
    response: progressing({
      phase: "copying",
      progress_bytes: 100,
      verified_bytes: 0,
      stop_requested: true,
    }),
    expected: {
      phase: "verifying",
      current_name: "MOV-1",
      completed_count: 0,
      progress_bytes: 1000,
      verified_bytes: 800,
      stop_requested: true,
    },
  },
  {
    label: "same-phase verified byte regression",
    current: progressing({
      phase: "verifying",
      progress_bytes: 1000,
      verified_bytes: 800,
      stop_requested: true,
    }),
    response: progressing({ phase: "verifying", progress_bytes: 1000, verified_bytes: 50 }),
    expected: { phase: "verifying", verified_bytes: 800, stop_requested: true },
  },
  {
    label: "a late response from the previous movie",
    current: progressing({
      phase: "copying",
      current_name: "MOV-2",
      completed_count: 1,
      progress_bytes: 1500,
      verified_bytes: 1000,
      items: [item("MOV-1", "completed"), item("MOV-2", "copying")],
    }),
    response: progressing({
      phase: "cleaning",
      completed_count: 0,
      progress_bytes: 1000,
      verified_bytes: 1000,
      stop_requested: true,
    }),
    expected: {
      phase: "copying",
      current_name: "MOV-2",
      completed_count: 1,
      progress_bytes: 1500,
      verified_bytes: 1000,
      stop_requested: true,
    },
  },
  {
    label: "the next movie starts after the count already incremented",
    current: progressing({
      phase: "cleaning",
      completed_count: 1,
      progress_bytes: 1000,
      verified_bytes: 1000,
      items: [item("MOV-1", "completed"), item("MOV-2", "pending")],
    }),
    response: progressing({
      phase: "copying",
      current_name: "MOV-2",
      completed_count: 1,
      progress_bytes: 1100,
      verified_bytes: 1000,
      stop_requested: true,
      items: [item("MOV-1", "completed"), item("MOV-2", "copying")],
    }),
    expected: {
      phase: "copying",
      current_name: "MOV-2",
      completed_count: 1,
      progress_bytes: 1100,
      verified_bytes: 1000,
      stop_requested: true,
    },
  },
  {
    label: "an older timestamp still confirms a requested stop",
    current: progressing({ phase: "verifying", progress_bytes: 1000 }),
    response: progressing({
      updated_at: "2026-10-18T11:59:59Z",
      phase: "copying",
      progress_bytes: 100,
      stop_requested: true,
    }),
    expected: { phase: "verifying", progress_bytes: 1000, stop_requested: true },
  },
  {
    label: "a same-second fatal result remains visible rather than preserving running progress",
    current: progressing({ phase: "verifying", progress_bytes: 1000, verified_bytes: 800 }),
    response: progressing({
      status: "needs_review",
      phase: "finished",
      message: "日志保存失败",
      progress_bytes: 100,
      verified_bytes: 0,
      stop_requested: true,
    }),
    expected: {
      status: "needs_review",
      phase: "finished",
      message: "日志保存失败",
      progress_bytes: 100,
      verified_bytes: 0,
      stop_requested: true,
    },
  },
])(
  "merges a late stop acknowledgement without losing progress: $label",
  async ({ current, response, expected }) => {
    let resolve!: (value: ArchiveTask) => void;
    vi.mocked(stopTransfer).mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    vi.mocked(getTransfers).mockResolvedValue({
      active: [current],
      history: [],
      next_cursor: null,
      busy: true,
      blocked_reason: null,
      concurrency: 3,
    });
    const { client } = mount();
    await waitFor(() => expect(screen.getByTestId("tasks")).toHaveTextContent("task-A:running"));
    await userEvent.click(screen.getByText("Stop A"));
    act(() =>
      client.setQueryData<TransferList>(["archive-transfers"], (old) =>
        old ? { ...old, active: [current] } : old,
      ),
    );
    await act(async () => resolve(response));
    expect(client.getQueryData<TransferList>(["archive-transfers"])?.active[0]).toMatchObject(
      expected,
    );
    expect(stopTransfer).toHaveBeenCalledExactlyOnceWith("task-A");
  },
);
