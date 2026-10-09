<!-- delegate-outcome: failed -->
# F：固定 H1 上传工具候选的有界真实诊断

## 结论

- A（同一恢复对象的消费者门槛）：实测支持 gate 生效。原恢复测试只锁了 gate=false 的拒读及清理保护；没有同一个 restored cache/token/body 的 gate=true 可读对照。独立 H1 scratch 中，真实 CacheManager producer 生成 snapshot、撤销源记录、恢复 snapshot，再由 fresh Resolver 对同一恢复目录和同一 token 做 true/false 对照：true 读回正文，false 返回 NONE 并记录 `UPLOAD_DISABLED`；一个无效 token 的最小反例返回 NONE。清理后 never 正文与 root 仍在。
- B（容量 CLI 等待自己的 server ready）：未启动六次计划运行。官方 scratch 的网络命名空间前置检查被内核以 `Operation not permitted` 拒绝；没有尝试在可外连的宿主网络上运行 CLI/pytest。故这不是 CLI 失败，PID、实际绑定端口、socket/ready 时序及 stop callback 执行后状态均未知，根因仍未知。
- 本报告任务结果为 `failed`：B 是本卡主要取证对象，因环境限制未完成；这与软件 pass/fail 正交。A 的实测成功不替代 B，也不证明工具稳定。没有产品代码、测试、配置或目标外文件改动；未启用生产上传。

## 基线与隔离

- 固定源：H1 `13e78042ac4ff97ae0fd42fa15031848259f985d`。目标分支：`card/vta-upload-F-diagnose-261009`。目标报告此前不存在。
- 诊断开始前的官方 scratch manifest：`$HOME/scratchpad/vt-QHD1cx/worktree`，HEAD=H1，669 个 tracked file；`data`、`liveconfig`、`.venv`、`.pytest_cache`、`config/config.jsonc` 均不存在；无 tracked symlink、无外链，和 main/原工作树 tracked inode 交集均为 0。`git show H1:<path>` 检出的白名单源 blob 与上列固定 SHA 对应。
- A 的实测在另一个官方 scratch：`$HOME/scratchpad/vt-Fu6wa1/worktree`，HEAD=同一 H1；测试/CacheManager/Resolver 的运行时 `__file__` 均指向该 scratch。独立 venv 由 `uv sync --frozen --offline` 创建，Python 3.11.15，离线安装 71 个锁定包；无外网访问。producer DB、正文、审计库均位于本次专用 `/tmp/vta-upload-f-diag-20261009/restore-pair-k0myhwxl/`，不在业务树。
- A 有两次未进入 producer 的准备失败，均保留并与软件结果分开：第一轮临时 harness 漏加 repo root 到 `sys.path`，报 `No module named 'src'`；修 harness 后系统 Python 3.12 环境缺 `loguru`。随后使用上述 scratch 独立离线 venv 完成唯一一次匹配 producer/consumer 对照；没有重复成功运行。
- B 前置检查在官方 H1 scratch、`env -i` 裸环境执行 `unshare -n` 并尝试启用该 namespace 的 loopback；命令退出码 1，错误为 `unshare failed: Operation not permitted`，未创建网络命名空间。`uv sync --frozen --offline` 可用，故已区分出已验证的 namespace 阻塞与可用的依赖环境。`strace` 未调用；不把本机有该程序误作诊断证据。
- 主干基线 API 在派发时不可用（卡面：`gh api request failed`）。继承红状态未能判定；本卡没有新增 CI run/job 结论。

## A：恢复对象与同对象 on/off 消费

### 原测试实际锁定范围

H1 `tests/integration/test_upload_restore.py` blob `b0b6ae6f2237e74b9a2d8ed059d02428bd90cffa`：

