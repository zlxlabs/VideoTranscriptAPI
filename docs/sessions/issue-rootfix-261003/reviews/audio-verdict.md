# PR #164 音轨严格准入独立验收

- Frozen PR 范围：`2995a379a898b1d1437ee02e22996e12ecebcc31..bf488affcef6c2ecd91f6c05a0539947bd671e32`；代码增量：`7f08586717f8abd565ccca0a7ad99b5cfc5f122b..bf488affcef6c2ecd91f6c05a0539947bd671e32`。
- 结论：**P2 only，建议修复以下测试/诊断缺口后再合入；本次不改实现。** 未发现 P1。
failure-visibility: p2-only

## 按完成条件验收

1. **ASR 入口准入：部分满足。** `src/video_transcript_api/api/services/transcription.py:2024-2032` 在 YouTube API `audio_path` 直调 ASR 前探测；`2418-2426` 在常规/预下载/选择下载汇合点、ASR 前探测；字幕直命中在 `1914` 后无需探测。两真实调用点足以支撑无状态共享函数。缺口见 P2-1：API 与 YouTube 优先下载的测试替身没有满足生产分支条件。
2. **归因、放行与字节：部分满足。** `_ensure_audio_track` 一次调用 `ffprobe`、30 秒超时、失败关闭；缺二进制/超时/非零/非法 JSON/缺 streams/非列表 streams/非对象元素均映射 `media_probe_failed`，真实无音轨映射 `no_audio_track`。`codec_type` 缺失的对象仍被当成“无音轨”（P2-2）；启动子进程的其它 `OSError` 未映射为 `media_probe_failed`（P2-3）。音频-only 与混合样本能到所选引擎；混合测试断言无 ffmpeg 调用，但没有比较准入前后的文件哈希（P2-5）。
3. **终态、通知、清理：满足。** admission 失败进入既有 `_fail_task_and_notify`；测试读取实际失败通知错误文本与 FAILED 终态。任务 finally 清理只回收当前任务目录；测试确认 foreign task 文件、缓存样本和 foreign task active 状态保留。既有 finally 仍执行，未发现准入分支绕过它。
4. **真实边界与断言约束：部分满足。** 新测试用 `ffmpeg -f lavfi` 动态生成音频-only、混合、视频-only 样本；真实 `ffprobe` spy 保存仓库实际 argv 与 JSON 至忽略目录 `data/temp/audio_admission_probe/`。缺失二进制、超时、非零、JSON/结构问题用隔离 mock。未发现恒 skip；缺口见 P2-1、P2-5。将常规入口 `_ensure_audio_track(local_file)` 临时替换成 `pass` 后，`test_audio_admission_runs_once_before_asr` 按预期 AssertionError；恢复后源码 SHA-256 为 `72180f32419e79e47c0b48bf0f1abe8a74540e9b182fb03a0adea6b15f4a3b7e`，实现相对 H0 diff 为空。
5. **诊断及真实使用风险：部分满足。** 无音轨与检查失败有具名日志。错误日志中的 `stderr_head` 只是任意首行截断/ASCII 转换（最多 160 字符），不是字段白名单（P2-4）；当前本机以 `-v quiet` 探测损坏媒体的真实 stderr 为 0 字节，降低了已证实泄露风险，但代码仍会记录非空原文。`codec_type` 缺失分类见 P2-2。未增加结果类型/状态；局部共享 helper 有两个真实调用点。
6. **定级：P2 only。** 本仓 risk-tier 为 personal；未用公网或多租户情境抬高影响。当前源码的两处 ASR 入口均有准入，已测失败都不调用 ASR、会落 FAILED 并清理；没有发现可复现的假成功绕过或高影响消费链故障。主干基线 API 在派发时不可用，继承红无法判定。

## Findings（仅标记，不在本卡修复）

### P2-1：YouTube API 与优先下载测试没有走到声称覆盖的分支

- 证据：生产 API 分支要求精确类名 `YoutubeDownloader`（`transcription.py:1881-1886`），测试替身实际名为 `YoutubeApiServerDownloader`（`tests/unit/test_transcription_audio_admission.py:286`）；去掉 API 分支准入调用后，三个声称覆盖该分支的测试仍通过。`RouteDownloader` 没有 `download_video_with_priority`，但测试把 `route="youtube"` 记作选择下载路径（测试 `232-240,391-404`），生产优先分支因此也未触发（`transcription.py:2384-2400`）。
- 违反：完成条件 1、4。
- 影响：入口调用点目前可在源码中确认，但回归测试不能约束这两条特别路径；相关分支以后被移除准入时测试仍可能全绿。

