# 本地媒体上传：A 卡安全基线

## 范围与批准边界

依据 2026-10-08 已批准的 v2（D16–D19、R1–R8）与最新授权“拆卡完整落地我们的设计方案”。本文件只持久化已批准的当前设计与跨卡 HTTP 合同；不是完整上传项目或生产启用许可。

A 卡交付：`cache.db` 内独立上传身份/期限/撤销记录；上传根任务与旧 `task_status.view_token` 分离；Resolver 只从上传记录解析 `upload_` capability；缓存/任务清理只在分享有效期间保护必要成果与根记录。`VTA_UPLOADS_ENABLED` 缺失或不是 `true` 时，上传分享读取拒绝，且写一条可 grep 的 `UPLOAD_DISABLED` 日志。开关关闭不改变清理保护。当前没有上传 HTTP 接收端、真实本地 ASR worker、上传 UI、生产额度或恢复证明。

主身份仍是现有 Bearer 用户。正文链接公开读取；提交归属由 owner 持有。每次正式上传独立成果，不跨上传去重；同一 owner 与同一幂等 key 是同一意图。上传开关不写入数据卷配置；没有完整恢复域证明，部署保持关闭。不得据代码测试宣称已授权生产部署。

## SQLite 与生命周期

单一存储仍是 `CacheManager`。新增 `local_uploads` 表含：`upload_id`、`owner_user_id`、幂等 key 和 key 创建时间、规范化请求 metadata 的 SHA-256 指纹、接收态、`retention`、唯一 root task / media / 独立 `upload_` token、创建时间、`expires_at`、write-once `revoked_at`、`error_code`。`UNIQUE(owner_user_id, idempotency_key)` 和 `BEGIN IMMEDIATE` 使并发重复登记只产生一个持久 receipt；同 key 元数据不同拒绝，不读新 body、不建第二个 root。

上传 root task 仍用现有 `task_status` 做进度和终态真相。由于旧列 `view_token TEXT NOT NULL`，上传根行写 `view_token=''`，`platform='local_upload'`；公开 token 只存 `local_uploads.view_token`。空 token 与 `upload_` token 都不准旧 `get_task_by_view_token` 或 audit 归属 legacy fallback 解析。写入撤销后不能复活；DB trigger 和 `COALESCE` 写入锁住 `revoked_at` 单调性。

接收 key 格式为 `<UTC_epoch_ms>-<UUID>`。首次登记只接受当前时刻之前 24 小时内的 key，未来最多容许 5 分钟时钟偏差；超过 24 小时的已有 owner/key 仍可查原记录，记录退休/清除后同 key 不得重新受理。此窗口是幂等记录生命周期，不是分享期限。当前 A 不做记录 GC；后续维护只能在确认查询/历史消费者及唯一记录窗口后回收。

`30d` 从 root 首次 `success` 或 `failed` 终态的 SQLite `completed_at` 固定加 30 天；未到终态时 `expires_at=NULL` 不等于长期，尚未终态的 root 只呈现处理中。`never` 才以 `expires_at=NULL` 表示不自动到期。子任务、重复终态写和重启不续期。撤销时间只写一次；关闭分享不取消已接受处理，也不专属擦除正文。

Resolver 每次新请求都查独立上传记录并核对开关、撤销和截止边界（`expires_at <= now` 已失效）；它不经旧 token fallback。有效 `never` 与有效 `30d` 均从既有 CacheManager 读真实缓存。失效、撤销、禁用均不返回新正文；失效内容随后按原全局清理规则处理。上传 reprocess 业务尚未实现，`upload_` token 不能走既有 task owner fallback，当前拒绝重处理。

## 清理与数据边界

- `cleanup_old_cache` 在现有媒体锁路径下检查有效上传 root 与 media 关系；有效短期和长期正文都保护，跟 `VTA_UPLOADS_ENABLED` 无关。撤销/到期后，缓存按已有 `video_cache.updated_at` 全局年龄规则回收。URL 缓存仍走原规则。
- `cleanup_task_status` 在 `BEGIN IMMEDIATE` 内再次检查 root 的有效分享资格；有效 root 留存，audit snapshot 因现有 `task_exists` 依赖仍可解析。inactive 上传和 URL task 按原 task retention 及快照过期次序清理。Keyset 扫描越过被保护记录，避免有效长期记录阻塞后续批次。
- 审计 ownership 不接受 `upload_` token 的 legacy fallback；有效分享所需根 task/snapshot 留存由现有 audit 消费者关系维持。历史展示仍由后续 B/D 卡实现。
- `data/temp` 中原媒体、转码副本和任务暂存文件仍是临时输入，不因长期正文分享而保护；现有 `TempFileManager` 保持其清理行为。

## 跨卡 HTTP 合同（B/C/D/E 消费）

### 请求与幂等

- 原生 raw `File`/`Blob` body；`Content-Type: application/octet-stream`；现有 `Authorization: Bearer ...`。
- `Idempotency-Key: <UTC_epoch_ms>-<UUID>`；服务器核验 24 小时受理窗口与最多 5 分钟未来时钟偏差。窗口内同 owner/key 唯一记录不能 GC；已有 key 的 receipt 仍可查；过窗且唯一记录已退休时拒绝，不能重新受理。
- `X-Upload-Metadata` 为 base64url 编码的 UTF-8 compact JSON，必须有界；字段 `filename`、`byte_size`、`title`（可空）、`source_url`（可空）、`retention`（`30d` 或 `never`）、`processing_options`（沿现有处理选项）。不得含本地路径、`user_id` 或 public token。
- 首次提交和接受动作必须由真实服务端边界验证 metadata；重复相同意图不读新 body、不重入队，只返回原 receipt；同 key metadata 变化返回 409。未知回执只查询，不自动换 key 或重发。

