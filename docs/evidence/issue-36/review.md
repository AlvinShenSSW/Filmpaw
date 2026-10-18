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
本地累计：服务 151 passed / 1 skipped（普通 symlink 权限），UI 113 passed，Rust 21 passed。
完整浏览器、SMB 与冻结产物证据见本目录 README。

## Files reviewed

- `transfer.py`、`routes_archive.py`、`directory_reads.py`：权限上下文、文件身份、阶段持久化、冲突/恢复及读锁。
- `app.py`、`session.py`、`db.py`、`routes_performers.py`：会话鉴权、数据库锁与迁移、目录瞬错分类。
- `ArchivePage.tsx`、`ArchiveTransferView.tsx`、`directoryReads.ts`：明确选择、双栏/尺寸、读取失败与有界重试。
- `TransferProvider.tsx`、`ExitCoordinator.tsx`、`archiveApi.ts`：任务恢复、显式幂等重试、停止/退出及离线标记。
- `main.rs`、`lifecycle.rs`、`worker_process.rs`、`opener.rs`：凭据握手、真实进程身份、退出与原双开授权。
- 对应用例、OpenAPI/生成客户端、CI 与版本配置、设计文档和验证证据。

Next: 按操作者明确指定，Kimi K3 是唯一外部门。`leave-open`，不自动合并或部署。

## Kimi K3 ????

???`kimi-code/k3`????????? `k3`?high?????????????? Codex ??????????????

???? `origin/main...45dd517`??? CLI ? 20 ??????????????stderr ??????????????????????????? K3 ??????????????????

> • - [P2] `app/server/filmpaw_server/scan.py:60` — A transient per-entry SMB failure is silently converted into a successful, incomplete directory snapshot. On Windows, `_is_hidden()` catches every `OSError` from `DirEntry.stat()` and returns `True`; consequently `list_subdirs()` omits that actor and returns 200 instead of surfacing a retryable failure. The new 1/2/4-second retry logic never runs, the UI treats the partial list as fresh, and a scan can incorrectly mark the omitted performer missing. Let the error propagate to `list_subdirs()`’s `SourceUnreachable` handling, or rethrow errors classified by `directory_error_retryable()`, so the route returns 503 and retries; add a per-entry transient-stat failure test.
>
>   REQUEST CHANGES

???????? stat ????????????????????????? 503 ? UI ????????????/??/???????????????????? RED ? GREEN?

??????? NTFS ??????????????????? 11 ???????????????????? Kimi ?????????????? 166 passed / 1 skipped?SMB ?????CI 39104073308 ???CTO ??????? K3 ?? `45dd517..4639355`?

?? Kimi ???

> • No high-confidence actionable findings. The R1 error propagation and fail-closed named-stream handling are correctly scoped and preserve source data at the checked boundaries. All 12 focused NTFS/named-stream and scan-atomicity regression tests passed using disposable fixtures.
>
>   APPROVE

**Gate status: CLEAN**?Confirmed & fixed??????????Disagreed??????????????K3 ?????????? 12 ??? NTFS/??????????????????????

???? CLI ??? stdin??????????????????????????????????? SKIPPED ??????????????? NAS ???schema ?????????????
