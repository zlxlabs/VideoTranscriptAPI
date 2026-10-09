# 本地上传 B1–B16 真实契约索引

本表把已批准测试计划中的 B1–B16 对到生产入口、断言和具体测试。测试只替换最后的
ASR、LLM HTTP 服务与通知接收端；路由、SQLite、文件、队列、CapsWriter SDK、模型 SDK、
导出和通知发送序列仍使用真实实现。具体浏览器 producer 证据见
`e2e/tests/local-upload-full-chain.spec.ts`；其输出目录保留浏览器 fetch 边界的 WAV 字节、
metadata、真实 ffprobe argv、CapsWriter SDK WebSocket frame 和最终通知 HTTP JSON。

| 契约 | 真实入口与实现 | 约束断言与测试 |
| --- | --- | --- |
| B1 鉴权、开关、metadata 先于 body | `routes/uploads.py`: `upload_capabilities`, `receive_upload`, `decode_upload_metadata`; `services/transcription.py`: `verify_token` | `test_real_http_stream_sqlite_dispatch_idempotency_and_owner_stop`, `test_disabled_and_missing_limits_reject_before_body`, `test_receiver_and_inflight_limits_reject_without_body_consumption`; body-read 计数为 0。 |
| B2 有界接收与真实容量拒绝 | `receive_upload`; `CacheManager` upload reservation; `_local_upload_write_admission` | `test_actual_free_space_shortage_rejects_before_body_or_file`, `test_declared_and_observed_limits_check_bytes_not_content_length`, `test_queue_full_has_no_success_receipt_or_dispatch`; 断言无 202、无文件、无 dispatcher 调用及释放 reservation。普通下载与生产磁盘峰值未作容量认证。 |
| B3 页面 producer 到 raw bytes | `src/web/static/js/app.js`: `UploadAPI.submitUpload`; 页面测试经真实 `/api/uploads` | `test_real_http_stream_sqlite_dispatch_idempotency_and_owner_stop`; 浏览器全链路测试捕获 `fetch` 实际 body 和 wire headers，比较 producer bytes、已落盘 bytes 与 SHA-256，并把 SQLite `local_uploads.request_metadata` 与 `task_status.processing_options` 规范化 JSON 后对照同一份 decoded wire 六字段（filename/byte_size/title/source_url/retention/processing_options）。真实 producer 为 `e2e/tests/local-upload-full-chain.spec.ts`，不使用合成 browser server。 |
| B4 同意图只建一次 | `CacheManager.register_local_upload`, `accept_local_upload` | `test_concurrent_same_owner_key_creates_one_sqlite_receipt` 用两个真实 SQLite connection，在 `BEGIN IMMEDIATE` / 查询之间控序并分别让 request-a、request-b 先赢；HTTP 同 key、metadata conflict、distinct key 由 `test_real_http_stream_sqlite_dispatch_idempotency_and_owner_stop` 断言。 |
| B5 durable admission 与 stale queue | `routes/uploads.py`: `receive_upload`; `services/transcription.py`: `process_task_queue`; `CacheManager.get_admitted_local_upload_by_task` | `test_queue_full_has_no_success_receipt_or_dispatch`, `test_http_queue_handoff_precedes_durable_acceptance_commit`, `test_sql_commit_failure_leaves_stale_queue_item_for_dispatcher_to_drop`, `test_lost_202_receipt_retry_queries_original_without_requeue`, `test_dispatcher_fails_stale_local_mapping_before_processing_or_executor_submit`。 |
| B6 文件路径与媒体准入 | `receive_upload`; `services/transcription.py`: `_ensure_audio_track`, `_probe_media_duration`, `_local_upload_write_admission` | `test_local_media_admission_rejects_before_asr_and_never_uses_url_downloader`; 浏览器全链路检查真实 ffprobe shim 记录的 argv 指向 SQLite 映射的 owned file，且实际 `/usr/bin/ffprobe` 完成探测。 |
| B7 共享 ASR 与源文件清理 | `services/transcription.py`: `process_task_queue`, `process_transcription`; CapsWriter SDK client | `test_dispatcher_uses_real_transcriber_then_cleans_owned_media_before_llm`, `test_local_upload_funasr_branch_observes_source_only_before_llm`; 浏览器全链路消费真实 SDK WebSocket 帧并断言转录后、LLM 前源文件已清理。 |
| B8 root 固定保留期 | `CacheManager.update_task_status`, `local_upload_share_is_active`; 上传启动孤儿恢复 | `test_root_terminal_sets_fixed_30_day_expiry_for_success_and_failure`, `test_startup_orphan_failure_anchors_upload_terminal_clock_once`, `test_never_expiry_and_revocation_are_write_once`；覆盖 success/failed 与 never。 |
| B9 单一 public read gate | `ViewTokenResolver.get_view_data_by_token`; `routes/views.py`: `view_transcript`, `handle_raw_export`, `handle_page_export`, `export_content`; summary route | `test_public_read_summary_exports_and_owner_history_close_without_content_bypass` 逐个请求 default、raw/page 的 calibrated、summary、notes、transcript、全部 export 及 summary；撤销后逐路断言 404 且正文不泄漏。`test_upload_task_uses_blank_legacy_column_and_separate_upload_token` 锁住空 token 不回退到 legacy task。 |
| B10 三个 owner reprocess caller | `routes/tasks.py`: `_admit_upload_reprocess`, `recalibrate`, `resummarize`, `generate_notes`; `CacheManager.get_active_local_upload_for_reprocess` | `test_reprocess_admission_holds_sqlite_order_against_concurrent_close` 与 `test_reprocess_snapshot_before_close_is_rejected_by_fresh_transaction` 参数化覆盖三个 endpoint 的 admission-wins / revoke-wins；断言 child blank legacy token、真实 child queue 或拒绝且零 child。 |
| B11 terminal publish 与撤销单调 | `CacheManager.update_task_status`, `revoke_local_upload`; terminal snapshot / notification consumers | `test_terminal_publish_and_revoke_are_both_write_once` 控制 publish-before-revoke 与 revoke-before-publish，断言截止值、撤销时间保持、正文仍留存但 fresh public reader 拒绝；`test_success_snapshot_reaches_final_http_payloads_in_order` 锁定通知顺序。 |
| B12 清理与仍有效成果 | `CacheManager.cleanup_old_cache`, `cleanup_task_status`, `cleanup_expired_local_upload_receiving`; temp source cleanup | `test_active_upload_cleanup_and_cache_publish_preserve_body_in_both_orders` 对真实 `save_cache` 与 cache cleaner 双顺序控锁；`test_cleanup_protects_active_upload_cache_even_when_feature_is_disabled`, `test_task_cleanup_keeps_effective_share_root_for_audit_and_url_rows_still_expire`, `test_revoke_and_task_cleanup_are_ordered_at_the_sqlite_guard`, `test_receiving_cleanup_rechecks_after_accept_wins_and_preserves_owned_file`, `test_receiving_cleanup_retires_before_unlink_and_accept_loses` 锁住独立清理边界。生产 restore 域/旧镜像未验证。 |
| B13 有限 key tombstone | `CacheManager.cleanup_expired_local_upload_receiving`, `register_local_upload` | `test_real_sqlite_upload_identity_owner_key_and_intent_window` 删除过期记录后再次提交旧 key 必须 24-hour 拒绝；`test_receiving_cleanup_retires_before_unlink_and_accept_loses` 断言 retired key 不会再受理。 |
| B14 真实通知 payload | `services/transcription.py` terminal producer; `utils/notifications/{router,channel,task_formatter}.py` | `test_accepted_upload_link_reaches_final_http_payload_with_public_capability`, `test_local_upload_terminal_producer_sends_public_share_url_and_source_label`; 浏览器全链路经真实通知 client 发出 HTTP JSON，断言完整 payload、链接、无磁盘路径，之后由真实 public reader验证撤销拒读。 |
| B15 页面交互与实际 producer | `src/web/static/{index.html,history.html}`、`static/js/app.js` | `e2e/tests/local-upload.spec.ts` 保留原页面回归；`e2e/tests/local-upload-full-chain.spec.ts` 用 Chromium 文件选择、metadata/header、实际 fetch body、页面历史和 owner DELETE 完成端到端断言。page `requestfailed` 与 HTTP ≥400 一律收集；非预期项必须为空。历史页换代只声明 `GET /api/audit/filter-options` 与 `GET /api/audit/history` 的 `net::ERR_ABORTED`。刻意撤销后的 raw GET 走 request API context，不属于 page 流。 |
| B16 最低镜像与 restore 安全 | `VTA_UPLOADS_ENABLED` 读取、public resolver、cleanup policy | `test_resolver_reads_active_upload_and_denies_disabled_revoked_or_expired`, `test_cleanup_protects_active_upload_cache_even_when_feature_is_disabled` 锁住关闭开关不泄漏且不误删。实际恢复域与 guard-only 最低镜像不在本地测试环境，保持未认证；不把此处测试当作 restore 或上线证据。 |

