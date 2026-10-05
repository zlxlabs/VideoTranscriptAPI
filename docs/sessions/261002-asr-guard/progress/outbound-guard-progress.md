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

## 里程碑四：CI 取证

- 当前阶段：等待主脑开 PR 取证
- 本段结论：最终分支已推送到 `origin/card/vta-128-outboundguard`，远端 ref 指向 `701a54fc96e9695d08bbe21a8f3e97c53a8c18e3`。当前没有该分支的 PR，因此尚未产生可核验的 gate run；执行器未越过仅授权 commit+push 的边界创建 PR。
- 关键决策与已否决方案：不把本地 `make test` 结果冒充 CI `SUCCESS`，不等待或伪造外部 gate 结论。
- 下一步唯一动作：主脑开 PR 后，用 `gh pr checks <N>` 核实 gate `conclusion == SUCCESS`，并保存守卫安装标记行。

## 里程碑五：按 family 收窄守卫

- 当前阶段：修复轮 1/1
- 本段结论：守卫只对 AF_INET/AF_INET6 做 loopback 判定与阻断；AF_UNIX 直达真实实现且不记入 blocked/allowed。新增 Unix 域套接字回归测试，并补回守卫文件 PEP8 空行。
- 关键决策与已否决方案：修法是减法，不新增状态或白名单；未用掩码剥离 IPv6 附加位，先按 `family in (AF_INET, AF_INET6)` 落地。
- 下一步唯一动作：提交并推送同一分支，由主脑负责 CI 取证。
