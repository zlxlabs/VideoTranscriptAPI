# 出站网络守卫进度

## 里程碑一：摸排出站面

- 当前阶段：摸排完成
- 本段结论：当前守卫只存在于 `tests/unit/test_asr_monitor_alert_delivery.py`，并同时拦截非 loopback DNS；`tests/conftest.py` 没有出站守卫。任务要求的全局判据尚未存在。
- 关键决策与已否决方案：沿用卡面锁定口径：DNS 放行，只拦非 loopback 的 `connect` 与 `connect_ex`；不引入静态白名单。
- 下一步唯一动作：把单份守卫实现迁移到 `tests/conftest.py`，并让告警投递测试复用它。
