import asyncio
import datetime
import threading

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ..utils.notifications import init_all_notifiers, shutdown_all_notifiers
from ..utils.observability import init_observability
from ..utils.ytdlp import YtdlpConfigBuilder
from ..llm import set_default_config, log_llm_stats
from ..llm.llm import log_llm_config_summary
from .context import (
    get_audit_logger,
    get_cache_manager,
    get_config,
    get_logger,
    get_static_dir,
    get_temp_manager,
)
from .routes import audit, health, tasks, users, views
from .services.transcription import process_llm_queue, process_task_queue


async def _periodic_maintenance(config: dict) -> None:
    """
    每日维护任务：按保留期清理过期缓存、任务状态记录与审计日志，防止磁盘/数据库无限增长。

    - storage.cache_retention_days：缓存（转录产物）保留天数，0 表示永久保留
    - storage.task_status_retention_days：task_status 表终态（success/failed）
      任务记录保留天数，0 表示永久保留。task_status 行同时被两个下游消费方
      依赖：/view/{view_token} 链接解析（get_view_data_by_token）与
      /api/audit/history 的 LEFT JOIN（用于回填标题/平台/状态/view_token）。
      因此该值不应短于 cache_retention_days 与 audit_log_retention_days
      两者中的任何一个；短于时 cleanup_task_status 会钳制为二者的较大值
      （并记 warning）（codex-review R10 #2）。cache_retention_days 或
      audit_log_retention_days 任一为 0（永久保留）时，task_status 清理会
      被整体跳过——只要有一个下游消费方永久保留，task_status 就必须永久
      保留才能不断链
    - storage.audit_log_retention_days：审计日志保留天数，0 表示永久保留

    同一次维护周期内，cleanup_old_cache 与 cleanup_task_status 共用同一个
    UTC now（本函数每轮循环只调用一次 datetime.now），避免两次清理各自独
    立调用 now() 之间出现竞态窗口：cleanup_old_cache 遍历删除文件可能耗时
    数秒甚至更久，若 cleanup_task_status 随后再独立取一次更晚的 now，两者
    cutoff 不一致，落在窗口内的记录会出现"缓存判定保留、任务判定删除"，
    打破"task_status 至少活得跟 cache 一样久"的不变式（codex-review R10 #1）。
    """
    logger = get_logger()
    storage = config.get("storage", {})
    cache_days = int(storage.get("cache_retention_days", 0) or 0)
    task_status_days = int(storage.get("task_status_retention_days", 180) or 0)
    audit_days = int(storage.get("audit_log_retention_days", 180) or 0)

    if cache_days <= 0 and task_status_days <= 0 and audit_days <= 0:
        logger.info("缓存、任务状态与审计日志保留期均未配置，定期清理任务退出")
        return

    logger.info(
        f"定期清理任务已启动: cache_retention_days={cache_days}, "
        f"task_status_retention_days={task_status_days}, audit_log_retention_days={audit_days}"
    )
    while True:
        try:
            # 本轮维护周期共用的 UTC 时间基准：只调用一次 now()，同时传给
            # cleanup_old_cache 和 cleanup_task_status，避免二者各自独立
            # 调用 now() 之间开出竞态窗口（codex-review R10 #1）。
            now = datetime.datetime.now(datetime.timezone.utc)
            if cache_days > 0:
                deleted = await asyncio.to_thread(
                    get_cache_manager().cleanup_old_cache, cache_days, now=now
                )
                if deleted:
                    logger.info(f"定期清理：删除 {deleted} 条超过 {cache_days} 天的缓存记录")
            if task_status_days > 0:
                # 钳制目标取 cache_retention_days 与 audit_log_retention_days
                # 两者的较大值：/view/{view_token} 依赖 cache 保留期，
                # /api/audit/history 的 LEFT JOIN 依赖 audit 保留期，
                # task_status 需要活得比两者都久（codex-review R10 #2）。
                # 二者任一为 0（永久保留）时该消费方永不过期，task_status
                # 也必须永久保留，因此把钳制目标同样置为 0（复用
                # cleanup_task_status 中 cache_retention_days<=0 时"跳过
                # 清理"的既有语义），而不是让 max() 把 0 当作"无要求"直接
                # 被另一侧的正数盖过。
                retention_floor_days = (
                    0 if (cache_days <= 0 or audit_days <= 0) else max(cache_days, audit_days)
                )
                deleted = await asyncio.to_thread(
                    get_cache_manager().cleanup_task_status,
                    task_status_days,
                    retention_floor_days,
                    now=now,
                )
                if deleted:
                    logger.info(f"定期清理：删除 {deleted} 条超过 {task_status_days} 天的终态任务状态记录")
            if audit_days > 0:
                deleted = await asyncio.to_thread(get_audit_logger().cleanup_old_logs, audit_days)
                if deleted:
                    logger.info(f"定期清理：删除 {deleted} 条超过 {audit_days} 天的审计日志")
        except Exception as exc:
            logger.exception("定期清理执行失败（下个周期重试）: %s", exc)
        await asyncio.sleep(24 * 3600)


