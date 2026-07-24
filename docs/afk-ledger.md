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
| #1 Scaffold + 全局壳 | in-progress | issue-1-scaffold | — | tick 0 启动 |
| #2 Server 扫描引擎 | pending | — | — | |
| #3 Server API | pending | — | — | |
| #4 UI 设置页 | pending | — | — | 依赖 #2, 可与 #3 并行 |
| #5 UI 表演者库 | pending | — | — | |
| #6 UI 归档对比 | pending | — | — | |
| #8 来源筛选 | pending | — | — | 在 #5 之后 |
| #7 打包+CI | pending | — | — | 收口 |

## 决策日志

- (tick 0) AFK 由操作者在原会话显式启动(/afk 不可用因 skill 装于会话启动后), 按 SKILL.md 规范内联执行。
