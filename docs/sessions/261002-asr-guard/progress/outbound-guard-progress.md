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

## 里程碑三：全量验证

- 当前阶段：本地验证完成
- 本段结论：`make test` 首轮与反向红验后的恢复轮均退出 0；临时非 loopback 连接用例按预期被守卫以断言失败拦截，随后已删除。手动 webhook 测试在未设置凭据时 6 项跳过，编译检查通过。
- 关键决策与已否决方案：未使用 `--no-verify`、测试过滤、外网白名单或门禁基线修改；没有把真实手动网络测试纳入默认守卫。
- 下一步唯一动作：提交并推送当前分支，获取 PR gate 的真实 `SUCCESS` 结论与守卫安装证据。
