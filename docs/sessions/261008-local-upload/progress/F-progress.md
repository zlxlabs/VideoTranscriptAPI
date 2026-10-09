# F 进度

## 2026-10-09 · 首个运维门槛单元

- **当前阶段**：implementing；首个只读运维门槛单元已验证并待提交/推送。
- **本段结论**：`uv run --frozen pytest -q tests/unit/test_upload_ops.py` 4 passed；CLI 只读取明确传入的消费配置/数据目录与当前进程开关，不加载 dotenv、不打印密钥、不启用/部署/恢复；即使四限额、开关和代码祖先检查满足，也会以 `UPLOAD_ENABLE_BLOCKED` 阻止生产启用，因为容量、独立恢复域和实际入口尚未知。
- **关键决策与否决方案**：消费配置解析沿用真实 `load_and_validate_config`；env 文件仅从 tracked compose 声明识别来源，绝不 source；兼容检查以 Git commit ancestry 与已知 A guard-only 完整 SHA 为边界，不用版本字符串比较；本地检查结果不作为 enable 权限或伪造容量凭据。首轮测试因预期把四个 `null` 错判为 `missing` 有 AssertionError 红，修正断言后全绿；生产代码未变异。
- **下一步唯一动作**：提交并推送该独立可绿单元，再继续恢复与容量运维证据。
