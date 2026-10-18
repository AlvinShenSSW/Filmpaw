# Issue #38 验证记录

范围：指定 batch 的一级空演员目录清理。所有文件操作使用新建临时目录；所有服务/E2E 显式指定临时 `FILMPAW_DB`，不访问真实片库。

## RED → GREEN

- API 首轮 5 个 404 失败、1 个鉴权用例通过，随后补齐路由与会话服务。
- 服务新增 37 项、API 6 项，覆盖严格判空、零字节/隐藏/系统文件、空子目录、目录 ADS、junction、目录身份替换、枚举中断、权限失败、准入与退出、A→B→A、幂等及上下文淘汰。
- 界面先复现旧清理结果误清掉重新创建的同名演员选择，改为一次性消费删除事件，缓存仅裁剪新确认的删除项。覆盖只读 1/2/4 秒退避、丢失响应、离线不重放、跨页、过期 batch 响应。
- 壳与退出界面先观察新 cleanup 协议失败，再补任务类型区分和等待退出；清理 ID 不送入停止传输接口。
- 后端完整回归：**209 passed / 1 skipped**。跳过项为既有普通 symlink 权限测试，junction 实测覆盖；UI **136 passed**（包含退出界面 13 项）、TypeScript/Biome 与生产构建通过；Rust **24 passed**。

## 实际浏览器

执行 `uv run --project app/server --locked --with playwright python docs/evidence/issue-38/browser-smoke.py`，使用生产 UI 构建、真实 Python 服务、Edge headless。

- 实际完成 2 个空目录删除；海报、隐藏文件、右侧片库对照目录和影片摘要保留。
- 服务接受请求后故意丢弃 HTTP 响应，UI 查询恢复结果；写请求总数为 1，没有自动重放。
- 删除当前选中演员后完整左栏刷新，详情退出；切页及 WebView 刷新仍展示本次会话结果。
- 960×640、1200×800、1920×1080、2560×1440 无页面溢出，按钮可见；无浏览器异常。
- [结果 JSON](browser-smoke.json)、[清理前](cleanup-ready.png)、[清理后](cleanup-complete.png)。浏览器夹具保留在报告所列独立 TEMP 目录便于检查。

## 本机与 SMB 回环

执行 `uv run --project app/server --locked python docs/evidence/issue-38/cleanup-smoke.py`，4 场景全部通过：[结果 JSON](cleanup-smoke.json)。

- 本机、现有 `\\localhost\C$` UNC、临时映射盘、签发上下文后更换映射目标。
- 每场景仅删除 2 个严格空目录，保留 4 个非空目录（海报、隐藏文件、空子目录、目录 ADS），batch 根和外部对照快照不变。
- 更换临时盘符映射后，任务仍使用原上下文的原生 UNC 最终路径，新的映射目标整棵目录结构、文件摘要和修改时间不变。
- 重发同一请求返回原结果，重新创建的同名空目录保留。
- 脚本使用关闭 stdin 的 180 秒有界子进程；只创建并撤销自己拥有的临时映射，不修改共享／ACL。TEMP 夹具、DB 与临时盘符均已清理。
- 另用 `--timeout-probe` 在临时映射仍活动时故意达到 3 秒期限，验证父进程可以回收自有映射、夹具与数据库；[超时回收证据](cleanup-smoke-timeout.json)。

## 自查与已知边界

设计经过两轮质询；实现经过两轮自查。没有 `docs/constitution.md` 时按 AGENTS.md、Issue #38 与设计逐项检查：修改范围、鉴权、原生删除句柄身份及路径、非递归删除、会话幂等、互斥、退出、真实数据库隔离、版本一致性。

清理结果只保留在当前服务会话，重启不自动恢复或继续删除。Windows 文件系统无法提供原生句柄核验时保留并报错。目录 ADS 的最终检查与删除存在已说明的非原子窗口，清理期间应停止外部写入。回环 SMB 验证不代表厂商 NAS 或真实断网验收。

Kimi K3 初审 **APPROVE WITH COMMENTS**，唯一启动失败退出意见已 RED 复现并修复，新增两例后相关服务/API **45 passed**；详见 [外审记录](review.md)。修复后 Windows CI、CTO、K3 定点复核的最终结论及桌面安装包 SHA-256 记录在交付 PR；不将实现者自查标作独立外审。
