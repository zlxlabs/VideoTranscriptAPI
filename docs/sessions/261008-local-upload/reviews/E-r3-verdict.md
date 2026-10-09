<!-- delegate-outcome: succeeded -->
failure-visibility: p2-only

# Local upload E 完整交付独立审查 verdict

## 结论与机器判定

执行器已按固定范围完成完整审查与规定验证，outcome 为 succeeded。代码审查有一项 P2，因此 verdict 的机器判定为 failure-visibility: p2-only。该 P2 影响 E2E 子进程临时目录的隔离，不阻断产品交付，也没有观察到临时数据逃逸。

唯一 finding 位于 e2e/tests/local-upload-full-chain.spec.ts:54：startRealServer() 将子进程 TMPDIR 固定为 /tmp，没有沿用 private scratch TMPDIR。真实 full-chain 子进程采样验证了这一点。仓库 verdict 含有触发条件、影响、严重度与交付阻断判断、B1–B16 矩阵和完整竞态/运行记录。

## 审查基线、范围和方法

- 固定主审范围：0af9901639f7b4940c6a9d051f467f2a13a971d1..b50c861ef276248db087466992d549cc8edc90fe。不是只看 144411ee..b50c 的四个增量问题。
- 逐个读完全范围 8 个路径的最终全文，并核对完整 git diff；另沿真实 source consumer 检查 upload router/cache、dispatcher/transcriber、view-token resolver、public views/reprocess/audit、浏览器页面和通知 consumer。只使用批准的 design 与 QA plan；未读取作者报告、PR 讨论、旧审查或会话材料。
- 完整改动规模：8 paths，1,315 insertions / 31 deletions。包括 394 行真实 Chromium full-chain、受控竞态、SQLite 与 loopback fixture、契约索引和测试说明。审查按代码和 consumer 断言判断，没有以契约索引文字代替实现证据。
- 批准 QA 计划 B1–B16 每项的 producer、consumer assertion、产物和状态见 verdict 矩阵。五组受控竞态逐项审查；没有将静态回显或单进程断言冒充跨边界 producer 输出。

## 全链路审查结论

新浏览器路径使用真实 Chromium 文件选择与 Blob/raw body，真正进入 FastAPI upload router；SQLite 写入 owned media 映射、接收元数据、任务和队列，然后由真实 dispatcher / transcriber 消费。实际 ffprobe 执行目标是 owned file；CapsWriter SDK 保持真实，通过 loopback WebSocket 产生与消费实际协议帧；LLM 和通知的替换只发生在最终本地 HTTP 服务边界。

终态正文由真实 server 生产、保存在 SQLite-backed cache，并由 public route 消费；页面继续通过 public URL、owner history 与 owner DELETE 完成 lifecycle。通知 JSON 来自真实 notification client，并在本地 HTTP sink 捕获。随后以同一 server 验证关闭分享后的 fresh read 拒绝。无预置终态 receipt/body/history，也没有将 ASR 方法整体替换为 mock。原有 D synthetic browser 回归仍保留，新 real-chain 独立运行。

B1–B16 的逐项 source/consumer/producer/runtime 记录、五组 race 顺序、OCR 状态和已验证边界均在仓库 verdict 文件中详列。范围和已运行的完整测试不支持声称已验证生产容量、真实模型效果、最低线上镜像或外部 restore 域；这些项目仍是明确的未知。

## 独立验证记录

官方 scratch-worktree 从 base b50c861ef276248db087466992d549cc8edc90fe 创建。创建后检查隔离目录缺席和 inode 分离；环境使用 scratch 自有 Python 3.11.16 / Node v24.14.0 / Chromium 151.0.7922.34，env -i，user/network namespace，离线且仅 loopback 可用。

