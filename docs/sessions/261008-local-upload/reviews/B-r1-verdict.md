failure-visibility: p2-only
# B 卡独立审查 verdict：固定 H0

审查对象：`6af391d20edab8dfd8b320ce6b91d4d19e3260e2..840facda8e0cb251a1197cf3647c5accd5c496ff`，risk-tier `personal`。只读该固定差分；未改产品或测试代码。

结论：发现 2 项 P2。**阻断本轮交付**，原因是 F1 违反批准 R3 的受理/队列交接不变式；P2 是个人使用风险定级，不代表契约合格。F2 记为 P2、不单独阻断。没有 P1。

## Findings

### F1 — SQLite accepted 提交先于队列 handoff（P2；阻断：是）

- 违反批准 R3：root、accepted 状态与一次 `queue.put_nowait` 必须处于同个无 await 的数据库临界步骤，只有该步骤成功提交才正式受理；不能依赖事后撤回补偿。源码：[uploads.py](../../../../src/video_transcript_api/api/routes/uploads.py#L321) 调用会自行提交的 `accept_local_upload`，返回后才在 [uploads.py](../../../../src/video_transcript_api/api/routes/uploads.py#L339) 入队；[cache_manager.py](../../../../src/video_transcript_api/cache/cache_manager.py#L783) 在 SQL 事务内创建 root/accepted，但不执行队列 handoff。QueueFull 另走 [uploads.py](../../../../src/video_transcript_api/api/routes/uploads.py#L342) → [cache_manager.py](../../../../src/video_transcript_api/cache/cache_manager.py#L868) 的事后撤回。
- 真实默认 HTTP 路径探针：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests/integration:src uv run --frozen pytest -q -p no:cacheprovider --tb=short /tmp/vta_upload_queue_order_probe.py`。探针由真实 ASGI 请求发送 raw producer bytes，经真实 SQLite/cache 和 runtime 队列；在实际 `put_nowait` 调用前由独立 SQLite 连接读行。预期「仍未提交」的断言故意为负对照并失败：实际读到 `state='accepted'`、root `status='queued'`，payload 为 `platform='local_upload'`、`url='https://display.example/recording?id=42'`。证明 accepted 已对外持久可见后才开始队列 handoff。
- 若进程在提交与入队间退出，receipt 已是 accepted，但内存队列没有任务；相同 key 只回原 receipt，不重入队。另有失败路径确证：`tests/integration/test_upload_intake.py::test_queue_put_failure_rolls_back_formal_receipt` 只验证 QueueFull 后的补偿；`test_sql_commit_failure_rolls_back_root_and_never_queues` 只覆盖入队前 SQL 失败，均未锁住 R3 顺序。
- 外部工具严重度：OCR `skipped`、无级别；本仓判定：P2。P1 两问：真实代码路径每次请求均先提交再调用队列，已由本地真实 HTTP/SQLite 探针测得；但实际生产进程是否在这段窄窗口退出未测。后果是单任务可能被恢复为失败且同 key 不重提，朋友可检查后用新 key 重传；频率和用户接受度未在生产验证，故不升 P1。
- 最小建议：把一次 `put_nowait` 移入现有 accepted/root SQL 临界步骤，成功提交后才返回 202；队列满由事务回滚自然留在未受理阶段，删除事后撤回路径。保留 dispatcher 的持久状态校验，用于丢弃「队列已写、SQL 提交失败」的孤儿项。

### F2 — receiving 清理在状态复核前删除源文件（P2；阻断：否）

- 违反接收文件所有权与清理交接约束：仅可清未移交文件。`cleanup_expired_local_upload_receiving` 在 [cache_manager.py](../../../../src/video_transcript_api/cache/cache_manager.py#L737) 读出候选后，先于 `BEGIN IMMEDIATE` 和状态复核在 [cache_manager.py](../../../../src/video_transcript_api/cache/cache_manager.py#L749) 删除文件，之后才在 [cache_manager.py](../../../../src/video_transcript_api/cache/cache_manager.py#L762) 检查它仍为 receiving。
- 临时 SQLite/文件并发探针用 barrier 固定交叉顺序：清理先选中已到期 receiving 行；accept 使用截止前时间成功提交；清理继续删文件并复核到 accepted。负对照要求源文件仍存在，命令以预期断言失败：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run --frozen pytest -q -p no:cacheprovider --tb=short /tmp/vta_upload_cleanup_accept_probe.py`。实测结果：清理返回 0（行保留），accepted 映射仍可查，但源文件不存在。默认 dispatcher 随后会因路径缺失而在 ASR 前将任务置 failed。
- 外部工具严重度：OCR `skipped`、无级别；本仓判定：P2。P1 两问：定时清理和接收可以并行，截止边界路径已由本地 SQLite/文件探针复现；真实部署的触发频率未测。后果是单个 accepted 上传文件丢失，用户可用新 key 重传；个人规模下不单独阻断。
- 最小建议：先在 `BEGIN IMMEDIATE` 中重读并原子退休仍为过期 receiving 的行，再只删除该退休行拥有的路径；已变为 accepted 的行不得授权清理器删文件。现有顺序测试 [test_upload_routes.py](../../../../tests/unit/test_upload_routes.py#L82) 未覆盖该交叉。

## 核验状态

- `uv run --frozen pytest -q tests/unit/test_upload_routes.py tests/unit/test_upload_dispatch.py tests/integration/test_upload_intake.py`：exit 0。通过不能推翻上述缺失的跨提交窗口断言。
- 本地 `env -i` 执行 `test_disabled_and_missing_limits_reject_before_body`：exit 0；示例四项额度仍为 `null`。worker readiness 由真实 `process_task_queue` 协程 fixture 覆盖。未测 CI、生产 systemd/ASR 容量或生产配置。
- 实际 `ffprobe` 探针：1.0 秒 WAV 检出 1 条音轨；ffmpeg 生成的无音轨 MP4 拒绝为 `no_audio_track`；非媒体文件拒绝为 `media_probe_failed`。无外部 ASR/LLM/通知调用。测试中的媒体消费者替代 ASR；生产容量、压缩媒体真实尺寸及中间文件上界保持 unknown。
- HTTP integration fixture 断言服务端落盘 bytes/SHA、SQLite root/media 行、真实队列/dispatcher 消费和临时文件移交；local media 单测确认 `create_downloader` 不得被调用。生产源路径与容量未部署验证。
- OCR 命令及 envelope、输入隔离偏差、源码/13 文件映射、基线和已知坏态证据见执行器报告。
