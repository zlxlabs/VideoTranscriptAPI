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

## 里程碑 4：T3 超时消息可定位

- **当前阶段**：implementing
- **本段结论**：`test_timeout_message_names_the_budget_and_its_seconds` 落在
  `test_capswriter_sdk_transport.py`，真 websockets 服务端 upgrade 后收下 SDK 的真实 final 帧
  并永不回，显式 `deadline_total=1.0` 触发真超时。实测 payload：
  `code="timeout"`、消息「转录超过deadline_total 1 秒（音频 0.2 秒）：远端转录阶段超时」、
  `elapsed=1.003s`、`frames=1`、`final_seen=True`。
- **关键决策与已否决方案**：媒体压到 3200 samples（0.2 秒）而不是复用既有的 1.0 秒素材——
  本地准备要跑两次 ffmpeg，1.0 秒预算下留不出稳定余量；断言同时要求 `frames>=1` 与
  `final_seen=True`，否则「预算秒数」可能来自「本地准备」阶段，消息里就不会有音频时长，
  断言会名不副实。
- **下一步唯一动作**：切回旧 pin 跑红验（T1 需真挂满 120s），逐条贴原文后还原。

## 里程碑 5：红验（旧 pin b0818dc）+ 补齐 T1b

- **当前阶段**：implementing
- **本段结论**：切回旧 pin 逐条红验，三条全部红在 `AssertionError`（无 ImportError /
  TimeoutExpired）：T2 `assert None is not None`；T3 `assert '1 秒' in '转录超过deadline_total：
  远端转录阶段超时'`；**但 T1（真被拒端口）5/5 全绿**——卡面「旧 pin 下真被拒端口挂满 120s」
  的前提在 CPython 3.11.15 上不成立。补 T1b 把连接失败压到与 `set_deadline()` 同一 tick 后，
  旧 pin 实测 120.203s、断言红在耗时，新 pin 全绿。
- **关键决策与已否决方案**：否决「把 T1 改成 stub 形态就算数」——单用 stub 会丢掉现场形态的
  覆盖；改为 T1a（真被拒端口，锁错误码 + 秒级）与 T1b（同 tick 拒绝，锁 #67 回归）并存，
  design.md 与盲区段同步记下这一分工。
- **下一步唯一动作**：概率性验收（T1/T1b 连续 5 轮全绿）+ 全量 `make test`。

## 里程碑 6：概率性验收 + 全量验证 + 收尾

- **当前阶段**：done（待验收）
- **本段结论**：T1/T1b 连续 5 轮全绿（每轮 ~0.75s）；卡面相关 8 个文件窄测
  `72 passed, 3 skipped in 3.84s`；全量 `make test` exit 0（5m17s），同套件另跑一次
  `3612 passed, 3 skipped, 5064 warnings, 1 subtests passed in 290.74s`。
  `git diff --check` 与 `uv lock --check` 均干净；`ps` 无遗留子进程；`data/temp` 已清空。
- **关键决策与已否决方案**：为拿到 pytest 计数行先试 `PYTEST_ADDOPTS="-v" make test`——否决：
  该环境变量会被 `test_manual_test_gate.py` 的嵌套 pytest 子进程继承，把嵌套 verbosity 从 -2
  抬到 -1，导致它解析不到「file: N」行而红（`ValueError: invalid literal for int()`）。
  这是本次唯一一次「新红」，且已证伪与本卡改动无关：单独跑该文件 `34 passed in 42.17s`。
  改为保留一次原样 `make test` 作权威证据（exit 0），再补一次同套件 `uv run --frozen pytest tests`
  取计数行；Makefile 多加的一个 `-q` 会让 pytest 落到 verbosity -2 而不打印计数行，这是仓现状。
- **下一步唯一动作**：验收方复核 `git show --stat` 与三条用例的红/绿原文。
