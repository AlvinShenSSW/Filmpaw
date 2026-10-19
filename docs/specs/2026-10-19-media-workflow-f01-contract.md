# F01：多 NAS 演员位置索引与只读候选契约

日期：2026-10-19。状态：实施规格初稿，待独立设计审阅；不表示已实现、通过原生验证或部署。

所属 [Filmpaw #46](https://github.com/AlvinShenSSW/Filmpaw/issues/46)，父项 [Taskpaw #271](https://github.com/AlvinShenSSW/Taskpaw/issues/271)。源码基线为 `acbb1b5de9bc5af9d8434fdb3c6f0246edf4bd13`。跨仓规范为 TaskPaw 仓库的 `docs/specs/2026-10-19-media-workflow-{epic,architecture,development-plan}.md`；其中架构 §8、§9.3、AC09/14 与开发计划 F01 边界是本稿依据。这些设计的位置不等于其已经发布到默认分支。

前置 [T01 #272](https://github.com/AlvinShenSSW/Taskpaw/issues/272) 当前仍为 **OUTSTANDING**；本稿仅冻结候选与目标身份的版本化 wire 边界，允许隔离的 schema/fixture 开发，不声称已与可交付的 T01 接通。当前操作者指定 Codex 实施、Claude Opus 唯一外审及 AFK 1.3.7；本仓旧设计的默认多门顺序不替代这次指令。

## 1. 需求、可见行为与硬边界

Filmpaw 在原有单数据库所有者内维护多个 NAS 根上的演员目录位置，向 Windows 主控返回可解释、带来源证据和覆盖状态的候选。名称、别名、外部编号、演员位置、根的原生对象身份分别表达。名字相同不证明是同一个人，演员目录相同不证明其中某影片版本相同。

正常工作流仍只有投片和一次最终确认。用户不需要为 F01 逐片配置演员 ID、提交 ready 文件或勾选技术声明。F01 不作目标选择，不生成批准，不创建演员目录，不移动、重命名、覆盖或删除 NAS 文件。多演员保留全部元数据及逐演员匹配关系，不自动复制到多个演员目录。没有可用候选或存在歧义时，T06 使用既有确认/异常交互处理。

Windows 为唯一主控；Filmpaw backend 是自身 SQLite 的唯一 writer，TaskPaw 只能调用受限本地 API。远端计算 Agent 和 Mac 桥不取得 Filmpaw 凭据、NAS 路径或写权限。真实部署主机、共享路径及演员信息不得进入本规格、fixture、通用错误或日志。

本次也修正实际的扫描入口，避免新候选 API 后面仍存在持数据库锁的无界 NAS 扫描。此承诺限定于源登记探测、源扫描及 F01 索引读取；既有归档、清理、Explorer 打开和任意本地目录浏览的全部进程隔离不是 F01 的已交付声明，归档/清理网络 I/O 由 F02 承接。

## 2. 已核实的代码依据

| 源码 | 现状与本次最小影响 |
| --- | --- |
| [db.py](../../app/server/filmpaw_server/db.py)，`SCHEMA_VERSION`、`DDL`、`_migrate` | 基线版本 4；sources/performers 以路径及一处演员目录为记录，aliases 属于 name_norm 分组。追加 v5 工作流索引及 v6 根级重试状态，不更换库、不改变既有演员 ID。 |
| [session.py](../../app/server/filmpaw_server/session.py)，`library_session`；[app.py](../../app/server/filmpaw_server/app.py)，`lifespan` | OS 文件锁贯穿 sidecar 生命周期，另有共享 SQLite 的 RLock。复用它们；扫描子进程不打开数据库。 |
| [routes_performers.py](../../app/server/filmpaw_server/routes_performers.py)，`_MATCH_SQL`、`list_performers` | 既有查询使用归一化名字/组级别名、双向子串，并返回 UNC。保留旧本地 UI 契约；新 workflow 投影禁止直接复用其含路径响应。 |
| [routes_sources.py](../../app/server/filmpaw_server/routes_sources.py)，`list_sources`、`add_source`、`scan_one`、`scan_all` | 列表/添加直接调用 isdir；扫描在 db_lock 内执行目录和图片 I/O。这些入口必须切到下述受管探测/快照流程。 |
| [scan.py](../../app/server/filmpaw_server/scan.py)，`list_subdirs`、`_refresh_thumb`、`scan_source` | 一级目录枚举、缩略图读取、数据库更新混在同一调用。拆为 collect/apply，完整枚举失败不 mark missing；缩略图保持既有成功/失败保留规则。 |
| [windows_identity.py](../../app/server/filmpaw_server/windows_identity.py)，`directory_identity` | 从同一 HANDLE 取得 native128；仅指定“不支持”错误且确认 NTFS 时允许独立 legacy64 命名空间。不得扩大回退错误集合或截断 ID。 |
| [path_resources.py](../../app/server/filmpaw_server/path_resources.py)，`capture`、`overlaps` | 已有只读目录/祖先/alias 证据；不能把它视为实际写入/删除授权。F01 将其原生调用放入隔离进程。 |
| [directory_reads.py](../../app/server/filmpaw_server/directory_reads.py) | 只提供错误分类，尚无进程生命周期保证；沿用 winerror 优先规则，补充结构化原因。 |
| [main.py](../../app/server/filmpaw_server/main.py)、[build_sidecar.py](../../app/server/build_sidecar.py) | 服务绑定 loopback；产物为 PyInstaller onefile，端口/PID 输出是启动提示而非完整就绪证明。新增固定 scan-worker 分支，不另建桌面程序。 |
| [app.py](../../app/server/filmpaw_server/app.py)，`session_guard` | 当前普通 GET 不统一验 token。新 workflow 的所有读端点必须明确认证，不能继承这个放行条件。 |
| [routes_archive.py](../../app/server/filmpaw_server/routes_archive.py)，`prepare_exit`；[lifecycle.rs](../../app/src-tauri/src/lifecycle.rs)、[main.rs](../../app/src-tauri/src/main.rs)、[worker_process.rs](../../app/src-tauri/src/worker_process.rs) | 正常桌面关闭先检查 archive prepare-exit，safe 后销毁窗口并原生终止 sidecar，绕过 Python lifespan；必须在既有检查中纳入 scanner，不能仅依赖 lifespan.close。 |

未发现独立的 constitution 文件；本仓 AGENTS、上述已批准产品设计和本次操作者范围共同约束实施。源读取和本文编写未启动服务、测试、扫描或访问生产数据库。

## 3. 身份与持久记录

所有新 DTO 使用 Pydantic，`schema_version=1`、strict、extra forbid；UUID 为规范字符串，revision 为严格正整数（拒绝 bool），时间为带时区 UTC，摘要为小写 SHA-256。相对路径使用 `/`，拒绝绝对路径、UNC、盘符、ADS、空段、`.`/`..`、保留设备名、尾随点/空格及大小写冲突。公共 DTO 不接受任意脚本、URL 或远端路径。

### 3.1 标识含义

- `library_id`：该 Filmpaw 库的持久 UUID，迁移/重启保留；复制库不能成为第二活跃 writer。
- `host_id` / `export_id` / `root_id`：独立 UUID，显示标签不是身份；真实 UNC/别名端点只在本地受保护映射中。host 分组必须覆盖已确认的同一 NAS 地址别名，不能用新别名绕过并发和熔断。
- `root_revision`：映射、允许范围或观察到的根身份变更时递增。原生身份观察保存 Windows namespace、卷/导出标识、目录 ID、同句柄取得的兼容 alias、证据 ID/时间和状态。路径字符串不充当原生身份。
- `location_id`：一处演员目录位置的持久 UUID。迁移可保留既有 performer UUID 作为初始 location_id，并另存 legacy_performer_id 关联；不能把路径替换后的新目录默认为原位置。相同 native 对象仅大小写变化/已证明的改名可保持 ID；无法证明则保留旧位置历史、建立待验证的新观察。
- `actor_ref`：调用方当前影片元数据内的稳定演员引用 UUID；不是凭名字生成的全球人物 ID。候选用 `matched_actor_refs` 关联全部输入演员。位置和人物不得混用；外部 `namespace/value` 只作为有来源的身份证据。
- `scan_id` / `attempt_id` / `owner_generation`：一次只读扫描及其进程归属。重试新 attempt，旧事实不覆盖；不能把 PID 本身当原生进程身份。

### 3.2 SQLite v5 索引及 v6 根级重试状态

仍由现有 connection/db_lock/session 管理；所有跨表变更在一次事务内完成。不可变观察详情可存规范 JSON，身份、版本、唯一性、状态及所有权条件必须有可查询列。

| 表 | 关键列与约束 |
| --- | --- |
| `workflow_index_meta` | singleton、library_id、index_revision、schema_version。revision 单调增加，不用时间排序替代。 |
| `workflow_roots` | root_id PK、legacy_source_id nullable、host_id/export_id、private mapping、mapping/root revision、active/tombstoned、当前 identity observation、last_success/last_attempt、health；v6 增加 nullable configuration_reason，独立于健康观察及 host 退避。legacy link 不级联删除工作流记录。 |
| `workflow_locations` | location_id PK、root_id RESTRICT FK、legacy_performer_id nullable、relative_actor_path、name/norm、identity、first/last_seen、present/not_observed/unknown、revision。保留历史位置，不以新扫描覆盖不同对象。 |
| `workflow_location_evidence` | evidence_id PK、location_id、kind、normalized value、provenance、observation revision/time/status、payload digest。涵盖 name、group_alias、explicit_external_id；名字匹配不是 verified external identity。 |
| `workflow_film_observations` | root/location、相对影片目录、目录身份、可用番号/标题/版本提示及来源、观察 revision/status。只读小型元数据，不遍历 hash 全库视频。 |
| `workflow_scan_operations` | scan_id PK、attempt_id unique、root/mapping revision、host_id、generation、state、起止时间、private process birth/handle-recovery references、result digest、error class；v6 增加 manual_intent，默认 false，绑定单次预约。持久保留 unknown。 |
| `workflow_host_state` | host_id PK、circuit state、失败次数、next_probe_at、owner generation；条件唯一索引保证每 host 最多一个未核销 scan owner。 |

`workflow_scan_operations` 上的未核销 owner 唯一约束包含 starting、running、stopping、unknown；不能仅在状态为 running 时唯一。配置上限之外的重复请求返回已有 operation 或 busy，不排无限队列。

现有 source DELETE 的本地 UI 意义仍是删除该来源索引及 legacy performers（既有 CASCADE）。删除前同事务将相应 workflow root/位置 tombstone、断开 legacy FK、递增 index revision；候选和历史身份/观察/scan owner 不被级联清除。正在扫描时先使其 revision 失效，迟到结果不得重新插回删除的 source。旧整数 source ID 再用也必须产生新的 root_id，不复活旧 tombstone。purge-missing 同样只处理 legacy 行、保留 workflow 历史关联。

alias 增删仍遵守原 name_norm 组级行为；同一事务递增 workflow index revision。新候选将它标为 `group_alias`，不会把该组所有同名目录偷偷合并成一个人。F01 不自动修改别名。

### 3.3 迁移与当前数据库归属

v4→v5→v6 在取得 library_session 后、API 可用前执行，并兼容已有候选 v5 库升级。先用 SQLite backup API 取得包含已提交内容的一致备份，每个版本步骤在同一事务内执行 DDL/数据迁移/版本写入；DDL/commit 任一步失败回滚该步骤，原 ID、thumb、settings、archive_tasks 和 operation_events 不丢失。v5→v6 只依据已有 root 错误事实迁移根级暂停，不把 shared host flag 扩散给健康 roots；旧操作默认没有人工重试意图，受影响根的旧 queued 请求取消，原 owner 和传输退避仍保留。备份校验和迁移失败不得自动创建“新空库”代替原库。新库得到同一 v6 schema，未知未来版本拒绝。

迁移不访问 NAS，不给旧行补造 native identity 或外部演员 ID；这些行初始为 `unverified`。T05 负责已有库选择/接管及组件安装；F01 不静默接管另一桌面实例，不打开 TaskPaw 的 workflow.db，不让 child 写 library.db。session 冲突明确失败。开发和所有 E2E/打包 smoke 显式使用临时 `FILMPAW_DB`，绝不使用默认生产路径。

## 4. 固定扫描流程与有界原生生命周期

### 4.1 进程只收集，父进程才提交

拆分 [scan.py](../../app/server/filmpaw_server/scan.py)：

```python
collect_snapshot(request: ScanRequest) -> ScanSnapshot        # child only; no DB
apply_snapshot(conn, request: ScanRequest, snapshot: ScanSnapshot) -> ScanResult
ScanSupervisor.request(root_id, *, kind, request_id) -> ScanTicket
ScanSupervisor.wait(ticket, *, deadline) -> ScanOutcome
ScanSupervisor.reconcile(host_id) -> OwnerOutcome
ScanSupervisor.close() -> ShutdownOutcome
```

`kind` 仅 `probe_root|scan_index`；绝不接收命令行模板或自由文件操作。request 由父进程从允许的 source/root 映射构建，包含 scan/attempt ID、root/mapping revision、generation、private root path、固定边界及协议版本。HTTP query 不能直接构造 private ScanRequest。

父进程持久登记 starting → 创建受管 worker → 核对 hello → 记录真实 PID/birth/Job 身份 → go。worker 在 go 之前不能执行任何 NAS I/O。child 不继承 DB、listener、会话 token 或其他无关句柄；仅使用白名单 pipe/必要运行环境，不执行外部工具，也不接受任意来源的 HTTP 请求。

worker 执行根 identity 前后复核、演员一级目录枚举、必要的影片子目录/小型元数据读取及已有缩略图处理。枚举覆盖范围、根身份、每项读取结果与失败分开记录。stdout 采用有上限的结构化消息，首条 hello、结果块序号和最后 complete/hash 必须匹配 request；stderr 仅保留有界、脱敏错误分类，不回显 private path。EOF、被截断 JSON、缺 complete、越界、错误 generation 均不是完整快照。

只有完整响应、所有必需枚举成功、根前后身份一致、退出及 I/O owner 收束被证明后，父进程才进入短数据库事务。事务复核 current source/root/mapping/generation；随后同时更新 legacy 可见索引和 workflow observations、last_success 与 index revision。与 source 删除、重映射、另一 scan 完成发生 CAS 冲突时丢弃迟到快照，保留 scan 事实。事务内禁止 stat/scandir、图片解码、网络身份调用或等待子进程。

目录枚举不完整、拒绝访问或超时，不更新 last_success，不把未观察位置 mark missing。只有完整的同一根快照才可把未出现的位置标为 `not_observed`；不是删除，也不能推断它在所有 NAS 上不存在。缩略图是可选显示资源：明确不存在时可以清除缓存，读取/解码失败保留旧图及错误状态；不得借缩略图错误发布半个演员成员集合。

### 4.2 Windows runner 与打包入口

新增窄 `scan_process.py`，服务于固定 scanner，不建立通用命令执行框架。Windows 用创建时 Job 绑定（`PROC_THREAD_ATTRIBUTE_JOB_LIST`）、禁止 breakaway、kill-on-job-close 和显式 handle list。创建后再加入 Job 存在的窗口不作为本契约实现。微软文档说明 Job list 可在创建时指定，适用 Windows 10/Server 2016 及以上；不支持或 Job 配置失败则拒绝启动扫描。[UpdateProcThreadAttribute](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)、[Job limit flags](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information)。

保留创建得到的 root process HANDLE；hello 中实际 worker PID 必须通过原生 birth identity 与 `IsProcessInJob` 核验并取得独立 HANDLE，不能只信任 stdout 的数字。运行中及终止前，对已观察 Job 成员保留身份绑定 HANDLE；获取失败/身份不明的已观察成员不能在下一次空快照中被遗忘。

终止发起与终止证明分开。到 deadline 后请求终止 Job，并在共同 cleanup deadline 内确认：root、实际 worker 及所有已知成员 HANDLE 已 signal；Job active count 为 0；stdin writer/stdout reader/stderr owner 已结束并 join；相关句柄成功关闭。线程等待超时、Popen 返回码或 Job active=0 中任何单一项都不够。无法证明则保留 owner、句柄/线程记录及私有缓冲，状态 `unknown_owner`，该 host 槽不释放，不启动替代扫描。正常可重试的 wait/close 故障允许对同 owner 核对；无法恢复的身份采样不确定性不能被自动清零。

不预设源码 venv、frozen onefile 恰好只有一层或两层进程。使用现有 `main.py` 的固定 `--workflow-nas-worker` 分支，在 socket、app/lifespan、DB 创建前分流；复用同一个 sidecar 产物，不新增 GUI 或专用安装器。PyInstaller 6.21 的源码区分同 EXE 启动的 SUBPROCESS；沿用受控父进程的运行时环境，不把 `PYINSTALLER_RESET_ENVIRONMENT=1` 当默认。实际打包的拓扑、依赖和解包目录生存期必须通过 frozen smoke 验证，不能仅凭源码分支宣称已可用。[PyInstaller 6.21 pyi_main.c](https://github.com/pyinstaller/pyinstaller/blob/v6.21.0/bootloader/src/pyi_main.c#L279)。

关闭 sidecar 先停新扫描，逐个收束 child/I/O，再关闭 DB/session；未收束时不能报告“干净停止”或启动第二 writer。崩溃恢复读取持久 owner，使用 birth identity/Job 可用证据核对旧进程，不按 PID 重用或“没有回复”推断退出。父进程死亡触发 Job 终止是遏制措施，不是新实例已取得核销证明。启动窗口身份丢失或原生查询拒绝时保留阻断状态，由后续 T08/T05 恢复流程处理，不自动丢掉 unknown。

R2 收窄恢复边界：新 owner 在创建 child 前取得真实 `RuntimeIdentity`（原 supervisor PID、creation FILETIME、Windows session ID、规范 MachineGuid 的域分离摘要），并与 launcher、hello worker、全部已知 Job member births 一起持久保存。取得 runtime 身份失败则在 spawn 前拒绝；不能写入猜测值。旧候选库的 owner JSON 缺 runtime 时仍能读取，但保持 unknown，不通过迁移补造历史身份。已有 sticky identity uncertainty、未解析成员、跨机器或跨 session、当前 runtime 仍等于原 runtime，以及原生查询拒绝均不满足跨进程恢复条件。

`scan_recovery.py` 仅对旧 owner 作有界本机只读检查。先核实原 runtime 已结束，再核实 launcher、worker 及所有已知 members。每项证据明确区分：同 birth 的已打开 HANDLE signal；完整 Toolhelp process snapshot 中无该严格有效 PID；已打开 PID 的 birth 不同，证明旧 birth 已消失。后两种绝不伪造 `signaled=true`。`OpenProcess` 错误 87 本身不证明消失：必须成功完成 `TH32CS_SNAPPROCESS` 枚举，以 `ERROR_NO_MORE_FILES` 结束、没有超出 65536 项上限、成功关闭 snapshot HANDLE，且列表无该 PID；其他拒绝保留 unknown。PID 重用仅记录旧 birth 已消失，禁止等待或终止新 birth。命名 Job 只能以查询权限 `OpenJobObjectW` 打开，不能创建同名替代 Job；存在时须 active=0 且 member list 空，只有同机同 session 且上述所有 births 已核实时，明确不存在的 Job 才可作为 absent 证据。所有新取得的 process/Job/snapshot HANDLE 均关闭成功后才能产出证明；关闭失败保留这些具体 HANDLE 以供下一次有界核对。[Toolhelp snapshot](https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/nf-tlhelp32-createtoolhelp32snapshot)、[Process32NextW](https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/nf-tlhelp32-process32nextw)、[OpenJobObjectW](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-openjobobjectw)。

跨进程恢复使用单独 `RecoveredOwnerProof`，不修改正常 `ExitProof`，不声称 join 了旧进程的 I/O 线程。证明绑定完整 request/owner 规范摘要、scan/attempt/generation、原始及检查者 runtime、每个已知 birth、Job 名称和 empty/absent 观察。`WorkflowIndex.recover_owner` 在短事务内重新严格校验这些字段与当前 unknown owner；只把该旧操作置 cancelled/released、关闭 publication，并将 typed recovery proof 留在 outcome journal。失败整笔回滚，重复核销幂等；旧完整快照或迟到 finish 不能发布。核销进程不代表目录恢复：last_success 不更新，已有 configuration_reason 不清除；若旧 outcome 的权限/身份错误曾被 unknown_owner 健康状态遮住，恢复相应根级暂停。

恢复消费链包括 dispatcher 启动时、显式 host reconcile、现有 prepare-exit 及 lifespan close；原生查询在共同 cleanup deadline 下进行，不能在 SQLite 事务或 ASGI event loop 内等待。启动恢复先于新扫描准入。只在 exact-bound 核销事务成功后才释放 host/全局名额；核对或保存失败继续保留 owner，退出仍拒绝。该机制只解决具有完整证据的普通崩溃，不扩大到任意进程管理、NAS I/O 或人工强制清理。

正常桌面关闭必须走既有 `POST /api/archive/prepare-exit` 的联合检查。先取得 scanner 的临时 admission fence，使新请求、已排队请求的启动与本次退出判定互斥；仍有运行、停止中或未证明退出的 owner 时不得返回 safe。只对同一 owner 作有界收束/核对，并在返回前持久保存已证明的归属状态；未知或保存失败保留 owner，不以退出请求删除其记录。只有 scanner 全部退出/I/O/句柄证明已完成且持久化成功，才在该 fence 内调用现有 `transfers.prepare_exit()`；transfer/cleanup 仍使用既有 admission 锁和返回语义。全部安全后才把 scanner fence 转为持续关闭状态，随后无额外可能失败的持久化步骤；safe 返回后不得再启动 scanner。这个协调过程不得持 db_lock 等待进程或网络。

扫描未收束或核对失败返回固定、脱敏的 HTTP 409 `detail="扫描尚未安全结束，已保留窗口，请稍后重试"`，沿用 Rust `ApiError` 与现有退出 UI 的保留窗口路径。不得伪造 transfer task_id、扩展 `ExitOperation`、增加 stop 端点或 force-close 入口。整体检查未成功时释放本次临时 fence，不把 scanner 或无关 transfer admission 永久锁成 closing：扫描检查失败不能先调用会锁定 transfer closing 的方法；transfer/cleanup 忙返回原 unsafe 结果时 scanner 恢复准入，其原有 unknown host 限制仍保留。用户取消退出后可继续正常工作，重试退出核对同一 retained owner；只有成功 safe 决定才保留关闭锁。此路径在原生销毁窗口前完成，不能把 Job kill-on-close 或未执行的 Python lifespan 当成退出证明。

### 4.3 首版实施 profile

以下是须进入类型化配置及测试的初始上限，不是现场性能保证；单元测试注入时钟和更短 deadline，不用真实长 sleep。

| 项 | 首版值/规则 |
| --- | --- |
| 每 host / 全局在途 owner | 1 / 4；unknown 占用名额。未证实 host 分组的别名放入同一个受限 unknown-host 槽，不能各自取得完整名额。 |
| 待扫描队列 | 每 root 最多一个合并请求，全局最多 64；满时 busy，不无限增线程/进程。 |
| probe / scan deadline | 10 s / 60 s，含启动和协议；cleanup 总预算 5 s。deadline 使用 parent monotonic。 |
| 输出上限 | 每消息 256 KiB；总结构化结果 64 MiB；stderr 尾部 32 KiB；超限判 partial，不截断后发布成功。 |
| 枚举边界 | 演员最多 10,000 个/root，影片目录最多 50,000 个/root；只在规定两层范围读索引，禁止跟随 reparse/链接。超限明确 incomplete。 |
| 元数据边界 | 单 NFO 最多 1 MiB；拒绝 DTD/entity 声明，不网络解析、不展开实体；只提取白名单字段。超限/不支持保留 metadata_unknown。 |
| 自动刷新/新鲜度 | 6 h 周期、24 h 最大索引年龄；启动和明确重扫可提前刷新。陈旧候选仍显示证据年龄，但不得用作“完整无匹配”或独立归档授权。 |
| 暂态失败退避 | 30 s、120 s、600 s，连续 3 次失败开路 15 min；半开仅一个 probe，先核对旧 owner。成功后清退避。 |
| 权限/认证/身份变化 | 不做定时快速重试；状态明确等待配置/身份核对。未知所有权不受开路计时到期豁免。 |

R1 根级恢复契约：`permission_denied`、`identity_changed`、`identity_unknown` 和 `not_directory` 只暂停出错 root，不能设置共享 host 配置锁或增加它的暂态失败次数。普通已认证的添加、单源扫描、全部重扫入口可提交一次持久人工重试意图；自动刷新没有此权限，配置失败前已有 queued 请求不能转成新的重试授权。暂停不能在点击、取消、失败、重启或另一 root 成功时清除，只有同根完整验证成功或显式映射替换才清除。人工重试仍受原 host/global owner、unknown、退避及 native identity 条件约束；不能把“再次扫描”当成接受不同目录对象。实际身份替换使用现有明确删除/重新添加流程，旧 workflow root/history 继续 tombstone 保留，不增设日常按钮或技术确认框。

所有边界都以结构化原因呈现；不为通过测试任意加大 timeout。真实冷启动、根规模和 SMB 取消行为由 T09 合成/现场只读验收验证，不能把模拟成功称为已经覆盖实际 NAS 驱动。

## 5. 索引事实、匹配与覆盖语义

### 5.1 数据来源

复用 `normalize()` 的 NFKC、大小写与繁简规则，保留原显示名。来源：现有 performer 名字、组级 aliases、扫描范围内可解析的小型 NFO 的明确 actor name/namespace-ID，以及影片目录/NFO 的标题、番号和版本提示。外部编号必须来自该 actor 的明确字段及 provider/namespace；影片编号、缩略图 URL、姓名 hash 不转成 actor ID。不认识的 NFO 格式保留 unknown，不猜测字段含义。

每项证据保留 root/location、相对元数据项、内容摘要、parser/protocol version、观测区间和结果；不把未经核实的 NFO 内容称为远端权威证明。调用方提供的 actor external IDs 是 query 条件，不能反写索引并自证匹配。明确 ID 冲突、同名不同 ID、同 ID 多位置都保留完整候选和冲突原因，不自动合并人物记录。

影片碰撞返回 `possible_same_code`、`same_name`、`version_hint_differs`、`version_unknown` 等证据。没有对正式视频全量 hash，因此不能宣称仅凭 NFO/文件夹名已证明“同一版本”或“安全重复”。同番号不同版仍需 T06 明确选择，F02 最终检验 expected-absent 与真实目标身份。

### 5.2 查询结果不是一个布尔 match

每个 root 返回 health：`fresh|stale|never_scanned|offline|permission_denied|identity_changed|partial|unknown_owner|tombstoned`，包含 last_attempt、last_success、当前观察时间、index revision 及 coverage。NAS 未醒/暂不可达与无权限区分；离线根保留最后成功位置和证据，不把失败扫描作为空列表。

结果同时含 `match_state=matched|ambiguous|no_match` 与 `coverage_state=complete|incomplete`。`no_match` 只有在允许 root 集合全部当前、成功且覆盖完整时才有确定含义；有未知/离线/过期覆盖时返回 `match_state=ambiguous`、`coverage_state=incomplete`、`reason=coverage_gap`，即便当前候选为零。多演员每个 actor_ref 各有匹配/覆盖摘要；一个演员的成功匹配不能掩盖另一演员未知。

排序只用于稳定呈现（精确外部 ID、精确名字、明确别名、模糊名字，随后稳定 location_id）；不会填入 selected、preferred 或自动归档许可。分页不得把“首页只有一条”解释成唯一匹配。返回总数、truncated/next_cursor 和全 root 覆盖摘要；游标绑定 query hash + index revision，版本变化返回 `index_changed` 要求重读，不混合两个索引版本。

## 6. 最小只读 API 与 T01 wire

### 6.1 本地认证与只读限制

现有服务仍只绑定 `127.0.0.1`。`create_app(..., workflow_index: WorkflowIndexConfig | None = None)` 显式注入独立只读 token、component/library identity 及允许 root 集合；缺配置时不启用 workflow 端点。T05 保存和交付此受保护配置，F01 不读取 TaskPaw 生产设置、不自己生成新主控。

新端点统一 `Authorization: Bearer <opaque-secret>`，常量时间比较、拒绝空值/错误值、禁止 token 在 URL。该 token 仅允许下面的准确读路由，不能调用 sources/alias mutation、scan、archive、cleanup、open/resolve、settings 或任意路径探测；旧 desktop session token 不自动转成 workflow token。`POST .../candidates/query` 虽用 JSON body 传查询，仍是无副作用读：不触发扫描、不登记映射、不更改 last_seen。middleware 对它作准确的读权限分支，不能给整个 POST 前缀放行。

| 方法 | 请求/响应 |
| --- | --- |
| `GET /api/workflow/v1/index` | library/component/protocol version、index revision、允许 roots 的脱敏健康摘要；无 raw DB/UNC 路径。 |
| `POST /api/workflow/v1/candidates/query` | `CandidateQuery` → `CandidatePage`；唯一候选查询入口。 |

未知 root 返回明确 422；未获允许的 root 返回 403，响应不枚举其存在性。错误只含固定 code/request_id/公开 IDs，不回传 OSError 文本、连接细节、secret、完整本机路径或目录清单。当前旧 UI 受控本地路由的路径显示不是新接口的安全模板；此 issue 不声称重写了所有 legacy GET 的认证。

### 6.2 固定 DTO

```text
CandidateQuery:
  schema_version, request_id, film_id, metadata_revision,
  final_revision, final_hash,
  actors[{actor_ref, name, aliases[], external_ids[{namespace,value,provenance_digest}]}],
  film{code?, title?, edition_hints[]}, root_ids[], proposed_film_relative_path,
  expected_index_revision?, cursor?, limit

CandidatePage:
  schema_version, library_id, component_id, index_revision, query_hash,
  generated_at, roots[RootCoverage], actor_results[], candidates[], total,
  coverage_state, match_state, reasons[], next_cursor?

LocationCandidate:
  candidate_id, location_id, location_revision, root_id, root_revision,
  host_id, export_id, host_label, root_label, actor_display_name,
  relative_actor_path, proposed_target_relative_path, matched_actor_refs[],
  match_reasons[], external_id_evidence[], film_conflicts[],
  observation_id, observed_at, last_success_at, identity_state,
  native_identity?, native_aliases[], coverage_state, candidate_digest
```

Query body 上限 64 KiB；演员最多 32、每演员 aliases 最多 32、外部 ID 最多 16；候选页 limit 1–200，普通字符串 2048、相对路径 1024 字符。name/alias 不可为空，unknown schema/version/type/路径拒绝，不静默截断。root_ids 必须为受控 root IDs，不能传 NAS 地址。

`candidate_id` 为 `library_id/index_revision/query_hash/location_id` 的规范摘要，不能单独用演员名或列表下标。digest 采用 UTF-8 排序 key 的紧凑 JSON，集合明确按稳定 ID 排序；纳入查询影片/元数据版本、位置/目标及证据，排除查询接收时间。native128 与 legacy64 带命名空间传递，不做截断；`native_identity=null` 表示不足以交给 F02 作提交身份。

### 6.3 跨仓映射与失效

T01 草案中的 `ApprovalBinding.metadata_revision` 对应 query metadata revision；`candidate_revision` 对应持久 index revision；`actor_ids` 使用当前影片元数据的 actor_ref，location_id 另在 candidate evidence 中绑定，不能冒充全球 actor ID。`TargetIdentity` 对应 root_id、opaque host/export identity、根 native identity、选定相对目标和 `expected_absent=true`。namespace/volume/object 的映射必须无损，兼容 alias 仅是证据，不能替换实际删除/提交句柄身份。

候选证据摘要必须覆盖完整被选 candidate、query hash、index/root/location revisions 和覆盖状态，供 T01 `ApprovalObservations.candidate/target` 保存。source 时间和本地接纳时间分开；新响应不能把旧 source observation 刷成新事实。

F01 不写 T01 context。主控 operator 在接纳新候选/根身份事实后调用 T01 的受限 CAS `put_approval_context`；旧批准不能因 final manifest 未变而继续授权新目标。F02 在新归档准入前重新验证当前 index/root revision 与真实 NAS 根身份；候选改变须先同步 context 并重新确认。跨库没有联合事务；此处是后续适配器责任及契约测试，不是 T01 已通过的联调证明。缓存候选无论多新都不能替代 F02 的实际句柄身份与不存在条件核验。

## 7. 旧入口与消费者的最小迁移

| 消费者 | 本次变化与兼容边界 |
| --- | --- |
| `GET /api/sources` | 只读 SQL/cached health，不调用 isdir。保留既有字段，新增 health_state/observed_at/last_success_at；旧 reachable 仅为 fresh 且最近成功的兼容布尔，不用 false 混称权限/未知。 |
| `POST /api/sources` | 仍是显式 desktop-session 操作；受管 probe 成功后才插入 source。准确不存在/非目录仍 422；暂态、权限、owner 未知以结构化原因拒绝，不误建来源。未入库 probe 也占受控 host/unknown-host 槽，有独立 private attempt，不丢进程。 |
| `POST /sources/{id}/scan`、`POST /scan-all` | 调用同一 supervisor；成功仍返回现有统计形状。使用异步有界 wait，不持 db_lock 等网络；批量请求并发/队列受同一 host/global 上限约束。请求总预算 65 s，未取得完整退出证明的项返回明确未完成/未知错误，不能回报成功后迟到写半快照。未完成项取消本次发布资格；旧 owner 收束前禁止新 attempt。 |
| `scan_source` | 移除服务器使用的直接网络调用路径，成为 collect/apply 的兼容组合仅供受控测试或改为显式快照入口；生产 routes 不再在 DB 锁内调用旧版本。 |
| `SettingsPage.tsx` | 最小显示健康类别/证据年龄及扫描未完成原因，保留原添加/扫描/删除动作；不新增逐片操作。`PerformersPage` 继续消费成功统计和同一来源 ID。 |
| `routes_performers.py` alias/purge | 原操作语义不变；添加 revision/tombstone 同事务钩子，防候选历史消失或版本不变。 |
| `routes_archive.py` prepare-exit | 在既有退出握手中协调 scanner admission fence、已持久化的 owner 退出证明及原 transfer/cleanup 检查；扫描未收束返回既有可处理的固定非 2xx 错误。Rust/退出 UI 的响应类型和正常保留窗口行为不改。 |
| 既有 archive/cleanup/local browser | 不接入 F01 的新只读 token，不让 candidate query 跳转这些路径。其全部网络隔离及安全写入另由 F02；本轮回归防止共享 DB/session/identity 改动破坏既有行为。 |

来源列表的即时 isdir 改为缓存健康是有意行为变化，不能仍在 UI 声称每次列表请求都刚刚探测 NAS。查询和 UI 的响应性验收至少覆盖旧 source scan 正在挂起的场景，不只测独立候选路由。

## 8. 文件职责与执行面

| 文件 | 计划改动 |
| --- | --- |
| `app/server/filmpaw_server/workflow_index.py`（新） | 严格 DTO、候选查询、只读投影、短事务 apply、索引 revision 与身份关系；只做本功能，不抽任意工作流框架。 |
| `app/server/filmpaw_server/scan_process.py`（新） | 固定进程启动、Job/HANDLE/I/O owner、协议边界及 ScanSupervisor/host 熔断。 |
| `app/server/filmpaw_server/scan_recovery.py`（新）、`scan_native.py`、`workflow_models.py` | R2 的持久 runtime/native birth 核验、独立 recovery proof 及新取得 HANDLE 归属；不扩展正常 ExitProof 或任意进程控制。 |
| `app/server/filmpaw_server/scan_worker.py`（新） | 固定只读请求、bounded collect、hello/go；无 DB、HTTP、归档或删除。 |
| `app/server/filmpaw_server/routes_workflow_index.py`（新） | 两个认证读路由和明确错误投影。 |
| `db.py`、`app.py`、`main.py` | v4→v5→v6 迁移、共享 owner 生命周期/配置/guard、worker 早分流；主服务默认握手保留。 |
| `scan.py`、`routes_sources.py`、`routes_performers.py` | collect/apply 切换及上述 legacy 消费者闭环。 |
| `routes_archive.py` | 仅将 scanner 纳入现有 prepare-exit；复用 `transfer.py` 已有锁内安全检查，不改变归档/清理的退出协议。Rust/UI 产品契约文件无需新增枚举或流程。 |
| `directory_reads.py`、`path_resources.py`、`windows_identity.py` | 优先原样复用；只有必要的 typed observation/错误投影可最小调整，不放宽原生身份安全条件。 |
| `app/server/tests/test_workflow_index*.py`、`test_scan_process*.py`（新）及既有对应测试 | 契约、状态/并发、迁移、原生及 frozen 回归；`test_archive_api.py`、既有 Rust lifecycle 和退出 UI 测试覆盖实际 prepare-exit 消费链。fixture 全为临时目录和虚构名称。 |
| `app/ui/src/SettingsPage.tsx` 及其测试、`app/ui/openapi.json`、`app/ui/src/client/` | 健康状态最小 UI 适配；后两者由生成器更新，不手写生成客户端。 |
| `build_sidecar.py` / `.github/workflows/ci.yml` | 仅必要的 worker 收集或 frozen smoke；不更换安装器或新建常驻服务。 |

本规划阶段唯一写入是本文件。后续实施的授权文件/命令由 driver 记录，表中“计划改动”不等于当前允许修改。

生成与验证命令（均是后续命令，本轮未运行）：

- 后端在 `app/server`：`uv lock --check`、`uv sync --locked`、`uv run --locked pytest -q`。聚焦先运行新增模型/迁移/匹配测试，再原生 scanner 和既有 scan/sources/performers/identity/transfer-concurrent 回归。
- OpenAPI 在 `app/server`：`uv run python -c "import json; from filmpaw_server.app import create_app; open('../ui/openapi.json', 'w', encoding='utf-8').write(json.dumps(create_app().openapi(), ensure_ascii=False, indent=1))"`。factory 的 schema 包含新路由定义，但缺配置时请求仍拒绝；导出不进入 lifespan、不打开 DB。
- 客户端在 `app/ui`：`pnpm install --frozen-lockfile`、`pnpm gen:client`；输入 `openapi.json` 和 `openapi-ts.config.ts`，输出 `src/client/`。验证 `pnpm typecheck`、`pnpm ci`、`pnpm test`。
- 打包在 `app/server`：`uv run python build_sidecar.py`，生成现有 sidecar_entry、build/dist 和 Tauri binaries。随后自动 frozen 测试显式设置临时 `FILMPAW_DB`、临时 source 和合成 token；启动/停止只限自己创建的实例。父 shell/worker 生命周期测试在 sidecar 产物存在后执行，不能依赖开发机遗留产物。

## 9. 验收与测试矩阵

| ID | 必须证明的结果 |
| --- | --- |
| F01-A01 | v4→v5→v6、已有候选 v5→v6、全新库、重复启动；注入 DDL/commit 失败后原数据与版本可核对。保留 source/performer IDs、alias、thumb、archive journal/event；未知未来 schema 拒绝。 |
| F01-A02 | 同名、繁简、别名、同 actor 外部 ID 多位置、同名 ID 冲突、多演员、不认识的 NFO：候选和理由完整，不自动选第一项、不合并人物、不凭名字造 actor ID。 |
| F01-A03 | 同番号不同版/多卷/目录同名仅返回可解释冲突与版本不确定；没有视频 hash 时不声称已证明重复。 |
| F01-A04 | 离线、拒绝权限、过期、从未扫描、截断、根身份变化均不等同 no_match。失败扫描不更新 last_success、不标记旧位置消失；恢复完整扫描才更新观察。 |
| F01-A05 | query 真只读；错误/空 token、legacy token、未允许 root、traversal、过大 body/分页/未知版本拒绝。新响应、错误、日志无 UNC/DB 路径/secret；只读 token 调任何 mutation/scan/open 失败。 |
| F01-A06 | 快照应用与 source 删除/重加、映射改动、alias 修改并发；CAS 只接纳当前 generation。legacy DELETE/purge 保留旧语义，workflow IDs、tombstone、owner 和证据不级联消失。 |
| F01-A07 | 第二 sidecar 不能取得同库 session；扫描 child 不持 DB 连接。挂起扫描期间并行执行 cached /sources、performer/query/health 与另一 host 的扫描，读接口在 1 s 测试预算内返回，数据库锁不包含网络等待。 |
| F01-A08 | 同 host 多 roots/地址别名、64+重复请求、4 个不同 host：owner/queue 数有界；未知 owner 占名额，重启/半开/重试不会另起同 host worker。 |
| F01-A09 | receiver 协议缺 hello/go、错误 PID/birth/Job、错误 revision、重复块、越界/短 EOF、无 complete/hash：不准 NAS I/O 或不提交快照；保留能够核对的 owner。 |
| F01-A10 | **Windows 原生**受控 worker 在本地命名管道 `ReadFile` 真正阻塞，另有 owned 临时文件；deadline 后用原生 HANDLE signal、Job 空、I/O join 证明退出再释放槽。它证明真实阻塞进程生命周期，不声称模拟了全部 SMB 驱动行为。 |
| F01-A11 | 确定性注入 TerminateJob/Wait/Query/OpenProcess/CloseHandle/reader join 失败及“active=0 但已知 HANDLE 未 signal”；保持 unknown，不无限重试或创建替代进程。采样身份丢失不能在后次空列表中自动清除。 |
| F01-A12 | 冷启动/暂态 WinError、权限 WinError、恢复、熔断半开使用注入时钟；正确分类且退避有上限。R1 覆盖同 unknown-host 两根隔离、失败 add、人工单次恢复、后台无权、取消/重启仍暂停、旧排队失效、实际身份替换拒绝及删除重加历史保留；未知 owner 和真实 host 退避不被人工按钮绕过。额外用 owned 临时访问控制/原生错误场景验证错误投影；不能通过修改真实 NAS ACL 制造测试。 |
| F01-A13 | 同路径目录替换、reparse/祖先切换、native128/legacy64/alias、零/无效身份：候选 identity_unknown/changed，不变成可提交目标。复用现有 Windows identity 回归，不扩大 fallback。 |
| F01-A14 | frozen 同EXE worker 和源码运行各自证明 hello 对应实际受管 worker；worker 模式不打开 socket/DB；主服务端口/PID握手保留；实际 prepare-exit 消费链按下列 D01 场景验证，关闭/崩溃窗口不误报干净退出。干净打包不能靠本机 build 遗留物通过。 |
| F01-A15 | 稳定分页与并发 index revision；新候选事实形成不同 digest。跨仓 JSON fixture 可映射 T01 metadata/candidate/target 字段，变更目标不能沿用旧 context；在 T01 前置通过前标记为契约测试而非真实联调。 |
| F01-A16 | 原 Settings/Performers 的成功扫描、统计、case-only rename、缩略图、删除确认与来源过滤仍有效；未知/权限不再显示成肯定离线。既有 archive/cleanup/session/identity 回归通过。 |

F01-A14 的 D01 退出回归须覆盖：

- transfer/cleanup 空闲但 scanner 运行或 unknown：真实 prepare-exit 返回固定 409；原 Rust/UI 错误路径保持窗口，不调用销毁或 sidecar 终止。
- 同 owner 的终止、采样、等待、I/O join 或状态持久化失败：不能返回 safe，owner/host 槽仍保留；之后重试只核对该 owner，证明完整且落库才可放行。
- 用确定性 barrier 让新请求或已排队 scan 与退出判定竞争；safe 之后均不能创建 worker 或发 GO。
- scanner 已空闲但 transfer/cleanup 忙：保留原 unsafe 响应；取消退出后新扫描仍可准入。scanner 失败时空闲 transfer 也不能被误锁 closing；取消/重试不清掉 unknown 限制。
- 全部 idle 且状态已落库：保持原 `safe=true, task_id=null` 响应形状并关闭所有新准入；源码及 frozen 产物通过实际退出握手验证，不能只测 lifespan。强制父进程死亡仍按未知恢复测试，不混作正常退出成功。
- R2：真实 owned source/frozen worker 在 hello 和持久 GO 授权之后、GO 送达之前，其父进程强制退出；不保留诊断 process/Job HANDLE，重启仍可依据真实 births、完整 process snapshot 与 Job absence 核销，旧快照绝不发布。另覆盖精确绑定、PID 重用、旧身份缺失、sticky 成员、跨 session、权限拒绝、枚举非终止错误、close 失败、事务回滚、幂等、迟到 finish、启动准入顺序及根级权限暂停保留。

人工/现场部分属于 T09：使用被授权的合成目录确认多 NAS 根映射和 alias 分组、真实冷启动/权限/身份能力。不得以实际正式影片做破坏性测试，不把 mocked error、管道阻塞或单机临时目录结果替代四 NAS 验收。

## 10. 风险、假设与交接

| 风险/尚未运行的假设 | 影响与处置 |
| --- | --- |
| 原生 HANDLE/SMB 返回的身份能力存在差异 | 严格保留 namespace 和同句柄证据；无法证明时 target identity 不可用。F02 必须再验证，不以路径猜测。 |
| NAS 地址别名可能无法自动证明同主机 | 未证实映射使用共同受限槽/明确 coverage gap；T05 初始映射与 T09 验证不增加每片用户步骤。 |
| NFO 缺 ID、错误 ID、同名和旧版本信息 | 原样保留证据级别/冲突，不能把解析成功升级为真实人物身份保证。 |
| 枚举范围与期限可能不足以覆盖现场库 | 输出 partial 和真实原因，不以局部结果 mark missing；上限调整须有后续测量/评审依据。 |
| Job 终止与 I/O 完成仍可能未知 | 保留 owner；查结果不自动发新扫描。unknown 是可达终态，不能为了界面绿色丢弃它。 |
| frozen worker 复用解包资源/启动环境 | 官方源码只证明机制存在；本项目真实 frozen smoke 尚未执行，关闭顺序和父进程崩溃必须验证。 |
| T01 wire/前置尚未交付 | 独立 fixtures 与模型映射可先做；不调用生产 T01，不宣布整合完成；变动回到 driver 分类并同步设计。 |

下一步是 driver 的独立设计审阅及前置判定。本初稿交付后冻结；只报告新增疑点，任何 review-driven 内容修订先由 driver 命名、分类并预留 F01 修复周期。F01 当前初始规划未消耗其默认 6 次 allowance；这不重置其他 issue 的周期。
