# Issue #36 验证记录

本目录使用虚构演员/影片和新建临时目录，未操作真实片库。

- `browser-smoke.json`：实际构建的 React UI + 实际临时 FastAPI 服务，明确选演员、初始不选片、
  同名冲突整批零移动、一次移动两部影片、逐文件 SHA-256/空目录/演员根图片核对，
  页面切换与刷新恢复任务。浏览器无运行错误。
  最新复跑还注入首次 HTTP 503、第二次成功响应中的 NAS 读取错误，1.031 / 2.032 秒后
  自动重试并恢复真实目录；用户勾选前传输 POST 为 0。
- `implemented-*.png`：实际应用截图；960×640、1200×800、1920×1080、2560×1440
  均无页面溢出，目录独立滚动、按钮保持可见。1080p→2K 的列表高度增加 360px。
- `smb-smoke.json`（及同目录脚本）：本机已有 SMB 回环共享上的可丢弃夹具，
  用于验证 UNC/SMB 实际文件操作；不代表已验收远程 NAS 所有厂商、断网或容量故障。
- `smb-large-file-smoke.json`：2049 MiB 单影片经实际归档 API 完成 SMB 回环移动，
  源生成流与目标 UNC 重读的独立 SHA-256 一致，演员海报及附属文件、空目录均符合预期。
  86 次任务状态请求最大耗时 2.699 ms（进程内 ASGI 处理时延，不是网络或 WebView 时延）。
  可复跑：`uv run --project app/server --locked python docs/evidence/issue-36/smb-smoke.py --video-mib 2049 --output docs/evidence/issue-36/smb-large-file-smoke.json`。
- `smb-post-review.json`：新增 NTFS 附加数据流保源检查后，再跑 32 MiB 实际 UNC/SMB
  传输，摘要一致、源成功清理、演员资料及空目录保留，临时夹具清理完成。
- `frozen-sidecar-smoke.py` / `.json`：实际 PyInstaller 产物的 PORT/PID 握手、0.5.0 版本、
  临时数据库、会话鉴权、错误 Origin 拒绝、无配置凭据时关闭写入、启动器退出后真实 worker
  继续存活，以及安全退出和精确进程回收。`frozen-sidecar-timeout.json` 记录 1 秒超时故障注入：
  无新 worker 残留、无新临时目录、无假成功报告。
  首次开发该超时探针时遗留一个独立临时目录（测试解包文件、测试 DB/锁文件）；
  删除工具仅返回 `rejected: blocked by policy`，未提供具体原因，按策略保留且未绕过。

服务测试覆盖复制、校验、提交、源清理、持久化失败、冲突/停止/恢复、目录替换及 Windows
junction；本机普通 symlink 创建缺少权限，因此该独立用例 skip，junction 用例实际执行。
长路径支持取决于 Windows/共享设置；不支持时明确失败并保源，不宣称所有长路径可移动。

真实 NTFS 回归覆盖文件/目录/零字节附加流、预检后新增并恢复 mtime、持有源删除句柄时新增流、
最后删除目录前新增流，以及未选中演员资料的范围对照。含流时整批不开始，提交后检测到则
保留剩余源并显示清理未完成。最终枚举→删除不是抵御外部并发写入的原子事务；传输时应停止
改动所选影片。共享模式语义参见 [Microsoft File Streams](https://learn.microsoft.com/en-us/windows/win32/fileio/file-streams)，
枚举接口参见 [GetFileInformationByHandleEx](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex)。

本地检查阶段：pytest 166 passed / 1 skipped；UI 113 passed、TypeScript 与 Biome 通过；
Rust 21 passed。CI、最终外部门和后续修复以 PR 与 AFK ledger 的最终记录为准。
