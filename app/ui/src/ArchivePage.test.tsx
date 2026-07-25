/** Issue #6 behavior tests: left-select autofill, pair decoupling, disabled
 * states, open-pair failure semantics, empty states. */

import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { theme } from "./theme";

vi.mock("./client", () => ({
  getSettingsApiSettingsGet: vi.fn(),
  putSettingsApiSettingsPut: vi.fn(),
  localSubdirsApiLocalSubdirsGet: vi.fn(),
  listPerformersApiPerformersGet: vi.fn(),
  openPairApiOpenPairPost: vi.fn(),
}));
vi.mock("./api", () => ({ serverBase: () => "http://127.0.0.1:8720" }));

import { ArchivePage } from "./ArchivePage";
import {
  getSettingsApiSettingsGet,
  listPerformersApiPerformersGet,
  localSubdirsApiLocalSubdirsGet,
  openPairApiOpenPairPost,
} from "./client";

const REC = {
  id: "a3f2aaaa-0000-0000-0000-000000000001",
  name: "倉木華",
  source_id: 1,
  source_label: "女优VI",
  unc_path: "\\\\Ant\\Video Station\\女优VI\\倉木華",
  is_missing: false,
  has_thumb: false,
  aliases: [{ id: 7, alias: "华姐" }],
};
const MISSING_REC = { ...REC, id: "c44d-3", name: "小红", is_missing: true, aliases: [] };

function envelope(items: unknown[]) {
  return {
    data: {
      items,
      total: items.length,
      page: 1,
      page_size: 50,
      source_count: 1,
      missing_total: 0,
    },
  };
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ThemeProvider theme={theme}>
        <ArchivePage />
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

beforeEach(() => {
  vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({
    data: { last_local_dir: "D:\\Downloads\\新片", db_path: "X:\\db" },
  } as never);
  vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue({
    data: { path: "D:\\Downloads\\新片", subdirs: ["倉木華", "小红"] },
  } as never);
  vi.mocked(listPerformersApiPerformersGet).mockResolvedValue(
    envelope([REC, MISSING_REC]) as never,
  );
  vi.mocked(openPairApiOpenPairPost).mockResolvedValue({ data: null } as never);
});

describe("ArchivePage", () => {
  it("restores last dir, lists subfolders; left click autofills search", async () => {
    renderPage();
    expect(await screen.findByText("D:\\Downloads\\新片")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: /倉木華/ }));
    expect(screen.getByLabelText("匹配搜索")).toHaveValue("倉木華");
    expect(await screen.findByText(/2 条匹配/)).toBeInTheDocument();
  });

  it("pair uses LEFT selection even after manual search edits (decoupling)", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /^小红$/ }));
    const searchBox = screen.getByLabelText("匹配搜索");
    await userEvent.clear(searchBox);
    await userEvent.type(searchBox, "倉木華");
    const pairButtons = await screen.findAllByRole("button", { name: /双开/ });
    await userEvent.click(pairButtons[0]);
    await waitFor(() =>
      expect(openPairApiOpenPairPost).toHaveBeenCalledWith({
        body: {
          local_dir: "D:\\Downloads\\新片",
          subdir: "小红", // left selection, not search text
          performer_id: REC.id,
        },
      }),
    );
  });

  it("pair disabled without a left selection and on missing cards", async () => {
    renderPage();
    const searchBox = await screen.findByLabelText("匹配搜索");
    await userEvent.type(searchBox, "倉木華");
    const buttons = await screen.findAllByRole("button", { name: /双开/ });
    for (const b of buttons) expect(b).toBeDisabled(); // no left selection

    await userEvent.click(screen.getByRole("button", { name: /^小红$/ }));
    const after = await screen.findAllByRole("button", { name: /双开/ });
    expect(after[0]).toBeEnabled(); // normal record
    expect(after[1]).toBeDisabled(); // missing record stays disabled
  });

  it("open-pair 422 surfaces re-select prompt path", async () => {
    vi.mocked(openPairApiOpenPairPost).mockResolvedValue({
      error: { detail: "本地目录不存在 — 请重新选择" },
    } as never);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /倉木華/ }));
    const pairButtons = await screen.findAllByRole("button", { name: /双开/ });
    await userEvent.click(pairButtons[0]);
    expect(await screen.findByText("本地目录不存在 — 请重新选择")).toBeInTheDocument();
  });

  it("unreachable saved dir shows re-select alert", async () => {
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue({
      error: { detail: "目录不存在或不可访问" },
    } as never);
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("请重新选择");
  });

  it("no-match empty state suggests aliases or a new performer", async () => {
    vi.mocked(listPerformersApiPerformersGet).mockResolvedValue(envelope([]) as never);
    renderPage();
    await userEvent.type(await screen.findByLabelText("匹配搜索"), "小C");
    expect(await screen.findByText(/库中没有「小C」/)).toBeInTheDocument();
    expect(screen.getByText(/可能用了别的艺名/)).toBeInTheDocument();
  });
});

