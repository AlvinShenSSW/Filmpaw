/** #34 导航接入: 通过真实 routeTree 断言, 而不是单独渲染页面组件。
 *
 * 单独渲染 AboutPage 无法发现"路由没挂上"或"左栏没有入口"; 也无法验证激活态与
 * `aria-current`。这里挂真实路由树 + 内存历史。 */

import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { theme } from "./theme";

vi.mock("./client", () => ({
  healthApiHealthGet: vi.fn(),
  getSettingsApiSettingsGet: vi.fn(),
  listSourcesApiSourcesGet: vi.fn(),
  addSourceApiSourcesPost: vi.fn(),
  deleteSourceApiSourcesSourceIdDelete: vi.fn(),
  scanOneApiSourcesSourceIdScanPost: vi.fn(),
  scanAllApiScanAllPost: vi.fn(),
  listPerformersApiPerformersGet: vi.fn(),
  localSubdirsApiLocalSubdirsGet: vi.fn(),
  putSettingsApiSettingsPut: vi.fn(),
  purgeMissingApiPerformersPurgeMissingPost: vi.fn(),
  addAliasApiPerformersPerformerIdAliasesPost: vi.fn(),
  deleteAliasApiAliasesAliasIdDelete: vi.fn(),
  deletePerformerApiPerformersPerformerIdDelete: vi.fn(),
  openPairApiOpenPairPost: vi.fn(),
  openPerformerApiPerformersPerformerIdOpenPost: vi.fn(),
}));
vi.mock("./api", () => ({ serverBase: () => "http://127.0.0.1:8720" }));
vi.mock("./pickDirectory", () => ({ pickDirectory: vi.fn() }));

import { routeTree } from "./App";
import {
  getSettingsApiSettingsGet,
  healthApiHealthGet,
  listPerformersApiPerformersGet,
  listSourcesApiSourcesGet,
} from "./client";

function renderAt(path: string) {
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ThemeProvider theme={theme}>
        {/* biome-ignore lint/suspicious/noExplicitAny: test-only router instance */}
        <RouterProvider router={router as any} />
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(healthApiHealthGet).mockResolvedValue({
    data: { status: "ok", version: __APP_VERSION__ },
  } as never);
  vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({
    data: { last_local_dir: null, db_path: "X:\\db\\library.db" },
  } as never);
  vi.mocked(listSourcesApiSourcesGet).mockResolvedValue({ data: [] } as never);
  vi.mocked(listPerformersApiPerformersGet).mockResolvedValue({
    data: { items: [], total: 0, page: 1, page_size: 50, source_count: 0, missing_total: 0 },
  } as never);
});

describe("rail navigation", () => {
  it("exposes exactly one interactive element per destination, each named", async () => {
    renderAt("/");
    // TanStack Router mounts asynchronously — query after the shell exists.
    const nav = await screen.findByRole("navigation", { name: "主导航" });
    const links = within(nav).getAllByRole("link");
    expect(links.map((l) => l.getAttribute("aria-label"))).toEqual([
      "表演者库",
      "归档对比",
      "设置",
      "关于",
    ]);
    // The old <Link><IconButton/></Link> produced a second tab stop per item.
    expect(within(nav).queryAllByRole("button")).toHaveLength(0);
  });

  it("marks the current destination with aria-current", async () => {
    renderAt("/about");
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "关于" })).toHaveAttribute("aria-current", "page"),
    );
    expect(screen.getByRole("link", { name: "设置" })).not.toHaveAttribute("aria-current");
  });

  it("clicking 关于 navigates there", async () => {
    renderAt("/settings");
    await userEvent.click(await screen.findByRole("link", { name: "关于" }));
    expect(await screen.findByRole("heading", { name: "Filmpaw" })).toBeInTheDocument();
  });
});

describe("the version/db self-check lives on ONE page (#34)", () => {
  it("/about shows it", async () => {
    renderAt("/about");
    expect(await screen.findByText("X:\\db\\library.db")).toBeInTheDocument();
    expect(screen.getByText("服务使用的数据库路径")).toBeInTheDocument();
  });

  it("/settings no longer duplicates it", async () => {
    renderAt("/settings");
    expect(await screen.findByText(/还没有扫描源/)).toBeInTheDocument(); // page rendered
    expect(screen.queryByText(/X:\\db\\library.db/)).not.toBeInTheDocument();
    expect(screen.queryByText(/server v/)).not.toBeInTheDocument();
  });
});
