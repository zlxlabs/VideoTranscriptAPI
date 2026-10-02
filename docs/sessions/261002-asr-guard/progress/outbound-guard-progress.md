# 出站网络守卫进度

## 里程碑一：摸排出站面

- 当前阶段：摸排完成
- 本段结论：当前守卫只存在于 `tests/unit/test_asr_monitor_alert_delivery.py`，并同时拦截非 loopback DNS；`tests/conftest.py` 没有出站守卫。任务要求的全局判据尚未存在。
- 关键决策与已否决方案：沿用卡面锁定口径：DNS 放行，只拦非 loopback 的 `connect` 与 `connect_ex`；不引入静态白名单。
- 下一步唯一动作：把单份守卫实现迁移到 `tests/conftest.py`，并让告警投递测试复用它。

## 里程碑二：改守卫

- 当前阶段：守卫迁移完成
- 本段结论：守卫现位于 `tests/conftest.py` 的 autouse fixture，按用例记录并校验显式预期阻断；告警投递测试删除了本地副本，DNS 改为放行断言，并新增 `connect_ex` 与 IPv6 loopback 覆盖。
- 关键决策与已否决方案：保留手动测试目录的非目标边界；未增加按文件或用例的外网白名单。
- 下一步唯一动作：执行全量 `make test`，确认真实测试集合没有非 loopback `connect` 或 `connect_ex`。