def create_app() -> FastAPI:
    config = get_config()
    logger = get_logger()

    # 最早接入错误上报（fail-open）：未配 SENTRY_DSN 时为 no-op
    init_observability()

    app = FastAPI(
        title="VideoTranscriptAPI",
        description="视频转录API服务",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    static_dir = get_static_dir()
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 调试中间件：记录 /view/ 请求的详细信息，用于排查外部 AI 工具的访问问题
    @app.middleware("http")
    async def log_external_access(request: Request, call_next):
        path = request.url.path
        if "/view/" in path or "/export/" in path:
            query = str(request.url.query)
            ua = request.headers.get("user-agent", "N/A")[:200]
            cf_ip = request.headers.get("cf-connecting-ip", "N/A")
            cf_ray = request.headers.get("cf-ray", "N/A")
            accept = request.headers.get("accept", "N/A")[:100]
            logger.info(
                f"[ExternalAccess] {request.method} {path}?{query} | "
                f"UA: {ua} | CF-IP: {cf_ip} | CF-Ray: {cf_ray} | Accept: {accept}"
            )
            response = await call_next(request)
            ct = response.headers.get("content-type", "N/A")
            cl = response.headers.get("content-length", "unknown")
            logger.info(
                f"[ExternalAccess] Response: {request.method} {path} | "
                f"status={response.status_code} | content-type={ct} | content-length={cl}"
            )
            return response
        return await call_next(request)

    app.include_router(health.router)
    app.include_router(tasks.router)
    app.include_router(audit.router)
    app.include_router(users.router)
    app.include_router(views.router)

    @app.on_event("startup")
    async def startup_event():
        # 启动时按配置的 temp_retention_hours 清理孤儿临时文件（崩溃/强杀残留）。
        # 此时无活跃任务，超龄的孤儿目录/文件会被清掉。
        temp_manager = get_temp_manager()
        old_files_count = temp_manager.clean_up_old_files()
        if old_files_count > 0:
            logger.info(f"启动时清理了 {old_files_count} 个旧临时文件")

        init_all_notifiers()

        # 启动恢复：把上次进程中断遗留的非终态任务（queued/processing/calibrating）
        # 标记为 failed。内存任务队列随进程崩溃丢失，否则这些任务会永久卡在处理中。
        try:
            recovered = get_cache_manager().recover_orphaned_tasks()
            if recovered:
                logger.warning(f"启动恢复：已将 {recovered} 个中断任务标记为 failed")
        except Exception as exc:
            logger.exception("启动恢复扫描失败: %s", exc)

        # 设置 LLM 模块默认配置（用于 JSON 结构化输出）
        set_default_config(config)
        logger.info("LLM default config set")

        # 打印每任务 provider+model+thinking 摘要（set_default_config 已注入 custom_patterns）
        log_llm_config_summary(config)

        # 初始化 yt-dlp 配置并验证 YouTube cookie
        logger.info("Initializing yt-dlp configuration...")
        ytdlp_builder = YtdlpConfigBuilder(config)
        ytdlp_builder.validate_cookie_on_startup()
        app.state.ytdlp_builder = ytdlp_builder

        logger.info("启动任务队列处理器")
        # 保存引用防止 task 被 GC 回收；done_callback 兜底：处理器一旦退出，
        # 新任务会永远停在 queued 且无任何报错，必须留下 critical 日志。
        queue_processor = asyncio.create_task(process_task_queue())

        def _on_queue_processor_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.critical(f"任务队列处理器异常退出，新任务将无法被处理: {exc}")
            else:
                logger.critical("任务队列处理器意外退出（无异常），新任务将无法被处理")

        queue_processor.add_done_callback(_on_queue_processor_done)
        app.state.queue_processor = queue_processor

        # 每日定期清理过期缓存与审计日志（cache_retention_days 此前从未生效）
        app.state.maintenance_task = asyncio.create_task(_periodic_maintenance(config))

        logger.info("启动LLM队列处理器线程")
        llm_thread = threading.Thread(target=process_llm_queue, daemon=True)
        llm_thread.start()
        app.state.llm_thread = llm_thread

        risk_config = config.get("risk_control", {})
        if risk_config.get("enabled", False):
            logger.info("正在初始化风控模块...")
            try:
                from ..risk_control import init_risk_control

                init_risk_control(config)
                logger.info("风控模块初始化完成")
            except Exception as exc:
                logger.exception("风控模块初始化失败: %s", exc)
                logger.warning("风控模块将被禁用")

        # 启动 ASR 服务监控
        try:
            from ..utils.asr_monitor import start_asr_monitor
            asr_monitor = start_asr_monitor(config)
            if asr_monitor:
                app.state.asr_monitor = asr_monitor
                logger.info("ASR 服务监控已启动")
        except Exception as exc:
            logger.warning(f"ASR 监控启动失败: {exc}")

        logger.info("API服务已启动")

    @app.on_event("shutdown")
    async def shutdown_event():
        # 优雅关闭只清理非活跃任务目录（hours=0 → 删所有非活跃项），
        # 避免删掉关闭瞬间仍在跑的任务正在使用的文件（D11）。
        temp_manager = get_temp_manager()
        temp_manager.clean_up_old_files(hours=0)

        log_llm_stats()

        # 停止 ASR 监控
        if hasattr(app.state, "asr_monitor") and app.state.asr_monitor:
            app.state.asr_monitor.stop()

        # 取消定期清理任务
        if hasattr(app.state, "maintenance_task"):
            app.state.maintenance_task.cancel()

        shutdown_all_notifiers()
        logger.info("API服务已关闭")

    return app
