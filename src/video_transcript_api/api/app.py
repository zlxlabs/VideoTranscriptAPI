import asyncio
import datetime
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ..utils.notifications import init_all_notifiers, shutdown_all_notifiers
from ..utils.observability import init_observability
from ..utils.ytdlp import YtdlpConfigBuilder
from ..llm import set_default_config, log_llm_stats
from ..llm.llm import log_llm_config_summary
from .context import (
    RuntimeContext,
    _retry_terminal_write_pending,
    bind_runtime,
    get_audit_logger,
    get_cache_manager,
    get_config,
    get_logger,
    get_runtime,
    get_static_dir,
    get_temp_manager,
    load_and_validate_config,
    run_with_runtime,
    unbind_runtime,
)
from .routes import audit, health, tasks, users, views
from .services.transcription import process_llm_queue, process_task_queue


def _repair_all_task_snapshots(audit_logger, cache_manager) -> int:
    """Backfill all legacy terminal rows through bounded 500-row operations."""
    total = 0
    while True:
        repaired = audit_logger.repair_task_snapshots(cache_manager, limit=500)
        total += repaired
        if audit_logger.repair_scan_complete:
            return total


async def _close_runtime_in_order(
    runtime,
    stop_background_owners=None,
    *,
    close_notifiers: bool = False,
) -> None:
    """Stop intake first, drain workers, then close notification clients.

    预算从这里（lifespan 的关闭入口）统一起算，覆盖 stop_background_owners
    （shutdown_event，见其内部临时目录清扫的说明）与 runtime.aclose() 两段
    （本地 codex review 第 16 轮 Q4）：此前只有 aclose() 内部自建一份
    WORKER_STOP_TIMEOUT_SECONDS 预算，stop_background_owners() 在它之前
    同步执行、完全不受约束——关闭总耗时不是"一份预算"，而是"无界前段 +
    有界后段"两段相加。现在改为整段共用 runtime.new_shutdown_deadline()
    算出的同一个 deadline，两段依次消耗剩余预算，与 RuntimeContext 内部
    "预算跨阶段累计、不重新充满"的既有原则完全一致。
    """
    deadline = runtime.new_shutdown_deadline()
    try:
        if stop_background_owners is not None:
            await stop_background_owners(deadline)
    finally:
        resources_safe = False
        try:
            resources_safe = await runtime.aclose(deadline=deadline)
        finally:
            if close_notifiers and resources_safe is not False:
                shutdown_all_notifiers()


