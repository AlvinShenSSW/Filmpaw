# Filmpaw App — 完整设计文档 (待操作者评审)

日期: 2026-07-24 · 状态: **v3 — 操作者评审通过 (2026-07-25)** · 语言: 中文
已按本文档 §10 建 GitHub issues → 全部经 AFK 执行。

## 0. 执行与评审链 (操作者已指定)

- 所有实现任务经 **AFK** 跑:每 issue 走完整瀑布(design→TDD→CI→评审→merge)
- 评审链: **CTO 自审 = Claude (cto-pr-review) → 外门 = Codex (codex-review) → 终审 = GLM 5.2 (kilo-review, 操作者显式选定的实验性 final gate)**
- Kimi 不在本项目主链;若 GLM 不可用 → 降级运行并按 AFK 规则向操作者报告
- merge 策略: **`merge-when-green`**(操作者已确认: CI 绿 + 三层评审全过 → 自动合并)
- **Issue 前置门**: issues 建立后,先由 **Codex 评审 issue 本身**(拆分粒度、依赖、验收标准、与本设计的一致性),
  发现的问题修正到 issue 后,**才允许开跑 AFK**。

## 1. 定位

跨多台 NAS 的表演者文件夹**索引** + 新下载电影的**归档对比**助手(Windows 桌面 app)。
App 只负责"找到并同时打开两边文件夹";拷贝/删除由操作者在 Explorer 手动完成。

## 2. 已拍板的决策

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 同名表演者跨 NAS | **各算一条** — 一行 = 一个 NAS 文件夹,名字可重复 |
| D2 | 重扫时文件夹消失 | **标记失效保留**(置灰,不删,可手动清理) |
| D3 | 技术栈 | **照搬 MDCx 结构**(Tauri 2 + React + Python FastAPI sidecar) |
| D4 | 模糊搜索 | **双向子串 + 繁简归一**(zhconv;倉木華↔仓木华互搜;"小红"↔"小红(仓木)"双向命中, 见 §4) |
| D5 | 别名 | **同名记录自动共享**(查询层实现: 命中别名→取 name→返回全部同 name 记录) |
| D6 | SMB 权限 | 跟随主机 Windows 会话凭据,app 不做账密管理 |
| D7 | 代码位置 | 本仓库 `app/` 目录,与 `skill/` 共存 |
| D8 | 评审链 | CTO=Claude, 外门=Codex, 终审=GLM 5.2 (见 §0) |
| D9 | 头像 | 表演者根目录 `folder.jpg` → 生成缩略图**存数据库**;没有则显示名字**首字**头像;**显示为竖版 poster 比例(约2:3), object-fit cover** |

## 3. 用户流程 (핵心三条)

