# Filmpaw

Filmpaw 索引多个 NAS 的表演者一级目录, 支持别名与繁简搜索, 并双开本地与选定的 NAS 目录,
便于人工核对。索引默认存入本机 SQLite; 不含云同步、遥测或第三方上传。

> **只读索引** —— Filmpaw 从不复制、移动或删除任何影片文件。它只记录哪些目录存在,
> 并把两个目录同时打开在资源管理器里, 归档动作由你自己完成。

## 功能

- **多 NAS 扫描**: 添加若干 SMB 共享目录, 扫描其一级子目录建立表演者索引; 目录消失只标记失效, 不删记录
- **别名与繁简互搜**: 别名挂在同名组上, `倉木華` / `仓木华` / 自定义别名互相可达; 双向子串匹配, `小红` 能搜到 `小红(仓木)`
- **归档对比**: 左栏列出本地下载目录的一级子目录, 右栏搜索库中记录, 一键**双开**两个资源管理器窗口人工核对
- **表演者海报**: 取各目录下的 `folder.jpg` 生成缩略图, 无图则显示名字首字

## 界面

主界面参见[设计稿](docs/specs/assets/filmpaw-ui-mockup-v5.html)与
[UI 验收矩阵](docs/specs/assets/ui-audit-v1.3.md)。

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