async def _periodic_maintenance(config: dict) -> None:
    """
    每日维护任务：按保留期清理过期缓存、任务状态记录与审计日志，防止磁盘/数据库无限增长。

    - storage.cache_retention_days：缓存（转录产物）保留天数，0 表示永久保留
    - storage.task_status_retention_days：task_status 表终态（success/failed）
      任务记录保留天数，0 表示永久保留。task_status 行同时被两个下游消费方
      依赖：/view/{view_token} 链接解析（get_view_data_by_token）。历史查询
      使用 audit.db 自有快照，不再依赖 task_status；清理前会归档快照并清空
      其中的 live view_token。该值仍不能短于 cache_retention_days，避免正文
      尚存时分享链接提前失效
    - storage.audit_log_retention_days：审计日志保留天数，0 表示永久保留

    同一次维护周期内，cleanup_old_cache 与 cleanup_task_status 共用同一个
    UTC now（本函数每轮循环只调用一次 datetime.now），避免两次清理各自独
    立调用 now() 之间出现竞态窗口：cleanup_old_cache 遍历删除文件可能耗时
    数秒甚至更久，若 cleanup_task_status 随后再独立取一次更晚的 now，两者
    cutoff 不一致，落在窗口内的记录会出现"缓存判定保留、任务判定删除"，
    打破"task_status 至少活得跟 cache 一样久"的不变式（codex-review R10 #1）。

    repair_task_snapshots（终态审计快照回填）与上面三类"按保留期删除"的
    清理在语义上互不相关——它只是把已经落终态但尚未生成/归档审计快照的
    历史行补齐，不受任何保留期开关控制。三个保留期若全部配置为 0（永久
    保留、都不清理），本函数此前会在下面直接整体 return、连带这个循环体都
    不会跑，导致 repair 也停摆：真启动时的一次性 _repair_all_task_snapshots
    仍会在 startup_event() 里跑一次，但进程长期运行期间新产生的、需要回填
    快照的终态行会永远等不到下一次周期性修复（本地 Codex review 发现）。
    因此这里只用 cleanup_enabled 控制"是否执行按保留期删除"这几步，循环体
    本身与 repair 调用永远执行，不受它影响。
    """
    logger = get_logger()
    storage = config.get("storage", {})
    cache_days = int(storage.get("cache_retention_days", 0) or 0)
    task_status_days = int(storage.get("task_status_retention_days", 180) or 0)
    audit_days = int(storage.get("audit_log_retention_days", 180) or 0)
    cleanup_enabled = cache_days > 0 or task_status_days > 0 or audit_days > 0

    if cleanup_enabled:
        logger.info(
            f"定期清理任务已启动: cache_retention_days={cache_days}, "
            f"task_status_retention_days={task_status_days}, audit_log_retention_days={audit_days}"
        )
    else:
        logger.info(
            "缓存、任务状态与审计日志保留期均未配置（永久保留），周期性删除已"
            "禁用；定期审计快照修复仍会按 24h 周期继续运行"
        )
    while True:
        try:
            # 本轮维护周期共用的 UTC 时间基准：只调用一次 now()，同时传给
            # cleanup_old_cache 和 cleanup_task_status，避免二者各自独立
            # 调用 now() 之间开出竞态窗口（codex-review R10 #1）。
            now = datetime.datetime.now(datetime.timezone.utc)

            # 启动恢复重试（本地 codex review 第 6 轮 G3；第 7 轮 H4 曾把判定
            # 边界从 created_at 时间戳字符串改成 rowid 水位线；CI review 第 5
            # 轮 P1 发现 rowid 水位线在非 AUTOINCREMENT 的 task_status 表上会
            # 被 rowid 复用打破——删除当前持有最大 rowid 的行后，下一次插入会
            # 复用那个 rowid，足以让本进程启动之后才创建的新任务落回水位线以
            # 下、被这里误杀写成 failed。改用进程启动时刻拍下的 task_id 快照，
            # 只有当 startup_event() 里的一次性 recover_orphaned_tasks() 调用
            # 本身抛过异常（RuntimeContext.recovery_pending 置位）才在这里
            # 重试一次；绝不能无条件把 recover_orphaned_tasks 加进每轮周期
            # 维护——那样会把本进程当前正在处理的 queued/processing/
            # calibrating 任务也标记 failed，误杀活任务。重试时带上
            # restrict_to_task_ids=进程启动时刻拍下的非终态 task_id 快照，只
            # 处理这个固定集合里的行——集合本身不会再增长，本进程后来受理的
            # 新任务无论 rowid 是否复用了旧行释放出来的编号，task_id 都不可能
            # 出现在里面，不会被波及（见 CacheManager.recover_orphaned_tasks
            # 的详细说明）。
            runtime = get_runtime()
            if getattr(runtime, "recovery_pending", False):
                restrict_to_task_ids = getattr(runtime, "startup_recovery_task_ids", None)
                recovered = await runtime.run_maintenance(
                    get_cache_manager().recover_orphaned_tasks,
                    restrict_to_task_ids=restrict_to_task_ids,
                )
                # 只有成功跑完才清除标志；异常会向上传播到本函数最外层的
                # try/except，下一轮维护会因为标志仍是 True 而再次重试。
                runtime.recovery_pending = False
                if recovered:
                    logger.warning(f"启动恢复重试：已将 {recovered} 个中断任务标记为 failed")
                else:
                    logger.info("启动恢复重试：无中断任务需要处理")

            # 运行期对账（本地 codex review 第 12 轮 P1 发现 c）：把非终态
            # 且不在进程内在途任务登记表、created_at 早于宽限期的任务行
            # 收敛为 failed——闭合"队列拒绝清理写入自身也失败"这类场景在
            # 服务持续运行期间无人处理的缺口（见 CacheManager.reconcile_
            # runtime_orphaned_tasks 的详细说明）。exclude_task_ids 传入
            # 登记表当前的全部 task_id（两个 kind 并集），确保仍在正常处理
            # 中的任务不会被误杀——登记表是比 created_at 时间阈值更强的
            # 保护。
            reconciled = await runtime.run_maintenance(
                get_cache_manager().reconcile_runtime_orphaned_tasks,
                exclude_task_ids=runtime.inflight_registry.all_task_ids(),
            )
            if reconciled:
                logger.warning(f"运行期对账：已将 {reconciled} 个疑似孤儿任务标记为 failed")

            # 终态写入待补偿重试（K1 桶 b，CI review 第 3 轮 major）：
            # llm_ops.process_llm_queue 提交失败分支双重失败（submit()
            # 失败 + 终态写 FAILED 也失败）时会把 task_id 登记进
            # RuntimeContext.terminal_write_pending（见该字段与
            # process_llm_queue 的文档）。这里每轮维护 drain 一次，对每个
            # id 重试写 FAILED，仍失败的重新登记回去留给下一轮——有界、
            # 可观察的补偿路径，不依赖运行期对账的宽限期猜测。getattr
            # 防御：历史测试用的裸 runtime 替身（不实现这两个方法）应当
            # 被当作"没有待补偿登记"，跳过这一步，与上面 recovery_pending
            # 标志的既有处理方式一致。
            drain_pending = getattr(runtime, "drain_terminal_write_pending", None)
            if drain_pending is not None:
                pending_terminal_writes = drain_pending()
                if pending_terminal_writes:
                    still_pending = await runtime.run_maintenance(
                        _retry_terminal_write_pending,
                        get_cache_manager(), pending_terminal_writes, logger,
                    )
                    resolved = len(pending_terminal_writes) - len(still_pending)
                    if resolved:
                        logger.warning(
                            f"终态写入补偿：{resolved} 个任务已通过运行期维护"
                            f"确认写入 failed"
                        )
                    if still_pending:
                        register_pending = getattr(
                            runtime, "register_terminal_write_pending", None,
                        )
                        if register_pending is not None:
                            for task_id in still_pending:
                                register_pending(task_id)
                        logger.error(
                            f"终态写入补偿：{len(still_pending)} 个任务仍未确认，"
                            f"留待下一轮维护重试: {sorted(still_pending)}"
                        )

            repaired = await get_runtime().run_maintenance(
                get_audit_logger().repair_task_snapshots,
                get_cache_manager(),
                500,
            )
            if repaired:
                logger.info(f"定期修复：检查并归档 {repaired} 条终态任务审计快照")
            if cache_days > 0:
                deleted = await get_runtime().run_maintenance(
                    get_cache_manager().cleanup_old_cache, cache_days, now=now
                )
                if deleted:
                    logger.info(f"定期清理：删除 {deleted} 条超过 {cache_days} 天的缓存记录")
            if task_status_days > 0:
                # 公开正文分享链路依赖 task_status；audit history 已改由
                # audit.db 快照独立保存元数据，不再延长 live view_token 寿命。
                retention_floor_days = cache_days
                deleted = await get_runtime().run_maintenance(
                    get_cache_manager().cleanup_task_status,
                    task_status_days,
                    retention_floor_days,
                    now=now,
                )
                if deleted:
                    logger.info(f"定期清理：删除 {deleted} 条超过 {task_status_days} 天的终态任务状态记录")
            if audit_days > 0:
                deleted = await get_runtime().run_maintenance(
                    get_audit_logger().cleanup_old_logs,
                    audit_days,
                    get_cache_manager().task_exists,
                )
                if deleted:
                    logger.info(f"定期清理：删除 {deleted} 条超过 {audit_days} 天的审计日志")
        except Exception as exc:
            logger.exception("定期清理执行失败（下个周期重试）: %s", exc)
        await asyncio.sleep(24 * 3600)


