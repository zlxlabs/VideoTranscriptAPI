<!-- delegate-outcome: pending -->

# SDK 65 升级设计：接入上游 #65 正式修复并锁住 3.11 调用方回归

- Issue：#166（本卡只锁回归与接入，**不关 issue**）
- Base commit：`ff92a175243dde36143da867a95e2fdad384e16c`
- 分支：`card/sdk65-upgrade-261004`
- 锁定决策：唯一 SDK pin = `b0818dc7859d1d8100e42f5c70cb75d34da422f7`（上游 PR #66 MERGED 提交）。
  不漂到上游 HEAD、不回退到 `858c6b975d8bdd2be0e47ac5a36483119894c529`；本卡不部署生产。

## 1. 背景与根因（上游已证，本卡不重写修法）

现场证据（commit `85ecb45a`，`docs/sessions/triage-261004/wire-probe.md`）：真实样本
177,572 bytes / 5.546688s，客户端 `send` 成功后服务端以同一 UUID 走完
`receive_complete → final_submit → task_end(done) → result_dispatched`，客户端 `recv()` 也收到同 UUID 的
`is_final=true` 帧，但 SDK 调用仍以 `AsrError(code=timeout)` 结束，耗时 120.152 秒，**没有返回 Transcript**。

上游根因（`docs/sessions/261004-sdk65/root-cause.md`）：`client.py` 的 `idle_watch()` 用
「无限循环 + 每轮 `asyncio.wait_for(queue.get())`」实现接收侧 idle 超时。CPython **≤3.11** 的
`asyncio.wait_for` 有吞取消分支（`tasks.py: except CancelledError: if fut.done(): return fut.result()`）：
被等待对象在同一 tick 已完成、外层同刻收到 `task.cancel()` 时返回结果而非传播 `CancelledError`。
`final` 到达恰好制造同刻条件；取消被吞后 `idle_watch` 进入下一轮全新等待，而那次取消已被消费，
于是 `_transcribe_connected` 的 `finally: await asyncio.gather(...)` 永久挂起，`_operation` 不返回，
直到外层预算到点抛 `AsrError(timeout)`。Python 3.12 用 `asyncio.timeout` 重写，无此分支 —— 只在 ≤3.11 复现。

上游修法（`b0818dc` 对 `client.py` 的唯一改动，41 行）：`idle_watch` 改用 `asyncio.wait({getter}, timeout=...)`
（内部 `_wait` 没有吞取消分支，取消一定上抛）+ `finally` 里 `getter.cancel()`；主循环用 `ordered = (upload, receive, idle)`
做错误优先定序，取消一定传播、已完成的 upload/idle 异常不会被同轮到达的 final 改判成成功。
本卡**不改适配层、不重写该修法**，只换 pin 并把调用方可见的行为钉住。

非目标（明确不做）：不修上游 #67（连接失败有界延迟）、#68（Python 3.10 兼容）；不加时限/重试/fallback；
不改下载器、配置与业务语义；不升级无关依赖。

## 2. 不变式（本卡成立才成立）

| 编号 | 不变式 | 锁定位置 |
| --- | --- | --- |
| I1 | 合法 final 到达且连接保持打开时，同步入口在远小于默认 120s 预算内**返回 Transcript 并写出非空产物** | `tests/unit/test_capswriter_sdk_transport.py::test_final_result_returns_and_writes_products_on_python311` |
| I2 | 修复必须是「final 已送达但调用未返回」消失，不是把预算调大：本仓 `deadline_total` 公式、SDK 默认预算、错误码语义一字不改 | 本卡 diff 不含 `capswriter_client.py` / `pyproject` 预算常量；`tests/unit/test_capswriter_deadline_budget.py` 原样通过 |
| I3 | 回归必须在**真实解释器 + 真实 websockets 客户端 + 真实 SDK 序列化帧**上判定，不能手造 `Transcript` | 测试跑子进程，内含真实 `websockets.serve`（随机端口、真实 `/health`）、真实 ffmpeg 转码、真实 `transcribe_file_sync` |
| I4 | 测试不得访问公网/生产：服务端只绑 `127.0.0.1:0`，媒体由测试现场合成 | 媒体文件由测试生成（0.2s 合成 WAV），不引用 triage 报告里的公网样本 URL |
| I5 | 锁定的 SDK 是那一个 SHA：`pyproject.toml` / `uv.lock` 的 rev 相等，运行时加载的 `client.py` 哈希 = 该提交该文件哈希 | 验收命令见第 4 节；子进程回报 `client_sha256` 并与父进程导入的同一文件比对 |
| I6 | ≥3.11 语义：只有 ≤3.11 能观察到这个回归；测试在 ≥3.12 上必须显式 skip 而不是悄悄恒绿 | 测试里 `sys.version_info >= (3, 12)` 时 skip，reason 写明回归不可观察 |
| I7 | 清理有硬截止：子进程有父进程硬超时 kill，卡片报告里任何一次运行都不能留下挂死进程或线程 | 父进程 `subprocess.run(timeout=45)`，子进程 `os._exit` 收尾 |

