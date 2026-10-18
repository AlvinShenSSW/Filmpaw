# Issue #36 — 单向归档传输

## 授权与范围

2026-10-18 操作者已审阅交互稿并授权 AFK，由 Codex 主导，**Kimi K3 单外门**。
此明确指令覆盖 AFK 默认双外门拓扑，本轮不是评审服务降级。范围仅 #36；
`leave-open`，完成后保留 PR，不自动合并、不部署。
应用与 sidecar 同步为 0.5.0，便于版本自检识别不支持新会话/进程身份协议的旧产物。

交互稿：[可交互 HTML](assets/filmpaw-archive-transfer-v1.html)。确认的额外约束：
详情头部缩小约一半；最大化时头像不放大，两个目录区域获得新增宽度与高度，
各自滚动，操作栏贴底。静态稿已验证 1200×800、1920×1080、2560×1440，
以及最大化/还原、选择保持、真实 Fullscreen API、独立滚动。

用户明确扩展原产品只读边界：仅在用户选定具体 NAS 演员、勾选影片并点击传输后，
允许本地 batch → NAS 的整文件夹移动。现有扫描、搜索和普通浏览仍不移动文件。
不做反向传输、覆盖、合并、自动改名、自动选演员、自动删空演员目录或重新刮削。

## 用户流程

1. 选 batch 和本地演员；匹配结果保持现有别名、繁简、手动搜索和双开。
2. 用户点击具体候选的“选择此目录”，才建立服务端上下文；即使只有一条候选也不自动选定。
3. 详情紧凑显示海报、姓名、NAS 来源/路径和双开；两列列出一级影片文件夹。
   不把演员根部图片、NFO 或其他散文件当影片。完整路径和长名称可查看/复制。
4. 左列可多选，右列仅供对照；独立自然排序。刚进入、切换 batch/演员/目标均清空选择。
5. 提交后进行整批预检，任一同名目录或文件冲突即整批零移动。运行期间新冲突则停止
   当前项和后续项，已完成项保留；用户自行处理，不自动重试。
6. 成功项从源列消失并出现在目标列。失败、清理未完成、待核对分别可见。
   全局任务状态挂在应用 Shell，切换页面不丢失任务。关闭时可继续运行、等完成后退出、
   或停止队列后退出；无法确认状态时不强行关闭。

### NAS 暂时不可读时自动重试（操作者追加）

目录读取遇到暂时的网络错误、共享忙或超时时，首次失败后按 1 / 2 / 4 秒间隔自动重试，
最多三次；显示正在重试及次数，耗尽后保留手动重试。覆盖 batch 演员目录和详情双栏。
已显示的列表在临时失败时保留并标记尚未刷新，不能当成最新可传输的目录快照。
切换 batch、演员或目标后取消旧请求/等待；相同目录恢复后保留仍然有效的影片选择。
权限、非法路径、失效绑定等永久错误不自动重试。

服务端对各侧返回明确的 `source_retryable / target_retryable`，不要求前端解析系统错误文字。
上下文与 batch 列表读取各有限制并发的读锁：上次 Windows/SMB 调用仍未返回时，新请求快速
返回 503，不能因为浏览器请求超时而无限叠加阻塞文件操作。读锁不占用数据库锁。
这项自动重试仅用于目录读取，不能重放传输提交、重新启动失败队列或自动删除源文件。

## 结构与契约

Python `TransferService` 管目录上下文、安全检查、唯一串行工作器、持久任务和恢复。
路由只负责校验输入及响应模型。React 负责选择与展示，不持有任意路径写入能力。
Tauri 负责随机会话凭据和安全退出，现有双开继续由前台壳调用。

所有 mutation 和 `/api/archive` 读取都要求 `X-Filmpaw-Session`。
Tauri 每次启动生成 32 字节 OS 随机凭据，64 位十六进制，经环境变量
`FILMPAW_SESSION_TOKEN` 交给子进程，经 init script 注入
`window.__FILMPAW_SESSION_TOKEN__`；不经 URL、公共 HTTP 接口或日志泄漏。
浏览器开发模式要求前后端显式共享此环境变量，Rsbuild 仅开发构建注入，生产产物不嵌入。
没有有效凭据时写入关闭；CORS 不是写入授权。

