import asyncio
import threading

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ..utils.notifications import init_global_notifier, shutdown_global_notifier
from ..utils.ytdlp import YtdlpConfigBuilder
from ..llm import set_default_config, log_llm_stats
from ..llm.llm import log_llm_config_summary
from .context import get_config, get_logger, get_static_dir, get_temp_manager
from .routes import audit, health, tasks, users, views
from .services.transcription import process_llm_queue, process_task_queue


def create_app() -> FastAPI:
    config = get_config()
    logger = get_logger()

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
        temp_manager = get_temp_manager()
        old_files_count = temp_manager.clean_up_old_files(hours=24)
        if old_files_count > 0:
            logger.info(f"启动时清理了 {old_files_count} 个旧临时文件")

        init_global_notifier()

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
        asyncio.create_task(process_task_queue())

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
        temp_manager = get_temp_manager()
        temp_manager.clean_up()

        log_llm_stats()

        # 停止 ASR 监控
        if hasattr(app.state, "asr_monitor") and app.state.asr_monitor:
            app.state.asr_monitor.stop()

        shutdown_global_notifier()
        logger.info("API服务已关闭")

    return app
