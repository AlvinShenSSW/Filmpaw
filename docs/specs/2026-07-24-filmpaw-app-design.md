# Filmpaw App — 设计文档

日期: 2026-07-24 · 状态: 已评审(操作者拍板全部决策点) · 语言: 中文

## 1. 定位

跨多台 NAS 的表演者文件夹**索引** + 新下载电影的**归档对比**助手(Windows 桌面 app)。
App 只负责"找到并同时打开两边文件夹";拷贝/删除由操作者在 Explorer 手动完成。

## 2. 已拍板的决策

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 同名表演者跨 NAS | **各算一条** — 一行 = 一个 NAS 文件夹,名字可重复 |
| D2 | 重扫时文件夹消失 | **标记失效保留**(置灰,不删,可手动清理) |
| D3 | 技术栈 | **照搬 MDCx 结构**(Tauri 2 + React + Python FastAPI sidecar) |
| D4 | 模糊搜索 | **子串匹配 + 繁简归一**(zhconv;倉木華↔仓木华互搜) |
| D5 | 别名 | **同名记录自动共享** — 给任一条加别名,同名的其他条也能被搜到 |
| D6 | SMB 权限 | 跟随主机 Windows 会话凭据,app 不做账密管理 |
| D7 | 代码位置 | 本仓库(github.com/AlvinShenSSW/Filmpaw),与 skill/ 共存 |

## 3. 数据模型 (SQLite)

```sql
sources    (id INTEGER PK, unc_path TEXT UNIQUE, label TEXT, last_scan_at TEXT);
performers (id TEXT PK,              -- 自动生成唯一 ID (uuid, 界面显示短哈希)
            name TEXT,               -- 文件夹名 = 表演者名, 可重复 (D1)
            source_id INTEGER REFERENCES sources(id),
            folder_name TEXT,
            unc_path TEXT UNIQUE,    -- \\Ant\Video Station\女优VI\倉木華
            first_seen_at TEXT, last_seen_at TEXT,
            is_missing INTEGER DEFAULT 0);          -- D2
aliases    (id INTEGER PK, performer_id TEXT REFERENCES performers(id),
            alias TEXT);
-- D5 实现: 搜索/匹配时按 name 关联 — 命中某别名 → 找到其 performer 的 name
--          → 所有同 name 记录一起返回。别名存储仍挂单条, 共享发生在查询层。
```

搜索归一化: `normalize(s) = zhconv(简体化(lower(trim(s))))`,name 与 alias 均建归一化辅助列或查询时归一。

## 4. 功能

### F1 设置 Tab
- 添加/删除 SMB 源目录(UNC 路径;添加时校验可达)
- 每源显示: 路径、人数、上次扫描时间、在线状态;单源"扫描"按钮 + 全局"全部重扫"
- 扫描逻辑: 枚举源下一级子目录 →
  - unc_path 已存在 → 更新 last_seen_at, 清除 is_missing
  - 新路径 → 新建 performer 记录(即使名字与已有记录相同, D1)
  - 库里有、盘上没了 → is_missing=1 (D2);源不可达 → 整源跳过并提示,不标失效

### F2 表演者库(首页)
- 表格: ID(短哈希)、名字、别名(chips, 可加/删)、位置(UNC)、状态(在线/失效)、打开按钮
- 顶部: 搜索框(名字/别名, 归一化匹配)、全部重扫
- 失效行整体置灰;打开按钮 = explorer.exe 打开 UNC 路径

### F3 归档对比(左右分栏)
- 左: 本地目录选择器 + 该目录一级子文件夹列表,单选高亮
- 右: 搜索框(选中左侧项时自动填入其文件夹名,可手改)→ 归一化子串匹配 name+alias
- 结果卡片: 名字、别名、UNC 路径、"双开"按钮
- 双开 = 同时打开左侧选中的本地文件夹 + 该卡片的 NAS 文件夹(两个 Explorer 窗口)
- 同一人多条记录 = 多张卡片,操作者自选开哪条(D1 的自然结果)

### F4 非功能
- Windows 10/11;SMB 走当前会话凭据 (D6)
- DB: `%APPDATA%\Filmpaw\library.db`
- 规模: 千级记录;扫描同步全量即可,无需增量/并发优化
- UI 中文;色调 橘黄 #EF9F27 / 白 / 灰(MDCx 式左侧图标导航栏)

## 5. 明确不做 (v1)

文件拷贝/移动/删除 · 视频元数据/封面/缩略图 · 爬虫刮削 · SMB 凭据管理 · 多用户

## 6. 技术结构 (对齐 MDCx)

```
Filmpaw/
├─ skill/            # 既有 AFK/评审 skills (不动)
├─ app/
│  ├─ src-tauri/     # Tauri 2 壳 (Rust), sidecar = filmpaw-server
│  ├─ ui/            # React 19 + MUI 7 + TanStack Router/Query + Zustand + rsbuild
│  └─ server/        # Python FastAPI + sqlite3 + zhconv, PyInstaller 打包 sidecar
└─ docs/specs/       # 本文档
```

- UI ↔ server: OpenAPI, hey-api 生成 TS client (同 MDCx)
- 打开文件夹: server 调 `explorer.exe <unc>`;本地目录选择: tauri-plugin-dialog
- 端口: server 起 127.0.0.1 随机端口,Tauri 注入给 UI

## 7. UI 设计稿

三屏 mockup 已评审通过(会话内 widget): 表演者库 / 归档对比 / 设置。
要点: 52px 左侧图标栏;橘黄仅用于当前页标识与主操作(全部重扫/双开/添加源);
失效行置灰;归档界面左栏选中项橘色高亮,右栏卡片式结果每条带双开。