describe("Kimi verify fixes", () => {
  it("search failure shows an alert with retry (not silence)", async () => {
    vi.mocked(listPerformersApiPerformersGet).mockRejectedValue(new Error("ECONNREFUSED"));
    renderPage();
    await userEvent.type(await screen.findByLabelText("匹配搜索"), "谁");
    expect(await screen.findByRole("alert")).toHaveTextContent("匹配加载失败");
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });

  it("pair sends dir and subdir separately (server joins, Kimi R4)", async () => {
    vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({
      data: { last_local_dir: "/mnt/downloads", db_path: "X:db" },
    } as never);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /^小红$/ }));
    const pairButtons = await screen.findAllByRole("button", { name: /双开/ });
    await userEvent.click(pairButtons[0]);
    await waitFor(() =>
      expect(openPairApiOpenPairPost).toHaveBeenCalledWith({
        body: { local_dir: "/mnt/downloads", subdir: "小红", performer_id: REC.id },
      }),
    );
  });
});

describe("stale results (Kimi R3)", () => {
  it("clearing the search hides previous results with the empty prompt", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /倉木華/ }));
    expect(await screen.findByText(/2 条匹配/)).toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("匹配搜索"));
    expect(await screen.findByText(/选择左侧文件夹开始匹配/)).toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: /双开/ })).toHaveLength(0);
  });
});

describe("chooseDir forensics (#18)", () => {
  it("shows the REAL error (not 'not found') when probe fails on a transient error", async () => {
    const { putSettingsApiSettingsPut } = await import("./client");
    vi.spyOn(window, "prompt").mockReturnValue("D:/Downloads/新片");
    // fresh mount with no saved dir so chooseDir is the only probe caller
    vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({
      data: { last_local_dir: null, db_path: "X:db" },
    } as never);
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValueOnce({
      error: undefined, // network/CORS reject shape
    } as never);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /选择本地目录/ }));
    expect(await screen.findByText(/无法读取目录: server 未响应/)).toBeInTheDocument();
    expect(putSettingsApiSettingsPut).not.toHaveBeenCalled();
  });

  it("persists only after the server accepts the anchor, using its canonical value", async () => {
    const { putSettingsApiSettingsPut } = await import("./client");
    vi.spyOn(window, "prompt").mockReturnValue("D:/Downloads/Movies/"); // fwd slash + trailing
    const CANON = "D:\\Downloads\\Movies";
    vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({
      data: { last_local_dir: null, db_path: "X:\\db" },
    } as never);
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue({
      data: { path: CANON, subdirs: ["倉木華"] },
    } as never);
    vi.mocked(putSettingsApiSettingsPut).mockResolvedValue({
      data: { last_local_dir: CANON, db_path: "X:\\db" },
    } as never);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /选择本地目录/ }));
    // fwd slashes + trailing separator normalized to canonical backslashes
    await waitFor(() => expect(putSettingsApiSettingsPut).toHaveBeenCalled());
    const arg = vi.mocked(putSettingsApiSettingsPut).mock.calls[0][0] as {
      body: { last_local_dir: string };
    };
    expect(arg.body.last_local_dir).toBe(CANON);
    expect(await screen.findByText(CANON)).toBeInTheDocument();
  });

  it("reverts and reports when the server rejects the anchor", async () => {
    vi.spyOn(window, "prompt").mockReturnValue("C:\\");
    // drive root keeps its separator (not drive-relative "C:")
    vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({
      data: { last_local_dir: null, db_path: "X:db" },
    } as never);
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue({
      data: { path: "C:\\", subdirs: [] },
    } as never);
    const { putSettingsApiSettingsPut } = await import("./client");
    vi.mocked(putSettingsApiSettingsPut).mockResolvedValue({
      error: { detail: "不能选择磁盘根目录" },
    } as never);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /选择本地目录/ }));
    expect(await screen.findByText(/保存目录失败: 不能选择磁盘根目录/)).toBeInTheDocument();
    // localDir not applied -> still the empty-prompt hint
    expect(screen.getByText(/选择你新下载电影所在的目录/)).toBeInTheDocument();
  });
});

describe("drive-root normalization (Codex #18)", () => {
  it("keeps the separator for a drive root instead of making it drive-relative", async () => {
    vi.spyOn(window, "prompt").mockReturnValue("C:/");
    vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({
      data: { last_local_dir: null, db_path: "X:db" },
    } as never);
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue({
      data: { path: "C:\\", subdirs: [] },
    } as never);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /选择本地目录/ }));
    await waitFor(() =>
      expect(localSubdirsApiLocalSubdirsGet).toHaveBeenCalledWith({ query: { path: "C:\\" } }),
    );
  });
});
