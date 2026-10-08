# 测试说明

测试使用 pytest，开发依赖位于默认同步的 `dev` dependency group：

```bash
uv sync
```

## 目录结构

| 路径 | 用途 |
| --- | --- |
| `tests/unit/` | 快速单元测试。 |
| `tests/cache/` | 缓存行为测试。 |
| `tests/integration/` | 组件集成测试。 |
| `tests/features/` | 功能级测试。 |
| `tests/llm/` | LLM 相关的本地测试。 |
| `tests/transcript/` | 转录兼容性与转换测试。 |
| `tests/deployment/` | 部署及健康检查相关测试。 |
| `tests/platforms/` | 仅有 `__init__.py` 和演示脚本 `demo_bilibili_metadata.py`，目前没有自动测试。 |
| `tests/manual/` | 需要人工明确确认的网络、服务或真实凭据测试。 |
| `tests/test_*.py` | 位于 `tests/` 根目录的补充测试。 |
| `scripts/perf/concurrent_load.py` | 手工并发压测脚本，不属于 pytest 回归测试。 |

## 常用命令

```bash
# 全量自动测试：发现 tests/ 下的测试，排除 tests/manual/
make test

# 卡片验证范围
uv run --frozen pytest tests/unit tests/integration

# 按需运行其他本地测试目录
uv run pytest tests/integration
uv run pytest tests/features
uv run pytest tests/llm
uv run pytest tests/transcript
uv run pytest tests/deployment
```

`make test` 是 CI 和本地的全量自动测试入口：执行 `uv sync --frozen`，再运行
`uv run --frozen pytest -q tests`。pytest 配置会排除 `tests/manual/`；根目录的
`tests/test_*.py` 和各自动测试目录都会被发现。

默认门禁会阻断指向非 loopback 地址的 AF_INET/AF_INET6 出站连接；DNS 解析、
loopback 与 Unix 域套接字放行，真实外网测试仍应放在 `tests/manual/`。

任务通知数量回归：`tests/unit/test_notification_budget.py` 从真实转录入口运行下载、平台字幕、YouTube API 字幕、部分/全部缓存命中与两种失败路径，断言每条路径仅一条终态消息、短编号抬头和成功正文；同文件还以 25 MiB 下载 fixture 锁定 generic 下载不发进度通知。发件箱重启补发与飞书卡片标题由 `tests/unit/test_terminal_notification_outbox.py` 覆盖。终态通知参数贯通（N3）：`tests/unit/test_notification_e2e_delivery.py` 用真实 `NotificationRouter` + 真实 `WeComChannel` / `FeishuChannel`，只把 `WechatNotifier.send_text` 与 `FeishuNotifier.send_card` 换成替身，对 `deliver_terminal_notification` 与 `deliver_pending_terminal_notifications` 两个入口断言双渠道首行统一抬头、正文携带快照总结（N1–N4 见 `docs/sessions/261007-notify-slim/design.md`）。

任务观测回归：`tests/unit/test_task_observability_report.py` 覆盖只读 CLI、旧 schema、
UTC 窗口、去重、缺字段与失败判据；`tests/integration/test_task_observability.py`
贯通 notes worker、cache/audit SQLite 与 CLI 子进程，验证归档修复、缓存清理和迁移。

配置生效值回归：`tests/unit/test_check_config_effective_values.py` 以真实 subprocess
跑 `main.py --check-config`，断言末行 JSON 含且仅含 9 个白名单 LLM 键、各带
`value` + `source`，且 `value` 与 `LLMConfig.from_dict` 逐字段相等、凭据 sentinel
不泄漏；`tests/unit/test_runtime_lifecycle.py` 邻侧只锁 stdout 形态与无副作用。
显式 `null` / `False`、缺失键、`fallback_to_original=False` 与空串
`fallback_strategy` 的来源标签各自独立覆盖。

CapsWriter final 返回回归（#166）：`tests/unit/test_capswriter_sdk_transport.py`
里的 `test_final_result_returns_and_writes_products_on_python311` 跑真实子进程 ——
进程内起真实 `websockets.serve`（随机 loopback 端口 + 真实 `/health`），按 SDK 实际
序列化的 UUID 回 final 并**保持连接打开**，再走本仓同步适配入口
`CapsWriterClient.transcribe_file`。它锁的是「final 已送达就必须返回 Transcript
与非空产物」，不是耗时数字：成功预算硬边界 10s，子进程硬截止 45s（父进程 kill）。
该缺陷只在 CPython ≤3.11 出现（3.12 重写了 `wait_for`），因此 ≥3.12 显式 skip；
skip 不等于守住回归，见 `docs/sessions/sdk65-upgrade-261004/design.md` I6。

