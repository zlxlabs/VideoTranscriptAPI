<!-- delegate-outcome: pending -->

# SDK 65 升级进度（card/sdk65-upgrade-261004）

- 派发：`dlg-20261004-101338-ef5061`
- Issue：#166（本卡不关）
- 锁定决策：唯一 pin `b0818dc7859d1d8100e42f5c70cb75d34da422f7`，不漂 HEAD；本卡不部署。

## 里程碑

- [x] M0 现场核查：worktree `card/sdk65-upgrade-261004` @ `ff92a175`，干净；旧 pin `858c6b9`，
      运行时 `client.py` SHA-256 = `ff476ad7cd40401b7ed77c7606cb4fc14dc3d529043c29c4714b199c13423cdf`，
      解释器 CPython 3.11.15，`websockets` 15.0.1。
- [x] M1 读完上游 `root-cause.md` / `independent-review2-verdict.md` 与 triage `wire-probe.md`，
      确认根因与修法边界（本卡不改适配层）。
- [x] M2 设计落盘：`docs/sessions/sdk65-upgrade-261004/design.md`（不变式 / TDD / 部署验收）。
- [x] M3 旧 pin 现场红证（未写测试，先用等价探针脚本确认红法可复现）：
      final 于 `+0.123s` 送达，同步入口 `+6.028s` 才以 `code=timeout` 结束，`success=False`、零产物。
- [x] M4 回归测试落盘（`tests/unit/test_capswriter_sdk_transport.py`），旧 pin 红：
      `AssertionError: sync entry did not return a transcript even though final was delivered:
      elapsed=6.029s logs=[... code=timeout ...]`（final 于 `+0.12s` 送达，零产物）。
      红是断言失败，不是超时 kill / import 错误。
- [x] M5 pin/lock 切到 `b0818dc`，`uv lock` 只改 rev 两处，`uv sync --frozen` 装成，
      运行时 `client.py` SHA-256 = `eccec1a69b81c4a2360d33e15f0ddb8adffb85725dc93d41f8e3150a0863f6c7`。
- [x] M6 绿且快：同一测试连续 5 次 EXIT=0（新 pin 下 `elapsed=0.116s`，final 于 `+0.082s` 送达）；
      capswriter 邻侧 7 个文件整跑 `66 passed, 3 skipped`；`make test` EXIT=0（3m10s，0 FAILED/ERROR）。
- [x] M7 提交、push、draft PR #168（Refs #166，未 ready/merge）：
      https://github.com/zlxlabs/VideoTranscriptAPI/pull/168 ，
      CI run 37195552335（`quality` SUCCESS、`primary` 因 draft SKIPPED）。