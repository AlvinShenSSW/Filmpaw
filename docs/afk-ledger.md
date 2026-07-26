# AFK 执行台账 — Filmpaw App (issues #1-#8)

> 本文件是 AFK 运行的唯一进度真相源。每个实质步骤(开分支/开PR/过评审/合并)后更新并随代码提交。
> 每个 cron tick 先读本文件 + `gh issue list` + `gh pr list` + `git branch -a` + `git status`,从第一个未完成步骤继续。

## 运行参数 (操作者授权, 2026-07-25)

- Scope: issues #1-#8, 波次 **1 → 2 → (3∥4) → (5,6) → 8 → 7**, 不越界
- Merge 策略: **merge-when-green** (评审链全过 + 检查绿 → 自动合并; #7 之前"绿"=本地 pytest/typecheck/biome 全过并在 PR 记录)
- 评审链: CTO 自审(Claude) → 外门 Codex (`codex exec --sandbox read-only`, **stdin 必须 `< /dev/null`**) → **终审 Kimi** (`node skill/kimi-review/kimi-gate.mjs`); 某 gate 不可用 → 降级运行并在 PR + 报告标注 (2026-07-25 操作者变更: GLM 超时不收敛退出主链)
- 每 issue: 分支 `issue-N-<slug>` off origin/main → 轻量设计段(写入 PR 描述) → TDD → 提交推送 → PR → CTO 自审 → Codex 外门(批量修) → GLM 终审 → merge → 更新台账
- 遵守 AGENTS.md「CLI invocation discipline」防挂规则
- 停机: 队列完成 → CronDelete + 最终报告; 连续 2 tick 无实质进展 → CronDelete + 状态报告 + STOP

## 进度

| Issue | 状态 | 分支 | PR | 备注 |
|---|---|---|---|---|
| #1 Scaffold + 全局壳 | **merged** ✅ | issue-1-scaffold(已删) | [#9](https://github.com/AlvinShenSSW/Filmpaw/pull/9) | 全评审链过: CTO 3fix / Codex 1P2 / GLM 两轮(R1 5项修, R2 无P1) → squash 合并 |
| #2 Server 扫描引擎 | **merged** ✅ | issue-2-scan-engine(已删) | [#10](https://github.com/AlvinShenSSW/Filmpaw/pull/10) | 27 tests; CTO/Codex×2/GLM×2 全链, GLM R1 抓到 P1 脏事务, R2 APPROVE WITH COMMENTS |
| #3 Server API | **merged** ✅ | issue-3-performers-api(已删) | [#11](https://github.com/AlvinShenSSW/Filmpaw/pull/11) | 42 tests; CTO 2fix / Codex 1驳回 / Kimi R1 1P1+2 R2 APPROVE-WC |
| #4 UI 设置页 | **merged** ✅ | issue-4-settings-ui(已删) | [#12](https://github.com/AlvinShenSSW/Filmpaw/pull/12) | Codex P1 CORS / Kimi P1 biome-CI + 类型化API; vitest 设施落地 |
| #5 UI 表演者库 | **merged** ✅ | issue-5-performers-ui(已删) | [#13](https://github.com/AlvinShenSSW/Filmpaw/pull/13) | Codex P1 清理范围+P2 分页; Kimi 错误浮现; 14 UI tests |
| #6 UI 归档对比 | **merged** ✅ | issue-6-archive-ui(已删) | [#14](https://github.com/AlvinShenSSW/Filmpaw/pull/14) | Codex CLEAN; Kimi 5轮(symlink逃逸/锚点/TOCTOU/服务端join); R5 无P1 收敛 |
| #8 来源筛选 | **merged** ✅ | issue-8-source-filter(已删) | [#15](https://github.com/AlvinShenSSW/Filmpaw/pull/15) | Codex CLEAN×2连; Kimi 失效重置; 26 UI tests |
| #7 打包+CI | **merged** ✅ | issue-7-packaging(已删) | [#16](https://github.com/AlvinShenSSW/Filmpaw/pull/16) | Codex CLEAN×3连; Kimi 3条修; CI 三跑收敛(CRLF→绿→冒烟绿); NSIS 产出+打包冒烟 |

## 决策日志

- (tick 0) AFK 由操作者在原会话显式启动(/afk 不可用因 skill 装于会话启动后), 按 SKILL.md 规范内联执行。
- (tick 0, #1) pnpm 11 allowBuilds 占位符导致自动 install exit 1 → app/ui/pnpm-workspace.yaml 置 core-js: false。
- (tick 0, #1) tauri CLI 必须从 app/ 调用(src-tauri 须为 cwd 子目录), 已写入 src-tauri/README。
- (tick 0, #1) CTO 自审改法: eval 注入换 initialization_script(防页面加载竞态); server stdout 读端口后持续排水(防 print 填满管道死锁); stderr inherit。
- (tick 0, #1) e2e 冒烟通过×2: filmpaw.exe + rsbuild:3000 + sidecar python 同时在跑。
- (tick 0, #1) GLM R2 无 P1 → 收敛合并。驳回: Unix树杀(§9不做)/py3.13(§8选型)/预置依赖。延后至 #7: panic UX、动态 CSP、Job Object 硬杀加固。
- (tick 0, gate) kilo CLI 评审完后 suggest 工具 schema bug 死循环→gate 超时收尾, verdict 需从 transcript 提取。kilo-gate 已加 --auto + fail-loud 写保护。
- (#2) Codex 外门: P1 sqlite 并发竞态→全 handler 串行锁+并发回归测试; P2 大小写改名重复建档→casefold 匹配。
- (gate) kilo review 固定起 6 轨多 agent 流程, 10 分钟工具上限不够 → 解法: detached 进程 + KILO_REVIEW_TIMEOUT_MS=1500000 + Monitor 监听。后续终审一律用此模式。
- (#2) GLM P1: 扫描异常未回滚→共享连接脏事务被后续 commit 持久化(毁库级)。已修+回归测试。延后: off-lock 缩略图解码、格式预检。
- (2026-07-25) 操作者指令: 终审 GLM→Kimi。GLM 两次超时(25/40min 皆死在 6 轨汇总), kilo 孤儿泄露已修。#3 起终审 = Kimi。
- (2026-07-25) 断流根治: 常驻 Monitor 心跳(20min)上线, session cron 降级为参考。规则: 每轮收尾必留在途唤醒源, 心跳兜底。
- (#4) 教训: 管道 `| tail` 会吞掉退出码 — 检查命令一律显式验证 $? 或不接管道。openapi 漂移防护+本地端点加固已转 #7。

## 🏁 运行结束 (2026-07-25 14:3x)

队列 #1-#8 全部 merged+closed。调度器已清理(cron d09476ca + 心跳 monitor)。
最终报告见会话记录。遗留加固项集中记录于 issue #7 正文(v1.1 批次)。

## v1.1 运行 (2026-07-25 操作者实测反馈, 4 issues)

- Scope: **#17 console窗口 → #18 目录不可访问bug → #19 源路径选择器 → #20 UI/UX打磨(必须 ui-ux-pro-max)**
- 流程同 v1.0: issue 先过 Codex 前置门 → 逐 issue 瀑布(CTO→Codex→Kimi, merge-when-green)
- 心跳 monitor 已重启; #20 依赖 #17-#19 先行(UI 打磨最后做避免冲突)

| Issue | 状态 | 分支 | PR | 备注 |
|---|---|---|---|---|
| #17 console 窗口 | **merged** ✅ | issue-17-no-console(已删) | #21 | CREATE_NO_WINDOW; Codex CLEAN/Kimi APPROVE; 冒烟3/3 |
| #18 目录不可访问 | **merged** ✅ | issue-18-local-dir-bug(已删) | #22 | 取证排除server; UI错误浮现修复; Codex盘符根/Kimi多分隔符; 31 tests |
| #19 源路径选择器 | **merged** ✅ | (已删) | #23 | 共享helper; Kimi 4轮收敛; 38 tests |
| #20 UI/UX 打磨 | **merged** ✅ | (已删) | #24 | ui-ux-pro-max 采纳; Kimi 6轮 A11y 硬核; token 零 hex; 43 tests |

## v1.1 运行结束 (2026-07-25)
队列 #17-#20 全部 merged。调度器已清理。重新打包新安装包中。

## v1.2 运行 (2026-07-25, issue #25)

| Issue | 状态 | PR | 备注 |
|---|---|---|---|
| #25 归档本地目录刷新 + 版本 0.2.0 | **merged** ✅ | #26 | 前置门 BLOCK→PASS(版本范围); Codex 外门 CLEAN; Kimi P2 stale-closure→APPROVE |

产物: Filmpaw_0.2.0_x64-setup.exe · 冒烟: 版本一致(health/openapi 0.2.0)、无黑窗、关窗零残留。调度器已清理。

## v1.3 运行 (2026-07-25, issues #28 优先 + #27)

- **#28** [BUG] 含逗号文件夹名双开失败 — 根因已实测复现: explorer 把逗号当参数分隔符, Python list2cmdline 仅对空格加引号 → 路径在逗号处截断 → 目标不存在 → explorer 回退到"文档"。修法: os.startfile(ShellExecuteW), 已验证正确。
- **#27** 头像太小 → 表演者库改 poster 网格(110×156, 3.2×) + 归档卡片 156×222 + 缩略图 512 + schema 1→2 真迁移 + 版本 0.3.0
- **评审链临时变更**: Codex 服务端 503 熔断(`biscuit_baker_service_me_circuit_open`, 非用量/非调用方式问题; CLI 已升 0.144.3→0.145.0 无效)→ 操作者指令: **外门改 GLM 5.2 (kilo)**, 终审仍 Kimi。Codex 恢复后切回。
- (2026-07-25) 评审链恢复: Codex 503 熔断约 40 分钟后自愈(探针验证), 外门切回 Codex。熔断期间 GLM 完成了 issue 前置门(质量高: #28 抓到 os.startfile 会把 204 变 500, #27 抓到迁移非原子 + NULL 谓词导致旧图永不重建), 但对 PR diff 评审 25 分钟仍未产出(读文件阶段超时)——结论: **GLM 适合 issue 级评审, 不适合 diff 级评审**。

| Issue | 状态 | PR | 备注 |
|---|---|---|---|
| #28 逗号路径双开 | **merged** ✅ | #29 | explorer 逗号截断 → os.startfile; GLM P1 保留 fire-and-forget |
| #27 头像放大 3x | **merged** ✅ | #30 | 网格 110×156 / 卡片 156×222 / 缩略图 512 / schema v2 迁移; Kimi 3 轮咬住缓存链(ETag+max-age)——否则升级后仍显示旧糊图 |

产物: **Filmpaw_0.3.0_x64-setup.exe** · 冒烟: 版本 0.3.0 一致(health/openapi)、open-pair 新契约、无黑窗、关窗零残留。
真实旧库迁移验证: v1+256px → 自动迁移 v2 → 重扫 → 512px 重建。调度器已清理。

## v1.3 运行结束 (2026-07-25)

## v1.4 运行 (2026-07-26, issue #31)

**#31 [BUG] 打开文件夹后 Explorer 不前置, 只在任务栏闪烁**

根因(已取证): Windows 只允许前台进程 / 收到最近输入事件的进程 / 由前台进程启动的进程激活窗口。点击时这些权利属于 `filmpaw.exe`, 但 `ShellExecuteW` 一直由 **sidecar** 调 —— 它一个条件都不占。修法: 打开动作移到 Tauri 壳, server 保持路径权威(壳只接受意图参数, 从不接受路径)。

### 前置门(Codex, gpt-5.6-sol): BLOCK ×3 → PASS WITH FIXES
1. 通用 `open_path(path)` 会让 WebView 绕过 containment → 改为壳只收 id/subdir, 回调 server 取路径
2. 我把路由(`/api/open-pair` 写成 `/performers/{id}/open-pair`)、body(漏了 `local_dir`)、错误矩阵(单开只有 404, 双开才 400/404/409)**全写错了** —— Codex 逐条核对代码抓出
3. 验收标准"窗口可见且非最小化"**不等于前置** —— 会把本缺陷判成通过
4. 采样算术错: 250ms × 连续 8 样本只跨 1.75s, 非 2s

### 验收(人工, 不可由无头测试替代)
| | 单开 | 双开 |
|---|---|---|
| 修复前(装机版 0.3.0) | **0/3** | **0/3** |
| 修复后 | **3/3** | **3/3** |

关键: 每次基线试验都**存在可见且未最小化的目标窗口**。若验收停在"有可见窗口", 六次基线会全部误判为通过 —— 这是 Codex 那条 P1 的价值所在。判定改为"每个稳定样本的 `GetForegroundWindow()` 解析后等于目标路径"。

探针 [`tools/foreground-probe.ps1`](../tools/foreground-probe.ps1) + 四组逐样本记录 [`docs/evidence/issue-31/`](evidence/issue-31/)。点击用 `SendInput` 注入真实输入 —— UIA 的 `InvokePattern` 不产生输入事件, 前台锁不触发, 基线会假通过。

### 教训
- **本地环境"干净不了"会掩盖 CI 失败**: 新增的 `cargo test` 步骤在 CI 挂掉, 真因是 `binaries/filmpaw-server-*.exe` 尚未构建(PyInstaller 步骤排在其后); 本地测不出来是因为目录里躺着上次打包的 sidecar。Kimi 报的是同一现象但归因到 `ui/dist` —— 照它的修法加 `pnpm build` 会继续挂。**结论对不等于归因对。**
- **CI 里此前完全没有 cargo**: 新增的失败边界矩阵本来不会被执行。
- **openapi 漂移检查对格式敏感**: 必须用 CI 的原样命令(`indent=1`, 无末尾换行)导出。
- **Kimi 的"工作线程 + 网络延迟超出激活窗口"实测不成立**: 独立实验延迟 0/500/1000/3000/5000/**8000**ms 全部成功激活。前台权利由"收到最近输入事件"决定, 不是会过期的时间窗。**复审提出无法复现的问题时, 先做实验再决定采纳与否。**

### 评审模型固定(操作者指令, 2026-07-26)
`CODEX_REVIEW_MODEL` 永久 = **gpt-5.6-sol**, 由 `codex-gate.mjs` 硬钉, **脱钩**于 `~/.codex/config.toml`(其默认为 gpt-5.6-terra) —— 否则为交互调一次参数就会静默改变外门判定的模型来源。手工 `codex exec` 评审也必须带 `-m`。

### 顺带处理: 真实库被测试夹具覆盖
`%APPDATA%\Filmpaw\library.db` 一度只剩早先 E2E 的夹具数据(4 个源指向 `Temp\fp20nas_*`)。杀掉全部进程 + 将夹具库改名备份后, 原 7.7MB 真实库(10 源 / 196 条 / 缩略图)完整恢复, **零丢失**(别名 0 条)。夹具库保留为 `library.db.fixture-2026-07-25.bak` —— 同名文件不该共存, 恢复机制未查清, 故不删。
**规则**: 任何 E2E/打包验证必须显式设 `FILMPAW_DB` 指向临时库, 绝不落默认路径。
