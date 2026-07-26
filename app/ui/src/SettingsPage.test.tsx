/** Issue #4 behavior tests: invalid-UNC error, scan states, delete confirm. */

import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { theme } from "./theme";

vi.mock("./pickDirectory", () => ({ pickDirectory: vi.fn() }));
vi.mock("./client", () => ({
  listSourcesApiSourcesGet: vi.fn(),
  addSourceApiSourcesPost: vi.fn(),
  deleteSourceApiSourcesSourceIdDelete: vi.fn(),
  scanOneApiSourcesSourceIdScanPost: vi.fn(),
  healthApiHealthGet: vi.fn(),
  getSettingsApiSettingsGet: vi.fn(),
}));

import {
  addSourceApiSourcesPost,
  deleteSourceApiSourcesSourceIdDelete,
  getSettingsApiSettingsGet,
  healthApiHealthGet,
  listSourcesApiSourcesGet,
  scanOneApiSourcesSourceIdScanPost,
} from "./client";
import { pickDirectory } from "./pickDirectory";
import { SettingsPage } from "./SettingsPage";

const SRC = {
  id: 1,
  unc_path: "\\\\Ant\\Video Station\\女优VI\\",
  label: "女优VI",
  last_scan_at: "2026-07-24T21:03:00",
  performer_count: 31,
  reachable: true,
};

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ThemeProvider theme={theme}>
        <SettingsPage />
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listSourcesApiSourcesGet).mockResolvedValue({ data: [SRC] } as never);
  vi.mocked(healthApiHealthGet).mockResolvedValue({
    data: { status: "ok", version: "0.1.0" },
  } as never);
  vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({
    data: { last_local_dir: null, db_path: "X:\\db" },
  } as never);
});

describe("SettingsPage", () => {
  it("shows red error on invalid UNC and adds nothing", async () => {
    vi.mocked(addSourceApiSourcesPost).mockResolvedValue({
      error: { detail: "路径不可达或不是目录: Z:\\bad\\" },
    } as never);
    renderPage();
    await userEvent.type(await screen.findByLabelText("新扫描源路径"), "Z:\\bad");
    await userEvent.click(screen.getByRole("button", { name: /添加源/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("路径不可达");
  });

  it("scan shows summary on success and red state on 503", async () => {
    vi.mocked(scanOneApiSourcesSourceIdScanPost).mockResolvedValue({
      data: { added: 2, refreshed: 29, missing: 1 },
    } as never);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /扫描/ }));
    expect(await screen.findByText("新增 2 · 更新 29 · 失效 1")).toBeInTheDocument();

    vi.mocked(scanOneApiSourcesSourceIdScanPost).mockResolvedValue({
      error: { detail: "源不可达" },
    } as never);
    await userEvent.click(screen.getByRole("button", { name: /扫描/ }));
    expect(await screen.findByText(/源不可达/)).toBeInTheDocument();
  });

  it("delete asks for confirmation showing the performer count", async () => {
    vi.mocked(deleteSourceApiSourcesSourceIdDelete).mockResolvedValue({} as never);
    renderPage();
    await userEvent.click(await screen.findByLabelText("删除源"));
    expect(await screen.findByText("删除扫描源?")).toBeInTheDocument();
    expect(screen.getByText("31")).toBeInTheDocument(); // record count in dialog

    await userEvent.click(screen.getByRole("button", { name: "删除" }));
    await waitFor(() =>
      expect(deleteSourceApiSourcesSourceIdDelete).toHaveBeenCalledWith({
        path: { source_id: 1 },
      }),
    );
  });

  it("no longer carries the db/version footer — it moved to 关于 (#34)", async () => {
    renderPage();
    await screen.findByLabelText("新扫描源路径"); // page finished rendering
    // Kept as a NEGATIVE assertion rather than deleted: this is what stops the
    // self-check from quietly reappearing here and drifting from /about.
    // The positive assertions now live in App.test.tsx, through the router.
    expect(screen.queryByText(/数据库:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/server v/)).not.toBeInTheDocument();
  });
});

describe("scan transport failure", () => {
  it("resets the row instead of sticking in scanning (Codex P2)", async () => {
    vi.mocked(listSourcesApiSourcesGet).mockResolvedValue({ data: [SRC] } as never);
    vi.mocked(scanOneApiSourcesSourceIdScanPost).mockRejectedValue(new Error("ECONNREFUSED"));
    renderPage();
    const btn = await screen.findByRole("button", { name: /扫描/ });
    await userEvent.click(btn);
    expect(await screen.findByText(/源不可达/)).toBeInTheDocument();
    expect(btn).toBeEnabled(); // not stuck in scanning
  });
});

describe("browse for source (#19)", () => {
  it("fills the input from the picker without a separate UI validation", async () => {
    vi.mocked(pickDirectory).mockResolvedValue("\\AntVideo Station女优VI");
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "浏览" }));
    await waitFor(() =>
      expect(screen.getByLabelText("新扫描源路径")).toHaveValue("\\AntVideo Station女优VI"),
    );
    // add still uses the server-side validating endpoint (not touched yet)
    expect(addSourceApiSourcesPost).not.toHaveBeenCalled();
  });

  it("cancel (null) leaves the input unchanged", async () => {
    vi.mocked(pickDirectory).mockResolvedValue(null);
    renderPage();
    await userEvent.type(await screen.findByLabelText("新扫描源路径"), "typed");
    await userEvent.click(screen.getByRole("button", { name: "浏览" }));
    expect(screen.getByLabelText("新扫描源路径")).toHaveValue("typed");
  });
});
