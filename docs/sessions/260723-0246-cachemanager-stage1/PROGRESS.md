# CacheManager Stage 1 进度

## 基线

- 工作树：`/home/zlx/projects/personal/VideoTranscriptAPI-worktrees/cachemanager-decompose`
- 分支：`refactor/cachemanager-decompose-stage1`
- 基线：`f6cc02c148d18825790530767def824e52bb006c`（本地 `main` 当前 HEAD）
- 范围：仅完成 T1/T2 文档与开工准备；不实现 T3。

## 验证

- 2026-07-23：`uv sync --extra dev` 成功。
- 2026-07-23：`uv run pytest tests/unit/test_cache_manager.py --junit-xml=/tmp/cm-smoke.xml` 退出码 0，`86 passed, 138 warnings in 5.47s`。

## 提交计划

1. T1：提交交接副本、调用面复核和本文件的基线记录。
2. T2：提交物理位置与依赖方向设计，同时记录 T1 的真实提交号。
3. 仅进度同步：记录 T2 提交号，避免提交自引用。

## 提交记录

- T1：`4d46d31948770f8644c5b433300a13af9a5166a5` — 复核 CacheManager Stage1 调用面。
- T2：`7ef6fba9f5f7bec869021ebe00bb3da7d634dd92` — 确定 CacheManager Stage1 服务落点；新增 `DESIGN.md` 并同步 T1 hash，未执行业务代码或新增测试。
- 进度同步：进行中；本次提交仅记录 T2 hash，不为该提交自引用追加无限提交。

## T3：ViewTokenResolver

- RED：`uv run pytest tests/unit/test_view_token_resolver.py --junit-xml=/tmp/stage1-t3-red.xml` 如预期失败；收集阶段出现 1 个 `ModuleNotFoundError`，原因是 `api.services.view_token_resolver` 尚未实现。
- GREEN：`python -m py_compile src/video_transcript_api/api/services/view_token_resolver.py src/video_transcript_api/cache/cache_manager.py` 成功；`uv run pytest tests/unit/test_view_token_resolver.py --junit-xml=/tmp/stage1-t3-green.xml` 通过（9 passed）。
- 范围回归：`uv run pytest tests/unit tests/cache --junit-xml=/tmp/stage1-t3.xml` 通过；JUnit 记录为 2576 tests、0 failures、0 errors、0 skipped。首次同命令在外部进度消息期间未完成、未生成 XML，已重新完整执行，本结果以第二次 JUnit 为准。
- T3：`7e7fcb15e8bb7d5c9f24fd9f39d36269324c7928` — 抽离 ViewTokenResolver 服务；CacheManager 保留四个带 Deprecated 注释的薄委托，未修改 routes、dedup、连接池、锁或 schema。

## T4：TaskDedup

- RED：`uv run pytest tests/unit/test_task_dedup.py --junit-xml=/tmp/stage1-t4-red.xml` 如预期失败；收集阶段出现 1 个 `ModuleNotFoundError`，原因是 `api.services.task_dedup` 尚未实现。
- GREEN：`python -m py_compile src/video_transcript_api/api/services/task_dedup.py src/video_transcript_api/cache/cache_manager.py` 成功；`uv run pytest tests/unit/test_task_dedup.py --junit-xml=/tmp/stage1-t4-green.xml` 通过（10 passed）。
- 范围回归：`uv run pytest tests/unit tests/cache --junit-xml=/tmp/stage1-t4.xml` 通过；JUnit 记录为 2586 tests、0 failures、0 errors、0 skipped。
- T4：`b5555721c477c080d474cb7dc8ad24f52e81dbaa` — 抽离 TaskDedup 服务；两个服务查询均通过注入实例直接复用 `_TASK_STATUS_PRIORITY_ORDER_BY`，未复制 CASE SQL 或创建连接。

## T5：调用点切换与实现阶段验证

- 调用面：将 audit 1 处、tasks 2 处、views 2 处直接改为 `ViewTokenResolver(cache_manager)`；将 `CacheManager.create_task` 的 URL 和 media 去重改为单个即时构造的 `TaskDedup(self)`。未修改 `api/app.py` 注释、路由错误映射、`asyncio.to_thread` 调度或任何连接/锁/schema。
- 调整前 targeted：`uv run pytest tests/unit/test_recalibrate.py tests/unit/test_resummarize.py tests/unit/test_history_routes.py tests/unit/test_cache_manager.py tests/unit/test_view_token_dedup.py --junit-xml=/tmp/stage1-t5-pre.xml` 通过（231 passed）。
- RED：调用切换后，同一 targeted 集合出现 5 个 `test_history_routes.py` 摘要端点失败；旧 mock 指向 `CacheManager.get_view_data_by_token`，而路由已直接构造 Resolver。
- GREEN：最小调整这些测试为 mock `audit.ViewTokenResolver` 构造边界并保留原有摘要、授权、状态断言；同一 targeted 集合再次通过（231 passed）。
- 范围回归：`uv run pytest tests/unit tests/cache --junit-xml=/tmp/stage1-t5.xml` 通过；JUnit 记录为 2586 tests、0 failures、0 errors、0 skipped。
- 静态检查：`uv run python -m compileall -q src tests/unit/test_view_token_resolver.py tests/unit/test_task_dedup.py` 成功；关键 modules import smoke 成功。项目未配置 mypy 或 pyright，未新增类型检查依赖。
- 验收：`rg` 确认 API 路由及 `create_task` 不再调用六个 facade；6 个 CacheManager facade 都保留 Deprecated 薄委托；`_TASK_STATUS_PRIORITY_ORDER_BY` 唯一定义仍在 CacheManager。`cache_manager.py` 从基线 3420 行降至 3123 行（净减 297 行）。
- T5：待本次调用点切换提交后补充 hash。