- uv sync、root npm ci、npm --prefix e2e ci：通过。
- make test：exit 0。输出有 5 个 skip 标记；未从流式输出取得最终逐项 summary，因此本报告不推测各 skip 原因。
- npm run test:web：14 files、186 passed。
- npm run test:browser：17 passed、1 worker、0 retries。
- 关键反验：在 scratch 单行禁用正常 page response observer 后，活性断言精确失败（预期大于 0，实际为 0）；恢复后字节与 git 版本相同，SHA-256 为 6cabefc26fc6bb6564c4a41c35cdb134ab79fe2123a3efb443071d561f1f81f4。
- 实际 full-chain 进程由 strace 采集到 argv/cwd/env；其中 TMPDIR=/tmp 是 finding 的直接消费环境证据。
- 实际 systemd transient unit 消费真实 scratch SQLite upload。unit 和 consumer 侧记录一致：success、退出码 0、功能开关/env、cwd、argv 和 transcript SYSTEMD_LOCAL_UPLOAD_RESOLVER_PROBE。

## OCR

按要求运行一次，primary / chain / outer 限时 20s / 45s / 65s。工具结果 status=skipped、reason=primary=leg_timeout、attendance ledger 写入成功、外层退出码 124。此项是 skipped，不是 clean；未重跑或更换 provider。

## 交付状态与文件完整性

只新增 verdict 文件，不改产品 source、tests、goals、配置、PR 或其它报告路径。产品源码对 base b50c861ef276248db087466992d549cc8edc90fe 的差异为零。提交 SHA 和 push 远端结果在完成提交后回填于此。

## B1–B16 契约矩阵