| 接口 | 输入/输出 |
| --- | --- |
| POST `/api/archive/contexts` | `{local_dir, subdir, performer_id}` → context |
| POST `/api/archive/transfers` | `{context_id, names, request_id}` → queued task |
| GET `/api/archive/transfers/latest` | 最新 task 或 null（包含重启后的待核对状态） |
| GET `/api/archive/transfers/{id}` | 固定 task |
| POST `/api/archive/transfers/{id}/stop` | 幂等停止请求，返回 task |
| POST `/api/archive/prepare-exit` | `{safe, task_id}`；与创建任务使用同一 admission 锁 |

Context 包含 `id, local_dir, subdir, performer_id, performer_name, source_label,
has_thumb, source_path, target_path, source_items, target_items, source_error, target_error`。
目录项为 `{name, size_bytes|null, files|null, error|null}`。列表只读一级目录；为避免
打开演员就递归遍历整个 NAS，大小可显示“未统计”，选中项在预检后给出精确大小。
读取失败与空列表不同，NAS 不可访问不伪装为零部影片。

Task 包含 context 的冻结身份/路径信息，以及 `id, request_id, context_id, status,
phase, current_name, progress_bytes, total_bytes, verified_bytes, completed_count,
total_count, conflicts, message, stop_requested, created_at, updated_at, items`。
item 对外含 `{name, status, size_bytes, progress_bytes, message}`。
内部文件清单、摘要和目录身份不通过 API 暴露。

Task status：`queued / running / completed / stopped / failed / needs_review`。
Phase：`preflight / copying / verifying / committing / cleaning / finished`。
逐项状态额外区分 `pending / cleanup_pending` 等，不能把目标已提交而源残留标成成功。
请求 ID 对应唯一任务；同 ID 不同请求拒绝，重试相同请求返回同一任务。

## 文件完整性与失败边界

- context 的 `local_dir` 只用来比对已批准 settings anchor，不是任意路径授权。
  服务端解析 actor 和 performer ID，记录路径、目录身份；提交再次检查 settings、
  performer 路径/失效状态和目录身份。开始后使用冻结快照，设置变更不改运行任务。
- 拒绝父级穿越、绝对/非法影片名、相同或嵌套源目标、链接/junction/reparse point。
  所有文件 I/O 在数据库锁之外；`db_lock` 仅用于读取快照或写入短状态事务。
- 完整预检所有选中影片的文件树、可读性、容量和大小写等价冲突，再执行第一项。
  目标普通文件同名也冲突；不猜番号。活动队列全局唯一，不接受第二队列。
- 每部先写独占、带任务身份的 NAS 暂存目录，复制完整树（含空目录），保留支持的时间属性。
  对目标重新读取 SHA-256，并核对完整清单与源未被改动；复制和校验分别显示进度。
- 提交前再次检查最终目录名，持久写 `committing` 和暂存身份/摘要；Windows 使用不覆盖的
  rename，Linux 测试使用原子的 NOREPLACE，不能用覆盖式 replace。
- 只有目标提交和内容核对完成后才进入 `cleaning`，且先持久化阶段。
  源按清单逐文件核对身份和摘要，再删除该确切文件；绝不对源无条件 rmtree。
  Windows 删除期间用受限共享文件句柄避免核对→删除间替换或改写默认数据流。
  检测到新增或变化内容时保留源。
- 当前不复制 NTFS 命名数据流（ADS）。选中影片内的文件或目录含额外流时，预检拒绝整批；
  复制后、清理前和删除前按确切句柄再次检查，提交后发现则显示 `cleanup_pending` 并保留源。
  无法可靠枚举流时也不删除。演员根目录及未选中文件的附加内容不影响选中影片。
  Windows 共享模式按数据流维护，最终枚举与删除不是原子操作；传输期间应停止对所选影片
  的外部写入，不能宣称可抵御敌对的并发新增流。
- 失败/停止在安全边界终止，后续项保持未开始；已完成项不回滚。
  目标完整但源清理失败显示 `cleanup_pending`；任务为 `needs_review`，不能重新盲搬。
- 启动恢复不自动继续搬运/删除。仅有同名目标不能认领：提交窗口需比对最终目录与
  原暂存目录身份和完整摘要。源已消失且目标身份/内容正确可核对为完成；源残留显示待核对。
  暂存残留保留准确路径/身份，不按通配路径删除其他文件。