## 手动测试门禁

`tests/manual/` 默认自动发现时被排除；即使显式传入某个手动测试文件，未设置
环境变量也会被跳过：

```bash
# 安全：收集并显示手动测试在默认情况下会被 skip
uv run pytest tests/manual/test_wechat_real.py -rs

# 仅验证已明确选择手动模式后的收集结果，不执行测试体
VTAPI_TESTS_MANUAL=1 uv run pytest tests/manual/test_wechat_real.py --collect-only
```

只有在明确了解真实网络、webhook 和凭据影响时，才设置
`VTAPI_TESTS_MANUAL=1` 执行手动测试。请勿将会发送 webhook 的测试作为常规
验收命令运行。

该开关只认 `1`：写成 `true` / `yes` / `TRUE` 等其它拼写一律不生效（手动测试
会发真实 webhook、用真实凭据，危险操作的开关应当保守）。它只控制
`tests/manual/` 下用例的收集与否，不影响其余测试，也不影响默认测试套件的
占位配置预热——预热由 `config.jsonc` 是否缺失决定。

## pytest markers

项目已注册以下 marker：

- `unit`：快速、无 I/O 的单元测试。
- `integration`：可能依赖服务的集成测试。
- `slow`：耗时较长或使用大数据的测试。
- `network`：访问真实外部服务的测试。

显式收集 `tests/manual/` 时，目录级配置会自动为所有项添加 `slow` 和
`network`。例如，以下命令可验证 marker 兜底不会选择手动网络测试：

```bash
uv run pytest tests/manual -m "not network" --collect-only
```

## 需要 ffmpeg / ffprobe 的测试

`tests/unit/test_transcription_audio_admission.py`（#159 音轨准入）在真实共享
准入边界上验证行为：样本由 `ffmpeg -f lavfi` **现场生成**（video-only /
audio-only / mixed），准入探测跑仓库自己启动的**真实 ffprobe**，并把真实 argv、
真实 JSON、样本大小与探测耗时写成验证产物到 `data/temp/audio_admission_probe/`。
因此该文件依赖 PATH 上的 `ffmpeg` 与 `ffprobe`；缺任一者时相关用例会带明确原因
skip（`-rs` 可见），此时**不能**认为准入已被验证。其它用 mock 媒体路径的测试
（下载/缓存/临时文件等）显式注入隔离的探测替身 `_ensure_audio_track`，不复用这批
真实 CLI 断言。

## 纯 mock 单元回归（无外部工具前置条件）

下面两个文件把外部依赖整个替身掉：ffprobe 子进程由 `subprocess.run` 的 mock /
一次性 runner 顶替，SDK 由 `transcribe_file_sync` 的 mock 顶替，媒体文件由
`tmp_path` 现场写。它们不读 `/tmp` 预置 fixture、不起真实子进程，所以**不会**因
缺 `ffmpeg` / `ffprobe` 而 skip——这与上一节的 skip 语义相反：把 `PATH` 清空到只剩
一个空目录后跑这两个文件仍是全 passed、无 `SKIPPED` 行，同一环境下上一节那个
文件则整段 skip（`-rs` 可见）。两节都要读：上一节证真实 argv/JSON，本节证编排与
数值契约。

CapsWriter 转录可观测性（#171）：`tests/unit/test_capswriter_observability.py`。
锁的不变量：SDK 给的 `task_id` 必须出现在开始、进度与终态日志里，SDK 没给时只写
`unknown`、不许编占位 id（`test_sdk_gets_on_progress_and_task_id_reaches_progress_and_terminal_logs`、
`test_task_id_comes_from_sdk_payload_never_fabricated`）；进度按
`PROGRESS_LOG_INTERVAL_SECONDS` 节流，首个事件必打（它带真实 `task_id`），窗口走完后
重新打开，否则长任务会再次静默（`test_progress_log_is_throttled_but_first_event_always_logged`、
`test_throttle_reopens_after_interval`）；媒体总时长未知时只写已处理秒数、不编百分比
（`test_progress_without_known_total_omits_percent`）；进度回调是第三方边界，回调内
抛异常只记 warning、转录照常成功（`test_progress_callback_exception_does_not_break_transcription`）；
观测代码不得自己去探测媒体（`test_progress_observability_adds_no_new_media_io` 把
`subprocess.run` 打成 AssertionError）。失败侧锁 `AsrError` 的 `code` 与原因随异常
消息走到通知文案（`test_failure_reason_travels_into_notification_message`、
`test_process_transcription_notifies_with_failure_reason`），以及上一次失败的细节不串进
下一次成功（`test_successful_run_clears_stale_failure_detail`）。前置条件只有项目自身
依赖（`capswriter_asr`、loguru）与 `tmp_path`；没有 skip 分支。

