## 2026-10-09 · 首个 worker 单元

- **当前阶段**：implementing；首个独立 worker 单元已通过，尚未提交其余 C 增量。
- **本段结论**：新增真实 dispatcher→持久 admission/media mapping→共享转录函数单元，断言映射文件字节、任务 id、媒体 id 和处理选项；`uv run --frozen pytest -q tests/unit/test_upload_worker.py` 通过。
- **关键决策与已否决方案**：转录输入严格由 SQLite 映射提供，队列 URL 只作展示值；沿用 B 已持久化的处理选项，不加 URL 下载或新处理入口。
- **下一步唯一动作**：提交并推送该单元，然后继续生命周期与终态期限实现。
