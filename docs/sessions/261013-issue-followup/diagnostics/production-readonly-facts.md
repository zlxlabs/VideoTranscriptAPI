# n305 production read-only facts: #211 and #191

核查时间：2026-10-13（Asia/Shanghai）。以下来自 n305 当前运行容器、实际应用进程环境、容器日志及只读 SQLite 事务；历史部署记忆只用于定位，不作为现网证据。

## #211: 发送方与接收方入口

- 运行对象：Compose 服务/容器 `video-transcript-api`，工作目录 `/opt/media/VideoTranscriptAPI`，运行中；镜像 ID `sha256:d9ae4d73aa07d80f8ae3f43a8a0f2a75abbb87fd2ab7d513b01ecffd4a64f1b2`。revision 标签未设置；进程环境 `GIT_SHA=9f14b4a35f46`（原样保留 12 位）。
- 日志 driver 为 `json-file`，轮转 `max-size=10m`、`max-file=3`；挂载为 `/opt/media/VideoTranscriptAPI/config` → `/app/config` 与 `.../data` → `/app/data`。
- 实际应用是容器 PID 25（`python3.11`，命令与 `main.py` 关联）；PID 1 是 `uv` 包装进程。服务解释器对应 dist-info：`zlx-ops-sdk 0.1.0`、`sentry-sdk 2.64.0`。
- PID 25 的运行环境：`SENTRY_DSN` 非空，长度 61 字节；解析接收端为 `100.107.95.24:9000`、project `29`。未输出 DSN 原值。`ZLX_OPS_DISABLED` 未设置。
- 容器从 `2026-10-10T17:39:05.955981469Z`（`2026-10-11 01:39:05.955981+08:00`）启动。`docker logs --since 168h` 可读窗口首末为 `2026-10-10T17:39:06.588959764Z`（`2026-10-11 01:39:06.588959+08:00`）至 `2026-10-13T05:06:15.703745036Z`（`2026-10-13 13:06:15.703745+08:00`），共 42,109 条带时间戳记录、5,887,786 字节；窗口包含启动记录。日志中 `GlitchTip 错误上报已接入` 1 条，release 为 `9f14b4a35f46`；`SENTRY_DSN 未设置` 和“初始化失败/降级”均为 0 条。
- 运行源码在应用启动钩子调用 `init_observability()`。包装器调用 SDK 后记录“已接入”，但该日志不证明 SDK 已 active，也不证明 GlitchTip 收到事件。当前容器晚于 10 月 9 日事故窗口启动，现有日志不能核对该窗口。
- 当前执行环境检查的常见查询令牌变量长度均为 0；DSN ingestion key 不等于查询凭据。未跨主机访问 GlitchTip，因此没有接收回执或 project 页面证据。

## #191: 任务记录与样本可用性

- 活跃任务库为 `/app/data/cache/cache.db`。SQLite URI 使用 `mode=ro` 并显式 `BEGIN`/`ROLLBACK`；连接前已有 `cache.db-wal` 与 `cache.db-shm`，查询后仍存在且大小未变；未使用 immutable、checkpoint 或写入操作。
- 实际 `task_status` 字段：`task_id, view_token, url, platform, media_id, use_speaker_recognition, status, title, author, created_at, completed_at, cache_id, llm_config, source_url, download_url, error_message, calibration_status, summary_status, processing_options, submitted_by, terminal_snapshot, chapters_status, progress`。不读取用户、令牌、URL、标题或正文值。
- 状态值域：`success=4534, failed=386, processing=1`；speaker 标记 `0=3438, 1=1483`。平台值域计数：`bilibili=3005, xiaoyuzhou=915, generic=603, youtube=196, xiaohongshu=32, wechat_channels=10, twitter=10, douyin=9, apple_podcast=2, local_upload=1, NULL=138`。成功记录 4,534 条均有 `completed_at`，覆盖 `2025-11-28 14:04:39Z` 至 `2026-10-13 04:53:53Z`（Asia/Shanghai：`2025-11-28 22:04:39+08:00` 至 `2026-10-13 12:53:53+08:00`）。
- 时长与耗时不是独立列：运行记录 JSON 路径为 `terminal_snapshot.observability.media_duration_s` 和 `terminal_snapshot.observability.stages.transcription.elapsed_ms`，完工锚点为 `completed_at`。4,534 条成功记录中，65 条同时有正数媒体时长、正数转录耗时和成功快照；其中 12 条是至少 1 小时的 `recorder://` 来源。65 条的最大 `媒体时长/转录耗时` 比值为 45.2853。
- 可安全提供最多 20 条成功样本的脱敏标量；task 指纹及逐条时间/时长仅保留在本派发私有报告，不放入仓库文档。#191 owner 规则与系数未改。

## 下一步唯一入口

主脑可在已存在且获授权的只读查询方式下，使用接收端 `100.107.95.24:9000`、project `29` 核对 GlitchTip 收据；本卡未发现当前执行环境里的查询令牌，不代表全舰队无权限，也不要求新建凭据。#191 已有满足条件的 65 条记录及私有脱敏样本，可供后续 owner 按既定规则判断；本卡不关闭 issue。
