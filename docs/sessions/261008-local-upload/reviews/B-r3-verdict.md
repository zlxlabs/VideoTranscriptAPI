# B 卡 R3 增量审查与完整复验

failure-visibility: p2-only

## 固定对象与结论

- 增量：`37907b9872ed9f59721afb53760965d32c204227..6a497263e67789b4ce8fef39d407e6e8f77faff3`（3 文件，115 增、0 删）。
- 全量：`6af391d20edab8dfd8b320ce6b91d4d19e3260e2..6a497263e67789b4ce8fef39d407e6e8f77faff3`（13 文件，2063 增、33 删）。
- 增量四问：通过。新增唯一生产行把规范化 `processing_options` 加入已有 `accept_local_upload` 队列 payload；未增加抽象、状态/事实源、fallback 或第二处理路径。队列 callback 对应 SQL/queue 无共同事务的发布边界，事务内 `put_nowait` 和 stale-item gate 有现存消费者及边界测试。
- 全量结论：1 项 P2，非阻断；未发现 P1。默认关闭、上传配额仍未知，不据此推断生产启用或漏洞。

## 增量四问及选项透传

| 问题 | 结论与证据 |
|---|---|
| 只修本轮登记的 R3/所有权合同？ | 是。`cache_manager.py:853` 从同一 `options_json` 生成队列字段；SQL/queue 顺序与临时文件所有权逻辑未变。 |
| 是否新增未经批准抽象？ | 否。仅现有方法的 payload 增加一字段。 |
| 是否新增状态、事实源或 fallback？ | 否。读取路径仍由现存 request metadata、root SQL 行、queue payload 顺序传递；未新增状态或降级。 |
| 是否留下新旧双路径？ | 否。HTTP 上传仍经同一 queue/dispatcher/consumer 路径。 |

`test_processing_options_cross_real_upload_boundaries` 三种 producer 输入（全 false、混合、默认）实际通过 FastAPI raw body、SQLite metadata/root 行、捕获的真实 `put_nowait` payload 和 dispatcher 捕获的 consumer kwargs，逐层与期望值比较。默认规范由既有 URL 契约测试 `tests/unit/test_processing_options.py::test_none_normalizes_to_all_true_including_chapters` 锁定；不是只测 normalize helper。commit-fault 用例还断言实际 queue payload 带选项，stale item 不进 executor/ASR。

## 全量不变式检查

- 认证、Content-Type、metadata 和声明大小检查均在 body 读取前；上传只 `request.stream()`，保存实际字节、SHA-256 和服务器生成路径；basename 仅展示，source URL 限 http(s)/无 userinfo，不走 URL downloader。
- 同 owner/key 重放不读 body、不重复入队；metadata 变化 409；不同 key 创建独立任务。Raw HTTP/SQLite/queue/consumer 由 `tests/integration/test_upload_intake.py::test_real_http_stream_sqlite_dispatch_idempotency_and_owner_stop` 覆盖。
- R3：root/accepted SQL 写入与一次 `put_nowait` 在同一无 await 临界步骤；queue-full 回滚；commit 失败后的真实 stale payload 被 dispatcher 持久 admission gate 拒绝。测试同时检查 queue 操作时 SQLite 的 receiving/root-none 行及 ASR/executor 未调用。
- 文件所有权：接收失败由接收端清理；正式接受后由 worker 清理。cleanup/accept 两个真实 SQLite 与文件顺序均有测试，分别锁定接受获胜保留源字节、cleanup 获胜后拒绝接受再 unlink。
- 容量：四项限制要求正有限值；真实磁盘 free space 加安全余量；实际 body bytes 与声明/上限对照；receiver/inflight/budget 保留与释放有边界测试。额度数值和生产容量仍未知。
- 音轨/时长：临时 WAV 经真实 HTTP 上传、真实 ffprobe、真实 dispatcher 到 `process_transcription`；2 秒输入在测试限额 0.0001 小时下以 `media_duration_limit` 失败，Transcriber 调用数为 0，worker 清掉源文件。ASR/LLM/通知外部服务未调用。
- 默认关闭：本执行环境 `VTA_UPLOADS_ENABLED` 长度为 0；真实 TestClient 请求在 body 前返回 503，能力为 disabled。既有 `test_local_upload_policy.py::test_resolver_uses_real_subprocess_environment_and_sqlite` 用真实子进程与 SQLite 验证缺失开关拒读/显式开关读取。Hosted CI、实际 systemd unit、生产容量及外部 ASR/LLM readiness 本轮未测，保持未知。
- 全量审阅包含 config、设计/里程碑契约、app/context/routes/transcription/cache 与新增单元/集成测试。按输入隔离要求未读取 `docs/sessions/261008-local-upload/progress/B-progress.md`、任何实施报告或旧 review 报告/正文。