### P2-2：缺失 `codec_type` 被误报成无音轨

- 证据：仅验证 streams 是列表且元素是 dict（`transcription.py:135-145`）；随后 `s.get("codec_type") == "audio"`（147）把 `{"streams":[{}]}` 归为 `no_audio_track`。spec 明确将无效 stream 结构归入 `media_probe_failed`；失败 fixtures 覆盖字符串元素，却没覆盖缺字段的对象（测试 `610-628`）。真实 ffprobe 产物样本中的 stream 都有 `codec_type`（测试 `576-584`），所以此处是畸形结构处理问题，不是对真实样本伪造契约。
- 违反：完成条件 2、5。
- 影响：检查结构异常会误导用户换源，而非报告检查失败。

### P2-3：ffprobe 启动时的非 FileNotFoundError/超时 OSError 未归一化

- 证据：调用只捕获 `FileNotFoundError` 与 `TimeoutExpired`（`transcription.py:99-115`）。用不可执行的 ffprobe 探针实际触发 `PermissionError(errno=13)`，异常逸出 helper，最终由 `process_transcription` 外层记录为通用 `转录任务异常`，未生成 `media_probe_failed`。外层仍失败并清理，没有发现放行。
- 违反：完成条件 2（探测失败原因须明确且不冒充无音轨）。
- 影响：服务环境中二进制不可执行/进程无法启动时，任务失败但消费者拿不到稳定的检查失败原因。

### P2-4：有界 stderr 仍是原文，不是白名单诊断

- 证据：`_bounded_stderr_head` 仅取首行、转 ASCII 并截至 160 字符（`transcription.py:57-63`）；returncode 错误日志直接写入该值（120-123）。诊断测试只保证假 secret 不进入用户通知，不断言日志排除了它（测试 `745-777`）。本机真实 quiet 模式坏文件 stderr 为空，故当前没有确认的真实泄露样本。
- 违反：完成条件 5 与仓库生产诊断的结构化字段/白名单要求。
- 影响：若底层工具将媒体内容、路径或其他输入回显到错误首行，该原文会进入日志；截断限制长度但不限制内容。

### P2-5：未锁定输入媒体字节在准入后保持不变

- 证据：混合样本测试断言一条 ffprobe、无 ffmpeg 调用（`tests/unit/test_transcription_audio_admission.py:495-513`），但不记录/对比下载文件准入前后的内容哈希；真实 producer 产物只记录样本大小。
- 违反：完成条件 2 的“无抽音轨/哈希字节变化”要求及完成条件 4 的独立断言约束。
- 影响：当前实现路径没有媒体转换调用的迹象，但字节不变契约没有回归断言锁定。

## OCR 与验证

- OCR 前置：status=`reviewed`，reason=`primary_selected`；profile=`minimax`，model=`MiniMax-M3.1-Flash-Preview`；4 条评论均 4/4 confirmed。已逐条核对：子进程 OSError、缺 `codec_type`、quiet stderr、PerfTracker 未计 probe。前三项见以上 finding/风险；PerfTracker 事实成立但卡面未要求该阶段进入性能摘要，未列 finding。
- 本地：`uv run --frozen pytest -q tests/unit/test_transcription_audio_admission.py tests/features/test_transcription_flow_regression.py tests/integration/test_task_observability.py tests/integration/test_temp_cleanup_integration.py tests/unit/test_capswriter_deadline_budget.py`：102 passed markers，0 failed，0 skipped。
- PR #164 H0 CI（run `37121530913`，job `111198424072`）：quality Tests 执行 `uv run --frozen pytest -q tests`；日志测试进度到 100%，共 3,567 项，3,562 passed、5 skipped、0 fail/error。质量 job SUCCESS；draft 状态下 primary/OCR/shadow jobs 为 SKIPPED，不能视为主审通过。`-q` 日志未显示 5 项具体名称。
- 实现/测试/配置未改动；仅新增本 verdict。完整记录见 delegate report。
