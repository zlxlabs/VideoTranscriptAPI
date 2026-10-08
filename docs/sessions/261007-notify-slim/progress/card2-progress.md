# Card 2 进度存档:偏慢提醒(⏳)

## 里程碑 1:计时模块与单元测试

- **当前阶段**:implementing(提交 1/3 完成)
- **本段结论**:新增 `utils/notifications/slow_alert.py`,实现分阶段计时(转录/大模型)、每任务一条 ⏳ 的共用"已提醒"标记、终态取消接口。门槛为模块常量(转录 `600 + 0.15×时长`,未知时长 1800s,大模型 600s),注释注明生产实测出处。到点回调与取消在同一把锁下互斥,回调以代数(generation)校验堵住 `Timer.cancel()` 对已到点回调无效的竞态窗口。单元测试 13 例全绿(门槛公式、恰好一条、跨阶段共用、排队不计入、终态先于到点、竞态 5 轮)。
- **关键决策与已否决方案**:计时实现选每任务 `threading.Timer`(daemon)而非单扫描线程——任务量级下线程开销可忽略,扫描线程需要额外的唤醒/退出管理;否决了"发送时回查 cache_manager 解析 view_url"——Timer 回调线程无 runtime contextvar 绑定,改为接线点在 worker 线程内解析好存进状态。
- **下一步唯一动作**:接线转录/大模型阶段与 `media_duration_s` 观测。

## 里程碑 2:转录/大模型阶段接线 + media_duration_s

- **当前阶段**:implementing(提交 2/3 完成)
- **本段结论**:转录阶段计时在 `process_transcription` worker 起点启动,两处拿到媒体时长后写入 `tracker.set_media_duration`(随 `terminal_snapshot.observability.media_duration_s` 落库)并重算门槛;五处转录→LLM 交接统一在 `_handoff_to_llm_stage` 成功出口收口 `finish_transcription`。大模型阶段在 `_handle_llm_task` 接线,以 payload 是否带 `perf_tracker` 键区分流水线任务,排除 recalibrate(calibrate_only)/resummarize(无该键)/notes(提前 return)三个非目标路由。窄验证 14 例全绿,相邻回归(media_duration_probe、notification_budget、deadline_budget)74 例全绿。
- **关键决策与已否决方案**:`perf_tracker` 键作流水线判据——routes/tasks.py 不在本卡 Scope-Globs 内,无法给 resummarize 加显式标志,而该键天然只有转录交接 payload 携带;否决了"在 LLM 阶段重查任务行判断来源"——多一次 DB 查询且语义不比 payload 键更可靠。
- **下一步唯一动作**:终态取消接线 + 正常速度任务零 ⏳ 的预算用例。

## 里程碑 3:终态取消接线 + 通知预算用例

- **当前阶段**:implementing(提交 3/3 完成,功能闭环)
- **本段结论**:终态唯一入口 `finalize_terminal_status_and_notify` 在 CAS 赢后调用 `slow_alert.cancel_all`,与到点回调同锁互斥,终态后绝无 ⏳。`test_notification_budget.py` 新增 `test_normal_speed_task_sends_no_slow_alert`,走真实下载+转录+LLM 成功终态流水线断言零条 ⏳、恰好一条 ✅。`tests/README.md` 登记新测试文件与预算约束。budget + slow_alert 合跑 23 例全绿。
- **关键决策与已否决方案**:取消点放在 CAS 赢分支(而非函数入口)——只有真正写入终态才取消,非终态写入(CALIBRATING 等经 update_task_status 的路径)不受影响;终态黏性下的 CAS 输家分支无需取消,赢家路径已覆盖。
- **下一步唯一动作**:红验(用例 3 去共用标记、用例 5 去终态取消),然后收尾全量验证。


