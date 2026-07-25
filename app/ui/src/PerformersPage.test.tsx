/** Issue #5 behavior tests: alias 409, open->missing refetch, purge confirm,
 * shared alias display, debounce search, empty states. */

import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { theme } from "./theme";

vi.mock("./client", () => ({
  listPerformersApiPerformersGet: vi.fn(),
  addAliasApiPerformersPerformerIdAliasesPost: vi.fn(),
  deleteAliasApiAliasesAliasIdDelete: vi.fn(),
  deletePerformerApiPerformersPerformerIdDelete: vi.fn(),
  openPerformerApiPerformersPerformerIdOpenPost: vi.fn(),
  purgeMissingApiPerformersPurgeMissingPost: vi.fn(),
  scanAllApiScanAllPost: vi.fn(),
}));
vi.mock("./api", () => ({ serverBase: () => "http://127.0.0.1:8720" }));

import {
  addAliasApiPerformersPerformerIdAliasesPost,
  listPerformersApiPerformersGet,
  openPerformerApiPerformersPerformerIdOpenPost,
  purgeMissingApiPerformersPurgeMissingPost,
} from "./client";
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
  vi.mocked(listPerformersApiPerformersGet).mockResolvedValue(envelope([P1, P2, MISSING]) as never);
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
    vi.mocked(openPerformerApiPerformersPerformerIdOpenPost).mockResolvedValue({
      error: { detail: "文件夹已不存在, 已标记失效" },
    } as never);
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