### 响应

- `GET /api/uploads/capabilities`（Bearer）：`{enabled:boolean, default_retention:'30d', retention_options:['30d','never'], limits:{max_file_mib,max_media_hours,receive_concurrency,upload_temp_budget_mib}}`。四项额度必须正且有限、来自经过真实消费环境验证的配置；当前生产值未知/未测，缺一项即 `enabled=false`。不得用示例值声称已测。
- `POST /api/uploads`：只有数据库正式接受后才返回 202；同意图返回已有 receipt，变化 metadata 返回 409。FastAPI 错误必须明确 status/detail，不得假 202。
- `GET /api/uploads/by-idempotency-key/{key}`（Bearer/owner）：已存在 receipt 或 404；不能借查询创建任务。
- receipt：`upload_id`、`state`（接收阶段，不复制 `task_status`）、`task_id`（未正式受理时 null）、`view_token`（正式受理后独立 `upload_` token，否则 null）、`retention`、`expires_at`（终态前与 `never` 为 null）、`share_active`、`error_code`（无错误为 null）。处理进度继续由既有 `GET /api/task/{task_id}` 承载。
- `DELETE /api/uploads/{upload_id}/share`（Bearer/owner）：`{upload_id, share_active:false, revoked_at}`。重复关闭返回原截止，不得复活。关闭/到期后所有新 public read 和新 reprocess 拒绝；已经授权的传输和已接受处理可继续。
- 本人历史增加 `source=upload` 过滤，先过滤再分页；upload item 可含 `upload_id/retention/expires_at/share_active/revoked_at/title/source_url`。bodygate 未实现前不得提供旁路摘要。

## 不变量与真实验收入口

| 不变量 | 实现入口 | 锁定测试 |
|---|---|---|
| owner/key 唯一、窗口/时钟偏差、metadata conflict、旧 receipt 查询与过窗拒绝 | `cache_manager.py::register_local_upload` | `tests/unit/test_local_upload_policy.py`（临时真实 SQLite，含并发登记） |
| root `view_token=''`、独立 `upload_` token；blank 与上传 token 不走旧别名 | `cache_manager.py::accept_local_upload/get_task_by_view_token`、`audit.py::check_view_token_ownership` | `test_upload_task_uses_blank_legacy_column_and_separate_upload_token`、Resolver 与 ownership 用例 |
| success/failed 首终态固定 30 天；never 不到期；撤销 write-once | `cache_manager.py::update_task_status/revoke_local_upload` | `test_root_terminal_sets_fixed_30_day_expiry_for_success_and_failure`、`test_never_expiry_and_revocation_are_write_once` |
| Resolver 对 active 30d/never 读真实缓存；missing/off/revoked/expired 不读；`UPLOAD_DISABLED` 可 grep | `view_token_resolver.py::_local_upload_task_info` | `test_resolver_reads_active_upload_and_denies_disabled_revoked_or_expired`、`test_upload_progress_token_resolves_before_30_day_terminal_clock` |
| 有效产物不被 cache/task/audit 清理删除；开关 off 不影响保护；关闭后恢复原清理；媒体仍临时 | `cleanup_old_cache/cleanup_task_status`；现有 `task_exists` 与 `TempFileManager` | local policy cleanup/controlled ordering tests、`tests/cache/test_task_status_cleanup.py`、`tests/cache/test_cache_cleanup.py` |
| URL 读、去重、history 与 task 行为不改变 | 原有 URL routes/CacheManager | `tests/unit/test_view_token_resolver.py`、`test_api_routes.py`、`test_history_routes.py` 及 cache cleanup 套件 |

当前真实开发验证入口：`uv sync --frozen`；重复五次 `uv run --frozen pytest -q tests/unit/test_local_upload_policy.py tests/unit/test_view_token_resolver.py tests/cache/test_task_status_cleanup.py`；完整 gate `make test`。对环境变量依赖的测试必须在实际消费 Resolver 的环境运行。部署/systemd 恢复及容量验证仍是上线前置条件；本卡不触及生产 unit、数据卷或服务。

## 非目标与依赖

A 不做 HTTP 接收/流式限额、队列原子交接、ASR/LLM 接线、上传完整 reprocess、网页、部署/备份恢复实测、生产容量验证、source URL fetch 或原媒体长期留存。B 消费该 store 与 Resolver；C 消费 root/终态与媒体清理合同；D 消费本节 wire/history 合同；E 证明真实 producer 到 HTTP/ASR/正文消费者；F 证明独立恢复域与单向关闭。

不增加账户平台、`LocalUploadService` 转发层、outbox、broker、重试/fallback、外部撤销系统或新配置源。没有 `config.py` 加载器；`VTA_UPLOADS_ENABLED` 是部署环境变量，故不写入 `config/config.example.jsonc` 或数据卷配置。没有正有限额度和恢复证明时，B 必须报告关闭，而不是启用默认占位值。
