# 实施进度

## 2026-10-08：设计与执行边界复核（resume dispatch `dlg-20261008-033438-64ae2b`）

- **阶段**：设计完成，待先提交设计后开始测试。
- **结论**：续派已明确授权仅修改 `src/video_transcript_api/api/services/transcription.py::_TaskNotifier.notify_task_status` 的签名与 `completion_receipt` 转发；其余转录流程不碰。沿用分支 `lead/lead-261008-16080b63`，基线仍为 `3a0c8a1be4c6714bd8d528d86ba6114864fd526a`，现场 clean。
- **决策**：普通成功每渠道 3 条逻辑消息（已接收 + 完整正文 + 分享短回执），失败每渠道 2 条；完整版先于短回执入既有通知队列；不改 schema/CAS/网页、不新增重试/发送框架。
- **顺序证据**：上一派发的只读核查已确认 `wecom-notifier` 0.3.1 企业微信与飞书在每 webhook 上各自使用 FIFO 单工作线程；当前派发仍必须通过真实 Router、channel、依赖队列和最终 HTTP JSON 替身验证顺序，旧核查不能替代 E2E。
- **否决**：不以顶层调用顺序证明最终 HTTP 顺序；不 mock `_TaskNotifier` 遮蔽参数传递；不通过反射、兼容 fallback 或 `completion_body` 暗协议绕开签名。
- **下一步**：先提交本设计、旧 N1/预算同步和本进度记录；然后新增真实 `process_transcription → _TaskNotifier → Router → 双渠道最终 HTTP` 红测试及首段提取/队列顺序测试；实现最小签名转发与共用回执渲染后小步提交。

## 2026-10-08：生产路径 E2E 与首段契约红测试

- **阶段**：TDD 红测试已落盘，待提交测试 checkpoint。
- **结论**：新增 `test_process_transcription_notify_via_reaches_both_real_channels`，从 full-cache `process_transcription` 真实运行到局部 `_TaskNotifier`、真实 Router/双 channel、依赖 FIFO 队列及替身外部 HTTP JSON；同时增加即时/outbox 两投递入口的 SQLite 序列化快照与顺序契约，以及 HTML 首段提取契约。
- **红证据**：`test_completion_share.py` 当前因生产 helper 尚未建立而 collection 报 `ModuleNotFoundError`；真实 `process_transcription → notify_via` E2E 已到达两渠道 HTTP consumer，但旧实现仅捕获每渠道一个最终 JSON（2 条），验收期望完整正文 + 回执的每渠道两条（4 条），在 `wait_for_payloads(4)` 断言失败。
- **决策**：只 mock HTTP POST；不 mock `notify_via`、Router、channel 或终态服务。`process_transcription` 使用已有缓存命中分支，避免额外网络/模型服务。
- **下一步**：提交红测试；增加共享首段/回执构造器与终态贯通，再给 `_TaskNotifier` 加最小签名参数转发，使真实 E2E 变绿。

## 2026-10-08：首个实现单元通过

- **阶段**：最小实现与首批回归通过，待提交。
- **结论**：新增 `completion_share.py`，将总结经既有 Markdown renderer 渲染后提取第一个 HTML `<p>` 的可见文本；两个终态投递入口均从快照生成完整正文和短回执。WeCom/Feishu channel 先提交完整消息、成功后再提交回执；整组结果要求两次提交均显式成功。已将 receipt 参数贯通 `terminal_status → _TaskNotifier → Router → WeCom/Feishu`。
- **验证**：完整 E2E 已通过真实 `process_transcription → _TaskNotifier → Router → 两渠道依赖 FIFO 消费者 → 最终 HTTP JSON 替身`，并覆盖即时与补发入口/SQLite 快照；窄测命令 119 passed（154 上游 deprecation warnings）。
- **决策**：保留现有单行 outbox、CAS 和至少一次重放窗口；不引入业务截断、预切分或发送 ledger。失败正文/缺失结果可见性继续由既有降级处理。
- **下一步**：提交此已通过单元；随后完善失败组接受条件/并发反复跑，完成 `make test` 和三项修复后红验，更新进度并开 draft PR。

## 2026-10-08：整卡验证与三项有效红验完成

- **阶段**：实现与本地验证通过；准备提交验证存档并发布 draft PR。
- **结论**：完整短测通过（119 passed）；`make test` 从头到 100% 退出码 0，输出含 3 个 skip、无失败（pytest 本地配置未打印总用例数）；真实 Router/channel/FIFO/最终 HTTP E2E 通过即时、补发和真实 `process_transcription → notify_via` 路径。
- **并发验证**：包含 outbox helper/dispatcher 互斥、slow-alert 入队顺序、期限与终态竞态的 3 个时序测试，连续 5 轮全部通过（每轮 3 passed）。
- **修复后红验**（每次均先有真修复提交，只临时改坏一个最小代码块；全部失败为 AssertionError，之后精确还原）：
  1. 交换 WeCom 完整正文/回执入队顺序：真实 producer E2E 报 `assert PERSISTED_SUMMARY in complete_body`，完整摘要错误地出现在短回执。
  2. 在 `build_task_status_content` 丢弃完整正文：同一真实 HTTP E2E 报 `assert PERSISTED_SUMMARY in complete_body`，payload 只包含 `[summary omitted]`。
  3. 首段提取故意返回错误文字：`test_receipt_uses_original_url_exact_view_url_and_first_paragraph` 报精确回执断言不等，期望 `第一段 重点。`、实收 `wrong excerpt`。
- **决策/守卫**：三次注入后仅恢复各自那一处，`git diff` 显示源代码无注入残留；不改 CI、测试守卫、依赖或生产环境。同步移除旧设计残留的“总结与完成合并”过时句。
- **验证环境**：本地 worktree `.venv` / CPython 3.11.15；pytest outbound guard 屏蔽非 loopback 网络，所有平台 HTTP 都由最终 `requests.post` 替身拦截；不承诺真实平台收信。
- **下一步**：提交本进度与旧文档同步；确认远端 head/PR 状态后只推本卡分支，创建 draft PR，并回查 URL、number、headRefName 与 CI 结论。
