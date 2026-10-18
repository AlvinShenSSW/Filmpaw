# Issue #38 外审记录

操作者指定 Codex 实现、Kimi K3 唯一外门，`leave-open`，不是默认双外审的服务降级。

初版 HEAD `7093e7115e2eaaac00ba5ee795093a5c8e17aa7b` 的 [Windows CI](https://github.com/AlvinShenSSW/Filmpaw/actions/runs/39111172082) 全绿后，CTO 实现者自查给出 **APPROVE WITH NON-BLOCKING COMMENTS**：无结构阻塞，保留文档已说明的会话历史、ADS 并发窗口和厂商 NAS 验证边界。没有重复运行已经通过的全套测试。

Kimi 实际调用 `kimi-code/k3`，关闭 stdin、15 分钟硬超时、只读审查 #38 相对 PR #37 的增量；本次正常返回结论。唯一外审意见为 **APPROVE WITH COMMENTS**：

> No blocking deletion-scope, data-integrity, concurrency, or stale-result issues found. One lifecycle edge:
>
> Low — failed worker startup can break shutdown — cleanup.py:264-269, 348-349. If Thread.start() raises, self._worker still references a never-started thread. A later close() calls join() on it, raising RuntimeError; through app.py:41-43, this can skip transfer-service and database shutdown. Clear the worker reference on startup failure or record successful startup and only join started threads. Add a start-failure/close regression test.

接受此意见。回归测试只注入 `Thread.start()` 失败，保留真实 Thread 对象与 join，先复现 `cannot join thread before it is started`；修复在启动失败分支清空工作器引用，仍发布失败结果、释放写操作准入，不执行删除。另验证同一失败请求不重放，新的显式请求仍可正常完成。

修复后的 CI、CTO 和同一 Kimi 会话的定点复核结论，连同最终安装包 SHA-256，记录在 [PR #39](https://github.com/AlvinShenSSW/Filmpaw/pull/39)，避免为追加最终状态反复触发仅文档构建。
