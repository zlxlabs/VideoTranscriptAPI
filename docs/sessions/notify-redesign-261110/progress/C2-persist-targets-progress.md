## 里程碑 1：resolver 与持久化主链路

- **当前阶段**：统一解析器、任务列迁移、五个入口落库与通知消费者切换已完成；进入全量回归与交付收尾。
- **本段结论**：提交、上传、重新校对、重新总结、生成笔记现统一解析目标并把 JSON 目标随任务写入；通知读取持久化字段，只有 `NULL` 旧行走用户配置兼容。resolver、迁移重开、终态重启补发、上传静默和 skill 请求体已由针对性测试覆盖。
- **关键决策与已否决方案**：`NULL` 与 `{}` 保持不同语义；坏 JSON fail fast，不伪装成旧行；上传元数据通知字段可选，避免破坏既有 producer 契约。
- **下一步唯一动作**：运行剩余全量回归并修复发现的问题，再完成范围/报告检查、提交推送与 draft PR。

## 里程碑 2：回归与交付完成

- **当前阶段**：实现已推送并提交 draft PR，等待主脑按远端完整 gate 验收。
- **本段结论**：全量 `tests/unit`、卡面窄验与新增验收用例通过；范围检查和报告契约通过。提交 `499fffc450eb7089a351b1214203cef8bf560b68` 已推送，draft PR #323 已创建。
- **关键决策与已否决方案**：`make test` 属于 ci-checks-v1 远端 Verify-Command，本机未运行；未等待远端 CI 终态，交由主脑验收。
- **下一步唯一动作**：主脑检查 PR #323 的远端完整 gate 与主审结果后决定是否标记 ready。

## 里程碑 3：修复 PR #323 质量门禁

- **当前阶段**：修复上传任务目标意外落为 NULL 的问题，开始完整 make test 回归。
- **本段结论**：`CacheManager.create_task` 与 `accept_local_upload` 对新任务的零目标默认值统一写成 JSON `{}`；旧行 `NULL` 兼容逻辑保留，真实旧行测试显式写入 NULL，不再拿新任务模拟 legacy。
- **关键决策与已否决方案**：不在测试 fixture 注入 `user_manager`，而是在 producer/storage 边界保证新行非 NULL；不改 systemd/Context fallback 来掩盖根因。
- **下一步唯一动作**：运行完整 `make test`，检查失败和耗时，必要时修复后重跑至 exit 0。

## 里程碑 4：完整 gate 回归通过

- **当前阶段**：修复已通过本地完整 `make test`，准备推送续派修复。
- **本段结论**：`make test` exit 0，完成耗时 467 秒，pytest 进度到 100%，3 个既有测试 skip；两个上传生命周期失败用例也单独通过。完整 suite 未复现 45 分钟挂起。
- **关键决策与已否决方案**：新任务的缺省收件目标固定为 `{}`；不通过给 RuntimeContext fixture 加 `user_manager` 或伪造 legacy 数据来绕过生产路径。
- **下一步唯一动作**：推送修复提交并核对 PR #323 仍为 OPEN/ready（非 draft）。

## 里程碑 5：续派修复交付完成

- **当前阶段**：修复提交已推送，PR #323 保持 OPEN/ready，等待主脑下一轮 CI 复核。
- **本段结论**：完整 `make test` 通过，耗时 467 秒，3 个 skip；两个原 CI 上传生命周期用例单独通过。新任务含直接 CacheManager 上传受理均写非 NULL 目标，远端 ref 已核对。
- **关键决策与已否决方案**：首轮 make test 中与旧行 fallback 相关的两个单元用例改为显式模拟 NULL 历史行或显式持久化新任务目标；没有给 runtime fixture 补 user_manager。
- **下一步唯一动作**：主脑复核 PR #323 新 head 的远端 quality gate，确认 CI 不再因上传 notify_targets NULL 失败或挂起。