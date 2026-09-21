# vta-terminal-notify 进度

## 里程碑 1

- 当前阶段：implementing / 里程碑 1 完成
- 本段结论：13 个终态写入点的状态通知收敛到 `finalize_terminal_status_and_notify`。`llm_ops.py` 的 `finally` 无条件发送与 `transcription.py` worker 顶层兜底忽略 CAS 返回值两处已改为 CAS 门控。红验 C 在注入旧行为后以 `assert 2 == 1` 转红，还原后门控生效。
- 关键决策与已否决方案：失败状态文案统一为 `【任务失败】`，`【LLM API调用异常】` 放进 error 字段以保留既有断言。成功路径的 `【任务完成】` 改由 helper 的 `notify_task_status` 发出，worker 只保留内容正文。HTTP 建行后清理（tasks.py）走 helper 但 `send_status_notification=False`。否决：继续让 helper 与内容通知各发一条 `【任务完成】`（会双发）。
- 下一步唯一动作：里程碑 2——在 `update_task_status` 同一事务插入 outbox pending，独立投递线程启动补发，helper 的即时发送改为只写状态。