def create_app(
    *,
    config_loader=load_and_validate_config,
    context_factory=RuntimeContext,
    start_background: bool = True,
) -> FastAPI:
    config = None
    logger = get_logger()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal config, logger
        config = config_loader()
        runtime = context_factory(config)
        token = bind_runtime(runtime)
        app.state.runtime = runtime
        try:
            runtime.start()
            logger = runtime.logger
            if start_background:
                await startup_event()
            yield
        finally:
            try:
                background_started = bool(
                    start_background and getattr(runtime, "started", False)
                )
                await _close_runtime_in_order(
                    runtime,
                    shutdown_event if background_started else None,
                    close_notifiers=background_started,
                )
            finally:
                unbind_runtime(token)

    app = FastAPI(
        title="VideoTranscriptAPI",
        description="视频转录API服务",
        version="1.0.0",
        lifespan=lifespan,
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
        token = bind_runtime(request.app.state.runtime)
        try:
            path = request.url.path
            if "/view/" in path or "/export/" in path:
                query = str(request.url.query)
                ua = request.headers.get("user-agent", "N/A")[:200]
                cf_ip = request.headers.get("cf-connecting-ip", "N/A")
                cf_ray = request.headers.get("cf-ray", "N/A")
                accept = request.headers.get("accept", "N/A")[:100]
                request.app.state.runtime.logger.info(
                    f"[ExternalAccess] {request.method} {path}?{query} | "
                    f"UA: {ua} | CF-IP: {cf_ip} | CF-Ray: {cf_ray} | Accept: {accept}"
                )
                response = await call_next(request)
                ct = response.headers.get("content-type", "N/A")
                cl = response.headers.get("content-length", "unknown")
                request.app.state.runtime.logger.info(
                    f"[ExternalAccess] Response: {request.method} {path} | "
                    f"status={response.status_code} | content-type={ct} | content-length={cl}"
                )
                return response
            return await call_next(request)
        finally:
            unbind_runtime(token)

    app.include_router(health.router)
    app.include_router(tasks.router)
    app.include_router(audit.router)
    app.include_router(users.router)
    app.include_router(views.router)

    async def startup_event():
        # 配置通过严格校验后再接入错误上报；--check-config 不会进入 lifespan。
        init_observability()
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
            # 本地 codex review 第 6 轮 G3：一次性启动恢复失败，不代表
            # 遗留的非终态任务就此不管——置位 recovery_pending，交给
            # _periodic_maintenance 在下一轮维护里限定 cutoff 重试一次
            # （见 RuntimeContext.recovery_pending / CacheManager.
            # recover_orphaned_tasks 的 cutoff 参数说明）。
            app.state.runtime.recovery_pending = True

        try:
            repaired = _repair_all_task_snapshots(
                get_audit_logger(), get_cache_manager()
            )
            if repaired:
                logger.info(f"启动修复：检查并归档 {repaired} 条终态任务审计快照")
        except Exception as exc:
            # Task rows remain authoritative and are never deleted on failure;
            # the daily bounded repair will retry.
            logger.exception("启动审计快照修复失败: %s", exc)

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
        app.state.runtime.background_tasks.append(queue_processor)

        # 每日定期清理过期缓存与审计日志（cache_retention_days 此前从未生效）
        app.state.maintenance_task = asyncio.create_task(_periodic_maintenance(config))
        app.state.runtime.background_tasks.append(app.state.maintenance_task)

        logger.info("启动LLM队列处理器线程")
        llm_thread = threading.Thread(
            target=run_with_runtime,
            args=(app.state.runtime, process_llm_queue),
            daemon=True,
        )
        llm_thread.start()
        app.state.llm_thread = llm_thread
        app.state.runtime.llm_thread = llm_thread

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

    async def shutdown_event(deadline: float) -> None:
        # 优雅关闭只清理非活跃任务目录（hours=0 → 删所有非活跃项），
        # 避免删掉关闭瞬间仍在跑的任务正在使用的文件（D11）。
        #
        # best-effort 纳入统一关闭预算（本地 codex review 第 16 轮 Q4）：
        # clean_up_old_files 内部是同步阻塞调用（遍历顶层条目 + 递归统计
        # 大小 + shutil.rmtree，见 tempfile_manager.py），此前直接同步调用、
        # 无 deadline，会让关闭总耗时不受 WORKER_STOP_TIMEOUT_SECONDS 约束。
        # 现在放到线程里跑，只用 deadline 的剩余预算有界等待；超时就放弃
        # 等待、不阻塞后续的 runtime.aclose()——清扫本身会在后台线程里自然
        # 跑完（无法从外部安全中途取消一个正在执行的 shutil.rmtree），只是
        # 不再等它。放弃等待不等于数据丢失或永久遗留：下次进程启动时
        # startup_event() 会无条件调用同一个
        # temp_manager.clean_up_old_files()（默认 retention_hours，见该
        # 调用处），是已经存在的兜底——这次关闭最多是把清扫时机推迟到下次
        # 启动，不会让孤儿文件永久残留。
        #
        # 实现要点（本地实测踩坑）：不能直接
        # asyncio.create_task(asyncio.to_thread(清扫函数)) 再对这个 task
        # 做有界 asyncio.wait——asyncio.to_thread/run_in_executor(None, ..)
        # 提交给的是事件循环的默认执行器；即便这里对它的等待超时放弃了，
        # 底层线程依然挂在默认执行器上，循环收尾阶段
        # （loop.shutdown_default_executor()，asyncio.run() 退出前会调用，
        # TestClient 关闭真实触发过这条路径）仍会强制等它跑完——"放弃
        # 等待"形同虚设，清扫多久，关闭就被拖多久。改为把清扫本身放进一个
        # 独立的 daemon 线程（不属于事件循环的默认执行器、不受它的收尾
        # 逻辑管辖），只对一个"至多等 remaining 秒"的 Event.wait 做有界
        # 等待——这个等待自身保证按时返回，可以安全地经由共享执行器
        # 等待，不会被清扫本身的时长拖累。
        temp_manager = get_temp_manager()
        remaining = max(0.0, deadline - time.monotonic())
        cleanup_done = threading.Event()
        cleanup_state: dict = {}

        def _run_temp_cleanup() -> None:
            try:
                cleanup_state["count"] = temp_manager.clean_up_old_files(hours=0)
            except Exception as exc:  # pragma: no cover - defensive
                cleanup_state["error"] = exc
            finally:
                cleanup_done.set()

        threading.Thread(
            target=_run_temp_cleanup, name="shutdown-temp-cleanup", daemon=True
        ).start()
        await asyncio.to_thread(cleanup_done.wait, remaining)
        if not cleanup_done.is_set():
            logger.warning(
                "优雅关闭：临时目录清扫未能在关闭预算内完成，放弃等待"
                "（清扫留给下次启动时的兜底扫描）"
            )
        elif "error" in cleanup_state:
            logger.exception(
                "优雅关闭：临时目录清扫失败", exc_info=cleanup_state["error"]
            )
        else:
            cleaned = cleanup_state.get("count")
            if cleaned:
                logger.info(f"优雅关闭：清理了 {cleaned} 个非活跃临时文件/目录")

        log_llm_stats()

        # 停止 ASR 监控
        if hasattr(app.state, "asr_monitor") and app.state.asr_monitor:
            app.state.asr_monitor.stop()

        # 取消定期清理任务
        if hasattr(app.state, "maintenance_task"):
            app.state.maintenance_task.cancel()

        logger.info("API服务已关闭")

    return app
