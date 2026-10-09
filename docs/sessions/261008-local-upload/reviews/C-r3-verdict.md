# PR207 C-r3 固定代码审查结论

## 结论

- 严重度：按 `personal` 使用边界（本人及 4–5 位可信用户、可信 Bearer）审查，未发现有实际触发路径支撑的 P1/P2 代码问题。没有把未经授权配置或假设中的不可信多租户风险升级为 finding。
- 交付阻断：代码层未发现需要阻止交付的问题；审查证据层不能标成完整 clean：OCR 审查跳过，托管 CI 未知，且受输入隔离限制，无法把 H1→H2 差异逐条关联到既有 finding ID。详见限制。
- 本文是独立代码与运行复验结论，不代表所有平台或真实 ASR 模型均已验证。

## 固定目标与审查范围

- 目标 HEAD：`ef425d77cdaecb693aba76ca6cb3f847ad5196e5`；分支：`card/vta-upload-C-review3-261009`。
- H1→H2 增量：`c216f6f07f90ab34322606ad604919a65e3d78f5..ef425d77cdaecb693aba76ca6cb3f847ad5196e5`。原范围 12 个文件 +524/-44；排除未打开的两份进度自述后，审查代码与测试为 11 个文件 +521/-44。
- PR203→H2 完整范围：`c7b38563eb2f06f5ca36c06c6c7da7de9c5e9995..ef425d77cdaecb693aba76ca6cb3f847ad5196e5`。原范围 25 个文件 +2048/-91；排除未打开的两份进度自述后，审查代码与测试为 23 个文件 +1997/-91。
- 只读了冻结范围中的代码、测试和 `docs/sessions/261008-local-upload/design.md`。没有读取实现报告、先前 review、progress 自述或顾问报告。
- 增量代码变动落在不变式 3、6、7、8 所涉修复路径，未识别到无关代码增量。因禁止读取旧 review，不能核验每一处修复与旧 finding ID 的精确映射；这是审计追溯限制，不是代码 finding。

## H1→H2 四问

1. **是否只改已登记事项：部分可核。** 增量 diff 范围和行为均符合本卡列出的 ASR/历史/通知修复边界；旧 finding ID 的逐条归属无法从许可输入中独立验证。
2. **是否引入未经批准抽象：否。** `_local_upload_write_admission` 是不变式 3 明确允许的唯一局部写入 admission callback。通知字段 formatter 在完整范围内有接收通知和终态通知两个实际调用方。
3. **是否新增无依据状态、事实源或 fallback：否。** 上传持久事实仍以 `local_uploads` 与任务状态映射为准；临时预算继续使用现有 `RuntimeContext.reserve_upload_temp`。未发现新增账本、配置、双写事实源或静默回退。
4. **是否保留新旧并行 URL/本地上传路径：否。** 本地任务通过持久 `media_path` 进入共享转录处理；正常 URL 路径保持原 downloader/字幕逻辑。未见本地上传另起一套转录或 URL 抓取分支。

## 八项不变式核对