## 竞态矩阵

| 竞态 | 受控顺序与 fresh consumer |
| --- | --- |
| 同 key 两事务 | `test_concurrent_same_owner_key_creates_one_sqlite_receipt` 分别让 request-a 和 request-b 先取得 SQLite 写事务。 |
| publish / revoke | `test_terminal_publish_and_revoke_are_both_write_once` 分别先提交 terminal publish、先提交 revoke；随后重新走 `/view/{token}`。 |
| cleanup / publish | `test_active_upload_cleanup_and_cache_publish_preserve_body_in_both_orders` 在真实 cache lock 和候选快照处暂停，确认最终数据库映射与文件正文仍存在。 |
| 三个 reprocess / revoke | lifecycle 两组参数化测试覆盖 `/api/recalibrate`、`/api/resummarize`、`/api/generate_notes` 的两种顺序；分别断言已接受 child 可完成和撤销先提交时零 child。 |
| read / expiry | `test_public_read_and_expiry_both_controlled_orders` 用同一测试时钟：首 GET 在截止前真实授权后暂停，主线程把时钟推进到截止时刻并确认在途请求尚未完成，再释放首响应（200/正文）；随后同时钟 fresh GET 为 404 且无正文。不改 `expires_at` 写入，不用 sleep。 |

以上 fixture 与状态检查都使用临时目录、独立 SQLite 和 loopback 服务；没有真实凭据、生产配置、
生产 ASR、外发通知或真实恢复域。B16 的外部镜像/恢复证明、生产容量和真实模型质量仍是独立未验证义务。
