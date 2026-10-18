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
vi.mock("./ExitCoordinator", () => ({ ExitCoordinator: () => null }));
vi.mock("./archiveApi", () => ({
  getLatestTransfer: vi.fn(),
  createTransfer: vi.fn(),
  createArchiveContext: vi.fn(),
  stopTransfer: vi.fn(),
  archiveError: () => "请求失败",
  isTransferActive: (task: { status: string } | null) =>
    task?.status === "queued" || task?.status === "running",
}));

import { routeTree } from "./App";
import { createArchiveContext, getLatestTransfer, stopTransfer } from "./archiveApi";
import {
  getSettingsApiSettingsGet,
  healthApiHealthGet,
  listPerformersApiPerformersGet,
  listSourcesApiSourcesGet,
  localSubdirsApiLocalSubdirsGet,
  putSettingsApiSettingsPut,
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
  vi.mocked(getLatestTransfer).mockResolvedValue(null);
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

describe("global archive task", () => {
  const restoreTask = {
    id: "restore-1",
    local_dir: "D:\\batch",
    subdir: "演员甲",
    performer_id: "actor-1",
    performer_name: "演员甲",
    target_path: "\\\\NAS\\演员甲",
    source_path: "D:\\batch\\演员甲",
    source_label: "主片库",
    has_thumb: false,
    status: "running",
    phase: "copying",
    completed_count: 0,
    total_count: 1,
    total_bytes: 100,
    progress_bytes: 20,
    verified_bytes: 0,
    conflicts: [],
    items: [],
    stop_requested: false,
  };

  it("restores the task actor and exact NAS target from another page", async () => {
    vi.mocked(getLatestTransfer).mockResolvedValue(restoreTask as never);
    vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({
      data: { last_local_dir: restoreTask.local_dir, db_path: "X:\\db" },
    } as never);
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue({
      data: { path: restoreTask.local_dir, subdirs: [restoreTask.subdir] },
    } as never);
    vi.mocked(createArchiveContext).mockResolvedValue({
      ...restoreTask,
      id: "restored-context",
      source_items: [],
      target_items: [],
      source_error: null,
      target_error: null,
    } as never);
    renderAt("/about");
    await userEvent.click(await screen.findByRole("button", { name: "查看目录" }));
    expect(await screen.findByRole("region", { name: "本地影片" })).toBeInTheDocument();
    expect(createArchiveContext).toHaveBeenCalledWith(
      { local_dir: "D:\\batch", subdir: "演员甲", performer_id: "actor-1" },
      expect.anything(),
    );
    expect(screen.getByRole("heading", { name: "演员甲" })).toBeInTheDocument();
    expect(putSettingsApiSettingsPut).not.toHaveBeenCalled();
  });

  it("does not replace a different approved batch when reopening an old task", async () => {
    vi.mocked(getLatestTransfer).mockResolvedValue(restoreTask as never);
    vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({
      data: { last_local_dir: "D:\\other-batch", db_path: "X:\\db" },
    } as never);
    vi.mocked(localSubdirsApiLocalSubdirsGet).mockResolvedValue({
      data: { path: "D:\\other-batch", subdirs: [] },
    } as never);
    renderAt("/about");
    await userEvent.click(await screen.findByRole("button", { name: "查看目录" }));
    expect(await screen.findByText(/请先选择这个 batch/)).toBeInTheDocument();
    expect(createArchiveContext).not.toHaveBeenCalled();
    expect(putSettingsApiSettingsPut).not.toHaveBeenCalled();
  });

  it("keeps task status and stop controls available after leaving the archive page", async () => {
    const task = {
      id: "job-1",
      status: "running",
      phase: "copying",
      performer_name: "演员甲",
      target_path: "\\\\NAS\\演员甲",
      source_path: "D:\\batch\\演员甲",
      source_label: "片库",
      current_name: "MOV-3",
      completed_count: 0,
      total_count: 1,
      total_bytes: 100,
      progress_bytes: 20,
      verified_bytes: 0,
      conflicts: [],
      items: [],
      stop_requested: false,
    };
    vi.mocked(getLatestTransfer).mockResolvedValue(task as never);
    vi.mocked(stopTransfer).mockResolvedValue({ ...task, stop_requested: true } as never);
    renderAt("/archive");
    expect(await screen.findByRole("status", { name: "归档任务" })).toHaveTextContent("正在复制");
    await userEvent.click(screen.getByRole("link", { name: "关于" }));
    expect(await screen.findByRole("heading", { name: "Filmpaw" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "归档任务" })).toHaveTextContent("MOV-3");
    vi.mocked(getLatestTransfer).mockResolvedValue({ ...task, stop_requested: true } as never);
    await userEvent.click(screen.getByRole("button", { name: "停止传输" }));
    await userEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "停止队列" }),
    );
    await waitFor(() => expect(stopTransfer).toHaveBeenCalledWith("job-1"));
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "归档任务" })).toHaveTextContent("正在安全停止"),
    );
  });
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
