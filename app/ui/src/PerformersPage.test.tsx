/** Issue #5 behavior tests: alias 409, open->missing refetch, purge confirm,
 * shared alias display, debounce search, empty states. */

import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { theme } from "./theme";

vi.mock("./client", () => ({
  listPerformersApiPerformersGet: vi.fn(),
  listSourcesApiSourcesGet: vi.fn(),
  addAliasApiPerformersPerformerIdAliasesPost: vi.fn(),
  deleteAliasApiAliasesAliasIdDelete: vi.fn(),
  deletePerformerApiPerformersPerformerIdDelete: vi.fn(),
  purgeMissingApiPerformersPurgeMissingPost: vi.fn(),
  scanAllApiScanAllPost: vi.fn(),
}));
vi.mock("./openTarget", () => ({ openPerformerFolder: vi.fn() }));
vi.mock("./api", () => ({ serverBase: () => "http://127.0.0.1:8720" }));

import {
  addAliasApiPerformersPerformerIdAliasesPost,
  listPerformersApiPerformersGet,
  listSourcesApiSourcesGet,
  purgeMissingApiPerformersPurgeMissingPost,
} from "./client";
import { openPerformerFolder } from "./openTarget";
import { PerformersPage } from "./PerformersPage";

const P1 = {
  id: "a3f2aaaa-0000-0000-0000-000000000001",
  name: "倉木華",
  source_id: 1,
  source_label: "女优VI",
  unc_path: "\\\\Ant\\Video Station\\女优VI\\倉木華",
  is_missing: false,
  has_thumb: true,
  aliases: [{ id: 7, alias: "华姐" }],
};
const P2 = {
  ...P1,
  id: "b810bbbb-0000-0000-0000-000000000002",
  source_id: 2,
  source_label: "女优V",
  unc_path: "\\\\EAGLE\\Video Station\\女优V\\倉木華",
};
const MISSING = {
  ...P1,
  id: "c44dcccc-0000-0000-0000-000000000003",
  name: "小红",
  is_missing: true,
  has_thumb: false,
  aliases: [],
};

function envelope(items: unknown[], extra?: Partial<Record<string, unknown>>) {
  return {
    data: {
      items,
      total: items.length,
      page: 1,
      page_size: 200,
      source_count: 2,
      missing_total: (items as { is_missing?: boolean }[]).filter((i) => i.is_missing).length,
      ...extra,
    },
  };
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ThemeProvider theme={theme}>
        <PerformersPage />
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listPerformersApiPerformersGet).mockResolvedValue(envelope([P1, P2, MISSING]) as never);
  vi.mocked(listSourcesApiSourcesGet).mockResolvedValue({
    data: [
      {
        id: 1,
        unc_path: "\\\\Ant\\VS\\女优VI\\",
        label: "女优VI",
        last_scan_at: null,
        performer_count: 2,
        reachable: true,
      },
      {
        id: 2,
        unc_path: "\\\\EAGLE\\VS\\女优V\\",
        label: "女优V",
        last_scan_at: null,
        performer_count: 1,
        reachable: true,
      },
    ],
  } as never);
});

describe("PerformersPage", () => {
  it("shows shared alias chips on every same-name row (D5)", async () => {
    renderPage();
    expect(await screen.findAllByText("华姐")).toHaveLength(2);
    expect(screen.getByText("共 3 条 · 2 个来源")).toBeInTheDocument();
  });

  it("first-char avatar fallback for rows without folder.jpg", async () => {
    renderPage();
    expect(await screen.findByLabelText("小红 首字头像")).toHaveTextContent("小");
    expect(screen.getAllByAltText("倉木華 头像")).toHaveLength(2); // thumb imgs
  });

  it("alias add conflict (409) surfaces the server detail", async () => {
    vi.mocked(addAliasApiPerformersPerformerIdAliasesPost).mockResolvedValue({
      error: { detail: "同名组内该别名已存在" },
    } as never);
    renderPage();
    const addButtons = await screen.findAllByRole("button", { name: /＋ 别名/ });
    await userEvent.click(addButtons[0]);
    const input = screen.getByLabelText("为 倉木華 添加别名");
    await userEvent.type(input, "华姐{Enter}");
    expect(await screen.findByText("同名组内该别名已存在")).toBeInTheDocument();
  });

  it("open failure (404 gone) refetches so the row turns missing", async () => {
    vi.mocked(openPerformerFolder).mockRejectedValue({
      status: 404,
      detail: "文件夹已不存在, 已标记失效",
    });
    renderPage();
    await userEvent.click(await screen.findByLabelText("打开 小红 的文件夹"));
    expect(await screen.findByText("文件夹已不存在, 已标记失效")).toBeInTheDocument();
    await waitFor(() =>
      expect(vi.mocked(listPerformersApiPerformersGet).mock.calls.length).toBeGreaterThan(1),
    );
  });

  it("purge confirm shows missing count and reports deletions", async () => {
    vi.mocked(purgeMissingApiPerformersPurgeMissingPost).mockResolvedValue({
      data: { deleted: 1 },
    } as never);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "清理失效" }));
    expect(await screen.findByText(/全库共 1 条失效记录将被删除/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "清理" }));
    expect(await screen.findByText("已清理 1 条失效记录")).toBeInTheDocument();
  });

  it("delete button only on missing rows; search empty state", async () => {
    renderPage();
    expect(await screen.findByLabelText("删除失效记录 小红")).toBeInTheDocument();
    expect(screen.queryByLabelText("删除失效记录 倉木華")).not.toBeInTheDocument();

    vi.mocked(listPerformersApiPerformersGet).mockResolvedValue(envelope([]) as never);
    await userEvent.type(screen.getByLabelText("搜索表演者"), "不存在的人");
    expect(await screen.findByText(/没有匹配「不存在的人」的记录/)).toBeInTheDocument();
  });
});

