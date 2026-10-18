# CTO PR Review — PR #37: 单向影片归档传输

- Author: Codex（实现者）
- Target: `feat/36-archive-transfer` → `main`
- Reviewed code: `4639355472f3d5551045310289d37f5c8975bfd0`
- CI: [39104073308](https://github.com/AlvinShenSSW/Filmpaw/actions/runs/39104073308) — SUCCESS，含 Windows NSIS 产物。
- Reviewed by: Codex，实现者 CTO 自检；本结论不冒充独立外审。

## Summary

实现已审批的明确选演员、双栏目录、多选整影片单向移动、紧凑头部与最大化自适应。
新增目录瞬错重试覆盖 1/2/4 秒退避、暂停和取消、失败保留列表；写操作无自动重放。
持久日志贯穿预检、暂存复制、双向摘要核对、不覆盖提交、精确源清理与重启核对。

## Decision

**APPROVE WITH NON-BLOCKING COMMENTS**。未发现待修复的结构性阻塞；远程 NAS 环境覆盖与数据库降级边界见下。

## Blockers

No blockers found.

## Non-Blocking Suggestions / boundaries

1. 物理 NAS 的真实断网尚未实测。已有临时目录故障注入、2049 MiB 实际 UNC/SMB 回环和独立 SHA-256 核对；部署前可用可丢弃影片补测目标 NAS。OS 的 SMB 调用不能由浏览器取消，读锁防止叠加，必要时仍需等待系统返回。
2. schema 3 为原子增量迁移，保留原片库记录，但没有自动降级。旧版拒绝新版数据库；回退需要升级前数据库备份和对应旧程序，且数据库回退不会撤销已经移动的影片。
3. `app/ui/README.md` 的 Biome 命令应为 `pnpm run ci`；随最终文档归档一起修正，不涉及实现。

## Production readiness

- [x] CI 全部通过；OpenAPI 漂移、UI 类型/lint/测试、服务、Rust、冻结 sidecar 与 NSIS 均执行。
- [x] 新行为与冲突、重复提交、停止、崩溃恢复、journal 失败、路径替换、junction、离线标记边界有测试。
- [x] 会话随机凭据、Origin 限制、上下文绑定、batch revision 和单数据库会话锁。
- [x] 首次冲突整批零移动；运行时冲突停止剩余项，已完成保留；无覆盖/反向传输。
- [x] 安全退出以真实 worker 句柄与原子 admission 为依据；无法确认时保留窗口。
- [x] 0.5.0 协议/版本变化已同步；恢复只核对，不自动续传或再次删除。
- [x] schema 迁移原子；故障回滚测试通过。
- [!] schema 降级需升级前备份，无自动 downgrade；见上。
- [x] 持久任务、阶段进度和明确失败/待核对/清理未完成状态可观察。
- [x] 目录一级读取，复制在串行工作器，状态查询不随文件大小阻塞；2 GB 以上 SMB 夹具通过。
- [x] 未使用真实片库/影片；所有测试服务器显式临时 FILMPAW_DB，无密钥提交。
- [x] Issue #36 与用户追加目录重试需求已覆盖。

## Targeted tests run

最终 CTO 阶段没有重跑整套测试：CI 已绿，没有新的需要实测验证的疑点。
先前实现自查已用 RED→GREEN 验证离线标记暂停目录却误启用传输、停止 mutation 被暂停两处问题。
本地最终累计：服务 166 passed / 1 skipped（普通 symlink 权限），UI 113 passed，Rust 21 passed。
完整浏览器、SMB 与冻结产物证据见本目录 README。

## Files reviewed

- `transfer.py`、`routes_archive.py`、`directory_reads.py`：权限上下文、文件身份、阶段持久化、冲突/恢复及读锁。
- `app.py`、`session.py`、`db.py`、`routes_performers.py`：会话鉴权、数据库锁与迁移、目录瞬错分类。
- `ArchivePage.tsx`、`ArchiveTransferView.tsx`、`directoryReads.ts`：明确选择、双栏/尺寸、读取失败与有界重试。
- `TransferProvider.tsx`、`ExitCoordinator.tsx`、`archiveApi.ts`：任务恢复、显式幂等重试、停止/退出及离线标记。
- `main.rs`、`lifecycle.rs`、`worker_process.rs`、`opener.rs`：凭据握手、真实进程身份、退出与原双开授权。
- 对应用例、OpenAPI/生成客户端、CI 与版本配置、设计文档和验证证据。

Next: 按操作者明确指定，Kimi K3 是唯一外部门。`leave-open`，不自动合并或部署。

## Kimi K3 独立外审

操作者明确指定 Kimi K3 为唯一外部门，实际模型为 `kimi-code/k3`，CLI 别名为 `k3`。Codex 负责实现和 CTO 自检，不充当自己的独立外审。

首轮审查 `origin/main...45dd517`，CLI 达到 20 分钟执行上限而未输出结论。门脚本误将 stderr 中的认证相关文字标为 SKIPPED；核对会话记录后排除了登录和配额问题。本轮临时适配器改为优先报告执行错误，随后接续同一 K3 会话取得正式结论。这次超时不属于认证失败，也未据此降级外审。

> • - [P2] `app/server/filmpaw_server/scan.py:60` — A transient per-entry SMB failure is silently converted into a successful, incomplete directory snapshot. On Windows, `_is_hidden()` catches every `OSError` from `DirEntry.stat()` and returns `True`; consequently `list_subdirs()` omits that actor and returns 200 instead of surfacing a retryable failure. The new 1/2/4-second retry logic never runs, the UI treats the partial list as fresh, and a scan can incorrectly mark the omitted performer missing. Let the error propagate to `list_subdirs()`’s `SourceUnreachable` handling, or rethrow errors classified by `directory_error_retryable()`, so the route returns 503 and retries; add a per-entry transient-stat failure test.
>
>   REQUEST CHANGES

确认并修复 R1：移除逐项 stat 失败时的吞错分支，让原始异常进入 `SourceUnreachable` 处理。瞬时目录错误返回 503，触发 UI 有界重试；权限或路径失效等永久错误仍返回 422。新增测试覆盖不返回不完整成功列表、读取恢复、扫描记录和时间戳不变，4 个新增用例均经历 RED → GREEN，相关 42 项测试通过。

实现者另以真实 NTFS 临时夹具独立复现附加流内容丢失，并补充按确切句柄核验的保源拒绝策略。11 项 NTFS 回归覆盖各阶段新增附加流及未选中资料对照；这项修复并非 Kimi 首轮提出的问题。完整服务回归为 166 passed / 1 skipped，32 MiB 实际 SMB 移动与独立摘要核对通过。修复提交 `4639355` 的 CI [39104073308](https://github.com/AlvinShenSSW/Filmpaw/actions/runs/39104073308) 全绿后，完成 CTO 自检，并请 K3 复审 `45dd517..4639355`。

最终 Kimi 正式结论原文：

> • No high-confidence actionable findings. The R1 error propagation and fail-closed named-stream handling are correctly scoped and preserve source data at the checked boundaries. All 12 focused NTFS/named-stream and scan-atomicity regression tests passed using disposable fixtures.
>
>   APPROVE

**Gate status: CLEAN**。Confirmed & fixed：首轮 R1 已确认并修复，附加流保源策略通过复审。Disagreed：无。K3 没有提出新的高置信度可操作问题，并实际执行了 12 项 NTFS／附加流及扫描原子性针对回归，全部通过。

本轮已取得有效独立外审结论，没有以首次超时的 SKIPPED 标记替代审查。远程 NAS 故障覆盖与 schema 降级边界仍按前文保留；合并策略仍为 `leave-open`，不自动合并或部署。

## 交付版本说明

用户随后要求将交付版本从 0.5.0 调整为 0.5.1，并提供桌面 `.exe` 产物。此次版本元数据调整不修改上述已审实现；0.5.0 的历史验证记录保留原版本。最新构建和检查状态以 [PR #37 checks](https://github.com/AlvinShenSSW/Filmpaw/pull/37/checks) 为准。

首个 0.5.1 CI 捕获 Rust HTTP 测试夹具关闭连接的间歇性 Windows 10054；本地 40 轮完整套件复现 2 轮失败。后续仅修正 `cfg(test)` 夹具的有界正常关闭顺序，并增加失败诊断，不改变产品逻辑；完成修复后 40 轮完整套件验证，重新进入 CI。该增量的最终 CI 与 K3 补充核对结论记录在 PR #37。
