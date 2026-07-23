# CacheManager Stage 1 进度

## 基线

- 工作树：`/home/zlx/projects/personal/VideoTranscriptAPI-worktrees/cachemanager-decompose`
- 分支：`refactor/cachemanager-decompose-stage1`
- 基线：`d23812b7e5ee68daccf7be33f59e8f5952c1547e`（`origin/main` PR 基线）。发布时发现本地 `main` 比 `origin/main` 领先两个无关提交，已将 7 个 Stage1 提交原样重放到 `origin/main`；没有设计或代码行为偏离。
- 范围：T1-T5 已完成；仅外移职责 10/11，不涉及连接池、锁或 schema。

## 验证

- 2026-07-23：`uv sync --extra dev` 成功。
- 2026-07-23：`uv run pytest tests/unit/test_cache_manager.py --junit-xml=/tmp/cm-smoke.xml` 退出码 0，`86 passed, 138 warnings in 5.47s`。

## 提交记录

- T1：`58fff4e` — 复核 CacheManager Stage1 调用面。
- T2：`dc10369` — 确定 CacheManager Stage1 服务落点；新增 `DESIGN.md` 并同步 T1 hash，未执行业务代码或新增测试。
- 进度同步：`d1f996e` — 同步 CacheManager Stage1 进度。
- T3：`e64f7a1` — 抽离 ViewTokenResolver 服务。
- T4：`217a599` — 抽离 TaskDedup 服务。
- T5：`5c9168d` — 切换 CacheManager Stage1 调用点。

## 合并与生产部署

- PR #30 已 squash merge 到 `main`：`6703321d47eb03df41709b5c8cce210f4a2c8dd2`；CI gate 成功。
- 2026-07-23 已部署到 `n305:/opt/media/VideoTranscriptAPI`。配置 preflight 通过，生产镜像为 `ghcr.io/zj1123581321/video-transcript-api:6703321d47eb`，运行 digest 为 `sha256:bd41f868836e0d60a74768aaf7bcd360d7b1fe8a02cdacfbd00b6747f0a9debf`。
- 容器状态为 `running/healthy`；服务器内 `/livez` OK，公开 `https://sum.lexgogo.site/livez` 连续 3 次 HTTP 200，`openapi.json` HTTP 200。

## Codex gate

| 轮次 | 结果 | P1 连续无发现计数 | 备注 |
| --- | --- | --- | --- |
| Round 1 | No findings | 1 | 无 P1。 |
| Round 2 | Gate passed | 2 | 无 P1；P2/P3 接受不修，见 NOTES。 |

## T3：ViewTokenResolver

- RED：`uv run pytest tests/unit/test_view_token_resolver.py --junit-xml=/tmp/stage1-t3-red.xml` 如预期失败；收集阶段出现 1 个 `ModuleNotFoundError`，原因是 `api.services.view_token_resolver` 尚未实现。
- GREEN：`python -m py_compile src/video_transcript_api/api/services/view_token_resolver.py src/video_transcript_api/cache/cache_manager.py` 成功；`uv run pytest tests/unit/test_view_token_resolver.py --junit-xml=/tmp/stage1-t3-green.xml` 通过（9 passed）。
- 范围回归：`uv run pytest tests/unit tests/cache --junit-xml=/tmp/stage1-t3.xml` 通过；JUnit 记录为 2576 tests、0 failures、0 errors、0 skipped。首次同命令在外部进度消息期间未完成、未生成 XML，已重新完整执行，本结果以第二次 JUnit 为准。
- T3：`e64f7a1` — 抽离 ViewTokenResolver 服务；CacheManager 保留四个带 Deprecated 注释的薄委托，未修改 routes、dedup、连接池、锁或 schema。

## T4：TaskDedup

- RED：`uv run pytest tests/unit/test_task_dedup.py --junit-xml=/tmp/stage1-t4-red.xml` 如预期失败；收集阶段出现 1 个 `ModuleNotFoundError`，原因是 `api.services.task_dedup` 尚未实现。
- GREEN：`python -m py_compile src/video_transcript_api/api/services/task_dedup.py src/video_transcript_api/cache/cache_manager.py` 成功；`uv run pytest tests/unit/test_task_dedup.py --junit-xml=/tmp/stage1-t4-green.xml` 通过（10 passed）。
- 范围回归：`uv run pytest tests/unit tests/cache --junit-xml=/tmp/stage1-t4.xml` 通过；JUnit 记录为 2586 tests、0 failures、0 errors、0 skipped。
- T4：`217a599` — 抽离 TaskDedup 服务；两个服务查询均通过注入实例直接复用 `_TASK_STATUS_PRIORITY_ORDER_BY`，未复制 CASE SQL 或创建连接。

## T5：调用点切换与实现阶段验证

- 调用面：将 audit 1 处、tasks 2 处、views 2 处直接改为 `ViewTokenResolver(cache_manager)`；将 `CacheManager.create_task` 的 URL 和 media 去重改为单个即时构造的 `TaskDedup(self)`。未修改 `api/app.py` 注释、路由错误映射、`asyncio.to_thread` 调度或任何连接/锁/schema。
- 调整前 targeted：`uv run pytest tests/unit/test_recalibrate.py tests/unit/test_resummarize.py tests/unit/test_history_routes.py tests/unit/test_cache_manager.py tests/unit/test_view_token_dedup.py --junit-xml=/tmp/stage1-t5-pre.xml` 通过（231 passed）。
- RED：调用切换后，同一 targeted 集合出现 5 个 `test_history_routes.py` 摘要端点失败；旧 mock 指向 `CacheManager.get_view_data_by_token`，而路由已直接构造 Resolver。
- GREEN：最小调整这些测试为 mock `audit.ViewTokenResolver` 构造边界并保留原有摘要、授权、状态断言；同一 targeted 集合再次通过（231 passed）。
- 范围回归：`uv run pytest tests/unit tests/cache --junit-xml=/tmp/stage1-t5.xml` 通过；JUnit 记录为 2586 tests、0 failures、0 errors、0 skipped。
- 静态检查：`uv run python -m compileall -q src tests/unit/test_view_token_resolver.py tests/unit/test_task_dedup.py` 成功；关键 modules import smoke 成功。项目未配置 mypy 或 pyright，未新增类型检查依赖。
- 验收：`rg` 确认 API 路由及 `create_task` 不再调用六个 facade；6 个 CacheManager facade 都保留 Deprecated 薄委托；`_TASK_STATUS_PRIORITY_ORDER_BY` 唯一定义仍在 CacheManager。`cache_manager.py` 从基线 3420 行降至 3123 行（净减 297 行）。
- T5：`5c9168d` — 切换调用点完成。

## 收工验证

- 最终范围回归：2586 tests、0 failures、0 errors、0 skipped（`/tmp/stage1-t5.xml`）。
- 静态验证：compileall 与关键模块 import smoke 通过；项目未配置 mypy/pyright，未新增依赖。
- 最终文档提交前状态：T1-T5 均已提交；工作树仅包含当时的文档同步和 `cache_manager.py` 两处空白机械修复。该历史阶段之后，PR #30 已合并到 `main` 并完成生产部署。