- `prepare-exit` 与 submit 同锁：活动时返回 busy，不改变任务；空闲时关闭 admission 后
  才返回 safe，消除“查询空闲→新任务开始→壳杀进程”竞态。强制结束仍依靠 journal。
- 同一数据库由 OS 排他文件锁保证单 sidecar，第二实例不能误将仍在运行的队列当作崩溃恢复。
  batch 变更同事务生成 revision，即使 A→B→A，原 context 仍失效。
- Windows 目录句柄拒绝写入共享，防止原地把已检查的空目录改成 junction。提交时仅直接父目录
  允许写入共享，因为 Windows rename 需要它；暂存根被持有的句柄保护，父目录始终非空，
  已实际验证原地 junction 设置被 ERROR_DIR_NOT_EMPTY 拒绝。提交仍使用同一暂存目录句柄。
- 持久状态保存失败时，内存返回明确的 `needs_review` 并禁止新队列，保留最后落库的 journal。
  工作线程结束后可安全退出，下次启动按持久证据核对，不会因陈旧 active 记录永久无法关闭。
- 壳握手首行仍为 `FILMPAW_PORT`，第二行为真实 Python `FILMPAW_PID`；壳立即持有该进程句柄。
  uv/PyInstaller launcher 退出不代表 worker 退出。退出判断和结束操作均基于真实句柄，避免 PID 复用。

## 数据库与兼容性

schema v3 增量新增 `archive_tasks(id TEXT PRIMARY KEY, request_id TEXT UNIQUE,
payload TEXT NOT NULL)`，JSON payload 保存完整任务清单和恢复证据。保留 v1→v2 迁移，
v2→v3 的建表与版本更新同一事务；不改动既有 sources、performers、aliases、缩略图、settings。
新的版本不允许旧应用无提示写入；旧版拒绝未来 schema 的既有行为继续生效。

README、关于页和旧设计定位同步说明用户主动归档。不在本 issue 改 AFK 工具链默认模型。
当前仓库没有 `docs/constitution.md`，constitution 自检据 AGENTS、#36、本设计与现有架构
执行，不能声称运行了不存在的 gate。

## 设计质询与结论

第一轮：前端指出上下文必须绑定用户看到的 batch、任务必须挂全局；引擎指出同名目标
不足以恢复认领、必须先落提交/清理状态；壳指出 GET active 后关闭存在竞态。
以上均纳入上文。第二轮检查执行器的文件身份、DB 锁范围、关闭 admission、凭据开发模式、
列表读取规模和非覆盖提交；通过故障注入验证，而非只靠正常复制测试。
实现后的质询修复了 stop 旧快照覆盖终态、全批可读预检、多实例恢复竞争、launcher/worker
身份混淆、原地 reparse 改写和状态保存失败的退出死锁，均附针对性验证。

## 验证与交付

先新增失败测试，再实现。pytest 使用临时源/目标与 DB，覆盖树内容、空目录、冲突零移动、
运行时新冲突、重复请求、上下文失效、链接/路径替换、复制/校验/提交/清理故障、停止和
两个崩溃窗口。UI 覆盖明确选目标、初始不选片、排序/多选、切上下文清空、冲突和刷新；
壳覆盖 token 和退出错误边界。全部 E2E/打包明确设置临时 `FILMPAW_DB`。

运行 `uv lock --check`、`uv run --locked pytest`（本项目无 web extra）、UI typecheck/Biome/
vitest、OpenAPI 导出与生成客户端、sidecar 打包后 cargo test，再检查 Windows CI/NSIS。
大屏布局验证保持 1080p、2K、还原窗口和长目录滚动。真实用户 NAS/影片不用于测试；
若无法取得可丢弃 SMB 环境，如实记录未验证的网络场景，不把本地文件系统测试冒充 SMB 验收。

分支 `feat/36-archive-transfer`；本地验证通过后提交并尽早创建 PR，CI 绿后 CTO 自检，
再运行 Kimi `kimi-code/k3` 唯一外门。外部 CLI 必须 stdin 关闭、有硬超时、后台运行。
收敛修复后保留 PR，并清理本轮 AFK 续跑调度器。
