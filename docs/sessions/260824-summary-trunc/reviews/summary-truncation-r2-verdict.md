# summary-truncation R2 独立审查 verdict（对抗视角）

## 审查范围与结论

- 审查对象：`f4ab77c4ad7d630ce2c1c1d23a5e44649f40ed74..5fd47cad624acedddfaf0f697250e79fb99f9845`（H0 冻结）。
- 风险等级：`personal`；P1 红线 = 数据丢失、静默出错、崩溃。
- 本轮新证据（相对 R1 正向核验）：① 落盘/通知/导出/缓存命中旁路穷举；② 超预算完整首答 × 压缩重试截断的运行探针；③ 空响应重试把 effort 改成 disabled 却继承外层 `max_tokens=None` 的源码探针；④ 新增测试的改坏注入红验（含 peek 清槽假绿对照）；⑤ 多快照 peek/pop 与 `usage_missing` 粒度探针。
- 结论：`pass-with-backlog`。无 P1。两条 P2 为 fail-loud / 守卫漏检边界，接受不修不阻塞合并；P3 与任务卡已知 backlog 保留。

## Findings 分级清单

### P1

无。

P1 两问总判定：本轮新证据没有找出「本轮截断/FAILED 响应被当成 GENERATED 落盘或发出」的可达旁路；失败路径返回 `SummaryStatus.FAILED` 且 `text=None`，查看页走失败文案。能找到的缺陷要么是把一份**已经完整**的超长首答一起丢掉（报得出来），要么只在 disabled + 空响应重试等窄组合上漏检截断。

### P2

1. **超预算完整首答被压缩重试截断连坐 FAILED**
   - 位置：`SummaryProcessor.process()` 在 `len(working_text) > hard_cap` 后的压缩重试里，`_is_truncated(sent_max_tokens, retry_response.usage)` 为真时直接 `return FAILED`，丢掉已经通过截断守卫的 `working_text`。
   - 违反不变式：**3**（`over_budget_accepted` 仅适用「超长但完整」——此处首答已完整且超字数硬顶；压缩重试的截断文本不应 GENERATED，但完整首答应走既有 over_budget_accepted，与「压缩重试抛异常仍保留首答」不对称）。
   - 运行探针：disabled、`completion_tokens=1000` 的 4380 字完整首答 + 压缩重试 `completion_tokens == max_tokens(6120)` → `status=failed, text=None`，日志 `summary_truncated_failed: compression retry truncated`。修复前同路径 `Exception` 会 `summary_over_budget_accepted` 并保留首答（`test_over_hard_cap_retry_failure_keeps_first`）。
   - P1 两问：真实使用下会被触发吗？**会**（仅 `reasoning_effort=="disabled"` 才下发 `max_tokens`；首答超 `hard_cap` 但 token 未触顶、压缩重试触顶）。后果能否接受？**可接受**：fail-loud「生成失败」，不是把截断文本当成功保存，也不是覆盖已有成功产物的静默错。不升 P1。
   - 处置：接受不修（可用 backlog；修法是压缩重试截断时回落到 `working_text` 的 over_budget_accepted，而不是整段 FAILED）。

2. **思考形态空响应重试强制 disabled，却仍不发 max_tokens**
   - 位置：`llm.py::_call_with_text_output` 第 2 次尝试起 `effort="disabled"`，但 `chat_kwargs["max_tokens"]` 仍用外层传入值；`SummaryProcessor` 按**原始** `reasoning_effort` 算出 `sent_max_tokens=None`（默认配置 `summary_reasoning_effort: "high"`）。
   - 违反不变式：**1**（disabled 形态的实际 provider 往返应为 `hard_cap×1.5`；该次重试在形态上已是 disabled）。
   - 源码探针：`empty_retry_reuses_chat_kwargs_max_tokens=True` 且 `effort_forced_disabled_on_retry=True`。截断守卫看的是外层 `sent_max_tokens`，对这次 disabled 重试恒为不判截断。
   - P1 两问：真实使用下会被触发吗？**偶发**（模块文档写明思考模型会空响应重发）。后果能否接受？**大体可接受**：后验字数硬顶仍在；是否被 provider 默认 cap 截断取决于网关，不是本 diff 能钉死的。不升 P1。
   - 处置：接受不修。若修，应在空响应重试改 effort 时同步 `resolve_summary_max_tokens(budget, "disabled")`，或让截断守卫看到该次实际下发的 `max_tokens`。这是对「总结调用」粒度的落实，不是重开锁定决策 1。

### P3 / 接受不修

1. **新增 `test_text_response_includes_usage_snapshot` 对 peek 不清槽契约假绿**
   - 它 `@patch peek_chat_result_usage`，把 peek 实现改成清槽后该测试仍绿；同一注入下既有 `tests/unit/test_llm_usage_capture.py::test_two_attempts_within_one_call_are_summed_not_last_write_wins` 与 `test_text_mode_records_usage` 变红。
   - 违反不变式：**4** 的*测试锁定强度*，不是生产行为（审计求和仍被旧测试锁住）。
   - P1 两问：不触发生产静默错。接受为测试保险不足。