| 项 | 实现与消费断言 | 实际 producer / 产物及验证状态 |
| --- | --- | --- |
| B1 鉴权、开关与 metadata 先于 body | routes/uploads.py 的 capabilities、raw intake、metadata decode；upload intake 集成测试断言拒绝请求 body-read 计数为 0。  Tests: test_real_http_stream_sqlite_dispatch_idempotency_and_owner_stop; test_disabled_and_missing_limits_reject_before_body; test_receiver_and_inflight_limits_reject_without_body_consumption | ASGI/FastAPI intake 请求走真实 Bearer 边界和真实 router；浏览器 full-chain 另走实际页面 POST。全量 make test 返回 0。  Runtime status: covered by make test, aggregate exit 0; 5 suite skip markers were not mapped to individual tests. |
| B2 有界接收与容量拒绝 | intake / upload reservation；声明与实际字节上限、并发/receiver、free-space、queue-full 路径断言拒绝时无 202、无 owned file/dispatch，并检查 reservation 释放。  Tests: test_actual_free_space_shortage_rejects_before_body_or_file; test_declared_and_observed_limits_check_bytes_not_content_length; test_queue_full_has_no_success_receipt_or_dispatch | 真实 HTTP intake 请求及实际 queue consumer；free-space 和队列故障使用受控本地 fixture。生产磁盘峰值和线上容量不在本次证据内。  Runtime status: covered by make test, aggregate exit 0; 5 suite skip markers were not mapped to individual tests. |
| B3 页面 producer 到 raw bytes | UploadAPI.submitUpload 到真实 raw intake；断言 producer 字节、owned file 字节、服务端 SHA-256 一致，并将 wire 六字段与 SQLite request metadata、task processing options 对照。  Tests: test_real_http_stream_sqlite_dispatch_idempotency_and_owner_stop; Chromium full-chain test at e2e/tests/local-upload-full-chain.spec.ts:168 | Chromium 文件选择和真实 fetch；observer 读取实际 Blob body 与 headers，但不修改请求。full-chain 运行通过。  Runtime status: make test aggregate exit 0; browser suite 17 passed, 1 worker, 0 retries. |
| B4 相同意图只受理一次 | register_local_upload / accept_local_upload 的真实 SQLite 唯一性、owner/key 与 metadata 冲突断言。  Tests: test_concurrent_same_owner_key_creates_one_sqlite_receipt; test_real_http_stream_sqlite_dispatch_idempotency_and_owner_stop | 两个独立 SQLite connection 控制 BEGIN IMMEDIATE 的先后；HTTP 层检查同 key、冲突零 body、不同 key 独立任务。  Runtime status: covered by make test, aggregate exit 0; 5 suite skip markers were not mapped to individual tests. |
| B5 持久受理与 stale queue | HTTP intake、SQL commit、真实 dispatcher durable-admission 查询；queue 满、commit 失败、丢失 202 回执、stale item 均有断言。  Tests: test_queue_full_has_no_success_receipt_or_dispatch; test_http_queue_handoff_precedes_durable_acceptance_commit; test_sql_commit_failure_leaves_stale_queue_item_for_dispatcher_to_drop; test_lost_202_receipt_retry_queries_original_without_requeue; test_dispatcher_fails_stale_local_mapping_before_processing_or_executor_submit | 真实 queue producer / dispatcher consumer；断言 stale payload 不进入 PROCESSING、executor 或 ASR。由全量 make test 执行，输出带有未逐项归因的 skip 标记。  Runtime status: covered by make test, aggregate exit 0; 5 suite skip markers were not mapped to individual tests. |
| B6 文件归属和媒体准入 | owned path 映射、_ensure_audio_track、时长与音轨限制，失败需在 ASR 前拒绝且不走 URL downloader。  Tests: test_local_media_admission_rejects_before_asr_and_never_uses_url_downloader; Chromium full-chain test at e2e/tests/local-upload-full-chain.spec.ts:168 | Chromium 上传真实合成 WAV；全链路真实 /usr/bin/ffprobe 探测，检查 shim 捕获的 argv 指向 SQLite owned file。  Runtime status: make test aggregate exit 0; browser suite 17 passed, 1 worker, 0 retries. |
| B7 共用 ASR 协议与源文件清理 | 真实 dispatcher、process_transcription、CapsWriter client；断言媒体在 LLM 前清理，失败路径不发布。  Tests: test_dispatcher_uses_real_transcriber_then_cleans_owned_media_before_llm; test_local_upload_funasr_branch_observes_source_only_before_llm | CapsWriter SDK 本体通过 loopback WebSocket 收发真实帧；模型服务仅在最终 HTTP/服务边界使用本地 fixture。没有替换整个 ASR 方法。  Runtime status: make test aggregate exit 0; browser suite 17 passed, 1 worker, 0 retries. |
| B8 root 固定保留期 | terminal success/failure、孤儿恢复与 never expiry 的固定 cutoff / write-once 断言。  Tests: test_root_terminal_sets_fixed_30_day_expiry_for_success_and_failure; test_startup_orphan_failure_anchors_upload_terminal_clock_once; test_never_expiry_and_revocation_are_write_once | 真实 SQLite terminal 写入由 lifecycle/policy 测试驱动；full-chain 完成后由真实 public reader 消费。通过全量测试入口，跳过标记原因未保留。  Runtime status: covered by make test, aggregate exit 0; 5 suite skip markers were not mapped to individual tests. |
| B9 单一 public read gate | ViewTokenResolver 与 view/raw/page/export/summary 入口；撤销后逐路断言 404 且不泄漏正文，legacy 空 token 不回退。  Tests: test_public_read_summary_exports_and_owner_history_close_without_content_bypass; test_upload_task_uses_blank_legacy_column_and_separate_upload_token | full-chain 走真实服务的正文读取、owner history 和 owner DELETE；独立生命周期测试逐路消费真实 FastAPI 路由。  Runtime status: make test aggregate exit 0; browser suite 17 passed, 1 worker, 0 retries. |
| B10 三个 owner reprocess callers | recalibrate、resummarize、generate_notes 经共享 SQLite 原子 admission gate；断言子任务 legacy token 空、child group 不串根。  Tests: test_reprocess_admission_holds_sqlite_order_against_concurrent_close; test_reprocess_snapshot_before_close_is_rejected_by_fresh_transaction | 两组 controlled-order 生命周期测试覆盖三 endpoint 的 admission-wins / revoke-wins，并检查实际 child queue 或零 child 拒绝。  Runtime status: covered by make test, aggregate exit 0; 5 suite skip markers were not mapped to individual tests. |
| B11 terminal publish 与撤销单调 | update_task_status、revoke_local_upload 与快照修复消费端；断言 revoked_at 不复活、正文保留而新读拒绝。  Tests: test_terminal_publish_and_revoke_are_both_write_once; test_success_snapshot_reaches_final_http_payloads_in_order | publish-before-revoke 与 revoke-before-publish 均由真实 SQLite/路由控制；fresh public reader 重查；通知 payload 顺序另有真实 HTTP sink 检查。  Runtime status: covered by make test, aggregate exit 0; 5 suite skip markers were not mapped to individual tests. |
| B12 清理与有效成果 | cache/task/receiving cleanup 与 publish 两顺序；开关关闭不清理有效 30d/never 成果，失败删除不得记成功。  Tests: test_active_upload_cleanup_and_cache_publish_preserve_body_in_both_orders; test_cleanup_protects_active_upload_cache_even_when_feature_is_disabled; test_task_cleanup_keeps_effective_share_root_for_audit_and_url_rows_still_expire; test_revoke_and_task_cleanup_are_ordered_at_the_sqlite_guard; test_receiving_cleanup_rechecks_after_accept_wins_and_preserves_owned_file; test_receiving_cleanup_retires_before_unlink_and_accept_loses | cache cleaner、真实 save_cache、SQLite 映射和文件字节由 Event/lock 控序消费。production restore / 旧镜像边界未验证。  Runtime status: covered by make test, aggregate exit 0; 5 suite skip markers were not mapped to individual tests. |
| B13 有限 key tombstone | receiving 清理退休 key 后的重新注册拒绝窗口，避免无限保留 upload body/tombstone。  Tests: test_real_sqlite_upload_identity_owner_key_and_intent_window; test_receiving_cleanup_retires_before_unlink_and_accept_loses | 真实 SQLite 记录经 cleanup producer 删除/退休，后续同旧 key 请求消费并断言可见拒绝。  Runtime status: covered by make test, aggregate exit 0; 5 suite skip markers were not mapped to individual tests. |
| B14 实际通知 payload | 终态通知 producer、通知 router/channel 与最终 payload；断言链接、完整摘要和回执顺序，无服务器路径/凭据。  Tests: test_accepted_upload_link_reaches_final_http_payload_with_public_capability; test_local_upload_terminal_producer_sends_public_share_url_and_source_label; Chromium full-chain test at e2e/tests/local-upload-full-chain.spec.ts:168 | full-chain 经真实通知 client 到本地 HTTP JSON sink；接收的真实 JSON 后续由真实 public reader 跟随 URL，并在 owner revoke 后验证拒绝。通知没有发往外部服务。  Runtime status: make test aggregate exit 0; browser suite 17 passed, 1 worker, 0 retries. |
| B15 页面交互和真实 producer | index/history/app.js 页面选择、metadata、错误收集、历史和 owner close。  Tests: e2e/tests/local-upload.spec.ts; Chromium full-chain test at e2e/tests/local-upload-full-chain.spec.ts:168 | Chromium 实际文件选择、页面请求与 fetch body。浏览器断言 page errors、requestfailed 和非预期 HTTP 错误；单行 observer 负控令正常响应计数不再增加时，toBeGreaterThan(0) 断言按预期变红。  Runtime status: make test aggregate exit 0; browser suite 17 passed, 1 worker, 0 retries. |
| B16 最低镜像与 restore | SQLite resolver/cleanup 测试锁住 feature disabled 拒绝正文且不误删有效成果。  Tests: test_resolver_reads_active_upload_and_denies_disabled_revoked_or_expired; test_cleanup_protects_active_upload_cache_even_when_feature_is_disabled | 本地有真实子进程环境与 SQLite 消费证据；没有真实最低 guard-only image 或 restore 域 producer，因此镜像/恢复安全仍未认证，不把本地证据外推为部署授权。  Runtime status: covered by make test, aggregate exit 0; 5 suite skip markers were not mapped to individual tests. External image/restore remains unverified. |

