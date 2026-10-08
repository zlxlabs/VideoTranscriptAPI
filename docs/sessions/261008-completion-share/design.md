# 设计：完整总结之后追加任务完成与分享摘要回执（261008-completion-share）

## 用户目标与相对旧方案的更新

用户将 261007 的“总结与完成合为一条”精简决策更新为：普通成功任务在企业微信、飞书依次收到「已接收 → 完整 AI 总结 → 任务完成与分享摘要」。成功预算由旧方案每渠道 2 条改为 3 条；不恢复下载、转录、缓存等进度推送。失败仍为已接收后追加一条 ❌。

## 成功消息格式与共用快照

每条成功终态仍只有一条 `task_terminal_notifications` outbox 记录。每个目标 channel 对该记录按顺序提交两条逻辑消息：

1. **完整完成正文**：沿用 `_render_completion_body` 的现有正文规则，含既有统计、状态/校对警告、模型信息及完整总结/校对正文。业务层不截断、不预拆卡；超长仍交由原通知库处理。
2. **完成分享短回执**：统一任务抬头 `✅ [#短编号] 标题`，非空时保留原始地址，放入现有 view URL 和可分享首段。首段为空时不编造文本；缺快照则显式提示总结未能载入并保留可点击 URL。

示例：

```text
✅ [#abc123] 视频标题
原始地址：https://example.com/video/123

总结和校对：https://example.com/view/view_token

总结的第一段简介（非整份长总结）。
```

URL 继续使用 task 的 view token 与既有 `get_base_url`；不暴露本地路径/内部 token。原始 URL 为空时省略。校对-only、禁总结、总结生成失败时不编造首段；回执保留 view URL 和简明状态提示，完整版继续保留现有状态与警告内容。

完整版与短回执均从同一个 SQLite `terminal_snapshot.result` 生成。即时投递、outbox 补发、`notify_via` 包装器调用同一规则。生产转录入口的 `_TaskNotifier.notify_task_status` 需只增加 `completion_receipt` 参数并向 Router 原样转发；不改其余转录流程。

## 首段提取规则

严格对应网页 `transcript.html` 的 `readSummaryFirstParagraph()`：对总结 Markdown 使用网页同源的 `render_markdown_to_html`，取渲染结果第一个 `<p>`，提取其可见文本并 trim。不得使用 Markdown 第一行替代。覆盖标题开头、粗体/斜体、链接文字、中文、多段和无 `<p>` 情况；没有段落时返回空串。不新增 LLM 请求或网页行为。

## 队列顺序与 outbox 语义

每个渠道的 `notify_task_status` 先提交完整正文，再提交短回执。现场已检查安装依赖 `wecom-notifier` 0.3.1：企业微信和飞书各有按 webhook 的 FIFO 队列及单工作线程；worker 完成当前消息所有分段后才消费后一条。因此同一调用线程先 enqueue 完整正文再 enqueue 回执，可保证同一 webhook 上完整版（包含它的全部库内分段）先于回执实际消费。E2E 必须使用真实 Router、真实渠道、真实依赖队列和消息生产者，仅替换外部 HTTP POST，并捕获最终 JSON body 顺序；不得以顶层 mock 调用顺序代替。

单渠道整组只有在完整版和回执都显式返回 True 时才返回 True。完整版提交失败时不提交回执；任一条失败则该 channel 整组 false。Router 仍按至少一个 channel 整组成功接受 outbox；否则保留现有重放窗口和至少一次交付语义，不新增 ledger/CAS/schema。底层异步接口 True 只表示已提交队列，不表示平台实际收信；本卡不真实发生产通知。

失败终态只送既有失败通知，不送完整版/回执。快照缺失、非 dict、空 dict 继承既有 `COMPLETION-BODY-MISSING` 日志与可见降级；内容确定性缺失仍允许 outbox 标记已发送，不因它永久重试。即时/补发并发继续由既有 `_DELIVERY_LOCK` 和 pending 检查互斥。

## 测试不变式

| 条件 | 代码 | 锁定测试 |
|---|---|---|
| 每任务仅一行 outbox；成功完整正文先于回执；失败一条 | `terminal_status.py`、`channel.py`、`wechat.py` | outbox 与 E2E payload tests |
| `notify_via` 参数不截断 | `transcription.py::_TaskNotifier.notify_task_status` | 真实 `process_transcription → notify_via → Router → 双渠道 HTTP` E2E |
| 即时与补发从 SQLite producer 快照字节重建相同两条消息 | `terminal_status.py` 两个投递入口 | 参数化 E2E + SQLite 字节断言 |
| 首段来自 HTML 首个 `<p>` 可见文本 | `utils/notifications/completion_share.py` | `test_completion_share.py` |
| `summary_status="disabled"` 时保留“总结未启用”、原始地址与 view URL，不复制总结正文 | `utils/notifications/completion_share.py` | `test_completion_share.py::test_disabled_summary_receipt_keeps_urls_without_summary_paragraph` |
| 最终企业微信 markdown_v2 与飞书 interactive payload 顺序/正文完整 | 真实 Router/channel/依赖 FIFO | `test_notification_e2e_delivery.py` |
| 3 条成功/2 条失败预算，不恢复进度推送 | 现有任务处理与终态通知入口 | `test_notification_budget.py` |
| 缺失快照三态可见降级、不永久卡住 | 终态 renderer 与回执 renderer | `test_terminal_notification_outbox.py` |

## 旧文档同步

`docs/sessions/261007-notify-slim/design.md` 中“成功总结与完成合并为一条”、每渠道成功预算 2 条、N1 每任务恰好一条逻辑终态消息的旧说法需要更新：仍为每任务一行 outbox；成功终态两条有序逻辑消息；正常任务合计 3 条，失败合计 2 条。网页分享行为、渠道选择、终态 CAS、数据库 schema 与现有至少一次语义不变。
