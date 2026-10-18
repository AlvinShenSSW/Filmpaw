# Filmpaw

Filmpaw 索引多个 NAS 的表演者一级目录，支持别名与繁简搜索、双开核对，
以及用户主动选择的本地 batch → NAS 整影片文件夹归档。索引与任务记录存入本机 SQLite；
不含云同步、遥测或第三方上传。

> **归档由你发起** —— 扫描和浏览只建立索引。选择具体 NAS 演员、勾选影片并点击传输后，
> 才会复制完整文件夹、校验内容、提交目标并清理源文件。只允许本地到 NAS；
> 同名冲突停止该演员的整组任务，其他独立任务继续；不覆盖、不合并、不自动改名。

## 功能

- **多 NAS 扫描**: 添加若干 SMB 共享目录, 扫描其一级子目录建立表演者索引; 目录消失只标记失效, 不删记录
- **别名与繁简互搜**: 别名挂在同名组上, `倉木華` / `仓木华` / 自定义别名互相可达; 双向子串匹配, `小红` 能搜到 `小红(仓木)`
- **归档对比**: 左栏列出本地下载目录的一级子目录, 右栏搜索库中记录, 一键**双开**两个资源管理器窗口人工核对
- **归档传输**: 明确选定 NAS 演员后，两列目录独立排序，本地多选单向移动；显示预检、复制、校验、清理进度，支持停止和中断结果核对
- **多任务与日志**: 默认同时传输两个演员，更多任务自动等待；任务中心分别显示进度并可独立停止。本机操作日志保留影片传输、源文件／目录删除、空目录清理和失败记录，重启后可查询
- **清理空演员目录**: 在左侧手动清理当前 batch 的一级空演员目录并刷新列表；batch 可以位于本机或 NAS 临时文件夹，其他目录和右侧片库不在清理范围。海报、隐藏文件、空子目录等任何内容都会保留
- **表演者海报**: 取各目录下的 `folder.jpg` 生成缩略图, 无图则显示名字首字

## 界面

主界面参见[设计稿](docs/specs/assets/filmpaw-ui-mockup-v5.html)与
[UI 验收矩阵](docs/specs/assets/ui-audit-v1.3.md)。
归档传输参见[已审阅的交互稿](docs/specs/assets/filmpaw-archive-transfer-v1.html)和
[实现设计](docs/specs/2026-10-18-archive-transfer-design.md)。

## 技术栈

| 层 | 选型 |
|---|---|
| 壳 | Tauri 2 (Rust) — 启动侧车、端口握手、由**前台进程**打开资源管理器窗口 |
| 前端 | React 19 · MUI 7 · TanStack Router/Query · rsbuild · biome · vitest |
| 服务 | Python 3.13 · FastAPI · uvicorn (uv 管理), 绑定 `127.0.0.1` 随机端口 |
| 存储 | SQLite (`%APPDATA%\Filmpaw\library.db`, 可用 `FILMPAW_DB` 覆盖) |
| 打包 | PyInstaller onefile 侧车 + NSIS 安装包, GitHub Actions 构建 |

## 开发

```bash
# 服务端
cd app/server && uv sync && uv run pytest -q

# 前端
cd app/ui && pnpm install && pnpm test && pnpm typecheck

# 壳
cd app/src-tauri && cargo test

# 开发模式(壳 + 前端 + 侧车)
cd app && ui/node_modules/.bin/tauri dev
```

> 跑任何端到端或打包验证前, **必须**把 `FILMPAW_DB` 指向临时库 —— 详见
> [AGENTS.md](AGENTS.md) 的项目铁律。

Tauri 会自动生成每次启动的会话凭据。独立浏览器开发需给服务和 UI 配置相同的
`FILMPAW_SESSION_TOKEN`（64 位十六进制）并开启 `FILMPAW_DEV=1`，详见
[服务开发说明](app/server/README.md)。没有有效凭据时禁止写入，开发模式也不绕过。

归档异常后请先查看逐项结果：**已复制，源清理未完成**表示 NAS 已有完整副本，本地仍有
残留；**待核对**表示退出或掉线导致最后状态尚不能确认。应用不会自动重传或继续删除。
可以双开手动核对，任务暂存目录以 `.filmpaw-transfer-` 开头，不会作为普通影片显示。

“清理空演员目录”只删除完全为空的一级演员目录，batch 根目录保留。结果弹窗只列已清理的
目录名称，任务栏仍统计删除、非空保留、安全跳过和失败；读取失败不会当作空目录。清理与传输互斥，清理中退出需等待
完成。清理状态保存在当前服务会话中，重启不会续删；响应丢失时先查询，应用不会自动重放
删除。清理期间请停止其他程序改动候选目录；附加数据流或无法可靠核验的目录会保留。

归档传输可切换演员继续提交，总等待和运行任务最多 100 个；同一源或目标演员目录及重叠路径
不能重复占用。每个演员内部仍按影片顺序处理，清理空演员目录需等待所有传输结束。任务中心的
「停止全部」只针对本次明确选择的任务；关闭应用时会等待所有任务和实际工作线程安全结束。

操作日志仅保存在本机数据库，不自动清除，按任务、操作及结果分页查询。日志中的「准备执行」
不代表成功；中断或成功回执保存失败会标为待核对，不自动补传或补删。影片提交与源清理分别
记录，部分源清理失败时可查看已确认删除的文件和目录。日志记录从升级后开始，旧清理历史不补造。

## 构建安装包

```bash
cd app/server && uv run python build_sidecar.py
cd app && ui/node_modules/.bin/tauri build
```

产物在 `app/src-tauri/target/release/bundle/nsis/`。

## `skill/` 是什么

[`skill/`](skill/) 是驱动本项目交付的**评审工具链**(AFK 自主执行 + Codex / Kimi / GLM /
MiMo 多方评审门), 不是本仓库的主体。它们如何协作见 [AGENTS.md](AGENTS.md); 每轮的实际
执行记录见 [docs/afk-ledger.md](docs/afk-ledger.md)。
