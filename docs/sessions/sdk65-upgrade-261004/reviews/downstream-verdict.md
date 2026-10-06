<!-- outcome: pass -->
failure-visibility: clean

# SDK65 下游接入独立审查结论

## 结论

**Review verdict：PASS；无 P1/P2 finding。**

审查对象固定为 `ff92a175243dde36143da867a95e2fdad384e16c..ac429d6e54ec8ee988f9dc6239bd72c3a0d8daad`，未把 review 工作树切换到目标分支，也未把目标分支合并进 review 分支。冻结 diff 中生产接入只把 `capswriter-asr` 从旧 pin 换到锁定的 `b0818dc7859d1d8100e42f5c70cb75d34da422f7`；回归测试覆盖真实同步入口、真实 SDK 序列化帧、真实 loopback WebSocket 服务、保持连接、UUID 回传、非空产物和及时返回。

## 不变式核对

| 条款 | 结果 | 证据 |
| --- | --- | --- |
| I1 final 到达且连接保持打开时及时返回 Transcript 和非空产物 | PASS | 目标 pin 的新增用例在真实 CPython 3.11.15、临时 detached worktree、真实 ffmpeg 与真实 `websockets.serve` 下连续 5 次通过；每次均断言 final 先到、同步入口随后返回、三类产物非空且正文等于服务端文本。 |
| I2 不通过调大预算掩盖问题 | PASS | `pyproject.toml:87`、`uv.lock:196`、`uv.lock:4511` 只更新 SDK rev；冻结 diff 不包含 `capswriter_client.py`、预算常量或 SDK 默认预算语义。真实 CI 的全量 pytest 也通过。 |
| I3 真实解释器、真实 SDK、真实序列化边界 | PASS | `tests/unit/test_capswriter_sdk_transport.py:47-169` 在子进程内启动真实 SDK 与 `websockets.serve`；父子进程用 SDK `client.py` SHA-256 互证，运行时报告 `eccec1a69b81c4a2360d33e15f0ddb8adffb85725dc93d41f8e3150a0863f6c7`。 |
| I4 不访问公网/生产 | PASS | 测试服务绑定 `127.0.0.1:0`，媒体由测试现场生成 0.2 秒 WAV；运行期间的仓库网络守卫明确阻断非 loopback AF_INET/AF_INET6 连接。 |
| I5 pin、lock、运行时 SDK 一致 | PASS | 目标提交本地 SHA 与 `origin/card/sdk65-upgrade-261004` 远端 SHA 均为 `ac429d6e54ec8ee988f9dc6239bd72c3a0d8daad`；`pyproject.toml` 与 `uv.lock` 指向同一 pin；临时环境实际安装 `capswriter-asr` b0818dc。 |
| I6 3.12 以上显式 skip，不把 skip 当回归通过 | PASS | `tests/unit/test_capswriter_sdk_transport.py:249-255` 显式 skip；PR #168 真实质量作业日志使用 CPython 3.11.17，故本次 CI 没有落入 skip。未来 CI 若切到 3.12，仍会失去该回归的观测能力，这是设计已披露的盲区。 |
| I7 子进程硬截止和清理边界 | PASS | `tests/unit/test_capswriter_sdk_transport.py:271-278` 使用 45 秒父进程硬截止，子进程正常/异常路径均 `os._exit`；5 次运行后无临时 detached worktree 残留。旧 pin 红验也在约 6.028 秒以断言失败结束，没有触发 45 秒 kill。 |

## Finding 分诊

没有 P1/P2 finding。累计 diff 为 `6 files changed, 367 insertions(+), 3 deletions(-)`，超过任务卡 `Diff-Lines-Target: 120` 与 `Diff-Lines-Hard: 250`；这是 delegate 预算的 `over_hard` 状态，按预算规则只作状态记录、不作为阻塞性 review finding。其中文档新增 128 行，非文档改动为 248 additions、3 deletions。

## 独立测试证据

### 目标 pin 绿验

在 `scratch-worktree.sh` 创建的 `ac429d6e54ec8ee988f9dc6239bd72c3a0d8daad` detached 沙箱中，以沙箱自己的 `.venv` 执行：

