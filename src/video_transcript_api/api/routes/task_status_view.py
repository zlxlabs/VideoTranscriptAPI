"""GET /api/task/{task_id} 的实现（含 C3 长轮询）。

从 tasks.py 拆出：tasks.py 已超文件体积 ratchet 上限（>800 行），
gate 禁止其净增长，新增逻辑必须放 sibling 文件。tasks.py 只留薄路由。

C3 长轮询：``GET /api/task/{id}?wait=<1-60>``——任务进终态或超时即返回，
响应 schema 与无 wait 一致；等待用 await asyncio.sleep 让出事件循环，
不占线程池；一次请求仍只写一行审计。
"""

import asyncio
import datetime
import time

from fastapi import HTTPException, Request

from ..context import (
    ApiError,
    get_audit_logger,
    get_cache_manager,
    get_logger,
    lazy_resource,
)
from ..services.transcription import TranscribeResponse
from ...i18n import t
from ...utils.task_status import TERMINAL_STATUSES, TaskStatus, http_code_for_status

logger = lazy_resource(get_logger)
audit_logger = lazy_resource(get_audit_logger)
cache_manager = lazy_resource(get_cache_manager)

# wait 挂起期间的轮询间隔（秒）。
TASK_WAIT_POLL_INTERVAL_SECONDS = 0.5


async def get_task_status_impl(
    task_id: str,
    request: Request,
    wait: int | None,
    user_id: str | None,
    api_key: str | None,
) -> TranscribeResponse:
    """查询任务状态；wait 给定时挂起至终态或超时（长轮询）。"""
    start_time = datetime.datetime.now()

    try:
        # 状态以持久化的 task_status 表为唯一真相源（重启不丢、覆盖全流程）
        task_info = cache_manager.get_task_by_id(task_id)
        if not task_info:
            processing_time_ms = int(
                (datetime.datetime.now() - start_time).total_seconds() * 1000
            )
            audit_logger.log_api_call(
                api_key=api_key,
                user_id=user_id,
                endpoint=f"/api/task/{task_id}",
                processing_time_ms=processing_time_ms,
                status_code=404,
                task_id=None,
                user_agent=request.headers.get("User-Agent"),
                remote_ip=request.client.host if request.client else None,
            )
            logger.warning("任务不存在: {}", task_id)
            raise ApiError(404, "task_id_not_found", task_id=task_id)

        status = task_info.get("status") or TaskStatus.QUEUED

        # C3 长轮询：非终态时挂起等待，直到进终态或超时；循环期间不写审计
        # （一次请求一行），超时后返回当时状态，响应 schema 与无 wait 一致。
        if wait is not None:
            deadline = time.monotonic() + wait
            while status not in TERMINAL_STATUSES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(TASK_WAIT_POLL_INTERVAL_SECONDS, remaining))
                task_info = cache_manager.get_task_by_id(task_id)
                status = task_info.get("status") or TaskStatus.QUEUED

        code = http_code_for_status(status)

        # progress is already dict|None from CacheManager.get_task_by_id
        progress = task_info.get("progress")

        # 干净的元信息形状 + 显式 status；正文走 view_token 获取
        data = {
            "status": status,
            "view_token": task_info.get("view_token"),
            "title": task_info.get("title"),
            "author": task_info.get("author"),
            "platform": task_info.get("platform"),
            "completed_at": task_info.get("completed_at"),
            "progress": progress,
        }
        if status == TaskStatus.FAILED:
            data["error"] = task_info.get("error_message") or t("error.task_failed")

        message_map = {
            TaskStatus.QUEUED: t("api.task_status.queued"),
            TaskStatus.PROCESSING: t("api.task_status.processing"),
            TaskStatus.CALIBRATING: t("api.task_status.calibrating"),
            TaskStatus.SUCCESS: t("api.task_status.success"),
            TaskStatus.FAILED: t("api.task_status.failed"),
        }
        message = message_map.get(status, t("api.task_status.query_succeeded"))

        processing_time_ms = int(
            (datetime.datetime.now() - start_time).total_seconds() * 1000
        )
        audit_logger.log_api_call(
            api_key=api_key,
            user_id=user_id,
            endpoint=f"/api/task/{task_id}",
            processing_time_ms=processing_time_ms,
            status_code=code,
            task_id=task_id,
            user_agent=request.headers.get("User-Agent"),
            remote_ip=request.client.host if request.client else None,
        )

        return TranscribeResponse(code=code, message=message, data=data)
    except HTTPException:
        raise
    except Exception as exc:
        processing_time_ms = int(
            (datetime.datetime.now() - start_time).total_seconds() * 1000
        )
        audit_logger.log_api_call(
            api_key=api_key,
            user_id=user_id,
            endpoint=f"/api/task/{task_id}",
            processing_time_ms=processing_time_ms,
            status_code=500,
            task_id=task_id,
            user_agent=request.headers.get("User-Agent"),
            remote_ip=request.client.host if request.client else None,
        )
        logger.exception("获取任务状态异常: {}", exc)
        raise ApiError(500, "task_status_failed", error=str(exc))
