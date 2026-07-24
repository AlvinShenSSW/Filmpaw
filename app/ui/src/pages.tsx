import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { useQuery } from "@tanstack/react-query";
import { fetchHealth } from "./api";

function Placeholder({ title, note }: { title: string; note: string }) {
  return (
    <Box sx={{ p: 3 }}>
      <Typography variant="h6" sx={{ fontWeight: 600 }}>
        {title}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
        {note}
      </Typography>
    </Box>
  );
}

export function PerformersPage() {
  return <Placeholder title="表演者库" note="issue #5 实现 — 列表/搜索/别名/失效清理" />;
}

export function ArchivePage() {
  return <Placeholder title="归档对比" note="issue #6 实现 — 本地目录 × NAS 匹配双开" />;
}

export function SettingsPage() {
  const health = useQuery({ queryKey: ["health"], queryFn: fetchHealth, retry: 1 });
  const state = health.isLoading
    ? "连接中…"
    : health.isError
      ? "未连接"
      : `已连接 · v${health.data?.version}`;
  return <Placeholder title="设置" note={`issue #4 实现 — 扫描源管理 · server: ${state}`} />;
}