### 流程 A — 初始建库
1. 首次启动 → 表演者库为空,显示空态引导"去设置添加扫描源"
2. 设置页粘贴 `\\Ant\Video Station\女优VI\` → 添加时探测可达性 → 入 sources
3. 按"扫描" → 枚举一级子目录 → 每个文件夹一条 performer 记录 → 返回统计(新增/更新/失效)
4. 重复 2-3 直至所有 NAS 源录入;以后定期"全部重扫"

### 流程 B — 日常归档 (核心场景)
1. 打开"归档对比" → 左侧选本地下载目录(记住上次路径)
2. 左列表显示一级子文件夹(倉木華 / 小红 / 小C…)
3. 点"倉木華" → 右侧自动以归一化子串匹配 name+alias → 显示 2 条(Ant 与 EAGLE 各一)
4. 点某条"双开" → 同时弹出两个 Explorer 窗口(本地 + NAS)→ 操作者手动比对拷贝/删除
5. 右侧无匹配 → 显示空态 + 手动搜索框(可能用了艺名,搜别名)→ 仍无 → 说明是新人,操作者自行处理
6. 处理完一个,点左侧下一个文件夹,重复

### 流程 C — 别名维护
1. 表演者库搜到某人 → 点"+ 别名" → 行内输入 → 回车保存
2. 同名其他记录自动共享该别名(D5, 查询层)
3. 别名 chip 上 × 删除;重名/空白输入拒绝并提示

## 4. 数据模型 (SQLite)

```sql
CREATE TABLE sources (
  id INTEGER PRIMARY KEY,
  unc_path TEXT NOT NULL UNIQUE,      -- \\Ant\Video Station\女优VI\  (标准化: 尾部补\)
  label TEXT,                          -- 可选自定义名, 默认取路径末段
  last_scan_at TEXT                    -- ISO8601, NULL=从未扫描
);
CREATE TABLE performers (
  id TEXT PRIMARY KEY,                 -- uuid4, 界面显示前4位短哈希
  name TEXT NOT NULL,                  -- 文件夹名, 可重复 (D1)
  name_norm TEXT NOT NULL,             -- normalize(name), 建索引
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  folder_name TEXT NOT NULL,
  unc_path TEXT NOT NULL UNIQUE,       -- \\Ant\Video Station\女优VI\倉木華
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  is_missing INTEGER NOT NULL DEFAULT 0,
  thumb BLOB,                          -- D9: folder.jpg 缩略图 (JPEG, 最长边 256px, q80), NULL=无
  thumb_mtime REAL                     -- 源 folder.jpg 的 mtime, 未变则跳过重生成
);
CREATE INDEX idx_performers_name_norm ON performers(name_norm);
CREATE TABLE aliases (
  id INTEGER PRIMARY KEY,
  performer_id TEXT NOT NULL REFERENCES performers(id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  alias_norm TEXT NOT NULL,
  UNIQUE(performer_id, alias_norm)
);
CREATE INDEX idx_aliases_alias_norm ON aliases(alias_norm);
```

- `normalize(s) = zhconv_to_simplified(lower(unicodedata.normalize('NFKC', s.strip())))`
  (NFKC 顺带处理全角/半角、全角括号→半角;大小写只影响拉丁字母)
- **匹配规则 = 双向子串** (对 name 与 alias 逐一判):
  `hit = rec_norm.contains(q_norm) or (len(rec_norm) >= 2 and q_norm.contains(rec_norm))`
  - 正向: 搜"小红" → 命中库里"小红(仓木)" (记录含搜索词)
  - 反向: 左侧文件夹叫"小红(仓木)" → 命中库里"小红" (搜索词含记录名; 记录名≥2字才反向, 防单字名噪音)
- 删除 source → 级联删其 performers(设置页删除时二次确认,提示影响人数)

## 5. 扫描算法

```
scan(source):
  if not os.path.isdir(source.unc_path): 返回 SOURCE_UNREACHABLE (不动任何记录)
  disk = { 一级子目录名 }                       # os.scandir, 忽略文件/隐藏/系统目录
  db   = { 该 source 的 performers }
  for name in disk - db.folder_names: 新建记录 (uuid, now, is_missing=0)
  for name in disk & db.folder_names: last_seen_at=now, is_missing=0
  for rec  in db 且 rec.folder_name not in disk: is_missing=1   # D2, 不删
  for rec  in 在盘记录:                                          # D9 缩略图
    jpg = rec.unc_path + '\folder.jpg'
    if jpg 存在 且 mtime(jpg) != rec.thumb_mtime:
        rec.thumb = Pillow 缩略图(最长边256, JPEG q80); rec.thumb_mtime = mtime(jpg)
    elif jpg 不存在: rec.thumb = NULL; rec.thumb_mtime = NULL
    # 读取/解码失败 → 保留原值并记 warning, 不中断扫描; 失效记录不动其 thumb
  source.last_scan_at = now
  返回 {added, refreshed, missing} 计数
```

- 同步执行(千级规模秒回);"全部重扫"逐源顺序执行,单源失败不影响其余
- NAS 离线 ≠ 失效:探测失败整源跳过并在 UI 提示"源不可达",记录保持原状

## 6. API 契约 (FastAPI, 127.0.0.1 随机端口)

```
GET    /api/sources                        → [{id, unc_path, label, last_scan_at, performer_count, reachable}]
POST   /api/sources        {unc_path,label?} → 201 | 409(重复) | 422(不可达, 带 detail)
DELETE /api/sources/{id}                   → 204 (级联删 performers)
POST   /api/sources/{id}/scan              → {added, refreshed, missing} | 503(不可达)
POST   /api/scan-all                       → [{source_id, ok, added, refreshed, missing | error}]

GET    /api/performers?q=&include_missing= → 分页列表(含 has_thumb 布尔); q 走归一化子串匹配 name+alias (D4/D5)
GET    /api/performers/{id}/thumb          → image/jpeg (Cache-Control 按 thumb_mtime) | 404(无 → UI 显示首字头像)
POST   /api/performers/{id}/aliases {alias}→ 201 | 409(该记录下重复)
DELETE /api/aliases/{id}                   → 204
DELETE /api/performers/{id}                → 204 (单条删除, UI 仅对失效行提供入口)
POST   /api/performers/purge-missing       → {deleted} (批量清理全部失效记录)
POST   /api/performers/{id}/open           → 打开 Explorer → 204 | 404(路径已不存在→顺带置 is_missing)

GET    /api/local/subdirs?path=            → 本地目录一级子文件夹名列表 (归档界面左栏)
POST   /api/open-pair {local_path, performer_id} → 双开两个 Explorer → 204
GET    /api/settings / PUT /api/settings   → {last_local_dir}   # 记住上次本地目录
```

- 打开文件夹: `subprocess.Popen(['explorer', path])`;UNC 直接可用 (D6)
- TS client 由 OpenAPI 生成 (hey-api, 同 MDCx 链路)

## 7. UI/UX 设计

### 7.1 总体
- MDCx 式 **52px 左侧图标导航**: 表演者库 / 归档对比 / 设置
- 色: 主橘 `#EF9F27`(当前页标识、主按钮), 深橘 `#BA7517`(次级强调/图标), 白底, 灰系文字分级
- 中文 UI;MUI 7 组件;窗口默认 1200×800, 最小 960×640

### 7.2 表演者库 (首页)
- 工具栏: 搜索框(实时过滤, 300ms debounce) · "显示失效"开关(默认开) · 全部重扫按钮
- 表格列: 头像(竖版 poster ≈34×48px 圆角, D9) / ID(短哈希, hover 显全) / 名字 / 别名(chips + 行内"+ 别名") / 位置(UNC, 中段省略, hover 显全) / 状态(●在线 ○失效) / 操作
- 头像: 有 thumb → 缩略图 object-fit cover;无 → 名字首字(橘底 #FDF3E3 深橘字 #B45E14);失效行头像随行降透明度
- 操作列: 打开文件夹;**失效行额外显示删除按钮(带确认)**;工具栏加"清理失效"批量按钮(确认 + 数量提示)
- 失效行整体置灰; 打开按钮对失效行仍可点(可能只是上次扫描时离线)
- 状态: 空库空态(引导去设置) · 搜索无结果空态
- 底栏: "共 N 条 · M 个来源"

### 7.3 归档对比
- 左栏(固定 260px): 目录选择按钮(tauri dialog) + 当前路径 · 一级子文件夹列表(单选高亮橘)
  - 记住上次目录, 启动自动载入; 目录不存在→提示重选
- 右栏: 搜索框(选中左项自动填入, 可手改; 清空=不显示结果) · 命中数提示 · 结果卡片列表
- **左右独立**: 手动修改搜索词**不清除**左侧选中 — 双开永远配对「左侧当前选中的本地文件夹 + 所点卡片的 NAS 文件夹」, 与搜索词无关(场景: 左选"小红", 手动搜出"小小白", 双开 = 本地小红 + NAS小小白)
- 左侧未选中任何文件夹时, 双开按钮禁用(tooltip "先选择左侧本地文件夹")
  - 卡片: 头像(竖版 poster ≈52×74px, D9, 同首字回退, 比列表更大便于认人) / 名字 + 别名(灰) / UNC 路径 / 状态 / **双开**按钮(失效条禁用双开, 提示"文件夹已失效")
  - 无匹配空态: "库中无此人 — 试试手动搜索别名, 或这是新人"
- 双开成功后卡片短暂高亮反馈, 不弹 toast 轰炸

### 7.4 设置
- 源管理: 添加输入框(校验 UNC 格式+可达性, 失败红字提示) · 源列表行(路径/人数/上次扫描/在线态 · 扫描 · 删除(确认弹窗, 提示将删 N 条记录))
- 扫描中: 行内 spinner + 完成后"新增 x · 更新 y · 失效 z"摘要
- 其他: DB 路径显示(只读) · 版本号

### 7.5 Mockups
三主屏设计稿已评审(会话内 widget, 2026-07-24);补充态(空态/扫描中/无匹配)见同日第二组 widget。

## 8. 技术结构与构建

```
app/
├─ src-tauri/      # Tauri 2 (Rust): 窗口壳, sidecar 启动/停止, externalBin=filmpaw-server
├─ ui/             # React 19 + MUI 7 + TanStack Router/Query + Zustand + rsbuild + biome
└─ server/         # Python 3.13 + FastAPI + sqlite3(标准库) + zhconv + pillow(缩略图); uv 管理; PyInstaller 打包
```

- server 启动: 绑 127.0.0.1:0 → 实际端口写 stdout → Tauri 读取注入 UI (同 MDCx sidecar 模式)
- DB: `%APPDATA%\Filmpaw\library.db`, 启动时建表(schema_version 表预留迁移)
- 打包: PyInstaller onefile → src-tauri/binaries → NSIS 安装包
- 测试: pytest(server 全覆盖: 扫描/归一化/API) · UI typecheck+biome · CI: GitHub Actions windows-latest

## 9. 明确不做 (v1)

文件拷贝/移动/删除 · 视频元数据/封面/缩略图 · 爬虫刮削 · SMB 凭据管理 · 多用户 · 自动更新 · macOS/Linux

## 10. Issue 拆分预案 (评审通过后建, 每个 = 一次 AFK 波次)

| # | Issue | 依赖 |
|---|---|---|
| 1 | Scaffold: app/ 三件套骨架 + 构建链打通 (tauri dev 能起, sidecar hello) | — |
| 2 | Server: DB schema + normalize + sources CRUD + 扫描引擎 (纯逻辑+测试) | 1 |
| 3 | Server: performers/aliases/搜索 API + 打开文件夹/双开 | 2 |
| 4 | UI: 设置页 (源管理+扫描) | 3 |
| 5 | UI: 表演者库 | 3 |
| 6 | UI: 归档对比 | 3 |
| 7 | 打包: PyInstaller + NSIS + CI 发布产物 | 4,5,6 |

依赖呈波次: 1 → 2 → 3 → (4,5,6 并行或顺序) → 7。