- `test_snapshot_restore_keeps_external_gate_off_and_protects_long_result`（101 行起）调用 `_new_upload`、`_write_real_result`、`_age_real_consumers`（24–62 行）；在撤销前复制 producer cache 与正文（108–114 行），对源 cache 撤销后把该 snapshot 复制为 restored cache（116–126 行）。
- restored cache 的记录断言 `revoked_at is None`、`retention == "never"`，但 fresh 子进程只以 `VTA_UPLOADS_ENABLED=false` 调 `_resolver_process`；测试断言 `RESULT=NONE`、`BODY=NONE`、`UPLOAD_DISABLED`（128–139 行）。因此它缺少同一恢复对象、同一 token、同一正文的 true 可读对照。不同 cache、旧源码消费者或其他测试中的正例均不能补成这个同对象对照。
- 测试随后断言 `cleanup_old_cache(days=1) == 0`、正文仍在、`cleanup_task_status(...) == 0`、root 仍在（141–148 行）。

### 本次真实匹配对照

- 使用上述 H1 测试文件中的真实 producer helper 生成本地 upload、`save_cache` 正文及 root；正文为 36 bytes，SHA-256 `89861627bcb6997502cb61976eee2f6e0ce22a0c691d415dc14b6735e5c155cc`。
- `pre-revocation-snapshot/cache.db` 与恢复后的 `restored-cache/cache.db` 都是 98,304 bytes，SHA-256 均为 `1e0362bd41abd4e666de5776f0c4bf2b0eb98cf6fdf0180e9febd7231e2cc0fb`；恢复记录 `revoked_at=null`、`retention=never`。正文恢复路径为 `local_upload/2026/202610/<synthetic-media-id>/transcript_capswriter.txt`，恢复后字节与 producer 完全一致。
- true 与 false 均各启动 fresh Python 子进程，argv 指向同一个 `restored-cache`；token 均为本次合成 token，SHA-256 `e5626833c40cca7c9558020b99e3edc76f12d05a2008731b01c49a2a857c74f0`。子进程环境只在 `VTA_UPLOADS_ENABLED` 上分别设 `true` / `false`，`PYTHONPATH` 指向实际 H1 scratch 的 `src`；未输出原始 token。

| 新鲜消费者 | gate | 退出码 | Resolver 实际结果 | 来源/判据 |
| --- | --- | ---: | --- | --- |
| 同 restored cache/token/body | true | 0 | `RESULT=success`，`BODY=Producer-created restore transcript.` | fresh Resolver；CacheManager 与 Resolver `__file__` 均映射到 H1 scratch |
| 同 restored cache/token/body | false | 0 | `RESULT=NONE`，`BODY=NONE`；可 grep `UPLOAD_DISABLED` | 同目录、同 token SHA、同正文字节 |
| 最小反例：同 cache 的无效 token | true | 0 | `RESULT=NONE`，`BODY=NONE` | 一个最小真实反例；token SHA 与正例不同 |

Resolver 运行时来源 SHA-256：CacheManager `082210a0aacfda08fa22ed13512bdb67096b099fbcf1da6594e1cb6131509243`（H1 blob `7afdfecf0697e618f7819202810390992ae13fe8`）；`view_token_resolver.py` `6f699b5f971379c6462316ef40a0afa8eca6072ee9c54ea2fab5cede980083a2`（H1 blob `a767da1daa4f56cc3d4cbf7d7fa551887869c30d`）。消费进程执行的 `ViewTokenResolver(cm).get_view_data_by_token(token)` 与原 helper 的核心调用相同，额外只打印来源路径/hash、gate、token hash 和结果，不改请求、等待或清理行为。

- 同一 restored CacheManager 随后实际运行两个 cleaner：`cleanup_old_cache` 返回 0、`cleanup_task_status` 返回 0；正文仍与原 36 bytes 相同，root 仍可读取。A 的证据核验器对上述 true/false/invalid 分支、DB hash、正文 hash 与清理结果逐项断言为 PASS。
- 结论限于单一本地 SQLite+正文对象与当前 H1 Resolver；不代表独立生产恢复域、生产数据或授权。

## B：容量 CLI 与聚焦 pytest 的有界运行

### 前置阻塞与六次分母

运行要求是同一新 H1 scratch、独立离线 venv、`env -i` 裸 shell、无外网网络命名空间；预定冷序列为直接 CLI 与现有 duration=20 聚焦 pytest 交替各 3 次。namespace 检查失败后没有启动任一 producer。下表的 `N/A` 表示未运行，不是退出码 0 或软件失败。