2. **多快照里任一次 `usage_missing` 即整次不判截断**
   - `_usage_from_snapshots` 设 `usage_missing=len(known)<len(snapshots)`，`_is_truncated` 见 `usage_missing` 直接 False。探针：`(missing, last.completion_tokens==max_tokens)` → `is_truncated=False`，尽管返回文本对应的最后一次已知 usage 已触顶。
   - 贴近锁定不变式 **2**「usage 缺失不误报」，粒度过粗：最后一跳有 usage 且 `>= max_tokens` 时本可判截断。触发面是 disabled + 文本空响应重试/json 自纠正（总结默认走文本模式）且首轮不报 usage。
   - P1 两问：组合偏窄，不升 P1。不重开「缺失不误报」决策，记粒度问题。

3. 任务卡已知 backlog 原样保留，不重复升级：扫描脚本无法区分思考烧 token 的成功调用与真截断；`usage_missing=True` 路径缺测试 pin；「重试未截断但 <50 字」缺测试 pin。

4. R1 已接受的扫描脚本 stdout JSON+TSV 混排、模板未 pin pending/disabled/skipped_short，本轮不复读。

## 五项攻击面逐项结论

### 1. 截断文本残留落盘路径

**明确排除「本轮截断/FAILED 响应被当 GENERATED 消费」的旁路。**

穷举结果：

| 通路 | 本轮 FAILED/截断文本去向 |
| --- | --- |
| `SummaryProcessor.process` | 截断重试失败返回 `text=None, status=FAILED`；截断正文只留在内存中的 `LLMResponse.text`，不作为 `SummaryResult.text` |
| `coordinator._generate_summary_if_needed` | 原样转发 `SummaryResult`；`summary_text=None` |
| `llm_ops._build_result_dict` | `"内容总结"=None`，`skip_summary=True`，`summary_status=FAILED` |
| `_save_llm_results` | `FAILED` 分支不写 `llm_summary.txt`，不把校对文本伪装成总结 |
| `_send_notification` | `skip_summary=True` 走校对文本分支，文案「生成失败」，通知体不含截断总结 |
| `transcription.py` 缓存全命中 | 首次 FAILED 无文件 → `has_llm_summary=False`，`need_summary=True` 会再跑；不会把本轮 FAILED 响应写成 generated |
| `ViewTokenResolver._resolve_summary_state` | 非 GENERATED 返回 `(status, None)`，查看页不渲染失败态的文件内容 |
| `audit.py` 摘要预览 | 仅 `summary_state==GENERATED` 才返回文本 |
| 模板浓缩提示 | 增加 `summary_state == 'generated'`，失败态测试锁死不出现「内容高度浓缩」 |
| 分享按钮 | `{% if summary_html %}`，失败态无 html |
| `?raw=summary` / export | 只读磁盘文件；首次 FAILED 无文件 → 404。**未**按 `summary_state` 设门（与 notes 不同），但这只会在「历史上已有 generated 文件、后来又 FAILED」时吐出**旧成功稿**，不是本轮截断正文 |

存量形态（非本 diff 引入、不占用本轮 P1）：FAILED 不删除已有 `llm_summary.txt`；`need_summary` 只看文件是否存在。查看页仍以 status 为准不展示旧稿。记 backlog 即可。

### 2. 既有行为回归

| 路径 | 结论 |
| --- | --- |
| 思考形态不发 `max_tokens` | 守住。`resolve_summary_max_tokens` 仅 disabled 返回 `budget.max_tokens`；默认 `None`/`high` 均不下发。测试已 pin。 |
| 超长压缩重试原语义 | **部分回归**：压缩重试 **Exception** 仍保留首答（测试绿）；压缩重试 **截断** 改为整段 FAILED（见 P2#1）。字数硬顶、取短者、over_budget_accepted 在「重试未截断」时仍工作。 |
| disabled 预算 | 守住。`hard_cap×1.5` 测试绿；S/M/L 曲线与 1.5 系数未改。 |
| `skipped_short` | 未改早退；coordinator 前置检查口径一致。 |
| 通知文案 | FAILED 仍走 skip_summary +「生成失败」，不含截断正文。 |
| 缓存命中 | 首次 FAILED 不落盘，不会被当成层已满足。 |

空响应重试把思考调用改成 disabled 却不补发 `max_tokens`：见 P2#2。不是思考模型「无上限生成」主路径的破坏（主路径按锁定本就不发），而是 disabled 重试没享受到 disabled 预算。

### 3. 测试假绿（改坏注入）

三条新增生产断言在改坏后变红；一条新增测试对 peek 清槽假绿，但旧审计测试能抓住。详见下方红验记录。不构成「新增测试恒真」。