describe("pagination", () => {
  it("offers load-more when total exceeds the fetched page (Codex P2)", async () => {
    vi.mocked(listPerformersApiPerformersGet).mockResolvedValue(
      envelope([P1, P2], { total: 450, missing_total: 0 }) as never,
    );
    renderPage();
    expect(await screen.findByText(/共 450 条 · 2 个来源 · 已显示 2/)).toBeInTheDocument();
    const more = screen.getByRole("button", { name: "加载更多" });
    await userEvent.click(more);
    await waitFor(() =>
      expect(
        vi
          .mocked(listPerformersApiPerformersGet)
          .mock.calls.some((c) => (c[0] as { query?: { page?: number } })?.query?.page === 2),
      ).toBe(true),
    );
  });

  it("purge confirm reports the GLOBAL missing count, not the view (Codex P1)", async () => {
    vi.mocked(listPerformersApiPerformersGet).mockResolvedValue(
      envelope([P1], { missing_total: 7 }) as never,
    );
    vi.mocked(purgeMissingApiPerformersPurgeMissingPost).mockResolvedValue({
      data: { deleted: 7 },
    } as never);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "清理失效" }));
    expect(await screen.findByText(/全库共 7 条失效记录将被删除/)).toBeInTheDocument();
  });
});

describe("error surfacing (Kimi P2)", () => {
  it("list failure shows an error alert with retry, not the empty state", async () => {
    vi.mocked(listPerformersApiPerformersGet).mockRejectedValue(new Error("ECONNREFUSED"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("列表加载失败");
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
    expect(screen.queryByText(/还没有表演者/)).not.toBeInTheDocument();
  });
});

describe("source filter (#8)", () => {
  it("passes source_id and updates footer to the selected source", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("共 3 条 · 2 个来源");

    const combo = await screen.findByRole("combobox");
    await user.click(combo);
    const listbox = await screen.findByRole("listbox");
    await user.click(await within(listbox).findByText("女优VI"));

    await waitFor(() =>
      expect(
        vi
          .mocked(listPerformersApiPerformersGet)
          .mock.calls.some(
            (c) => (c[0] as { query?: { source_id?: number } })?.query?.source_id === 1,
          ),
      ).toBe(true),
    );
    expect(await screen.findByText(/当前来源: 女优VI/)).toBeInTheDocument();
  });

  it("default shows all sources (no source_id param)", async () => {
    renderPage();
    await screen.findByText("共 3 条 · 2 个来源");
    const calls = vi.mocked(listPerformersApiPerformersGet).mock.calls;
    expect(
      calls.every(
        (c) => (c[0] as { query?: { source_id?: number } })?.query?.source_id === undefined,
      ),
    ).toBe(true);
  });
});

describe("source filter resilience (Kimi)", () => {
  it("resets the filter when the selected source disappears (P2)", async () => {
    const user = userEvent.setup();
    renderPage();
    const combo = await screen.findByRole("combobox");
    await user.click(combo);
    await user.click(await within(await screen.findByRole("listbox")).findByText("女优VI"));
    expect(await screen.findByText(/当前来源: 女优VI/)).toBeInTheDocument();

    // the source gets deleted server-side; next sources fetch omits it
    vi.mocked(listSourcesApiSourcesGet).mockResolvedValue({
      data: [
        {
          id: 2,
          unc_path: "\\EAGLEVS女优V\\",
          label: "女优V",
          last_scan_at: null,
          performer_count: 1,
          reachable: true,
        },
      ],
    } as never);
    // simulate a sources refetch (rescan success invalidates ["sources"])
    const { scanAllApiScanAllPost } = await import("./client");
    vi.mocked(scanAllApiScanAllPost).mockResolvedValue({ data: [] } as never);
    await user.click(screen.getByRole("button", { name: "全部重扫" }));
    expect(await screen.findByText(/2 个来源/)).toBeInTheDocument(); // reset to all
  });
});

describe("poster grid (#27)", () => {
  it("renders poster tiles at the enlarged size with all row features kept", async () => {
    renderPage();
    // poster ~3x the old 34x48
    const img = (await screen.findAllByAltText("倉木華 头像"))[0];
    expect(img).toHaveStyle({ width: "110px", height: "156px" });
    // first-char fallback scales too
    expect(await screen.findByLabelText("小红 首字头像")).toHaveStyle({ width: "110px" });

    // every table-era affordance survives the grid rewrite
    expect(await screen.findAllByText("华姐")).toHaveLength(2); // shared alias chips
    expect(screen.getAllByRole("button", { name: /＋ 别名/ }).length).toBeGreaterThan(0);
    expect(screen.getByLabelText("打开 小红 的文件夹")).toBeInTheDocument();
    expect(screen.getByLabelText("删除失效记录 小红")).toBeInTheDocument();
    expect(screen.getAllByText("● 在线").length).toBeGreaterThan(0);
    expect(screen.getByText("○ 失效")).toBeInTheDocument();
    expect(screen.getByText("共 3 条 · 2 个来源")).toBeInTheDocument(); // footer stats
  });

  it("archive cards use the larger card poster", async () => {
    // covered in ArchivePage.test.tsx render; here assert the size table itself
    const { POSTER } = await import("./PerformersPage");
    expect(POSTER.card.w).toBe(156);
    expect(POSTER.card.h).toBe(222);
    expect(POSTER.grid.w / 34).toBeGreaterThanOrEqual(3); // "至少大三倍"
  });
});
