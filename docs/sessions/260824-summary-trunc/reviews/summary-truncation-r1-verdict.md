# summary-truncation R1 独立审查 verdict

## 审查范围与结论

- 审查对象：`f4ab77c4ad7d630ce2c1c1d23a5e44649f40ed74..5fd47cad624acedddfaf0f697250e79fb99f9845`。
- 风险等级：`personal`；冻结上界为 H0 `5fd47cad624acedddfaf0f697250e79fb99f9845`。
- 结论：`pass-with-backlog`。
- 本轮没有发现 P1 或 P2 阻塞项；已知 backlog 与新增 P3 测试/脚本建议接受不修，不阻塞合并。

## Findings 分级清单

### P1

无。

P1 两问总判定：在真实使用方式下，没有发现会把本次截断响应静默当作新生成总结落盘、导致崩溃或造成数据丢失的触发路径；审查到的失败路径均返回明确的 `SummaryStatus.FAILED` 或不写总结文件，因此后果可接受。

### P2

无。

### P3 / 接受不修

1. OCR 标注为 low：`scripts/scan_truncated_summaries.py` 同时向 stdout 输出逐行 JSON 和制表格，若调用方把 stdout 当稳定 JSON 流解析会失败。人工判断：这是诊断脚本输出契约/易用性问题，不影响总结生成、截断判定、状态迁移或落盘；真实使用方式下不是 P1，接受为 P3 backlog。
2. OCR 标注为 low：模板回归测试只显式覆盖 `failed` 与 `generated`，没有分别 pin `pending`、`disabled`、`skipped_short`。人工判断：生产条件已经是精确的 `summary_state == 'generated'`，其余状态均不会渲染提示；遗漏的是测试覆盖而非当前行为，违反不变式 5 的测试锁定强度但不违反实现行为，接受为 P3 backlog。
3. 任务卡已知 backlog 保留原分诊：扫描脚本不能区分思考 token 成功调用与真截断；`usage_missing=True` 路径缺测试 pin；“重试未截断但小于 50 字”路径缺测试 pin。它们不在本轮重复升级，均不阻塞。

上述 P3 的两问答案均为：真实使用下可能触发，但后果是诊断可读性或测试保险不足，不会把截断总结作为本轮 `GENERATED` 结果保存，因而不达到 personal 风险等级 P1 红线。

## 五条不变式核验

1. **预算参数**：`resolve_summary_max_tokens()` 只在 `reasoning_effort == 'disabled'` 时返回 `budget.max_tokens`；其他形态返回 `None`。`llm.py` 的文本调用只有在值非 `None` 时才把 `max_tokens` 放进 provider 请求。S/M/L 预算和 1.5 系数未改。
2. **截断代理判定**：`SummaryProcessor._is_truncated()` 先要求已下发 `sent_max_tokens`，再拒绝 `usage is None`、`usage_missing` 或缺失 `completion_tokens`，最后使用 `completion_tokens >= sent_max_tokens`，等值会判截断。
3. **失败终态与落盘**：首次截断只进入一次 `_retry_after_truncation()`；重试仍截断、过短或异常时返回 `SummaryResult(text=None, status=SummaryStatus.FAILED)`。`_save_llm_results()` 只有 `summary_status == GENERATED` 且文本非空才写总结文件；FAILED 分支明确跳过总结文件，不会把截断文本作为 `GENERATED` 保存。超预算接受分支只接收已通过截断守卫、仍有完整文本的结果。
4. **usage 桥接审计契约**：`LLMClient.call()` 用 `peek_chat_result_usage()` 构造响应 usage，`finally` 仍由 `_record_usage()` 使用 `pop_chat_result_usage()`；后者继续对全部快照求和落库。多快照手工探针确认：响应给截断判定使用最后一个已知 completion 值，而审计记录 prompt/completion/total 仍分别对全部快照求和。
5. **浓缩提示**：模板条件已经是 `show_summary_stats and summary_percentage <= 20 and summary_state == 'generated'`；失败态的模板测试确认不会出现“内容高度浓缩”提示，generated 态仍保留提示。

## 降层审查三问

### 1. FAILED 写入前的不可逆动作

这里的 FAILED 是 `SummaryStatus.FAILED`，不是任务总状态 `TaskStatus.FAILED`：总结失败仍可伴随校对成功，任务总流程可以成功结束，但总结状态如实为 failed。

在 `SummaryProcessor` 返回 FAILED 之前，最多已经发生：首次总结 LLM 外部调用、一次语义重试调用，以及每次 `LLMClient.call()` 的 usage 审计记录；这些是必要且不可逆的外部调用/审计动作。截断文本仍只在内存中，不在此阶段落盘。

随后 `_save_llm_results()` 会按既有产物流程撤销旧 `llm_status.json`、保存校对产物，并在总结分支跳过 `llm_summary.txt`，最后写入 `summary_status=failed`。总结失败路径本身不发送“总结成功”通知；成功通知只在任务 success CAS 成功后执行，并由 `skip_summary`/失败状态走校对文本和“生成失败”文案。没有看到截断总结文本先通知后失败的旁路。

### 2. 截断守卫值在真实部署形态下是否可靠

`max_tokens` 是本地计算的 hard cap × 1.5，仅 disabled 形态下下发；`completion_tokens` 来自 llm-compat 返回的 `ChatResult.usage`，由 usage_context 在每次真实 provider 往返后追加快照。一次调用内多次 provider 往返时，响应 usage 使用最终已知快照的 completion 值，审计仍保留全部快照的总和。

