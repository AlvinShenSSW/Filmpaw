# AFK 执行台账 — Filmpaw App (issues #1-#8)

> 本文件是 AFK 运行的唯一进度真相源。每个实质步骤(开分支/开PR/过评审/合并)后更新并随代码提交。
> 每个 cron tick 先读本文件 + `gh issue list` + `gh pr list` + `git branch -a` + `git status`,从第一个未完成步骤继续。

## 运行参数 (操作者授权, 2026-07-25)

- Scope: issues #1-#8, 波次 **1 → 2 → (3∥4) → (5,6) → 8 → 7**, 不越界
- Merge 策略: **merge-when-green** (评审链全过 + 检查绿 → 自动合并; #7 之前"绿"=本地 pytest/typecheck/biome 全过并在 PR 记录)
- 评审链: CTO 自审(Claude) → 外门 Codex (`codex exec --sandbox read-only`, **stdin 必须 `< /dev/null`**) → 终审 GLM (`node skill/kilo-review/kilo-gate.mjs`, `KILO_REVIEW_MODEL=zai-coding-plan/glm-5.2`); **Kimi 不参与**; 某 gate 不可用 → 降级运行并在 PR + 报告标注
- 每 issue: 分支 `issue-N-<slug>` off origin/main → 轻量设计段(写入 PR 描述) → TDD → 提交推送 → PR → CTO 自审 → Codex 外门(批量修) → GLM 终审 → merge → 更新台账
- 遵守 AGENTS.md「CLI invocation discipline」防挂规则
- 停机: 队列完成 → CronDelete + 最终报告; 连续 2 tick 无实质进展 → CronDelete + 状态报告 + STOP

## 进度

| Issue | 状态 | 分支 | PR | 备注 |
|---|---|---|---|---|
| #1 Scaffold + 全局壳 | **merged** ✅ | issue-1-scaffold(已删) | [#9](https://github.com/AlvinShenSSW/Filmpaw/pull/9) | 全评审链过: CTO 3fix / Codex 1P2 / GLM 两轮(R1 5项修, R2 无P1) → squash 合并 |
| #2 Server 扫描引擎 | **merged** ✅ | issue-2-scan-engine(已删) | [#10](https://github.com/AlvinShenSSW/Filmpaw/pull/10) | 27 tests; CTO/Codex×2/GLM×2 全链, GLM R1 抓到 P1 脏事务, R2 APPROVE WITH COMMENTS |
| #3 Server API | next | issue-3-performers-api | — | Wave 3; #4 可并行(依赖#2已并入main) |
| #4 UI 设置页 | pending | — | — | 依赖 #2, 可与 #3 并行 |
| #5 UI 表演者库 | pending | — | — | |
| #6 UI 归档对比 | pending | — | — | |
| #8 来源筛选 | pending | — | — | 在 #5 之后 |
| #7 打包+CI | pending | — | — | 收口 |

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
