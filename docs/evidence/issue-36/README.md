# Issue #36 验证记录

本目录使用虚构演员/影片和新建临时目录，未操作真实片库。

- `browser-smoke.json`：实际构建的 React UI + 实际临时 FastAPI 服务，明确选演员、初始不选片、
  同名冲突整批零移动、一次移动两部影片、逐文件 SHA-256/空目录/演员根图片核对，
  页面切换与刷新恢复任务。浏览器无运行错误。
- `implemented-*.png`：实际应用截图；960×640、1200×800、1920×1080、2560×1440
  均无页面溢出，目录独立滚动、按钮保持可见。1080p→2K 的列表高度增加 360px。
- `smb-smoke.json`（及同目录脚本）：本机已有 SMB 回环共享上的可丢弃夹具，
  用于验证 UNC/SMB 实际文件操作；不代表已验收远程 NAS 所有厂商、断网或容量故障。

服务测试覆盖复制、校验、提交、源清理、持久化失败、冲突/停止/恢复、目录替换及 Windows
junction；本机普通 symlink 创建缺少权限，因此该独立用例 skip，junction 用例实际执行。
长路径支持取决于 Windows/共享设置；不支持时明确失败并保源，不宣称所有长路径可移动。

本地检查阶段：pytest 125 passed / 1 skipped；UI 100 passed、TypeScript 与 Biome 通过；
Rust 21 passed。CI、最终外部门和后续修复以 PR 与 AFK ledger 的最终记录为准。
