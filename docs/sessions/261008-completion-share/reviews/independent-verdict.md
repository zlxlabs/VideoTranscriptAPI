verdict: fail
failure-visibility: p2-only

# 独立审查：完成分享回执

## 范围与结论

- 冻结范围：`3a0c8a1be4c6714bd8d528d86ba6114864fd526a..835168a7de72b424fb1adb005182b2e0edf3b5a2`；风险等级：`personal`（`CLAUDE.md:3`）；契约：`docs/sessions/261008-completion-share/design.md`。
- 审查结论为 fail：成功摘要关闭状态的回执分支目前能正确渲染，但没有自动化测试锁定这项 spec 行为，未满足“每项不变式对应代码与测试”的验收纪律。
- 产品交付阻断：否。当前代码的关闭状态探针通过；没有发现 P1 或当前发送行为错误。此 verdict 失败表示审查验证缺口，不表示已证明产品发送失败。
- 未执行生产发送；真实 HTTP E2E 只替换最终 `requests.post`，使用真实 Router、渠道、wecom-notifier 队列和 payload producer。

## Finding

### P2 — 总结关闭状态的回执缺少回归锁

- 触发条件：成功终态快照中 `result.stats.summary_status == "disabled"`。该状态由 `SummaryStatus.DISABLED` 定义（`src/video_transcript_api/utils/llm_status.py:47`），生产结果会写入 `result_stats`（`src/video_transcript_api/api/services/llm_ops.py:808`）。
- Spec：禁用总结时回执应保留 view URL 并给出简明状态提示（`design.md:25`）。实现分支位于 `src/video_transcript_api/utils/notifications/completion_share.py:87-88`。
- 证据：`tests/unit/test_completion_share.py:53-76` 覆盖缺失快照、生成失败、`skip_summary` 和仅校对，但没有 `summary_status="disabled"` fixture；全仓测试检索也没有回执断言覆盖此状态。一次性绝对 fixture 探针当前通过，返回“总结未启用”，但不会锁定后续回归。
- 严重度：P2（当前路径正确，缺少的是持久回归验证）。交付阻断：否；但本卡要求每项不变式有对应测试，故本次验证不能判 pass。后续应补一条关闭状态回执测试。

## OCR finding 裁决

OCR envelope：`status=reviewed`、`coverage=complete`、MiniMax 主腿、1 条确认 finding，退出码 0。OCR 认为总结非空但渲染无 `<p>` 时回执无状态提示属于静默遗漏；本审查驳回该 finding：spec 明确无段落时返回空串且不编造首段（`design.md:12,31`），网页 `transcript.html:1159-1175` 也在没有 `<p>` 时只保留标题和 URL。新增 fixture `test_completion_share.py:11-24` 锁定“只有标题”时提取空串，回执与网页行为一致。

## 不变式核对

| 不变式 | 代码 | 锁定测试/证据 |
|---|---|---|
| 每任务一行 outbox；inline helper 与 dispatcher 不双发 | `api/services/terminal_status.py:175-207,376-403` | `test_terminal_notification_outbox.py::TestOutboxSchema::test_task_id_unique_constraint`；`::TestSerialDeliveryIsExclusive::test_r12_helper_and_dispatcher_never_double_send`（5 轮） |
| 成功先完整正文后回执；一组两次显式成功才算渠道成功 | `utils/notifications/channel.py:308-334,368-395`；`utils/notifications/wechat.py:338-372` | `test_notification_e2e_delivery.py::test_success_snapshot_reaches_final_http_payloads_in_order`；`test_notification_router.py::TestCompletionReceiptGroupResults` |
| `notify_via` 不丢回执参数，SQLite producer 快照字节进入最终双渠道 payload | `api/services/terminal_status.py:210-262`；`api/services/transcription.py:1005-1018` | `test_notification_e2e_delivery.py::test_process_transcription_notify_via_reaches_both_real_channels`；同文件断言 SQLite 快照 UTF-8 字节和 HTTP JSON payload |
| 首段取同源 HTML 首个 `<p>` 可见文本；无段落为空，不截第二段 | `utils/notifications/completion_share.py:10-55` | `test_completion_share.py::test_extracts_visible_text_from_first_rendered_paragraph`；`::test_receipt_uses_original_url_exact_view_url_and_first_paragraph` |
| 缺失快照显式降级，不因确定性缺失永久重试 | `api/services/terminal_status.py:265-301`；`completion_share.py:72-77` | `test_terminal_notification_outbox.py::TestSuccessWithoutPersistedResultDegrades` |
| 成功 3 条、失败 2 条；不恢复进度推送；完整正文不截断 | `channel.py:203-240`；`completion_share.py:58-104` | `test_notification_budget.py::test_success_paths_prepare_full_summary_and_share_receipt`、`::test_large_generic_download_sends_no_progress_notification`；真实 payload E2E 断言完整两段正文 |
| 禁用总结提示 | `completion_share.py:87-88` | **缺少自动化回归测试**；当前一次性探针通过（见上方 P2 finding） |

新增 `completion_share.py` 的解析与格式化服务于回执这一功能；success renderer 由 inline 和 outbox replay 两个入口调用。差异没有新增持久化状态、配置、模型请求、fallback、业务层截断或分卡；现有队列 True 仍只表示 enqueue。

## 独立验证

| 命令/验证 | 退出码 | 结果 |
|---|---:|---|
| `timeout 1200s ocr-review --repo <worktree> --from 3a0c8a1... --to 835168a... --audience agent --concurrency 4 --background-file <摘要>` | 0 | `reviewed`、`coverage=complete`；stdout JSON 已保存到 `/tmp/vta197-ocr.R6b1dH/stdout.json` |
| `UV_PROJECT_ENVIRONMENT=$PWD/.venv make test` | 2 | 唯一失败为未改动的 `tests/unit/test_cache_task_recovery.py::TestDrainNonTerminalTasksOnShutdown::test_deadline_budget_stops_early_and_leaves_remainder_non_terminal`，0.428 秒超过 0.3 秒阈值；主干基线不可用，继承红状态未能判定 |
| 卡面 7 个指定 pytest 文件（`uv run pytest -o addopts='' -q ...`） | 0 | 119 passed |
| 3 个并发/时序 node id 连续 5 轮 | 0（每轮） | 每轮 3 passed；逐轮输出保存在 `/tmp/vta197-verify.494a1n/stress-round-{1..5}.log` |
| `scratch-worktree.sh <主仓> 3a0c8a1... -- ...test_process_transcription_notify_via_reaches_both_real_channels` | 1（预期） | 仅拷入冻结 H0 E2E 文件后由 `AssertionError` 失败（只收到 2 个 payload，断言期望 4）；无 ImportError，证明断言对本次实现有约束力 |
| 禁用状态绝对 fixture 探针 | 0 | 收到“总结未启用”、原始 URL 和绝对 view URL；该探针不是持久测试 |
