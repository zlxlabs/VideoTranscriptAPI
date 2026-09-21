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

## 段落 3 — transcript.html 中断横幅

- 当前阶段：implementing，模板横幅完成
- 本段结论：横幅插在 `block content` 最顶部、统计信息之前，不在任何布局条件块内；success 渲染不含横幅。路由级 TestClient 与 Jinja 直渲染都看到横幅 + 正文。
- 关键决策与已否决方案：样式写在 transcript.html 的 extra_css（base.html 不在允许修改范围）；复用 `.section` 与 CSS 变量，不用 status-error 以免看起来像整页失败。
- 下一步唯一动作：更新 web_view.md 并做红验 + 全量测试。

## 段落 4 — 文档、红验与全量收尾

- 当前阶段：implementing，文档与收尾完成
- 本段结论：`web_view.md` 记录了 interrupted 呈现语义。反向红验把产物判据放宽成 `file_path` 后，空壳用例以 AssertionError 转红（interrupted vs failed），已还原判据行。全量 `uv run --extra dev pytest tests/unit tests/features tests/integration -q` 退出码 0。顺手把 interrupted 日志从失效的 `%s` 改成 f-string（loguru）。
- 关键决策与已否决方案：audit `/summary` 在提取成功后仍返回 envelope `status=success`（表示预览可用），不把 hover 客户端的成功信封改成 interrupted。
- 下一步唯一动作：push 并写 report.md。