## 五组受控竞态

1. **同 owner/key 双连接：** test_concurrent_same_owner_key_creates_one_sqlite_receipt 分别让两个真实 SQLite connection 先取得写事务；消费断言只有一个 receipt/task/queue entry。HTTP 集成另外消费冲突请求并断言不读取新 body。
2. **publish / revoke：** test_terminal_publish_and_revoke_are_both_write_once 分别先 publish 和先 revoke，再由 fresh /view/{token} 读取；撤销保留，正文不被新请求读出。
3. **cleanup / publish：** test_active_upload_cleanup_and_cache_publish_preserve_body_in_both_orders 在真实 cache lock / 候选快照处控序，并从最终 SQLite mapping 与文件 bytes 检查有效成果仍在。
4. **三个 owner reprocess / revoke：** test_reprocess_admission_holds_sqlite_order_against_concurrent_close 与 test_reprocess_snapshot_before_close_is_rejected_by_fresh_transaction 覆盖三个 owner endpoint 的两种先后；检查已受理 child 可完成、撤销先赢则拒绝且零 child。
5. **read / expiry：** test_public_read_and_expiry_both_controlled_orders 首次请求真实授权后暂停，用同一时钟推进到 cutoff，确认在途请求尚未完成再放行（200/正文）；fresh request 读取同一 resolver 后为 404 且无正文。没有依赖 sleep 猜测顺序。

