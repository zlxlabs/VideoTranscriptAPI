# sdk71-pin-261005 进度

## 里程碑 0：现场核查 + 设计落盘

- **当前阶段**：implementing（改代码前）
- **本段结论**：上游 `492fe19` 存在且 `3d7436c` / `e117f02` 均为其祖先，`sdk/` 差异只有
  `client.py`(+67/-14)、`pyproject.toml`、`README.md` 三个文件，与卡面一致。现 pin 只出现在
  `pyproject.toml:87` 与 `uv.lock` 两处。
- **关键决策与已否决方案**：T1 落 `test_capswriter_sdk_transport.py` 并用「裸 socket 回 /health
  后关闭监听」构造连接被拒——否决「用 `websockets.serve` 起一个只在 WS 阶段失败的端口」之外的
  简化写法：`websockets.serve` 无法只让 /health 通过而 WS 连接被拒；若端口从头关闭，异常发生在
  `_check_server` 阶段，走不到 `deadline_watch` 取消路径，锁不到 #67。T2 用 `getattr` 取
  `_auto_budget`（否决直接 import 私有名：旧 pin 下会红在 ImportError，红验无效）。
- **下一步唯一动作**：改 `pyproject.toml` 的 rev 并 `uv lock`。
- **验证预算估算**：仓内无 `tests/pytest-test-durations.json`（`ls tests/` 只有 README.md 与子目录），
  无机器可查基线。按既有实测估：旧 pin 红验 1 轮（T1 需真挂满 120s 自动预算，≈2.5 分钟）、
  窄测 3 轮（≈3 分钟）、`make test` 全量 2 轮（按 #166 卡实测单轮 10-15 分钟估）。
  合计 > 30 分钟 → 分段跑 + 分段 commit 是硬要求。