| 不变式 | 代码与测试证据 | 结论 |
| --- | --- | --- |
| 1. 接收、认证、默认关闭、有限正值限制；接收源有 hash；owner/key 同意图只回原 receipt；SQL 持久受理与 queue 交接一致；dispatcher 只消费持久 mapping；退休与接收并发有序；选项传到 consumer | `api/routes/uploads.py` 的 `_uploads_enabled`、边界检查、流式计数/hash 和 `accept_local_upload(... enqueue=put_nowait)`；`cache/cache_manager.py::accept_local_upload` 在同一无 await 事务中写 root、mapping 并交队，`get_admitted_local_upload_by_task` 验证 durable mapping；`api/services/transcription.py` 在 ASR 前校验路径与映射，stale 项失败清理。覆盖 `tests/integration/test_upload_intake.py` 的真实 HTTP/SQLite/queue、提交失败 stale item、lost-202 receipt、选项传递、接收/退休竞争测试；`tests/unit/test_upload_worker.py` dispatcher mapping 测试。 | 通过 |
| 2. 本地上传用服务器文件走共享 ASR/LLM；CapsWriter 结果由 token join 与时间坐标契约决定；URL 原语义不变 | `api/services/transcription.py` 的 `local_media_path` 分支跳过 URL 下载器及字幕抓取；`transcriber/capswriter_client.py` 与 `transcriber/transcriber.py` 的输出契约。覆盖 `tests/unit/test_capswriter_contract.py::test_body_is_token_join_not_transcript_text`、空/缺字段和错误契约用例，以及生命周期和 FunASR 分支测试。 | 通过；空文本等合法旧 URL 语义按原路径保留 |
| 3. 上传源、ASR 输出、兼容 JSON 和 atomic tmp 峰值都在实际写前用同一 runtime 预算准入 | intake 先预留源字节；`api/services/transcription.py::_local_upload_write_admission` 统计 task dir 当前文件的真实字节，加待写 UTF-8 tmp 大小并调用同一 `reserve_upload_temp`；CapsWriter atomic writer 在创建 tmp 前回调，文本和 JSON 输出都经过它。覆盖 `tests/unit/test_upload_temp_budget.py` 的旧 target/UTF-8 与拒绝前无 tmp 测试及 caps writer 写入测试。真实 producer 探针见下文。 | 通过 |
| 4. 真实队列消费者开始 LLM 前，原源与 taskdir 产物已删除；终态清理幂等；reprocess 从文字继续 | `api/services/transcription.py` 转录阶段 finally 清理本地 taskdir；`_handoff_to_llm_stage` 清理后才入 LLM queue；本地 reprocess 使用已保存文字。覆盖 `tests/integration/test_local_upload_lifecycle.py::test_dispatcher_uses_real_transcriber_then_cleans_owned_media_before_llm` 及三路 reprocess 覆盖。 | 通过 |
| 5. root 终态时间与保留期限固定，关闭/过期/空 token 不复活；公开读写与 reprocess 按真实 owner/share 授权 | `cache/cache_manager.py` 的 `local_uploads` schema、write-once terminal/revocation trigger、active share 查询及事务性 reprocess admission；`api/routes/tasks.py` 与本地内容路由。覆盖 lifecycle 中 owner/share/reprocess、关闭期间读写与事务竞争、公开 summary/export 和空 token 用例。 | 通过 |
| 6. source URL history 在 SQL 计数与分页前排除 local upload，owner/mixed 数据返回正确 items 与 total | `api/routes/audit.py` 的 source URL 查询将 local upload snapshot 从 URL history 排除；`tests/unit/test_history_routes.py::test_source_url_excludes_local_upload_snapshots_before_pagination` 与 `tests/integration/test_local_upload_lifecycle.py::test_upload_history_filters_owner_before_page_limit` 检查分页和 owner 过滤。 | 通过 |
| 7. 接受与终态通知由真实 producer 到既有 router/channel/FIFO/HTTP payload，包含安全标题/来源标签/分享链接且不泄露路径 | `api/routes/uploads.py` 组装 accepted payload；`utils/notifications/task_formatter.py` 被接受与终态两个调用点复用；终态 producer 通过现有 router 发送。覆盖 `tests/unit/test_notification_e2e_delivery.py` 的最终 HTTP payload/顺序，以及生命周期的独立公开能力通知测试。上传只展示 source URL，不据此拉取媒体。 | 通过 |
| 8. 接受通知维持同步；意外异常 fail-loud，但 commit 后 acceptance/root/queue 保持一次；重复 key 只查 receipt | `api/routes/uploads.py` 在提交与 queue handoff 后同步调用通知，无新增 catch 吞异常；明确返回 False 仍走原错误日志及 202，意外 throw 会成为 HTTP 500，已提交 receipt/queue 不撤销。覆盖 `tests/integration/test_local_upload_lifecycle.py::test_upload_notification_exception_keeps_acceptance_and_receipt`、`tests/integration/test_upload_intake.py::test_lost_202_receipt_retry_queries_original_without_requeue`。 | 通过 |

## 实际 producer 与边界证据