## Finding：P2（非阻断）— 错误标记写入异常会跳过接收资源清理

- 违反合同：失败必须显式失败，同时仅接收方清理未移交文件，并释放接收并发、inflight 与预算；失败标记写库失败不应让临时资源永久占用。
- 位置：`src/video_transcript_api/api/routes/uploads.py:346` 开始的 `finally`；`uploads.py:348` 先调用 `set_local_upload_error`，文件清理在 `uploads.py:352`，预算/inflight/接收槽释放分别在 `uploads.py:356`、`uploads.py:358`、`uploads.py:360`。
- 触发与实测：真实 TestClient + 临时 SQLite/cache/temp，上传实际字节少于 metadata `byte_size`，进入 `declared_size_mismatch`；将 `set_local_upload_error` 故障注入为 `sqlite3.OperationalError`。结果 HTTP 500、body_reads=1、receiving row `error_code=null`、inflight_count=1、reserved_bytes_count=1、未清上传文件数=1。错误向上抛出，但它发生在清理之前，导致后续释放语句未执行。测试使用故障注入，不声称发生过生产 SQLite 故障。
- 工具严重度：OCR 未报该项；无外部工具定级。本仓 P1 两问：真实上传消费链已在本地 TestClient 运行，持久 SQLite 写故障频率/生产环境未量，默认开关关闭；若触发，会耗尽接收槽/inflight/预算直到进程重启。按 personal 单机小规模和异常故障条件定 P2，不定 P1。
- 最小建议：让现有文件/槽/inflight/预算清理在错误标记写库抛错时仍执行，同时保留原 SQLite 异常与 HTTP 500；无需 retry/fallback 或新状态。
- 交付判定：不阻断本轮 review 交付；这是失败路径资源回收缺口，建议后续修复。本卡不改应用代码。

## OCR 与窄验证

- OCR：`reviewed`，`coverage=complete`，`findings=[]`，`cli_status=complete`；`verify_status=skipped` 因无 finding。前置运行 `OCR_LOCAL_SINGLE_LEG_TIMEOUT_S=120`、`OCR_LOCAL_CHAIN_BUDGET_S=300`，外层 timeout 390 秒；主腿耗时约 103 秒。OCR 只扫本轮增量，不替代完整人工复验。
- `uv run --frozen pytest -q tests/unit/test_upload_routes.py tests/unit/test_upload_dispatch.py tests/integration/test_upload_intake.py`：exit 0，全绿点阵；输出 CPython 3.11.15、安装 71 packages、本地 outbound network guard。warning 仅 lark/pydantic deprecation。
- 已知坏态对照：窄测里的 `test_sql_commit_failure_leaves_stale_queue_item_for_dispatcher_to_drop` 注入 SQL commit failure，确认一条真实 queue item 被丢弃、executor/worker 未调用、资源清理；上文 P2 故障注入则把“失败标记写坏”喂给接收 finally，实际断言到资源泄漏。
- 生产 ASR、LLM、通知、CI/systemd 和容量未验证；没有触碰真实 config/data/.env、部署或服务。