## 3. TDD 计划

1. **红（先证旧 pin 真的坏）**：`858c6b9`（旧 pin，`client.py` 哈希 `ff476ad7…`）下跑新测试。
   预期红法：假服务端在 **~0.1s** 就按 SDK 真实 UUID 回完 final 并**保持连接打开**，但同步入口直到
   `deadline_total`（测试内收到 6s）才以 `AsrError(code=timeout)` 结束，`CapsWriterClient.transcribe_file`
   返回 `(False, [])` —— 断言 `success is True` 转红，且报文能同时给出「final 已送达时刻」与「调用返回时刻」。
   红必须是 `AssertionError`（期望/实际差异），不是超时 kill、不是 import 错误。
2. **绿**：把 `pyproject.toml` 与 `uv.lock` 的 rev 换成 `b0818dc…`，`uv sync --frozen` 重装，
   同一条测试转绿：`success is True`、耗时 < 10s、产物非空且正文等于服务端 `text_accu`。
3. **重复性**：新 pin 下同一条测试连续 ≥5 次全绿（≥3.11）。
4. **邻侧不回归**：`test_capswriter_contract.py`、`test_capswriter_deadline_budget.py`、
   `test_capswriter_retry.py`、`test_capswriter_samples_total_contract.py`、
   `test_capswriter_transcode_argv.py` 整文件跑，外加 `make test` 全量。

## 4. 验收命令（主脑复核用）

```bash
uv sync --frozen                                     # 锁文件可安装
uv run --frozen python -c "import hashlib,capswriter_asr.client as c;print(hashlib.sha256(open(c.__file__,'rb').read()).hexdigest())"
#   期望 eccec1a69b81c4a2360d33e15f0ddb8adffb85725dc93d41f8e3150a0863f6c7
git diff -- pyproject.toml uv.lock                   # 只应出现 rev 两处
uv run --frozen pytest -q tests/unit/test_capswriter_sdk_transport.py
make test
```

## 5. 部署验收（第二步另卡，本卡不做）

第二步只重建并替换 **API 镜像**（`video-transcript-api`），验收判据：

1. **server 端不动**：CapsWriter 服务端进程、代码与配置零改动；SDK 只存在于 API 镜像内。
2. **原字节保留**：`config/config.jsonc`、`config/users.json`、`.env`、`docker-compose.yml` 在升级前后
   SHA-256 逐字节相等（升级前后各算一次并留档），不得借机「顺手改配置」。
3. **缓存不能冒充引擎成功**：验收请求必须用**本次镜像首次未见过的媒体/新任务**，产物里的
   `task_id` 与服务端日志同 UUID 对得上；命中旧缓存的请求不算引擎成功证据。
4. **#166 仅真实成功后关**：只有当真实样本经新镜像端到端转录在远小于 120s 内返回并产出正文，
   且服务端日志出现同 UUID 的 `task_end(done) → result_dispatched`，才允许关闭 #166；
   任一环节缺失时保持 open。

## 6. 风险与边界

- 上游只修了 final 收尾主缺陷。#67（连接失败时错误交付被 `wait_for` 吞取消拖到预算到点）与
  #68（Python 3.10 上 `except TimeoutError` 抓不到 `asyncio.TimeoutError`）本卡明确不修，
  本仓 `requires-python >= 3.11`、`.python-version` = 3.11，#68 不落在本仓运行环境。
- I6 是本测试的真实盲区：若哪天的 CI 解释器换成 ≥3.12，这条测试会 skip 而不是守住回归。
  该事实必须写进验收结论，不允许把 skip 读成「回归已锁住」。