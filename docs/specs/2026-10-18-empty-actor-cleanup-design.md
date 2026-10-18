# Issue #38 — 指定 batch 的空演员目录清理

日期：2026-10-18。实现与设计：Codex。范围依据 [Issue #38](https://github.com/AlvinShenSSW/Filmpaw/issues/38) 及操作者澄清。

## 范围与交付

归档页左侧增加“清理空演员目录”按钮，一次检查当前 batch 的一级演员目录，删除真正为空的目录，刷新完整左侧列表。batch 可以在本机、UNC 或 NAS 临时文件夹；授权来自明确选择的 batch，不能按设备类型禁止 NAS batch，也不能扩展到兄弟目录或右侧片库。batch 根与所有非空内容保留。

Codex 主导，Kimi K3 唯一外门（明确指定，非降级），`leave-open`。依赖尚未合并的 PR #37，分支 `feat/38-clean-empty-performers` 从 `5c26c22` 建立，PR base 为 `feat/36-archive-transfer`，不擅自合并前置 PR。交付补丁 0.5.2，沿用桌面 NSIS 交付方式，不安装或部署。

## 判断与文件系统边界

1. 左侧 `list_subdirs()` 过滤隐藏项，影片枚举忽略普通文件，都不能用于判空。工作器在绑定的 batch 下完整物化一级目录快照，根枚举中途失败时零删除。普通根文件不作为候选；隐藏／系统演员目录跳过并报告（与左侧可见演员范围一致）。链接、junction、重解析点跳过，不沿链接访问目标。
2. 演员目录的原始枚举必须零条目。影片、folder.jpg、NFO、零字节文件、隐藏／系统文件和任何子目录都属于非空；不递归清理，不更改属性／ACL，不使用 rmtree。
3. batch 上下文由服务器签发，绑定保存的 `last_local_dir`、revision、规范路径和实际根目录身份。写请求只接受 context_id / request_id，不接受删除路径、NAS 目标或演员名称列表。提交时再次核对设置 revision，包括 A→B→A；开始后冻结原 batch，切换设置不能改写任务目的地。
4. Windows 保持 batch 和祖先保护句柄；新清理的候选目录必须从实际 DELETE 句柄获取身份与规范最终路径，验证父目录属于绑定根，再通过同一句柄非递归删除。不能把另一次 path.stat 当作句柄身份证明。规范路径／身份查询不可用时保留并报告。UNC／映射盘不构成拒绝理由；磁盘／共享根不是合法 batch。
5. 删除句柄上最后检查目录自身 ADS；存在任何命名流（含零字节）或无法核对时保留。操作系统非递归删除保护并发新增普通条目。Windows 默认流共享模式不能原子禁止并发新增 ADS；最后枚举与删除间隙不宣称具有这种保证，清理时外部程序应停止改动候选目录。

## 服务与生命周期

新增独立 `CleanupService`，不伪装为影片传输，也不引入通用任务框架或数据库迁移。请求快速返回任务，单工作器执行可能缓慢的目录 I/O；状态／结果保存在本次 sidecar 会话，切页／WebView 刷新可查询恢复。进程重启后不恢复或续删，旧上下文失效，重新读取目录再由用户显式开始新一轮。界面不得把丢失的历史结果宣称为成功。

清理与传输共用 `TransferService._admission` 原子准入。清理只在短锁内登记操作 ID；文件系统 I/O 不持 admission 或 DB 锁。传输、清理、退出均检查清理占用；任务终结并完成 I/O 才释放。重复 request_id 返回同一任务，不能再次枚举／删除新出现的同名空目录；上下文只消费一次。缓存有界，失效上下文拒绝，不自动重建权限。服务关闭等待清理工作器，随后再关闭 DB。

退出协议仅作小扩展：`ExitOut` / Rust `ExitStatus` 增加可选 `operation: "cleanup" | "transfer" | null`。原 transfer 的省略/null 继续视为传输；cleanup busy 返回 `safe:false, task_id:<cleanup-id>, operation:"cleanup"`。安全状态必须 task_id=null、operation=null。壳将 operation 透传到退出提示。清理时提供继续运行／完成后退出，不把清理 ID 传给 stopTransfer；最终仍经原子 prepare-exit 才关窗。未知／失联保留窗口。

### HTTP 合同

均位于 `/api/archive`，继承凭据与 Origin 检查。

- `POST /cleanup-contexts`，只读创建上下文：`{batch_path:string}` → 201 `{id,batch_path,batch_revision}`。读取瞬错返回 503，永久绑定／权限错误返回 4xx；可按 1/2/4 秒重试只读调用。
- `POST /cleanups`：`{context_id,request_id}` → 202 `CleanupTask`；未知字段拒绝。无自动重放。
- `GET /cleanups/latest` → `CleanupTask | null`。
- `GET /cleanups/{id}` → `CleanupTask`，不存在 404。新 GET 只查状态，不执行文件操作。

`CleanupTask`：`id,context_id,request_id,batch_path,batch_revision,status,current_name,checked_count,total_count,deleted_count,kept_count,skipped_count,failed_count,message,items`。
`status`：`queued | running | completed | failed`；`items`：`{name,status,message}`，状态 `deleted | not_empty | skipped | failed`。
单项失败保留并继续，最终 completed 仍可有 failed_count；根／全局错误为 failed 且保留已确认结果。已不存在归入 skipped，不虚报为本次 deleted。

## UI 与刷新

- 按钮位于左侧路径／刷新附近，文字入口避免挤压地址。按钮显式说明只删当前 batch 的空演员目录；点击即启动，不额外增加用户未要求的审批步骤。
- shell 级 CleanupProvider 保存提交中、运行中、结果未知和终态；POST `networkMode:always`、`retry:false`，网络恢复不自动提交。未知结果仅查询或由用户显式使用原 context/request ID 确认同一任务，不生成新 ID 自动执行。
- 清理与传输的提交中／忙／未知状态双向禁用写入口；后端仍负责真正互斥。全局状态区优先展示活动操作，不叠加两个高任务栏占用目录区域。
- 任务展示检查数量、当前目录，结束后显示删除／非空保留／跳过／失败数量和可查看明细。无候选时明确没有可清理目录。
- 对同一 batch 代次，已确认 deleted 的名称立即从旧左侧缓存移除；当前选中被删时清 selected 和 target，保留独立搜索词。然后重新读取整个左侧列表及失效的影片上下文。刷新失败与清理结果分开提示，保留可信结果，不伪造空列表。
- 清理结果应用时同时核对 batch 路径、revision／界面切换代次；A→B 或 A→B→A 的旧响应不得清掉新选择。正在清理可切页；切换 batch 不重定向已接受任务。

## 文件分工与验证

- backend：`cleanup.py`、`transfer.py` 准入接入、针对清理的服务测试；严格句柄验证局限在新功能，避免无关改写传输引擎。
- integration：清理路由／模型、app 生命周期、OpenAPI/生成客户端、版本、设计与 ledger。
- UI：CleanupProvider/API/结果组件、ArchivePage/ArchiveTransferView、App 任务条、ExitCoordinator 及对应测试。
- shell：Rust ExitStatus 与提示事件类型及正确/非法组合回归。

先 RED 再 GREEN：严格空矩阵、根枚举中断零删、单项权限部分失败、链接／替换／新增内容／ADS、真实删除句柄身份、A→B→A、重复请求、任务与退出竞态、未知响应无重放、删除当前选择但刷新失败、切换 batch 旧结果隔离。临时 UNC 与映射路径（条件允许时）检验范围只限目标 batch。

全套 pytest、UI 类型/Biome/测试与生产构建、Rust 测试、OpenAPI 漂移和 Windows NSIS CI。所有服务器/E2E 显式临时 FILMPAW_DB，只使用新建可丢弃目录。实际 NAS 不可模拟的厂商行为或真实断网不宣称已覆盖。

## 设计质询记录

第一轮：三方实现自查发现列表过滤不能判空、仅前端互斥不够、清理 ID 不能伪装成 transfer、刷新失败时已删选择残留、路径 stat 不等同删除句柄身份、ADS 并发保证不可夸大。以上均纳入设计；这些同模型质询不是 Kimi 外审。

第二轮：三方确认无新增阻塞。采用小型独立会话内工作器，不新增持久清理历史或数据库迁移；操作标识明确区分 cleanup 与 transfer。版本 0.5.2，先观察接口/界面/壳 RED 再实现。Windows 原生句柄检查不支持的文件系统保留目录并报错；不退化为字符串前缀或递归删除。
