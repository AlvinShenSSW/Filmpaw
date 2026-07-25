import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";

export { SettingsPage } from "./SettingsPage";

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