- 在由 scratch wrapper 从固定 HEAD 创建的隔离 worktree 中，使用 `uv sync --frozen`、`UV_OFFLINE=true` 和 `strace -f -qq -e trace=execve -s 2048` 运行真实锁定版 CapsWriter SDK（rev `492fe191e3f9568ea178b61970c732c9d37c4e29`）、真实 `Transcriber`、原子 writer、runtime 预算，以及本机 fixture CapsWriter/FunASR 服务。没有替换 `transcribe_file` 或手工伪造 ffmpeg argv；完整命令、内联脚本和输出记录在外部执行报告。
- `strace` 捕获实际子进程 argv：输入 `upload-source.bin` 交给 ffmpeg `-map 0:a:0 ... -f flac pipe:1`；第二个 ffmpeg 从 `pipe:0` 解码实际 FLAC 到 `s16le pipe:1`。SDK 实际发给本机 WebSocket 的 FLAC 为 10,010 bytes，具有 `fLaC` magic。SDK 没有额外 ffprobe 子进程。
- 合成输入为 8,044 bytes。真实 `Transcriber` 写出 `.txt` 97 UTF-8 bytes（保留首尾空格）及 `_funasr.json` 439 bytes；sidecar join 与文本完全一致。真实 writer 对旧 target + source + tmp 峰值先做 admission；成功后 runtime reservation 等于实测峰值。另以 65,539-byte 合法结构化 ASR 输出 fixture 和已占用的其他任务 reservation 触发超预算：写入前拒绝，回调时峰值为 73,581 bytes，目标及 tmp 均未创建。该 fixture 证明字节/拒绝契约，不代表模型输出容量上界。
- 实际 FunASR client 访问本机 HTTP capabilities 与 WebSocket，上传体逐字节等于源文件，发出 `terms`；client 运行期间源目录内容保持不变，没有额外 FunASR 文件写入。服务仅是最后网络边界 fixture，不声称真实模型转录质量。
- 成功路径与拒绝路径的 ffmpeg argv、探针输出和完整复现命令写入外部报告。探针 scratch 在 wrapper 退出时回收。

## 运行验证

- 指定窄验证命令 `uv run --frozen pytest -q tests/unit/test_upload_temp_budget.py tests/unit/test_upload_worker.py tests/integration/test_local_upload_lifecycle.py tests/integration/test_upload_intake.py tests/unit/test_history_routes.py tests/unit/test_notification_e2e_delivery.py tests/unit/test_capswriter_contract.py`：退出码 0。
- `make test` 按要求仅运行一次，工作目录为已知为空的 scratch worktree，不是实现树或审查树；初始无配置文件、无 `.venv`、无软链，与主仓 inode 不同；`UV_OFFLINE=true`，pytest 输出显示 outbound network guard 已安装。退出码 0，收集 3,812 项：3,809 passed、3 skipped。三个 skip 含缺少真实 CapsWriter preflight fixture（`tests/unit/test_capswriter_samples_total_contract.py:61`）；因此不是完整真实 ASR 验证。scratch wrapper 退出后清理，代码树未被测试写入。
- 两条 H1 对照在 H1 上均由目标行为 assertion 失败（history 行数为 2 而非 1；预算观测 372 而预期 37+30），不是导入、属性或 schema 错误。它们说明对照具有行为区分力。
- OCR wrapper envelope：`/tmp/vta-pr207-ocr-envelope.json`；状态 `skipped`，primary 与 backup 均为 `leg_timeout`，coverage 为 null。空 findings 不代表 clean。
- 托管 CI 基线在派发时 `gh api request failed`，没有 run/job 级结论；不能判断本轮是否有继承红或新红，亦未等待业务 CI。

## 限制与判定

- 代码范围未发现需要 P1/P2 处理的 finding，也未发现代码交付阻断。报告保留的审查证据限制是：H1 finding ID 追溯受输入隔离约束不可核实；OCR skipped；hosted CI 未知；真实模型/外部 ASR 服务未运行。它们不被写成通过。
- 本次没有修改应用、测试、配置或 workflow；唯一交付为本 verdict 文档。

failure-visibility: skipped
