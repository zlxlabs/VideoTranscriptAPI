# B-r2 独立复审结论

failure-visibility: p2-only

## 结论

- H0..H1 四问通过；R3 入队顺序和 receiving 清理时文件所有权的增量修复符合登记范围，没有新增无依据抽象、状态或双路径。
- 全量 `6af391d..H1` 复审发现 2 项 P2 和 1 项 P3。工具没有给出 finding 严重度；OCR 两腿均超时，状态为 `skipped`，不能记作 clean。
- 三项 finding 都已存在于 H0；不归因于本轮 H0..H1 修复增量。
- **B 功能交付阻断：是，待处理 P2-1。** 上传协议接受 `processing_options`，而真实消费者忽略它，导致用户选择的处理流程不生效。该结论是功能契约阻断，不是 P1 定级。P2-2 与 P3-3 可独立记入后续修复，不改变此阻断理由。
- 风险档为 personal。三个问题均未满足 P1 两问：实际链路可触发，但当前默认关闭且生产使用未验证；后果是额外处理/错误回执或错误诊断，没有已证实的数据丢失、静默错误结果或崩溃。

## 审查基线

- 增量：`840facda8e0cb251a1197cf3647c5accd5c496ff..37907b9872ed9f59721afb53760965d32c204227`（7 files，+246/-44）。
- 全量：`6af391d20edab8dfd8b320ce6b91d4d19e3260e2..37907b9872ed9f59721afb53760965d32c204227`（13 files，+1948/-33）。
- 全量审查覆盖实现、测试、示例配置及设计/任务文档；未读取进度报告或既有 review 正文。

## 增量四问

1. **仅修登记的 R3 与文件所有权合同：通过。** 事务中的一次同步 `put_nowait`、stale dispatcher gate、cleanup 先退休再 unlink，以及 accept/cleanup 两种真实顺序的测试，与批准 R3 一致。
2. **未经批准抽象：无。** 新增的可选 `enqueue` callback 只有 HTTP producer 使用；它让实际 queue put 落在 `BEGIN IMMEDIATE` 的同一无 await 临界步骤，避免 CacheManager 导入队列。属于此交接边界的必要调用形态。
3. **新增状态/事实源/fallback：无。** 没有增加持久状态、重试或事后补偿；仅有临时文件退休路径。原 queue-full compensation 已删除。
4. **新旧双路径：无。** HTTP route 删除 commit 后入队路径，改为唯一 callback 路径。

## Findings

### P2-1：上传请求的 processing_options 未到达消费者（阻断 B 功能交付）

- **违反契约：** `design.md` 的 metadata 合同（`processing_options` 沿用现有处理选项，约第 36 行）。
- **代码：** `uploads.py:321-334` 将规范化 options 存入 acceptance；`cache_manager.py:818,831-858` 写入任务记录，但队列 payload 只含 `id/url/platform`；`transcription.py:576-578,616-618` 按队列字段缺失回退到全部默认开启，虽读了 request metadata 却只取 title。
- **真实触发与结果：** H1 临时 HTTP→SQLite→真实 asyncio queue→`process_task_queue` 探针请求四项均为 false；队列实际 payload 不含 options，消费者传给 `process_transcription` 的四项均为 true。`test_http_queue_handoff_precedes_durable_acceptance_commit`（`test_upload_intake.py:464-498`）锁定的 producer payload 也只检查这三个字段，未断言 options。
- **工具严重度 / 本仓判定：** 无外部工具标注；P2。P1 问一：合法请求在开关启用且用户关闭某选项时可触发，生产开关当前默认关闭、真实生产使用未验证。P1 问二：额外校对/总结/章节/说话人处理可能增加资源消耗，但未见数据丢失、隐私越权或不可接受后果证据，故不升 P1。
- **最小修正：** 让实际 producer payload 带上已规范化 options，或让 dispatcher 从持久的 root/upload 记录读取；增加 producer 到 `process_transcription` 的断言。因这是公开请求字段的功能语义，lead 接受 B 前应处理或明确改写契约。

### P2-2：正文开始前的 setup 异常留下无错误的 receiving 回执

- **违反契约：** R3/receipt 合同要求失败不伪装成仍在接收；同 key 只返回原回执，不能假装已受理。
- **代码：** `uploads.py:257-262,284-293,346-349`。`body_started` 只在 `create_task_dir`、写 receiving path、queue 检查之后设为 true；finally 仅在其为 true 时写错误码。
- **真实触发与结果：** 临时 HTTP 探针令真实 `temp_manager.create_task_dir` 抛 `OSError`：POST 返回 500，SQLite receipt 保持 `state=receiving,error_code=NULL`；同 key 重放返回 200 且读取 0 个 body chunk。预留预算和 inflight 名额已释放，但该 key 在过期清理前不能重传。
- **工具严重度 / 本仓判定：** 无外部工具标注；P2。P1 问一：真实文件系统故障路径已在请求 handler 复现，是否在生产发生未知；当前开关默认关闭。P1 问二：可用新 key 重新提交且无已证实数据丢失，主要后果为回执误显接收中及旧 key 暂不可复用，故不升 P1。
- **最小修正：** 在已知的正文前 setup 失败点写明确错误码后继续原异常传播；不要用宽泛 catch、重试或补偿状态。