### 4. 多快照 usage 桥接

**排除跨调用残留；记录粒度问题为 P3。**

- `LLMClient.call` 入口 `pop` 预清理 + `finally` 再 `pop` 记审计。探针：`FatalError` 后 `peek_chat_result_usage()==()`。
- `peek` 不清槽：连续两次 peek 长度同为 2，pop 后为 0。
- `_usage_from_snapshots`：prompt/total **求和**、completion 取 **最后一次已知**、`usage_missing` 只要有一跳缺失就 True。审计 `_record_usage` 仍对全部快照求和（既有 `test_two_attempts_within_one_call_are_summed_not_last_write_wins` 锁死）。peek 不改变 pop 契约。
- 总结默认文本模式；json_object Self-Correction 不走总结。文本空响应重试才会在同一次 `call_llm_api` 里追加多快照。
- 线程池：`SummaryProcessor` 单线程；contextvars 槽按线程隔离。校准线程池的 copy_context 不在本 diff。未见 peek 窗口内嵌套 `LLMClient.call`。
- 漏检：任一次 missing 则不判截断（P3#2）。

### 5. 扫描脚本与文档

**SQL 注入排除；配置注释与实现一致。**

- `scripts/scan_truncated_summaries.py` 的 `since` / `min_completion_tokens` 走 `conn.execute(..., (since, min_completion_tokens))` 参数绑定，用户输入不进 SQL 文本。`--db` 只是本地 sqlite 路径，owner 诊断脚本，非注入面。
- 已知 backlog（思考成功 vs 真截断分不清）仍成立，脚本只是 `stage='summary' AND completion_tokens >= 2400`。2400 = S 带 L=800 时 `hard_cap=1600 × 1.5`，与实现一致。
- `config.example.jsonc` 注释改为「max_tokens 仅对 `reasoning_effort == "disabled"` 下发（hard_cap × max_tokens_multiplier）；思考模型不下发」。与 `should_send_max_tokens` / `resolve_summary_max_tokens` 一致；S/M/L 键与 1.5 系数未改。

## 红验注入记录

改坏前工作树干净（H0）。每次确认注入行后再跑测试；还原只还原改坏处。结束时 `git diff` 为空。

| # | 注入点 | 生效证据 | 目标测试 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `summary_processor.py` `_is_truncated`：`>=` 改 `>`，哨兵 `# RED-VERIFY` | `sed -n '236p'` → `return usage.completion_tokens > sent_max_tokens  # RED-VERIFY` | `test_first_truncation_retry_still_truncated_fails`、`test_first_truncation_retry_success` | **红**（等值触顶被当成 GENERATED，200 字 x 落盘） |
| 2 | `transcript.html` 去掉 `and summary_state == 'generated'`，哨兵 `{# RED-VERIFY omit generated guard #}` | `grep -n RED-VERIFY` → line 372 | `test_failed_summary_state_omits_condensation_hint` | **红**（失败态出现「内容高度浓缩」）；`test_generated_...` 仍绿 |
| 3 | `should_send_max_tokens` → `return False  # RED-VERIFY never send max_tokens` | `sed -n '99p'` | `test_should_send_max_tokens_only_for_disabled`、`test_resolve_summary_max_tokens`、`test_disabled_reasoning_effort_sends_max_tokens` | **红**（`None != 6750` / `None != 6120`） |
| 4 | `peek_chat_result_usage` 增加 `_chat_usage_log.set(())  # RED-VERIFY peek clears slot` | `grep -n RED-VERIFY` → usage_context.py:182 | `test_text_response_includes_usage_snapshot` | **仍绿（假绿）** |
| 4b | 同上 | 同上 | `test_two_attempts_within_one_call_are_summed_not_last_write_wins`、`test_text_mode_records_usage` | **红**（审计变成 `usage_missing=1` 全 0）。不变式 4 由旧测试锁死，不靠新增 mock 测试 |

## 全量测试

```
cd ~/projects/personal/VideoTranscriptAPI-worktrees/summary-trunc-r2
uv run python -m pytest tests/llm tests/unit -q
```

- exit code: `0`
- 进度到 `[100%]`；仅既有 pydantic/lark 弃用告警与少量测试函数返回非 None 告警。

对抗探针（`PYTHONPATH=src uv run python`，不入库）：probe1 确认 P2#1；probe2/5 确认 peek 不清槽、异常路径无残留；probe3 确认响应 usage 取最后一次 completion、prompt/total 求和；probe4 确认 P3#2；probe6 确认 P2#2。

## 熵增（本轮不重复 R1 表）

本 diff 新增抽象均已在 R1 回答过第二消费者/单消费者必要性。本轮未发现新的无消费者包装层。不新增 P2。

## 交付物

只新增本文件，未修改源码、测试或配置（红验均已还原）。路径：

`docs/sessions/260824-summary-trunc/reviews/summary-truncation-r2-verdict.md`
