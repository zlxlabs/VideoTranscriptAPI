import asyncio
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
    RecalibrateRequest,
    TranscribeRequest,
    TranscribeResponse,
    verify_token,
)
from ...utils.notifications import send_view_link_wechat
from ...utils.accounts.user_manager import get_user_manager

logger = get_logger()
config = get_config()
audit_logger = get_audit_logger()
cache_manager = get_cache_manager()
task_results = get_task_results()

router = APIRouter(prefix="/api", tags=["tasks"])


def _normalize_empty_string(value: str | None) -> str | None:
    """将空字符串规范化为 None"""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


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

    # 规范化空字符串为 None
    normalized_download_url = _normalize_empty_string(request_body.download_url)

    # 规范化 metadata_override 中的空字符串
    normalized_metadata_override = None
    if request_body.metadata_override:
        metadata_dict = request_body.metadata_override.model_dump()
        # 过滤掉 None 和空字符串
        filtered_metadata = {
            k: v for k, v in metadata_dict.items()
            if v is not None and (not isinstance(v, str) or v.strip())
        }
        # 只有在有有效字段时才设置 metadata_override
        normalized_metadata_override = filtered_metadata if filtered_metadata else None

    logger.info(
        f"收到转录API请求 - URL: {url}, 说话人识别: {request_body.use_speaker_recognition}, "
        f"自定义企微webhook: {request_body.wechat_webhook is not None}, "
        f"download_url: {normalized_download_url}, metadata_override: {normalized_metadata_override}"
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

    # 提前解析 URL，提取 platform+media_id 用于同源视频去重
    parsed_platform = None
    parsed_media_id = None
    try:
        from ...utils.url_parser import URLParser
        parsed_url = URLParser().parse(url)
        parsed_platform = parsed_url.platform
        parsed_media_id = parsed_url.video_id
        logger.info(f"URL预解析成功: platform={parsed_platform}, media_id={parsed_media_id}")
    except Exception as e:
        logger.warning(f"URL预解析失败，降级到精确URL匹配: {e}")

    try:
        task_info = cache_manager.create_task(
            url=url,
            use_speaker_recognition=request_body.use_speaker_recognition,
            download_url=normalized_download_url,
            platform=parsed_platform,
            media_id=parsed_media_id,
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
                "download_url": normalized_download_url,
                "metadata_override": normalized_metadata_override,
            }

            try:
                await task_queue.put(task)
                logger.info(f"任务已加入队列: {task_id}, URL: {url}")
            except asyncio.QueueFull:
                logger.warning("任务队列已满，拒绝任务: %s", url)
                raise HTTPException(status_code=503, detail="任务队列已满，请稍后重试")

            try:
                display_url = url

                # 如果用户提供了 metadata_override.title，优先使用它
                if normalized_metadata_override and normalized_metadata_override.get("title"):
                    title = normalized_metadata_override["title"]
                    logger.info(f"使用用户提供的标题: {title}")
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
                logger.info(f"已发送任务创建通知: {task_id}，使用URL: {display_url}")
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
            wechat_webhook=effective_webhook,
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


@router.post("/recalibrate", response_model=TranscribeResponse)
async def recalibrate(
    request_body: RecalibrateRequest,
    request: Request,
    user_info: dict = Depends(verify_token),
):
    """重新校对接口

    仅重新执行校对步骤（跳过下载、转录、总结），需要 recalibrate 权限。
    """
    view_token = request_body.view_token
    user_id = user_info.get("user_id")
    api_key = user_info.get("api_key")

    start_time = datetime.datetime.now()

    audit_logger.log_api_call(
        api_key=api_key,
        user_id=user_id,
        endpoint="/api/recalibrate",
        user_agent=request.headers.get("User-Agent"),
        remote_ip=request.client.host if request.client else None,
    )

    # 权限检查
    um = get_user_manager()
    if not um.check_permission(user_info, "recalibrate"):
        logger.warning(f"用户 {user_id} 无 recalibrate 权限")
        raise HTTPException(status_code=403, detail="无重新校对权限")

    # 通过 view_token 获取缓存数据
    cache_data = cache_manager.get_cache_by_view_token(view_token)
    if not cache_data:
        logger.warning(f"view_token 对应的缓存不存在: {view_token}")
        raise HTTPException(status_code=404, detail="未找到对应的转录数据")

    # 验证有转录数据
    transcript_data = cache_data.get("transcript_data")
    if not transcript_data:
        logger.warning(f"缓存中无转录数据: {view_token}")
        raise HTTPException(status_code=400, detail="缓存中没有转录数据，无法重新校对")

    task_info = cache_data.get("task_info", {})
    platform = cache_data.get("platform")
    media_id = cache_data.get("media_id")
    use_speaker_recognition = cache_data.get("use_speaker_recognition", False)
    video_title = cache_data.get("title", "")
    author = cache_data.get("author", "")
    description = cache_data.get("description", "")
    cache_file_path = cache_data.get("file_path")

    # 创建新任务（复用原 view_token）
    task_id = cache_manager.generate_task_id()
    try:
        with cache_manager._get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO task_status
                (task_id, view_token, url, platform, media_id,
                 use_speaker_recognition, status, title, author)
                VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?)
            ''', (
                task_id, view_token, task_info.get("url", ""),
                platform, media_id, use_speaker_recognition,
                video_title, author,
            ))
        logger.info(f"重新校对任务创建成功: {task_id}, view_token: {view_token}")
    except Exception as e:
        logger.error(f"创建重新校对任务失败: {e}")
        raise HTTPException(status_code=500, detail=f"创建重新校对任务失败: {e}")

    task_results[task_id] = {
        "status": "processing",
        "message": "正在重新校对",
        "view_token": view_token,
    }

    # 准备转录内容（与 _handle_llm_task 的输入格式一致）
    transcript_text = ""
    transcription_data_for_llm = None
    if cache_data.get("transcript_type") == "funasr":
        transcription_data_for_llm = transcript_data
        from ...transcriber import FunASRSpeakerClient
        funasr_client = FunASRSpeakerClient()
        transcript_text = funasr_client.format_transcript_with_speakers(transcript_data)
    else:
        transcript_text = transcript_data

    # 确定 webhook
    effective_webhook = (
        request_body.wechat_webhook
        or user_info.get("wechat_webhook")
        or config.get("wechat", {}).get("webhook")
    )

    # 放入 LLM 队列
    from ..context import get_llm_queue
    llm_queue = get_llm_queue()

    llm_task = {
        "task_id": task_id,
        "url": task_info.get("url", ""),
        "display_url": task_info.get("url", ""),
        "platform": platform,
        "media_id": media_id,
        "video_title": video_title,
        "author": author,
        "description": description,
        "transcript": transcript_text,
        "use_speaker_recognition": use_speaker_recognition,
        "transcription_data": transcription_data_for_llm if use_speaker_recognition else None,
        "is_generic": False,
        "wechat_webhook": effective_webhook,
        "calibrate_only": True,  # 标记仅校对模式
    }

    try:
        llm_queue.put(llm_task)
        logger.info(f"重新校对任务已加入 LLM 队列: {task_id}")
    except Exception as e:
        logger.error(f"重新校对任务加入队列失败: {e}")
        raise HTTPException(status_code=500, detail=f"任务加入队列失败: {e}")

    processing_time_ms = int(
        (datetime.datetime.now() - start_time).total_seconds() * 1000
    )
    audit_logger.log_api_call(
        api_key=api_key,
        user_id=user_id,
        endpoint="/api/recalibrate",
        processing_time_ms=processing_time_ms,
        status_code=202,
        task_id=task_id,
        user_agent=request.headers.get("User-Agent"),
        remote_ip=request.client.host if request.client else None,
        wechat_webhook=effective_webhook,
    )

    return TranscribeResponse(
        code=202,
        message="重新校对任务已提交",
        data={"task_id": task_id, "view_token": view_token},
    )
