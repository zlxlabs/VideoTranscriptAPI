# C1 止血进度（dlg-20261110-094517-dde50b）

## 段 1

- **当前阶段**：实现与测试完成，待提交/推送/开 draft PR。
- **本段结论**：租户通知不再回落全局——router 租户方法只发 `webhooks` dict 里声明了地址的渠道，渠道对象删除 `webhook or self.webhook` 兜底；跨渠道 fallback 删除；`_resolve_delivery_webhooks` 对非 legacy 提交者剔除等于全局配置的地址；零目标终态记 `NOTIFY_TARGET_SKIPPED` 并落账不再重试；ASR 监控改走显式 `router.send_ops_alert`（唯一全局路径）；tasks.py 三处重建入口删全局拼接；`NOTIFY_PROFILE` 启动/热更新按用户打渠道清单。不变式轴表 1–7 由新增 `tests/unit/notifications/test_tenant_notify_no_global_fallback.py` 锁定（HTTP 边界打桩），7 个用例在 base 语义下失败、1 个（legacy 保留全局）是保持现状正例。
- **关键决策与已否决方案**：`_notification_accepted` 从 any 改 all（任一声明渠道未接受即整组重试），零目标（router 返回空 dict）在调用 all 之前单独判定，避免 `all({})` 恒真；router 渠道初始化不再依赖全局 webhook（恒建两个渠道对象），`is_enabled` 语义变为「渠道对象可用（运维告警有目标）」；删除 router 租户方法已无人消费的单 `webhook=` 形参（反熵条款）；NOTIFY_PROFILE 的 legacy fallback 分支不假定 fallback_config 段是 dict（结构合法性归 config 校验器，坏配置场景不得比 ConfigError 先炸 AttributeError——test_runtime_lifecycle 曾抓到）。已否决：给零目标 outbox 行加新列/新状态（禁改 schema，用 notified_at 落账 + 日志区分）；保留跨渠道 fallback 加开关（卡面锁定删除）。
- **下一步唯一动作**：提交实现+测试+进度，跑 scope check，push 并开 draft PR。
