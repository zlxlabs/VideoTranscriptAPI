# vta-view-partial-fallback 进度存档

## 段落 1 — resolver 回落 + interrupted 状态

- 当前阶段：implementing，resolver 回落完成
- 本段结论：`get_view_data_by_token` 的 failed 分支在 platform/media_id 可取且缓存正文非空时返回 `status=interrupted`，并复用 success 的数据组装（`_assemble_content_view` 的第二个消费者是 interrupted 路径）。空壳缓存（空字符串 / 仅 key_info.json / 目录存在但无正文）仍返回 `failed`。
- 关键决策与已否决方案：产物可用只认 `llm_calibrated`/`transcript_data` 非空字符串，不认目录存在性。抽 `_assemble_content_view` 是因为 success 与 interrupted 两处消费同一组装。
- 下一步唯一动作：改 views 路由/导出与 audit 摘要端点，让 interrupted 走正文页和可导出路径。

## 段落 2 — 路由 / 导出 / 审计摘要消费点

- 当前阶段：implementing，消费点改完
- 本段结论：`interrupted` 与 `success` 共用 `_CONTENT_VIEW_STATUSES`，走 `transcript.html` 并调用 `_prepare_success_view`；raw/page 导出按成功同款读磁盘文件。audit `/summary` 发现 task_status 仍是 failed 的前置门，failed 无正文仍返回原来的 202，failed+interrupted 才提供 summary。
- 关键决策与已否决方案：不把 failed 一律放进摘要成功门（会改变无产物失败任务的 202 形状）；不把 interrupted 复用成 success。
- 下一步唯一动作：在 transcript.html 顶部加中断横幅并做渲染取证。
