# DESIGN-note：把「测试不污染自己、不打真实外网」从偶然变成判据（#138 / #128）

本批两张卡：卡 A 修测试自身的后台线程泄漏（quick/S），卡 B 把出站网络守卫提升为全局门禁判据（big/M）。用户 2026-10-02 已点头本文档「方案要点」节。

## 目标

- #138：`tests/` 跑完后不留后台线程，下一条用例的进程级状态不被上一条污染。
- #128：默认门禁（`make test` = `pytest -q tests`）下不存在指向非 loopback 的出站 connect，且这条不变式有自动判据守着。

## 非目标

- 不修 `ASRMonitor` 生产代码的生命周期（不新增 `join()` / `close()`），不改通知器吞错逻辑（另议）。
- 不动 `tests/manual/`（`norecursedirs` + conftest 双重排除，不在门禁覆盖内），不重写公开仓 git 历史（用户 2026-10-02 定）。
- 不做 PR #117 的 token 时间轴对齐实现。

## 为什么不是分区 / 删除 / 约定

- **分区**：泄漏方（`test_asr_alert.py` 起线程）与受害方（`test_asr_monitor_alert_delivery.py` 的进程级守卫）分属两个文件、两个用例；能靠「各管各的 teardown」消失，但污染源不止这一个文件，未来任何起后台线程的用例会重犯——只有全局判据（卡 B）能把受害方的假设变成契约。
- **删除**：`ASRMonitor.start()` 本身是生产在用的生命周期入口（`start_asr_monitor()`，`src/video_transcript_api/utils/asr_monitor.py:262`），不可删；删「stop 不 join」也做不到——线程存活期由 `check_service` 的网络超时决定，不 join 就必然越界到下一条用例。
- **约定**：「记得在 teardown 里收尾」正是 #128 立单的起因（今天安全靠所有发通知的测试恰好都记得 mock），约定无法被自动检查抓住。

## 方案要点与已否决方案

- **要点**：
  - 卡 A 只改 `tests/unit/test_asr_alert.py`：teardown 收尾处 join monitor 线程并断言线程已退出；补一条锁死「用例结束后 monitor 线程不再存活」的测试。
  - 卡 B 把守卫从模块级提到 `tests/conftest.py` 的 autouse，口径按 #128 原帖：**DNS 放行、只拦非 loopback connect**；`tests/unit/test_asr_monitor_alert_delivery.py` 改为复用全局守卫，不留第二份实现。
  - 两卡文件集零交集（卡 A：`tests/unit/test_asr_alert.py`；卡 B：`tests/conftest.py` + 该守卫文件），可并行；验证命令不抢独占资源——全仓 `tests/` 只有一处 `.listen(`（`test_asr_monitor_ws_probe.py:127`，端口 0 随机），无固定端口、无共享 DB、无绝对路径写盘。
- **已否决**（接手人不得重新提起）：
  - 给 `ASRMonitor` 加生命周期方法（#138 正文方向 3）：触及 `src/` 属结构性变更，测试侧 join 已足够；只有卡 A 实测证明测试侧收不了尾才重开。
  - 给守卫加白名单/过滤以忽略其它 loopback 流量：PR #137 已明确否决（把问题藏起来）。
  - 连 DNS 一起拦：会打断 SSRF/URL 校验用例的真实解析路径，用户 2026-10-02 拍板按 #128 原口径。
  - 从 n305 拉生产 `config.jsonc` 给开发机：会让开发调试打生产。

## 关键不变式

1. [实测] 泄漏源是 `tests/unit/test_asr_alert.py` 的 `TestServiceCheck::test_start_and_stop`（该文件第 151 行 `monitor.start()`），**不是** #138 正文写的 `test_asr_monitor_ws_probe.py`——后者全文未调用 `start()`。判据：`grep -rn "\.start()" tests/unit/test_asr_alert.py tests/unit/test_asr_monitor_alert_delivery.py tests/unit/test_asr_monitor_ws_probe.py` 只有 `test_asr_alert.py:151` 一处业务命中；服务名 `TestASR` 全仓只在该文件出现（`grep -rln TestASR tests/ src/`）。锁死测试：卡 A 新增的线程存活断言。
2. [实测] `ASRMonitor.stop()`（`src/video_transcript_api/utils/asr_monitor.py:80-83`）只置 `_running=False`，不 join；`_monitor_loop`（同文件 `:138-153`）先跑完整轮 services 检查再进等待循环，所以 `stop()` 返回后线程仍会打完一轮，包括 `ws://localhost:9999` 的握手与 `logger.warning("service ... check failed (1/3)")`（`:176`）。锁死测试：卡 A 的 teardown 断言。
3. [实测] 现有守卫在 `tests/unit/test_asr_monitor_alert_delivery.py:91-121`，是**模块级** autouse，且 `fake_getaddrinfo` 一并拦非 loopback DNS；#128 要求的口径相反（DNS 放行，`tests/unit/test_input_validation.py::TestWebhookURLValidation` 等 SSRF 校验用例需要真实解析，见 #128 实测表）。锁死测试：卡 B 的全局守卫用例 + 注入红验。
4. [实测] 默认门禁下非 loopback connect 计数为 0（#128 于 2026-10-02 `main@64033148` 实测 44 行出站记录，connect 全指向 loopback）。这是快照不是不变式——它正是卡 B 要用判据守住的东西。

## 待验证前提

1. [推断] 把守卫提升为全局 autouse 后，没有测试依赖真实非 loopback connect（否则全量门禁会红）。验证入口：`make test` 全量退出码；红了就说明存在这类用例，需在卡 B 报告里点名而不是加豁免白名单。
2. [推断] 卡 B 的判据在真实 CI（self-hosted runner + `zlxlabs/gate` 复用工作流）里同样成立。验证入口：PR 的 gate run `conclusion == SUCCESS`（`gh pr checks`，不接受 SKIPPED）。

## 验收路径

1. 入口：本地 `make test`（`pytest -q tests`，`tests/manual/` 不在覆盖内）；CI 侧 gate run。
2. 步骤：
   - 卡 A：`uv run --frozen pytest tests/unit/test_asr_monitor_alert_delivery.py tests/unit/test_asr_monitor_ws_probe.py tests/unit/test_asr_alert.py -q` 连跑 ≥20 次，失败数恒 0，且日志中不再出现别的用例 teardown 带 `service TestASR check failed`；再跑 `make test`。
   - 卡 B：注入红验（故意让某用例真连一次外网，该用例必须转红，贴原文）；再跑 `make test`；最后在 PR 上确认 gate run `conclusion == SUCCESS`。
3. 预期：两条不变式各被一条测试锁死，且判据在开发机与 CI 两侧都验过。
