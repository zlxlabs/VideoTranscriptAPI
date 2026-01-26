import datetime
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from ..context import (
    get_audit_logger,
    get_cache_manager,
    get_config,
    get_logger,
    get_task_queue,
    get_task_results,
)
from ..services.transcription import (
    TranscribeRequest,
    TranscribeResponse,
    verify_token,
)
from ...utils.notifications import send_view_link_wechat

logger = get_logger()
config = get_config()
audit_logger = get_audit_logger()
cache_manager = get_cache_manager()
task_results = get_task_results()

router = APIRouter(prefix="/api", tags=["tasks"])


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_video(
    request_body: TranscribeRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    user_info: dict = Depends(verify_token),
):
    url = request_body.url
    if not url:
        logger.warning("请求未提供视频URL")
        raise HTTPException(status_code=400, detail="视频URL不能为空")

    logger.info(
        "收到转录API请求 - URL: %s, 说话人识别: %s, 自定义企微webhook: %s, source_url: %s, 完整请求体: %s",
        url,
        request_body.use_speaker_recognition,
        request_body.wechat_webhook is not None,
        request_body.source_url,
        request_body.model_dump(),
    )

    start_time = datetime.datetime.now()
    user_id = user_info.get("user_id")
    api_key = user_info.get("api_key")

    audit_logger.log_api_call(
        api_key=api_key,
        user_id=user_id,
        endpoint="/api/transcribe",
        video_url=url,
        user_agent=request.headers.get("User-Agent"),
        remote_ip=request.client.host if request.client else None,
    )

    try:
        task_info = cache_manager.create_task(
            url=url,
            use_speaker_recognition=request_body.use_speaker_recognition,
            source_url=request_body.source_url
        )
        task_id = task_info["task_id"]
        view_token = task_info["view_token"]

        task_results[task_id] = {
            "status": "queued",
            "message": "任务已加入队列",
            "view_token": view_token,
        }

        try:
            effective_webhook = (
                request_body.wechat_webhook
                or user_info.get("wechat_webhook")
                or config.get("wechat", {}).get("webhook")
            )

            task_queue = get_task_queue()
            task = {
                "id": task_id,
                "url": url,
                "use_speaker_recognition": request_body.use_speaker_recognition,
                "wechat_webhook": effective_webhook,
                "user_info": user_info,
                "source_url": request_body.source_url,
                "metadata_override": request_body.metadata_override.model_dump() if request_body.metadata_override else None,
            }

            try:
                await task_queue.put(task)
                logger.info("任务已加入队列: %s, URL: %s", task_id, url)
            except asyncio.QueueFull:
                logger.warning("任务队列已满，拒绝任务: %s", url)
                raise HTTPException(status_code=503, detail="任务队列已满，请稍后重试")

            try:
                # 优先使用 source_url 用于平台识别和通知显示
                display_url = request_body.source_url or url

                # 如果用户提供了 metadata_override.title，优先使用它
                if request_body.metadata_override and request_body.metadata_override.title:
                    title = request_body.metadata_override.title
                    logger.info("使用用户提供的标题: %s", title)
                else:
                    # 根据平台生成默认标题
                    title = "转录任务已创建"
                    if "youtube.com" in display_url or "youtu.be" in display_url:
                        title = "YouTube视频转录"
                    elif "bilibili.com" in display_url or "b23.tv" in display_url:
                        title = "Bilibili视频转录"
                    elif "xiaoyuzhoufm.com" in display_url:
                        title = "小宇宙播客转录"
                    elif "xiaohongshu.com" in display_url or "xhslink.com" in display_url:
                        title = "小红书内容转录"
                    elif "douyin.com" in display_url:
                        title = "抖音视频转录"

                send_view_link_wechat(
                    title=f"🎬 {title}",
                    view_token=view_token,
                    webhook=effective_webhook,
                    original_url=display_url,
                )
                logger.info("已发送任务创建通知: %s，使用URL: %s", task_id, display_url)
            except Exception as exc:
                logger.exception("发送任务创建通知失败: %s, 错误: %s", task_id, exc)
        except Exception as queue_exc:
            logger.exception("任务加入队列失败: %s, 错误: %s", task_id, queue_exc)
            raise HTTPException(status_code=500, detail=f"任务加入队列失败: {queue_exc}")

        processing_time_ms = int(
            (datetime.datetime.now() - start_time).total_seconds() * 1000
        )
        audit_logger.log_api_call(
            api_key=api_key,
            user_id=user_id,
            endpoint="/api/transcribe",
            video_url=url,
            processing_time_ms=processing_time_ms,
            status_code=202,
            task_id=task_id,
            user_agent=request.headers.get("User-Agent"),
            remote_ip=request.client.host if request.client else None,
        )

        return TranscribeResponse(
            code=202,
            message="任务已提交",
            data={"task_id": task_id, "view_token": view_token},
        )
    except HTTPException:
        raise
    except Exception as exc:
        processing_time_ms = int(
            (datetime.datetime.now() - start_time).total_seconds() * 1000
        )
        audit_logger.log_api_call(
            api_key=api_key,
            user_id=user_id,
            endpoint="/api/transcribe",
            video_url=url,
            processing_time_ms=processing_time_ms,
            status_code=500,
            user_agent=request.headers.get("User-Agent"),
            remote_ip=request.client.host if request.client else None,
        )
        logger.exception("提交转录任务失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"提交转录任务失败: {exc}")


@router.get("/task/{task_id}", response_model=TranscribeResponse)
async def get_task_status(
    task_id: str,
    request: Request,
    user_info: dict = Depends(verify_token),
):
    start_time = datetime.datetime.now()
    user_id = user_info.get("user_id")
    api_key = user_info.get("api_key")

    try:
        if task_id not in task_results:
            processing_time_ms = int(
                (datetime.datetime.now() - start_time).total_seconds() * 1000
            )
            audit_logger.log_api_call(
                api_key=api_key,
                user_id=user_id,
                endpoint=f"/api/task/{task_id}",
                processing_time_ms=processing_time_ms,
                status_code=404,
                task_id=task_id,
                user_agent=request.headers.get("User-Agent"),
                remote_ip=request.client.host if request.client else None,
            )
            logger.warning("任务不存在: %s", task_id)
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

        task_result = task_results[task_id]
        code = 200
        if task_result.get("status") in {"queued", "processing"}:
            code = 202
        elif task_result.get("status") == "failed":
            code = 500

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

        return TranscribeResponse(
            code=code,
            message=task_result.get("message", "获取任务状态成功"),
            data=task_result.get("data"),
        )
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
        logger.exception("获取任务状态异常: %s", exc)
        raise HTTPException(status_code=500, detail=f"获取任务状态失败: {exc}")


@router.get("/webhook-stats")
async def get_webhook_stats(user_info: dict = Depends(verify_token)):
    return TranscribeResponse(
        code=200,
        message="限流器已迁移至 wecom-notifier，不再提供详细统计",
        data={
            "deprecated": True,
            "message": "Rate limiter has been migrated to wecom-notifier package",
            "suggestion": "Rate limiting is now handled automatically by wecom-notifier",
        },
    )


@router.get("/webhook-status")
async def get_webhook_status_info(
    webhook_url: str,
    user_info: dict = Depends(verify_token),
):
    return TranscribeResponse(
        code=200,
        message="限流器已迁移至 wecom-notifier，不再提供详细状态",
        data={
            "deprecated": True,
            "webhook_url": webhook_url[:50] + "..." if len(webhook_url) > 50 else webhook_url,
            "message": "Webhook status is now managed by wecom-notifier package",
            "suggestion": "All webhooks are automatically rate-limited by wecom-notifier",
        },
    )
