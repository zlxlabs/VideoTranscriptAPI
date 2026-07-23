# T1 调用面复核

复核时间：2026-07-23（EDT）

## 基线与统计偏差

本次以 worktree 基线 `f6cc02c` 对 `src/video_transcript_api` 执行实时 `rg`。交接文档的「8 个调用点、4 个文件」应按**名称出现**理解，不能当作 API 的实际直接调用统计：

- API 层实际直接调用为 **5 处 / 3 个路由文件**：`api/routes/audit.py` 1 处、`api/routes/tasks.py` 2 处、`api/routes/views.py` 2 处。
- `api/app.py` 的唯一命中只是 retention 配置 docstring 中的文字说明，并非调用。
- 另外，`CacheManager.create_task` 内有 2 个去重调用（URL 精确匹配、`(platform, media_id)` 语义匹配）。
- 私有辅助方法 `_resolve_summary_state`、`_get_llm_config_by_view_token` 只在 `CacheManager.get_view_data_by_token` 内部调用；没有 API 层直接调用。

因此，T3/T4 修改调用方时以「5 个 API 直接调用 + CacheManager 内部 2 个 dedup 调用」为实际面；对外兼容由 CacheManager facade 的原方法承接。

## 六个方法与调用方契约

| 方法 | 实际调用方 | 输入与成功返回 | 未命中／错误契约 |
| --- | --- | --- | --- |
| `get_view_data_by_token(view_token)` | `audit.py:get_task_summary`（`asyncio.to_thread`） | 返回 `status == success` 的视图字典；调用方仅在 `summary_state == generated` 且 `summary` 非空时返回前 300 字符摘要。 | 返回 `None` 或非 `success` 时响应 `200`、摘要不可用；调用异常由路由记录后转 `HTTP 500`。 |
| `get_view_data_by_token(view_token)` | `views.py:export_content`（`asyncio.to_thread`） | 返回含 `cache_dir`、`title`、`platform` 等的视图字典，用于导出文件与元数据。 | `None` 或无效/不存在 `cache_dir` 都响应 `404`；读取文件异常响应 `500`。 |
| `get_view_data_by_token(view_token)` | `views.py:view_transcript`（`asyncio.to_thread`） | 返回以 `status` 区分 `processing`、`failed`、`file_cleaned`、`success` 的视图字典，后续渲染 HTML 或 raw/page 导出。 | `None` 按 raw/page/普通页面分别返回 `404` Response 或 error 模板。 |
| `get_cache_by_view_token(view_token)` | `tasks.py:recalibrate` | 返回 `get_cache` 格式并附加 `task_info`；调用方读取 `transcript_data`、`task_info.task_id` 进行归属校验与重新校对。 | `None` 响应 `404`；缺少转录文本响应 `400`。方法自身吞掉 SQLite/文件异常并返回 `None`。 |
| `get_cache_by_view_token(view_token)` | `tasks.py:resummarize` | 同上；调用方以已有转录内容和 `task_info` 进行归属校验、总结层重跑。 | `None` 响应 `404`；无转录文本响应 `400`。 |
| `_resolve_summary_state(task_info, cache_data)` | 仅 `get_view_data_by_token` 内部 | 返回 `(summary_state, summary_text)`；`generated` 仅在有真实文本时携带文本，其他状态返回 `None` 文本。 | `task_status.summary_status` 与 `llm_status.json` 都缺失时，按历史文件推断：有文本为 `generated`，无文本保守为 `skipped_short`。 |
| `_get_llm_config_by_view_token(view_token)` | 仅 `get_view_data_by_token` 内部（当前任务无 `llm_config` 时回退） | 返回同一 `view_token` 下最新非空 JSON `llm_config`，供 cache-hit 任务继承模型配置。 | 无记录、JSON/SQLite 异常均为 `None`；调用方允许 `llm_config` 为 `None`。 |
| `get_existing_task_by_url(url, use_speaker_recognition=False)` | `CacheManager.create_task` 内部（去重策略 1） | 返回任务字典，调用方复用其 `view_token`。 | `None` 时继续 `(platform, media_id)` 去重；异常记录日志并返回 `None`。 |
| `get_existing_task_by_media(platform, media_id, use_speaker_recognition=False)` | `CacheManager.create_task` 内部（去重策略 2） | 返回任务字典，调用方复用其 `view_token`。 | `platform` 或 `media_id` 为空即 `None`；无命中/异常时生成新 token。 |

## 必须保留的不变式与风险

- `SummaryStatus` 的诚实状态模型必须完整保留：`generated`、`skipped_short`、`failed`、`pending`、`disabled`。特别是未生成摘要不能重新变成文本占位符。
- 共享排序常量为 `CacheManager._TASK_STATUS_PRIORITY_ORDER_BY`：`success` 优先、`failed` 最后、其余状态按 `created_at DESC`。它同时被 `get_task_by_view_token` 与两个去重方法使用；外移时禁止复制 SQL 字面量或改回枚举状态排序，以避免再次遗漏 `calibrating` 或未来状态。
- 新服务若直接导入 `CacheManager`，易与 CacheManager 对服务的 facade 导入形成循环依赖；应通过构造函数接收已有 manager，采用类型检查期导入或 `Protocol`，并让运行时依赖单向。
- 两个 API 读取路径已使用 `asyncio.to_thread`；服务迁移不能悄然改变同步 SQLite + 文件读取的调度模型。两个 tasks 写路径当前同步调用，仍需保留其现有异常映射。

## Backlog / 接受不修

- P2：`create_task` 直接构造 `TaskDedup(self)`，没有经由 CacheManager facade 的虚派发。接受不修：不存在受支持的子类覆盖契约，直接服务调用是本阶段已裁决的边界；改回 facade 会弱化外移结果。
- P3：history route 测试让 `ViewTokenResolver` mock 返回 mock CacheManager，可能掩盖服务装配回归。接受不修：该组测试隔离鉴权和响应契约；Resolver 有直接单测，且最终范围回归已通过，额外装配测试机制的收益不足。
- graphify 仅使用既有图作背景参考，因图偏旧未重跑；度数观察跳过，且它从来不是完成门槛。
- 没有设计偏离。唯一事实偏差是 T1 复核将交接中的「8 个调用点」修正为 5 个 API 直接调用 + `create_task` 内 2 个 dedup 调用；这一统计已在本文与 PROGRESS 固化。

## PR 发布记录

- PR #30 首次创建发现范围污染（19 files / 43,794 deletions）；通过 rebase 校正为 13 files / 363 deletions。仅推送 GitHub，未推送 Gitee。
- 初次校正后的 CI head 为 `28efdc3`，`gate / gate` 已 success；本次文档同步会触发新的 gate。

## 生产部署记录

- PR #30 已 squash merge 到 `main`（`6703321d47eb03df41709b5c8cce210f4a2c8dd2`），CI gate 成功；2026-07-23 已部署到 `n305:/opt/media/VideoTranscriptAPI`。配置 preflight 通过，容器恢复为 `running/healthy`，服务器内 `/livez` OK，公开 `/livez` 连续 3 次和 `openapi.json` 均为 HTTP 200。
- 部署时 GHCR 的旧 DNS 缓存导致镜像 pull 超时。获用户授权后以 live-restore 重启 Docker daemon，`video-history` 与本服务均恢复 healthy；这是运行环境 DNS 缓存问题，不是代码缺陷。