### P3-3：已读完正文的 SQLite commit 故障被标为 receive_incomplete

- **契约关联：** R3 要求失败的正式接受不伪装成成功；此处 HTTP 500 已显式失败，但 receipt 的 `error_code` 是诊断字段。文档没有枚举错误码词表，因此按 P3 可观测性意见记录，不把它当功能契约阻断。
- **代码：** `uploads.py:321-339,346-349`。commit 异常未设 `failure_code`，finally 将它统一写成 `receive_incomplete`。
- **真实触发与结果：** 隔离 SQLite 故障探针已读取一个完整 body chunk，注入 acceptance commit 异常后响应 500、stale queue item 被 dispatcher 丢弃；receipt 为 `receiving`，`error_code=receive_incomplete`。现有 `test_sql_commit_failure_leaves_stale_queue_item_for_dispatcher_to_drop` 验证了 commit 回滚与 stale item 拒绝，但未断言该错误码。
- **工具严重度 / 本仓判定：** 无外部工具标注；P3，非阻断。P1 两问：故障路径已在隔离环境复现，实际生产发生未知；响应和 receipt 显示失败且未正式受理，错误仅在诊断阶段名称，后果可接受。
- **最小修正：** 对 acceptance commit 失败使用准确阶段码，并保留当前回滚、stale item 丢弃行为。

## 验证证据与限制

- H0 红对照：在隔离 scratch worktree 的 H0 上临时复制 H1 producer 测试 `test_http_queue_handoff_precedes_durable_acceptance_commit`，以预期 `AssertionError` 观察到 queue put 时独立 SQLite 视图已是 `accepted/root queued`；H1 同测试观察到 `receiving/root absent`。
- H1 四项真实 producer/cleanup 顺序测试通过：`test_http_queue_handoff_precedes_durable_acceptance_commit`、`test_sql_commit_failure_leaves_stale_queue_item_for_dispatcher_to_drop`、`test_receiving_cleanup_rechecks_after_accept_wins_and_preserves_owned_file`、`test_receiving_cleanup_retires_before_unlink_and_accept_loses`。实际队列 payload、独立 SQLite 行、dispatcher `executor.submit` 和媒体文件 bytes 均有断言。
- 窄测通过：在已装依赖的共享 venv 中运行 `uv run --frozen --no-sync pytest -q tests/unit/test_upload_routes.py tests/unit/test_upload_dispatch.py tests/integration/test_upload_intake.py`（35 passed）；`--no-sync` 避免改动本树依赖环境。
- A/URL/runtime/maintenance 回归通过：`uv run --frozen --no-sync pytest -q tests/unit/test_local_upload_policy.py tests/unit/test_view_token_resolver.py tests/unit/test_api_routes.py tests/unit/test_history_routes.py tests/unit/test_runtime_lifecycle.py tests/cache/test_task_status_cleanup.py tests/cache/test_cache_cleanup.py`；输出全绿，无失败。
- 缺省关闭消费环境负对照通过：`env -i PATH=../../VideoTranscriptAPI/.venv/bin:/usr/bin:/bin PYTHONPATH=src python -m pytest -q tests/integration/test_upload_intake.py::test_disabled_and_missing_limits_reject_before_body`，1 passed。
- 合成媒体本地预检通过：真实 ffmpeg 生成 1 秒音频和无音轨视频，`_ensure_audio_track` 读出 1.000 秒并以 `no_audio_track` 拒绝视频；未调用外部 ASR。
- H1 实际 worker hold 探针确认 source bytes 与 `2x` reservation 在处理期间保留，receiver slot 在接收后释放；worker 完成后 reservation/inflight 清零。
- OCR wrapper：`OCR_LOCAL_SINGLE_LEG_TIMEOUT_S=120 OCR_LOCAL_CHAIN_BUDGET_S=300 timeout 360s ocr-review --repo "$(git rev-parse --show-toplevel)" --from 840facda8e0cb251a1197cf3647c5accd5c496ff --to 37907b9872ed9f59721afb53760965d32c204227 --audience agent --concurrency 4 --background-file /tmp/vta-upload-B-r2-ocr-bg.md`；envelope=`skipped`，原因 `primary=leg_timeout; backup:deepseek=leg_timeout`，两腿各 120 秒。该项不代表 clean。
- Verdict 机读抽取输出 `p2-only`；`git diff --cached --check` 无输出（通过）；verdict 63 行。
- 未验证真实生产配置、生产/systemd 环境、真实 ASR/LLM/通知、部署恢复域和容量；示例四项额度为 null，不能据临时测试额度或合成文件推生产容量。