```text
uv venv --python <uv-managed-cpython-3.11> .venv
uv sync --frozen
uv run --frozen python -VV
uv run --frozen python -c "import hashlib, importlib.metadata as md; import capswriter_asr.client as c; print(md.version('websockets')); print(hashlib.sha256(open(c.__file__, 'rb').read()).hexdigest())"
for run in 1 2 3 4 5; do
  uv run --frozen pytest -q tests/unit/test_capswriter_sdk_transport.py::test_final_result_returns_and_writes_products_on_python311
done
```

结果：CPython 3.11.15、`websockets=15.0.1`、SDK 文件 SHA 与 I5 一致；`GREEN_RUN=1..5` 均 `[100%]`，scratch worktree 已清理。

### 旧 pin 红验

在 base `ff92a175243dde36143da867a95e2fdad384e16c` 的独立 detached 沙箱中，仅拷入目标新增测试文件后执行同一用例。结果为 `RED_EXIT=1`、`EXPECTED_ASSERTION_RED=1`；失败落在 `tests/unit/test_capswriter_sdk_transport.py:306` 的 `payload["success"] is True` 断言，子进程日志显示：

```text
elapsed=6.028s
code=timeout
原因: 转录超过deadline_total：远端转录阶段超时
```

这是预期的 AssertionError 红，不是导入错误、依赖错误或硬超时 kill；旧 pin 实际为 `858c6b975d8bdd2be0e47ac5a36483119894c529`，锁定 `websockets` 仍为 15.0.1。

### 真实 CI

PR #168 真实 `gate / quality` 作业日志记录 `Using CPython 3.11.17`，执行 `uv run --frozen pytest -q`，进度到 `[100%]` 且作业 conclusion 为 `SUCCESS`。这是 draft PR：`gate / primary` 为 `SKIPPED`，aggregator 的 draft 绿不能当作主审已执行；本报告仅把 quality 作业作为运行时与全量测试证据。

### OCR 前置扫描

`ocr-review` envelope 为 `status=reviewed`、`coverage=complete`、`findings=[]`。外层 zsh 收尾因误写特殊变量 `status` 返回非零，但 envelope 已完整产出且主腿为 `minimax`；不把该 shell 收尾错误误判成 OCR skipped。

## 已知盲区与未判定项

- 3.12 以上按设计 skip，不能证明该环境仍守住 ≤3.11 的回归；本次已用真实 CI 3.11.17 和本地 3.11.15 覆盖。
- 主干基线作业在派发时 `gh api request failed`，因此无法用同名首失败步骤判定继承红；本次结论来自冻结 diff 和独立运行证据。
- 未调用公网或生产 ASR，也未把不存在的媒体夹具 skip 当作本次引入的通过；新增回归使用现场合成媒体并实际执行。

## 四问记录

- **踩坑**：review 工作树 `HEAD` 是 base `ff92a175`，冻结审查头是另一个已验证的 `ac429d6e`；若直接用 `HEAD` 会漏掉全部目标 diff。
- **闸**：pin/lock 唯一性、运行时 SDK SHA、CPython 3.11、真实 WebSocket/health、UUID、保持连接、产物、旧 pin AssertionError 红和 5 次绿均已逐项核对；未部署、未发公网 ASR 请求、未修改业务源。
- **偏差**：当前默认解释器是 3.12.3，因而所有关键运行改用 uv 管理的 3.11.15；CI 实际为 3.11.17。记忆探针原样失败：`memory 巡检报告不可用：memory_dir_mismatch（memory-doctor/latest.json）`。巡检摘要：`summary: orphan 0 owned 0 unattributable 0 too-new 0 recent-7d 0 stale-over-7d 0 missing_ledger_repos 0`。
- **最贵步骤**：目标 pin 沙箱的 `uv sync --frozen` 加 5 次真实子进程回归，以及读取真实 CI 全量测试日志；它们分别验证了依赖可安装性、重复性和 3.11 消费环境，而不是只看静态 diff。

## 实际 SHA 与现场

```text
base local:   ff92a175243dde36143da867a95e2fdad384e16c
head local:   ac429d6e54ec8ee988f9dc6239bd72c3a0d8daad
head remote:  ac429d6e54ec8ee988f9dc6239bd72c3a0d8daad
review tree:  clean before verdict write; no scratch worktree remained
```
