# F 进度

## 2026-10-09 · 首个运维门槛单元

- **当前阶段**：implementing；独立只读门槛单元已提交 `03828d3231003fc6ef3f91e52d772950e8b4f796`、推送并由 draft PR #208 承接。
- **本段结论**：`uv run --frozen pytest -q tests/unit/test_upload_ops.py` 4 passed；`scripts/ops/local_upload_check.py` 只读取明确传入的消费配置/数据目录与当前进程开关，不加载 dotenv、不打印密钥、不启用/部署/恢复；即使四限额、开关和代码祖先检查满足，也会以 `UPLOAD_ENABLE_BLOCKED` 阻止生产启用，因为容量、独立恢复域和实际入口尚未知。
- **关键决策与否决方案**：消费配置解析沿用真实 `load_and_validate_config`；env 文件仅从 tracked compose 声明识别来源，绝不 source；兼容检查以 Git commit ancestry 与已知 A guard-only 完整 SHA 为边界，不用版本字符串比较；本地检查结果不作为 enable 权限或伪造容量凭据。首轮测试因预期把四个 `null` 错判为 `missing` 有 AssertionError 红，修正断言后全绿；生产代码未变异。
- **下一步唯一动作**：完成恢复与容量工具的单元和本地沙箱验证，提交第二里程碑并在 clean tree 重跑容量探针。

## 2026-10-09 · 恢复消费者与容量探针

- **当前阶段**：implementing；真实 SQLite+正文 snapshot/restore 回归、A guard-only 真实源码消费者测试、受控容量探针和运维文档已落盘，当前窄测 11 passed，尚未提交。
- **本段结论**：fresh Resolver 子进程接收恢复前 DB 且外部 gate 明确 false，实际拒绝读；current cleaner 保留有效 never 正文/root。独立 subprocess 从真实 A guard-only commit `6af391d20edab8dfd8b320ce6b91d4d19e3260e2` 运行 resolver/cleaner，active share 可读、upload/blank legacy alias 不可读、cache/task 两个 cleaner 不删有效长成果；pre-A commit 由 ancestry 负例拒绝。容量脚本实际成功完成一次 raw HTTP/两接收者/loopback URL 下载与 ASR rejector 实验，但当时 source tree dirty，HEAD 值不足以作最终版本证据，需 clean commit 后复跑。
- **关键决策与否决方案**：恢复 fixture 只用 pytest `tmp_path`；还原撤销前快照会让 SQLite 内 `revoked_at` 回到 NULL，所以唯一安全结论是部署域 gate 关闭且没有独立撤销来源时不得再开；脚本仅对 loopback 服务/下载/ASR fixture 工作，失败终态是预期而不代表 ASR 容量。移除 config example 中消费者 validator 不接受的 `funasr_spk_server.send_terms` 示例键；消费者默认值仍为 false，不修改运行时消费者。
- **下一步唯一动作**：提交本里程碑，然后 clean-tree 重跑 `scripts/perf/local_upload_capacity.py` 并保存真实观测。