## 固定审查范围与具体 Finding 证据

完整全文审查和实际 consumer 核对的路径：

- docs/testing/local-upload-contracts.md
- e2e/tests/local-upload-full-chain.spec.ts
- tests/README.md
- tests/fixtures/local_upload_loopback.py
- tests/integration/test_local_upload_lifecycle.py
- tests/integration/test_upload_races.py
- tests/local_upload_real_server.py
- tests/unit/test_local_upload_policy.py

源范围严格为 0af9901639f7b4940c6a9d051f467f2a13a971d1..b50c861ef276248db087466992d549cc8edc90fe。读取了这 8 个文件完整最终正文，并运行完整范围 git diff；同时核对了 upload route、cache、transcription dispatcher、view-token resolver、public read/export、三个 reprocess handler、audit/history、浏览器 app 与通知消费者。实际差异为 1,315 insertions / 31 deletions，8 files changed。未用仅增量 review 替代全文审查。

### Finding：P2，非阻断

在 e2e/tests/local-upload-full-chain.spec.ts 第 54 行，startRealServer() 的 spawnEnv 显式写入 TMPDIR=/tmp。触发条件是在父 E2E 进程已获得私有 scratch TMPDIR 的隔离运行环境中启动 full-chain API 子进程；子进程不会继承父进程的该值，而会把 Python/依赖库可能产生的临时文件放到 scratch 外。实际 strace 捕获的子进程 argv 为 scratch .venv/bin/python tests/local_upload_real_server.py --run-dir <scratch run dir>，cwd 是 scratch worktree，env 明确为 TMPDIR=/tmp。其 run-dir、SQLite、媒体和浏览器产物都在 scratch，本轮未观察到 /tmp 上的业务或临时写入。

严重度为 P2；交付影响为非阻断。建议使用本次 private scratch TMPDIR，并由测试验证实际 child env。修复不属于独立 reviewer 本卡授权范围，没有更改实现或测试 source。

### 实际环境边界的证据