| 次序 | 计划对象与 argv | 结果/退出码 | PID/地址/ready/cleanup |
| ---: | --- | --- | --- |
| 1 | `python scripts/perf/local_upload_capacity.py --duration-seconds 20` | 未启动；namespace 前置阻塞；N/A | 无 producer，N/A |
| 2 | `pytest -q tests/integration/test_upload_restore.py::test_local_capacity_probe_exercises_real_http_payload_and_loopback_overlap` | 未启动；namespace 前置阻塞；N/A | 无 producer，N/A |
| 3 | 同次序 1 | 未启动；namespace 前置阻塞；N/A | 无 producer，N/A |
| 4 | 同次序 2 | 未启动；namespace 前置阻塞；N/A | 无 producer，N/A |
| 5 | 同次序 1 | 未启动；namespace 前置阻塞；N/A | 无 producer，N/A |
| 6 | 同次序 2 | 未启动；namespace 前置阻塞；N/A | 无 producer，N/A |

因此本卡没有 CLI 异常、pytest 退出码、真实 argv/env/config bytes/hash、PID、实际绑定端口、socket 状态、服务就绪信号、请求前后时间戳或 child cleanup 后状态。没有读取或回显服务原始日志；没有慢启动对照，也没有把任何准备失败记成 CLI failure。

### H1 源码所能支持的有限事实（不是实测根因）

- `scripts/perf/local_upload_capacity.py::run_experiment`（238 行起）在 ExitStack 内先建 loopback ASR rejector、合成 config/users/media fixture（257–298 行）。API child 由 `Popen([sys.executable, main.py, --start, --config, config_path], cwd=PROJECT_ROOT, env=clean_env, stdout=service_log, stderr=STDOUT)` 启动（300–306 行）；`clean_env` 只显式放入 PATH/HOME/LANG/LC_ALL/TZ/PYTHONPATH/VTAPI_USERS_JSON/VTA_UPLOADS_ENABLED。配置由 tracked 示例派生，API、媒体与 ASR fixture 均指定 loopback；临时配置、用户、日志与 data 位于脚本的 `TemporaryDirectory`。这些是源代码路径，不是本卡某次启动的实际 payload。
- 固定 `time.sleep(2)` 后只做 `process.poll()`；仍存活才发 `GET /api/uploads/capabilities`（308–324 行）。源码中没有在请求前轮询 listen socket 或以 readiness event 等待。focused pytest 在 `tests/integration/test_upload_restore.py:150–180` 启动同一个 CLI，duration=20，subprocess timeout=90 秒。
- API child 的 `ExitStack` stop callback 在启动后立即登记（257、307 行）。`_stop_process`（210–213 行）在 callback 执行时先检查 `poll()`，仍存活才 `terminate()`，随后 `wait(timeout=10)`。本卡没有运行到该 callback，因此不能声称实际调用了它、child 在 callback 后已回收，或错误发生在进程退出之后。
- 所以，固定 2 秒是可检验的启动敏感点，但没有本次 spawn/listen/capabilities/退出时间线，也没有可控延迟 listen 的对照；不能据此宣告它是另一次报告失败的根因。根因保持 unknown。

## 边界、后续与产物

- 最小后续范围仅是一次有界现场：提供能创建无外网 network namespace 且 loopback 可用的官方 runner，在该环境使用新的 H1 scratch 与离线 venv，按上表原序列执行六次，并在每次只采白名单 PID/phase/地址/时间差/进程退出与 stop callback 后状态。若仍不能提供 namespace，停止在已知环境阻塞，不把宿主网络当替代品、不改生产、不扩展候选机制。
- 不改代码、不修复测试、不新增 seam；A 证据不能替代完整独审。M4/M5、真实生产容量/恢复域/授权仍未知，上传生产开关保持关闭。
- 唯一仓库产物：`docs/sessions/261008-local-upload/diagnosis/F-local-consumers.md`。本报告的源证据 SHA 是 H1；最终报告提交 SHA、push remote/ref 与 clean 状态由派发结果回执记录，避免在被提交文件中写入会自我循环的 commit SHA。
