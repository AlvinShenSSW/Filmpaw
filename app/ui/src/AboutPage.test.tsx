/** #34 关于页: 五种版本状态 + 两个数据源各自的失败态 + 路由接入。
 *
 * 重点不是"渲染出字来", 而是**失败态不能互相污染** —— health 挂了必须说"服务
 * 不可用", 绝不能顺带报成"版本不一致"(那会把用户引向重装安装包这条错路)。 */

import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { theme } from "./theme";

vi.mock("./client", () => ({
  healthApiHealthGet: vi.fn(),
  getSettingsApiSettingsGet: vi.fn(),
}));

import { AboutPage, versionState } from "./AboutPage";
import { getSettingsApiSettingsGet, healthApiHealthGet } from "./client";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ThemeProvider theme={theme}>
        <AboutPage />
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
});

describe("versionState (pure — the five states in §D)", () => {
  it("loading is its own state, never conflated with failure", () => {
    expect(versionState("1.0.0", undefined, true, true)).toEqual({ kind: "loading" });
  });

  it("no server version means unavailable — NOT a mismatch", () => {
    expect(versionState("1.0.0", undefined, false, true)).toEqual({ kind: "unavailable" });
  });

  it("equal versions match", () => {
    expect(versionState("1.0.0", "1.0.0", false, true)).toEqual({
      kind: "match",
      server: "1.0.0",
    });
  });

  it("packaged mismatch is an install problem", () => {
    expect(versionState("1.0.0", "0.9.0", false, true)).toEqual({
      kind: "mismatch-packaged",
      server: "0.9.0",
    });
  });

  it("dev mismatch is normal and must not be reported as a stale install", () => {
    expect(versionState("1.0.0", "0.9.0", false, false)).toEqual({
      kind: "mismatch-dev",
      server: "0.9.0",
    });
  });
});

describe("AboutPage", () => {
  it("shows the name, the intro and both versions", async () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "Filmpaw" })).toBeInTheDocument();
    expect(screen.getByText(/索引多个 NAS 的表演者一级目录/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getAllByText(`v${__APP_VERSION__}`).length).toBeGreaterThanOrEqual(2),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the db path the SERVICE opened", async () => {
    renderPage();
    expect(await screen.findByText("X:\\db\\library.db")).toBeInTheDocument();
    expect(screen.getByText("服务使用的数据库路径")).toBeInTheDocument();
  });

  it("health failure says the service is down and does NOT claim a version mismatch", async () => {
    vi.mocked(healthApiHealthGet).mockResolvedValue({ error: { detail: "boom" } } as never);
    renderPage();
    expect(await screen.findByText(/本地服务不可用/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/安装包异常/)).not.toBeInTheDocument();
    // settings still loaded, so its own value must survive the other failure
    expect(await screen.findByText("X:\\db\\library.db")).toBeInTheDocument();
  });

  it("settings failure degrades only the db path", async () => {
    vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({
      error: { detail: "boom" },
    } as never);
    renderPage();
    expect(await screen.findByText(/无法读取设置/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("both failing still renders the page and reports each failure separately", async () => {
    vi.mocked(healthApiHealthGet).mockResolvedValue({ error: {} } as never);
    vi.mocked(getSettingsApiSettingsGet).mockResolvedValue({ error: {} } as never);
    renderPage();
    expect(await screen.findByText(/本地服务不可用/)).toBeInTheDocument();
    expect(await screen.findByText(/无法读取设置/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Filmpaw" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("a packaged version mismatch raises an alert with a recovery path", async () => {
    vi.stubGlobal("__FILMPAW_PORT__", 8720);
    vi.stubEnv("NODE_ENV", "production");
    vi.mocked(healthApiHealthGet).mockResolvedValue({
      data: { status: "ok", version: "0.0.1" },
    } as never);
    renderPage();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/安装包异常/);
    expect(alert).toHaveTextContent(/请重新安装/); // an error must offer a way out
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });
});
