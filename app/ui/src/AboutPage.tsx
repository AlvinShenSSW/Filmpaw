/** 关于页 (#34) — 名称、简介, 以及一个常驻的版本自检点。
 *
 * 双版本不是装饰: 本项目发生过 "PyInstaller 失败但 tauri build 成功 → 装了个
 * 陈旧 sidecar" 的事故, 当时难查正是因为界面上无从分辨版本。把 UI 版本(构建期
 * 来自 tauri.conf.json)与服务版本(运行时来自 /api/health)并排显示, 不一致时
 * 报警, 使这类事故在打开这一页时就暴露。
 *
 * 局限: 双 SemVer 只能发现"版本号不同"的陈旧产物 —— 同一 0.4.0 内的旧构建它
 * 认不出来。真正的构建新鲜度检查在 CI(frozen-sidecar smoke 比对 health 与
 * tauri.conf.json), 页面上也如实写明。
 */
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import ReportProblemOutlinedIcon from "@mui/icons-material/ReportProblemOutlined";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { useQuery } from "@tanstack/react-query";
import { getSettingsApiSettingsGet, healthApiHealthGet } from "./client";
import { tokens } from "./theme";

export const ABOUT_INTRO =
  "Filmpaw 索引多个 NAS 的表演者一级目录, 支持别名与繁简搜索, 并双开本地与选定的 NAS 目录, " +
  "便于人工核对。索引默认存入本机 SQLite; 不含云同步、遥测或第三方上传。";

export type VersionState =
  | { kind: "loading" }
  | { kind: "unavailable" }
  | { kind: "match"; server: string }
  | { kind: "mismatch-packaged"; server: string }
  | { kind: "mismatch-dev"; server: string };

/**
 * Pure so both branches are testable without faking a Tauri runtime.
 *
 * `isPackaged` must NOT be derived from isTauri()/__TAURI_INTERNALS__ alone —
 * those are true in `tauri dev` too, where a version difference is normal and
 * must not be reported as a broken install.
 */
export function versionState(
  appVersion: string,
  serverVersion: string | undefined,
  isLoading: boolean,
  isPackaged: boolean,
): VersionState {
  if (isLoading) return { kind: "loading" };
  if (!serverVersion) return { kind: "unavailable" };
  if (serverVersion === appVersion) return { kind: "match", server: serverVersion };
  return {
    kind: isPackaged ? "mismatch-packaged" : "mismatch-dev",
    server: serverVersion,
  };
}

/** PROD build AND running inside the shell (the port is injected only there). */
export function detectPackaged(): boolean {
  return (
    process.env.NODE_ENV === "production" &&
    (window as { __FILMPAW_PORT__?: number }).__FILMPAW_PORT__ !== undefined
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <Box sx={{ display: "flex", gap: 2, alignItems: "baseline", flexWrap: "wrap" }}>
      <Typography variant="body2" color="text.secondary" sx={{ minWidth: 148 }}>
        {label}
      </Typography>
      <Box sx={{ flex: 1, minWidth: 0 }}>{children}</Box>
    </Box>
  );
}

export function AboutPage() {
  const health = useQuery({
    queryKey: ["health"],
    retry: false,
    queryFn: async () => {
      const r = await healthApiHealthGet();
      if (r.error || !r.data) throw r.error ?? new Error("health");
      return r.data;
    },
  });
  const settings = useQuery({
    queryKey: ["settings"],
    retry: false,
    queryFn: async () => {
      const r = await getSettingsApiSettingsGet();
      if (r.error || !r.data) throw r.error ?? new Error("settings");
      return r.data;
    },
  });

  const state = versionState(
    __APP_VERSION__,
    health.data?.version,
    health.isPending,
    detectPackaged(),
  );

  return (
    <Box sx={{ p: 3, maxWidth: 720 }}>
      <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, mb: 1 }}>
        <InfoOutlinedIcon sx={{ color: tokens.orangeDeep }} />
        <Typography variant="h5" component="h1" sx={{ fontWeight: 600, color: tokens.ink }}>
          Filmpaw
        </Typography>
      </Box>

      <Typography variant="body1" sx={{ color: tokens.ink, lineHeight: 1.75, mb: 3 }}>
        {ABOUT_INTRO}
      </Typography>

      <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
        <Row label="应用版本">
          <Typography variant="body2" sx={{ fontFamily: tokens.mono, color: tokens.ink }}>
            v{__APP_VERSION__}
          </Typography>
        </Row>

        <Row label="服务版本">
          {state.kind === "loading" && (
            <Typography variant="body2" color="text.secondary">
              读取中…
            </Typography>
          )}
          {state.kind === "unavailable" && (
            <Typography variant="body2" sx={{ color: tokens.bad }}>
              未知 — 本地服务不可用, 请重启应用
            </Typography>
          )}
          {state.kind !== "loading" && state.kind !== "unavailable" && (
            <Typography variant="body2" sx={{ fontFamily: tokens.mono, color: tokens.ink }}>
              v{state.server}
            </Typography>
          )}
        </Row>

        {state.kind === "mismatch-packaged" && (
          // role=alert + icon + text: never signal by colour alone.
          <Box
            role="alert"
            sx={{
              display: "flex",
              gap: 1,
              alignItems: "flex-start",
              border: `1px solid ${tokens.bad}`,
              borderRadius: 1,
              p: 1.5,
            }}
          >
            <ReportProblemOutlinedIcon fontSize="small" sx={{ color: tokens.bad, mt: "2px" }} />
            <Typography variant="body2" sx={{ color: tokens.ink }}>
              安装包异常: 应用版本 v{__APP_VERSION__} 与服务版本 v{state.server} 不一致,
              很可能装到了 陈旧的服务组件。请重新安装最新安装包。
            </Typography>
          </Box>
        )}

        {state.kind === "mismatch-dev" && (
          <Typography variant="body2" color="text.secondary">
            开发模式: 界面与本地服务由不同来源启动, 版本不同属正常, 不代表安装包有问题。
          </Typography>
        )}

        <Row label="服务使用的数据库路径">
          <Typography
            variant="body2"
            sx={{
              fontFamily: tokens.mono,
              color: settings.data?.db_path ? tokens.ink : tokens.bad,
              // Long UNC paths must wrap rather than overflow the pane.
              overflowWrap: "anywhere",
              userSelect: "text",
            }}
          >
            {settings.isPending
              ? "读取中…"
              : (settings.data?.db_path ?? "未知 — 无法读取设置, 请重启应用")}
          </Typography>
        </Row>
      </Box>

      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 3 }}>
        版本比对只能发现版本号不同的陈旧组件; 同版本号的旧构建由 CI 的冻结二进制冒烟检查覆盖。
      </Typography>
    </Box>
  );
}