守卫对 usage 缺失、网关不上报或多快照中任一快照缺失采取保守不判截断，避免误报；代价是无法识别该类环境下的真截断。该取舍符合冻结不变式 2，但它不是 provider `finish_reason` 的强保证，生产网关若丢失 usage 会产生漏检而不是把正常结果误判失败。

### 3. 防线覆盖“写入”还是“行为”，是否存在旁路

防线覆盖两层：处理器层在检测到截断后不返回文本，状态层在 `_save_llm_results()` 只允许 GENERATED 写总结文件。审查了协调器结果适配、缓存保存分支、查看页状态解析、缓存命中通知路径：新一次处理没有另一条可以绕过 `SummaryProcessor` 直接把响应文本写入 `llm_summary.txt` 的旁路；缓存读取路径只读取既有产物，并受 `summary_status` 约束，不会把本次 FAILED 响应重新当成 generated。

仍需注意 usage 缺失时按锁定决策“不判截断”，因此不可观测 provider 的不完整文本在信息论上无法被这条代理守卫识别；这属于已锁定的保守取舍，不作为本轮 P1 重开。

## 熵增审查

逐项回答“第二个消费者是谁 / 单消费者是否必要”：

| 新增抽象 | 第二个生产消费者 | 单消费者必要性判断 |
| --- | --- | --- |
| `LLMUsage` | 无；当前由 `SummaryProcessor._is_truncated()` 消费，测试只是假实现 | 必要。它是 `LLMClient` 到处理器的类型化 usage 边界，避免把快照桥接细节泄漏到处理器；删除会破坏截断判定所需的响应元数据。
| `should_send_max_tokens` | 无；由 `resolve_summary_max_tokens()` 调用 | 必要但范围窄。它把冻结决策“仅 disabled 下发”作为可单测的命名策略，避免在调用方重复比较 `reasoning_effort`。
| `resolve_summary_max_tokens` | 无；当前由 `SummaryProcessor.process()` 调用 | 必要但范围窄。它隔离 `SummaryBudget` 与 provider 参数的 `None` 语义，预算计算不需要知道 reasoning policy。
| `peek_chat_result_usage` | 无；当前由 `LLMClient.call()` 调用 | 必要。它与既有 `pop_chat_result_usage()` 分工，响应读取不清空槽，才能同时满足新截断判定和不改变审计求和契约。
| `_SUMMARY_TRUNCATION_RETRY_SUFFIX` | 无；当前仅截断重试使用 | 必要。它集中保存一次截断重试的完整提示，避免把语义重试文案和普通超预算重试混写；当前没有新增配置或状态。

这些抽象均有当前行为或测试契约上的单消费者必要性，没有发现只为未来复用而引入的包装层、fallback 或额外状态；本维度不产生 P2 finding。

## OCR 对照与人工分诊

OCR 前置扫描真实返回 `status=reviewed`、`profile=minimax`、`coverage=complete`。工具标注不是本仓库最终严重度，逐条核对如下：

| 工具标注 | 本仓判定 | 两问答案 |
| --- | --- | --- |
| medium：表格打印用 `or ''` 会抹掉 0 | 反驳。当前表格列不包含 OCR 所称的 `prompt_tokens`/`total_tokens`/`duration_ms`，且默认筛选 completion token 已高于阈值；不构成当前 diff 的核心缺陷 | 不适用 P1；真实默认运行不触发，后果不可达到数据丢失/静默错结果/崩溃 |
| low：JSON 行后再输出 TSV 表 | 接受为 P3 backlog；是脚本 stdout 契约问题，不影响生产总结行为 | 可能影响把 stdout 当 JSON 流的诊断调用方，但后果可接受，非 P1 |
| low：缺 pending/disabled/skipped_short 模板测试 | 接受为 P3 backlog；条件本身精确屏蔽所有非 generated 状态 | 真实模板行为正确，缺的是保险测试，不会造成截断文本落盘 |
| high：截断失败测试缺 `call_count == 2` | 反驳。测试的第一响应带 `completion_tokens == max_tokens`；若跳过重试，结果会走 GENERATED 而不是 FAILED，现有状态断言已经能使回归变红 | 不触发 P1；工具建议是冗余断言 |
| low：重试测试的 `max_tokens` 局部变量未被使用 | 反驳。OCR 引用的代码与 H0 不一致；当前相关测试确实使用该变量构造 usage，不能据此落 finding | 不触发 P1；事实前提不成立 |

## 运行证据

- `cd ~/projects/personal/VideoTranscriptAPI-worktrees/summary-trunc && uv run pytest tests/llm tests/unit -q`
  - exit code: `0`
  - 末行：`-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html`
  - 结果进度到 `[100%]`；仅有既存依赖弃用告警及少量测试函数返回非 `None` 告警。
- `PYTHONPATH=src uv run python` 手工不变式探针：exit code `0`，输出 `manual invariants passed`；覆盖多快照响应 usage、审计全量求和、`>=` 等值截断、`None`/usage 缺失不判截断。
- `git diff --check f4ab77c4ad7d630ce2c1c1d23a5e44649f40ed74 5fd47cad624acedddfaf0f697250e79fb99f9845`：exit code `0`。

## 交付物与提交证据

本轮只新增本 verdict 文件，未修改源码、测试或配置。提交证据在完成提交后补录于执行器 report.md；本文件路径满足：

`docs/sessions/260824-summary-trunc/reviews/summary-truncation-r1-verdict.md`
