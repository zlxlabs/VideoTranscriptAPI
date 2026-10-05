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
## 里程碑 1：pin 升到 492fe19 + uv.lock 重锁

- **当前阶段**：implementing
- **本段结论**：`pyproject.toml:87` 与 `uv.lock` 两处 rev 同步到 `492fe191...`，`uv lock`
  只重写了 capswriter-asr 的 `source` 与 `requires-dist` 两行，无其它包版本/来源变动。
  运行时 `client.py` 哈希 = `0490b5f877917e55f995e5ea9a81da226b47cda52e406bf361207c70550eb01b`，
  与主脑预取的期望值一致；仓内已无 `b0818dc` 残留。
- **关键决策与已否决方案**：只改 `[tool.uv.sources]` 一行后跑 `uv lock`（不手改 lock 文本，
  不带 `--upgrade-package`）——否决议其它无关依赖一起升级。
- **下一步唯一动作**：写 T1 连接被拒秒级失败用例。

## 里程碑 2：T1 连接被拒秒级失败

- **当前阶段**：implementing
- **本段结论**：`test_connection_refused_fails_fast_with_connection_lost` 落在
  `tests/unit/test_capswriter_sdk_transport.py`，真实子进程 + 真实裸 socket `/health` + 真 ffmpeg +
  真 `transcribe_file_sync`。新 pin 实测 payload：`code="connection_lost"`、`elapsed=0.188s`、
  消息「无法连接服务端 WebSocket: [Errno 111] Connect call failed」——错误码取自 WebSocket
  连接阶段而非健康检查阶段，这正是上游 #67 的触发形态。
- **关键决策与已否决方案**：端口自查（`port_is_closed`）放在 SDK 调用**之后**——放在之前时，
  探测用的 `create_connection` 会占住 listen backlog，被健康检查线程 accept 掉，
  `/health` 拿到空请求（首轮就踩到，`request_line` 为空串）。监听套接字改为
  `accept()` 之后、回包之前就关闭，消除「回包后、转码完成前」的竞争窗口。
- **下一步唯一动作**：写 T2 自动预算公式用例。

## 里程碑 3：T2 自动预算 = 时长 × 4 + 120

- **当前阶段**：implementing
- **本段结论**：`test_sdk_auto_budget_is_duration_times_four_plus_120` 与
  `test_sdk_auto_budget_grows_with_duration_and_matches_repo_budget` 追加到
  `test_capswriter_deadline_budget.py`，24 passed。断言是行为性质（`_auto_budget(0)==120`、
  `≥ 时长×3.5`、`_auto_budget(93.1)==93.1*4+120`、随时长单调），不是抄实现。
- **关键决策与已否决方案**：取符号一律走 `getattr(sdk_client, "_auto_budget", None)` +
  `assert ... is not None` + `assert callable`——否决直接 `from ... import _auto_budget`
  （旧 pin 下红在 ImportError，红验无效）。顺带修正文件头 docstring 里「SDK 自动预算是
  max(120, duration+60)」这句已失效的描述（只改说明，未动任何既有断言）。
- **下一步唯一动作**：写 T3 超时消息可定位用例。
