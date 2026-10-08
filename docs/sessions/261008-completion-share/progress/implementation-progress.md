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