转录时限预算的时长来源（#173 修 #155）：`tests/unit/test_media_duration_probe.py`。
走真实入口 `process_transcription`，复用 `test_transcription_audio_admission.py` 的
`wired` fixture 与替身类（同一套真实入口协作方，不另造会漂移的副本），并让
`RouteDownloader` 复现缺陷现场——generic 路线从不写 `last_media_duration`。锁的不变量：
音轨准入**同一次** ffprobe 解出的 `format.duration` 真的作为 `media_duration` kwarg 传到
CapsWriter 层（`test_generic_path_duration_reaches_transcriber`）；修复不新增探测——
`SingleProbeRunner` 对第二次子进程调用当场 AssertionError，argv 必须带 `-show_format`、
零次 ffmpeg，且探测目标就是本次下载的媒体（`test_no_extra_media_probe`）；探测拿不到
合法时长时回退 `downloader.last_media_duration`、两边都拿不到时保持既有两参调用形状
（`test_unusable_probe_duration_falls_back_to_downloader_duration`、
`test_no_duration_anywhere_keeps_two_arg_call_shape`）；负数 / NaN / Inf / 空串 /
畸形数字 / JSON null / `format` 非对象一律当「没探测到」，判据读的是 CapsWriter 层真正
收到的实参（`test_invalid_duration_never_fabricates_a_value`、
`test_format_without_dict_is_not_duration`）；拒绝语义不变，无音轨与探测失败仍是具名原因
+ 引擎零调用（`test_no_audio_track_is_still_rejected_without_engine_call`、
`test_probe_failure_is_still_check_failure`、`test_admission_rejection_still_raises_invalid_media`）；
准入认可的时长必须被预算公式真正采纳（`deadline_total == duration*4+120`，而不是悄悄
退回 SDK 自动预算），准入返回 `None` 时预算侧同样拿不到 deadline
（`test_admission_returns_the_probed_duration`、
`test_admission_returns_none_for_unusable_duration`、
`test_every_admitted_duration_is_accepted_by_the_deadline_budget`）。前置条件：无真实
ffprobe / ffmpeg、无预置 fixture、没有 skip 分支。拒绝路径的完整矩阵仍由上一节那个
需要真实 ffprobe 的文件覆盖。

LLM 处理器空输入守卫（#179）：`tests/unit/test_empty_input_processors.py`。
锁的不变量：`PlainTextProcessor._calibrate_segments` 与 `SpeakerAwareProcessor._calibrate_chunks` 在输入分段/分块列表为空时直接返回与空输入同构的空结果，不启动 `ThreadPoolExecutor(max_workers=0)`，不发起 LLM 调用；`NotesProcessor.process` 在空 chapters payload 下走 `is_valid` 校验失败返回 `FAILED`，不发起 LLM 调用；端到端空文本/空对话校对返回诚实状态 `calibration_status=none` 且不抛 `ValueError`。

转录时限预算的零时长守卫（#190）：`tests/unit/test_capswriter_deadline_budget.py`。
锁的不变量：`media_duration` 为 0 或负数时与「拿不到时长」完全同路——
`_transcription_deadline` 返回 `None`，且真的不把 `deadline_total` 键传给
`transcribe_file_sync`，由 SDK 改用转码后的实际采样数算时长
（`test_non_positive_duration_is_treated_as_unknown`，
覆盖 `0` / `0.0` / `-5.0`；`test_unknown_duration_omits_deadline_kwarg` 与
`test_transcription_deadline_math_equivalence` 把同一判据铺进参数化矩阵与直调断言）。
背景：直播录制的容器头部会把 `format.duration` 写 0，旧行为按公式算出
`0*4+120=120` 秒的预算，确定性掐断本来能跑完的任务。预算值本身没变（同一条
`duration*4+120` 公式），变的只是 0 的来源从本仓显式路径交回 SDK
（`test_sdk_auto_budget_is_what_covers_zero_duration`；
`tests/unit/test_youtube_api_duration.py::test_repo_budget_formula_matches_sdk_auto_budget`
锁住两侧仍相等）。同 PR 把 `test_media_duration_probe.py` 里
`test_every_admitted_duration_is_accepted_by_the_deadline_budget` 的 `"0"` 用例移出——
0 不再是「准入认可的已知时长」。

## 并发压测

`scripts/perf/concurrent_load.py` 会提交本地 API 任务，并使用真实抖音和 B 站
URL。它是手工压测工具，不会被 pytest 收集，也不应在 CI 或没有明确授权的环境
运行。仅在本地服务、授权和外部访问均已确认后，才可手动运行：

```bash
uv run --extra perf python scripts/perf/concurrent_load.py
```
