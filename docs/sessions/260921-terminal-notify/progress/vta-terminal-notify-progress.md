# vta-terminal-notify 进度

## 里程碑 1

- 当前阶段：implementing / 里程碑 1 完成
- 本段结论：13 个终态写入点的状态通知收敛到 `finalize_terminal_status_and_notify`。`llm_ops.py` 的 `finally` 无条件发送与 `transcription.py` worker 顶层兜底忽略 CAS 返回值两处已改为 CAS 门控。红验 C 在注入旧行为后以 `assert 2 == 1` 转红，还原后门控生效。
- 关键决策与已否决方案：失败状态文案统一为 `【任务失败】`，`【LLM API调用异常】` 放进 error 字段以保留既有断言。成功路径的 `【任务完成】` 改由 helper 的 `notify_task_status` 发出，worker 只保留内容正文。HTTP 建行后清理（tasks.py）走 helper 但 `send_status_notification=False`。否决：继续让 helper 与内容通知各发一条 `【任务完成】`（会双发）。
- 下一步唯一动作：里程碑 2——在 `update_task_status` 同一事务插入 outbox pending，独立投递线程启动补发，helper 的即时发送改为只写状态。

## 里程碑 2

- 当前阶段：implementing / 里程碑 2 完成
- 本段结论：`task_terminal_notifications` 表在终态 CAS 同一事务插入 pending。投递线程启动时先扫 pending 补发，文案带原始 `completed_at`。helper 先 claim 再发，避免与投递线程双发。三条恢复路径写 failed 即产生 pending。
- 关键决策与已否决方案：outbox INSERT 失败随事务回滚状态写，不扩展 `terminal_write_pending`（该通道是内存 set，进程死即丢，正是本卡要消除的）。两态 pending→sent，claim 时 attempts 0→1，崩溃不重发。DummyCacheManager 无 claim 方法时 helper 仍即时发送，保住既有 feature 测试。
- 下一步唯一动作：里程碑 3——中间态文案加「进行中」语义，emoji 匹配同步，文档记录 outbox / 至多一次 / fire-and-forget / 飞书缺口。

## 里程碑 3

- 当前阶段：implementing / 里程碑 3 完成
- 本段结论：中间态改为「转录完成（进行中）…后面还有校对/摘要」与「开始处理（进行中）」。`channel.py` / `wechat.py` 的「转录完成」匹配改为 substring，并加用例锁死新文案 emoji 不是默认值。文档记录 outbox、至多一次、fire-and-forget 残留和飞书缺口。
- 关键决策与已否决方案：中间态文案不用「处理中」，避免 `_get_status_emoji` 里「处理」分支先于「转录完成」把 emoji 打成 🔄。
- 下一步唯一动作：跑全量 `uv run --extra dev pytest tests/unit tests/features tests/integration -q`，写报告并 push。

## 验收回派 R1

- 当前阶段：review-fix / R1 完成
- 本段结论：helper 直接调用 `claim_pending_terminal_notification` / `mark_terminal_notification_sent`，删掉 getattr 鸭子类型。DummyCacheManager 用内存 dict 实现这三个方法。
- 关键决策与已否决方案：否决「claim 缺失时同步直发」——那是静默降级通道。
- 下一步唯一动作：R2 给终态状态通知加 view 链接，calibrate_only 路径先写红验再实现。

## 验收回派 R2

- 当前阶段：review-fix / R2 完成
- 本段结论：`build_task_status_content` 增加可选 `view_url`，helper 与 dispatcher 都填 `{base}/view/{view_token}`。dispatcher 不再往 error 里拼 `view=`，避免双链接。calibrate_only 成功路径红验先转红后转绿。
- 关键决策与已否决方案：不新增第二条消息。链接加在既有状态通知正文。
- 下一步唯一动作：全量 pytest 后 push。

## 验收回派 R8.1

- 当前阶段：repairing / R8.1 完成
- 本段结论：终态通知 outbox 增加 `claimed_at` 租约列，新库建表与既有 `_migrate_database()` 均覆盖；claim/list 只允许无租约或已过期租约的行，claim 后立即重复领取被拒。新增过期租约恢复与慢发送跨轮询周期不重复投递测试。
- 关键决策与已否决方案：租约时长固定为模块级 `TERMINAL_NOTIFY_LEASE_SECONDS = 120`，使用 UTC `YYYY-MM-DD HH:MM:SS` 文本，与 SQLite `CURRENT_TIMESTAMP` 同口径；不引入三态 `sending` 或额外配置项。
- 下一步唯一动作：R8.2 收紧通知返回值为真实渠道字典，并在异常/全 False 后立即释放租约。

## 验收回派 R8.2

- 当前阶段：repairing / R8.2 完成
- 本段结论：`_notification_accepted` 现在只接受至少一个渠道为真的字典，非字典结果会记录类型和值并按失败处理；发送异常与渠道全 False 都立即清空租约，保留 attempts 并允许下一轮补发。所有允许范围内的通知替身已返回真实字典，并同步内存 outbox 的租约字段。
- 关键决策与已否决方案：不为 MagicMock 或其他测试替身保留宽松默认，不新增 fallback；未知返回形态统一 fail-closed，`{"wechat": True}` / `{"wechat": False, "feishu": True}` 才能标记 sent。
- 下一步唯一动作：R8.3 删除重复测试类并完成红验、全量测试与收尾报告。

## 验收回派 R7.1

- 当前阶段：review-fix / R7.1 完成
- 本段结论：helper 把 title/author 转发给 update_task_status，终态行与 terminal_snapshot 不再丢标题作者。
- 关键决策与已否决方案：不新增字段，只转发改造前各站点本来就传的 title/author。
- 下一步唯一动作：R7.2 有界至少一次补发。

## 验收回派 R7.2

- 当前阶段：review-fix / R7.2 完成
- 本段结论：发送抛异常或渠道全 False 不标 sent；补发条件改为 attempts < 3。漏发窗口改为可能重复一条，但不会静默丢失。
- 关键决策与已否决方案：主脑拍板有界至少一次，不重新论证至多一次。
- 下一步唯一动作：R7.3 抑制通知的路径标 sent，避免 dispatcher 补发。

## 验收回派 R7.3

- 当前阶段：review-fix / R7.3 完成
- 本段结论：send_status_notification=False 时立即 mark sent，HTTP 建行清理路径不会被 dispatcher 补发【任务失败】。
- 关键决策与已否决方案：不新增列；不改 update_task_status 落库。
- 下一步唯一动作：全量 pytest 后 push。