独立 scratch 的依赖安装成功：uv sync、root npm ci、e2e npm ci。测试环境为 env -i、私有 Python 3.11.16 / Node v24.14.0 / Chromium 151.0.7922.34、user/network namespace、离线、仅 loopback 可达，IPv4/IPv6 主路由为空。make test exit 0，输出可见 5 个 skip 标记但没有可靠的逐项 summary；npm run test:web 为 14 files / 186 passed；npm run test:browser 为 17 passed、1 worker、0 retries。

负控只在 scratch 临时将正常 response observer 的计数从 += 1 改为 += 0；浏览器测试准确因活性断言期望大于 0、实际为 0 而失败，其他 16 个浏览器测试通过。恢复后的文件与 git 原文逐字节相同，SHA-256 为 6cabefc26fc6bb6564c4a41c35cdb134ab79fe2123a3efb443071d561f1f81f4。

systemd 证据来自真实 transient unit 对 scratch SQLite upload 的消费，而不是静态配置：Result=success、ExecMainStatus=0、VTA_UPLOADS_ENABLED=true；真实 WorkingDirectory、argv、HOME、TMPDIR、PYTHONPATH 均由 systemctl show 和消费者 JSON 记录，ViewTokenResolver 返回 SYSTEMD_LOCAL_UPLOAD_RESOLVER_PROBE。full-chain process 的 argv/cwd/env 同样由 strace 实际采样。

OCR 一次运行的完整结果为 status=skipped、profile=null、model=null、reason=primary=leg_timeout、findings=[]、reason_chain=[{leg: primary, reason: leg_timeout}]、attendance_ledger_write=ok；primary/chain/outer 限额 20s/45s/65s，外层 exit 124。记录为 skipped，不算 clean，也未重试。

未验证的边界：生产容量与最小镜像、真实 restore 域、真实外部通知/ASR、生产反向代理、真实模型质量与恢复后回滚。这些是明确的未知，不构成本地功能通过证据或部署授权。

## 完整 subprocess 环境与发布边界

full-chain 的实际 API child 由 strace 观察到 argv、cwd、env。spawnEnv 完整 payload 定义为：PATH=父 process.env.PATH（代码 fallback 为 /usr/bin:/bin），HOME=父 process.env.HOME（代码 fallback 为 /tmp），TMPDIR=/tmp，LANG=C.UTF-8，PYTHONUTF8=1，PYTHONUNBUFFERED=1。cwd=REPO_ROOT；argv 为 scratch .venv/bin/python、tests/local_upload_real_server.py、--run-dir、scratch run directory。实际运行确认 child 收到 TMPDIR=/tmp。完整解析值保存在约定的本机外部报告。

systemd 实际消费者环境由 systemctl show 和其自身写出的 JSON 一致采集：PATH=<captured review PATH>，HOME=<private scratch>/review-private/home，TMPDIR=<private scratch>/review-private/tmp，PYTHONPATH=<private scratch>/worktree/src，PYTHONUTF8=1，VTA_UPLOADS_ENABLED=true；cwd=<private scratch>/worktree。argv 为该 scratch 的 .venv/bin/python、review-private/consume.py、review-private/systemd-db、synthetic upload token、review-private/systemd-consumer.json。消费结果 status=success、transcript=SYSTEMD_LOCAL_UPLOAD_RESOLVER_PROBE。token 是临时合成值，未记录其内容。完整实际路径保存在本机外部报告。

跨文件/进程 payload 保持实际 consumer 来源：浏览器 observer 比较实际 Blob body 和 headers；服务端 SQLite request_metadata / processing_options 与 decoded 六字段比较；CapsWriter loopback 端接收真实 SDK frame；本地通知 HTTP sink 捕获实际 JSON；systemd consumer 读取自己的 env/cwd/argv 并实际调用 ViewTokenResolver。外部通知、生产凭据和生产服务均未使用。
