# SDK pin 升到 492fe19：锁连接被拒秒级失败 / 自动预算公式 / 超时消息

- Task-Id：（未分配）
- Fixes-Issue：169
- 上游：`zlxlabs/CapsWriter-ASR-Server` master HEAD `492fe19`（含 PR #70 `e117f02`、PR #71 `3d7436c`）
- 本仓 pin：`b0818dc7859d1d8100e42f5c70cb75d34da422f7` → `492fe191e3f9568ea178b61970c732c9d37c4e29`

## 背景与上游两张 PR 修了什么（已核对，非推断）

上游仓 `zlxlabs/CapsWriter-ASR-Server`，`492fe19` 是 master HEAD，`3d7436c`（PR #71）与
`e117f02`（PR #70）都是它的祖先（`git merge-base --is-ancestor` 实测）。从本仓现用 pin
`b0818dc` 到 `492fe19`，`sdk/` 的全部差异只有 3 个文件：`capswriter_asr/client.py`（+67/-14）、
`sdk/pyproject.toml`（`requires-python` `>=3.10` → `>=3.11`）、`sdk/README.md`。不新增文件，
不改协议帧，不改服务端 HTTP/WS 接口。

- PR #70（`e117f02`）：消灭剩余 `asyncio.wait_for`。两处：发送帧的
  `await asyncio.wait_for(ws.send(frame), idle_timeout)`、以及 `deadline_watch` 里的
  `await asyncio.wait_for(deadline_changed.wait(), remaining)`。CPython ≤3.11 的
  `wait_for` 有 `except CancelledError: if fut.done(): return fut.result()` 吞取消分支，
  被等待对象与 `task.cancel()` 落在同一 tick 时返回结果而非上抛 → 取消被消费后
  `finally: asyncio.gather(...)` 永久挂起。**连接被拒正是这条路径**：连接异常让
  `operation` task 立刻结束并向外抛，但 `deadline_watch` 的 `wait_for` 吞掉取消，
  `gather` 挂死，调用直到预算到点才以 `AsrError(code="timeout")` 收场（旧 pin 实测 120.1s）。
  修法：改用 `asyncio.wait({future}, timeout=...)`（内部 `_wait` 无吞取消分支）+ `finally: cancel()`。
- PR #71（`3d7436c`）：自动预算由 `max(120.0, duration + 60.0)` 改为
  `_auto_budget(duration) = duration * 4 + 120`，语义是 **watchdog（挂死检测）而非识别时限
  SLA**；顺带把预算秒数与音频时长写进超时消息。依据是 93.09 秒音频实测远端 306.3 秒
  （3.29× 实时），旧公式只给 153 秒会在识别仍在跑时误杀。

## 本仓消费边界

本仓 `capswriter_client.py` 在拿得到媒体时长时显式传 `deadline_total = duration*4 + 120`
（`_transcription_deadline`），与上游新自动预算**逐字相等**；拿不到时长时不传，走自动预算。
本次不退休该显式传参（主脑已拍板），因此：

- 显式路径的预算数值不变（等值迁移），显式调用语义逐字未变；
- 自动预算路径（recorder:// / generic 下载路径、以及本卡的直接 SDK 调用）拿到上游新公式与新消息。

## 不变式

| 编号 | 不变式 | 锁定位置 |
| --- | --- | --- |
| I1 | 指向确定关闭的端口时，同步入口秒级抛 `AsrError(code="connection_lost")`，不挂到自动预算 | T1 用例（子进程 + 真实 SDK 调用） |
| I2 | 自动预算 = 时长 × 4 + 120；≥ 时长 × 3.5；时长 0 时不低于 120 秒 | T2 用例 |
| I3 | 超时消息同时含预算名字与预算秒数（算得出时长时还含音频时长） | T3 用例 |
| I4 | 本仓显式 `deadline_total` 路径的预算数值与既有断言一字不变 | `tests/unit/test_capswriter_deadline_budget.py` 原样通过（未改） |
| I5 | pin 唯一：pyproject rev = uv.lock rev = 运行时 `client.py` 哈希 `0490b5f877917e55f995e5ea9a81da226b47cda52e406bf361207c70550eb01b` | 验收命令 + 测试内自查（子进程回报哈希与父进程导入文件比对） |
| I6 | 测试不触网：服务端只绑 `127.0.0.1:0`，媒体现场合成 | 测试源码 URL 字面量自检 |
| I7 | 清理有硬截止：子进程有父进程硬超时 kill，任何一次运行不留下挂死进程 | 父进程 `subprocess.run(timeout=...)`，子进程 `os._exit` 收尾 |

## 盲区（必须写进验收结论，不许当成「已锁住」）

- T1/T3 只在 CPython ≤3.11 上是回归（`wait_for` 吞取消是 ≤3.11 特有）；≥3.12 上这两条用例
  恒绿。测试应显式记录解释器版本；主脑验收时若 CI 换到 ≥3.12，须在结论里写明 T1 不再守回归。
- T2 锁的是「上游公式的数值性质」，不是本仓的识别时限预测能力；真实时长/实时因子分布
  仍只有 93.09 秒一个样本。

## 用例落位与形态

三条用例全部落在 `tests/unit/test_capswriter_sdk_transport.py`（该文件已有「真实解释器子进程 +
真实 websockets 服务端」的骨架，且自带 ffmpeg 前置断言），不新建文件：

- **T1 `test_connection_refused_fails_fast_with_connection_lost`**：子进程里先用一个**裸 socket
  HTTP 服务端**回答 SDK 的 `GET /health`（真实协议 v2 JSON），随即 `close()` 掉监听套接字 →
  该端口此后确定拒绝连接（子进程自查 `socket.create_connection` 抛 `ConnectionRefusedError`
  并回报 `port_closed=True`），再走 `transcribe_file_sync(...)` 且**不传 `deadline_total`**
  （默认预算路径）。断言 `AsrError.code == "connection_lost"` 且 `elapsed < 5s`。
  - 为什么用「裸 socket /health + 关闭监听」而不是 `websockets.serve`：SDK 的 `_operation` 先
    `await _check_server(url, ...)`，再 `_transcode`，再 `connect`。若端口从一开始就关闭，
    连接异常发生在**健康检查**阶段，根本走不到 `deadline_watch` 的取消路径，锁不到 #67。
    裸 socket 形态才能让「/health 通过 → WebSocket 连接被拒」这一组合成立，也就是上游
    #67 的真实触发形态。
- **T2 `test_sdk_auto_budget_is_duration_times_four_plus_120`**：放在
  `tests/unit/test_capswriter_deadline_budget.py`（预算公式的家），取符号用
  `getattr(capswriter_asr.client, "_auto_budget", None)` + `assert ... is not None`，
  不直接 import 私有名 → 旧 pin 下红在 `AssertionError` 而不是 `ImportError`。
- **T3 `test_timeout_message_names_the_budget_and_its_seconds`**：真实 `websockets.serve` 只收帧
  永不回 final（连上但不回），显式 `deadline_total=1.0` 触发真超时；断言 `code == "timeout"`
  且消息同时含 `deadline_total`、`1 秒`、`音频`。

T2 的额外自查：`getattr` 拿到符号后先断言它可调用再取数值，旧 pin（无 `_auto_budget`）与
「有符号但公式错」分别红在不同消息上，归因不混淆。