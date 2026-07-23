import asyncio
import datetime
import json
import queue
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from ..context import (
    get_audit_logger,
    get_cache_manager,
    get_config,
    get_inflight_registry,
    get_logger,
    get_task_queue,
    get_user_manager,
    lazy_resource,
)
from .audit import check_view_token_ownership
from ..services.transcription import (
    RecalibrateRequest,
    ResummarizeRequest,
    TranscribeRequest,
    TranscribeResponse,
    normalize_processing_options,
    verify_token,
)
from ..services.view_token_resolver import ViewTokenResolver
from ...utils.llm_status import SummaryStatus
from ...utils.notifications import send_view_link_wechat, get_notification_router
from ...utils.task_status import TaskStatus, http_code_for_status

logger = lazy_resource(get_logger)
config = lazy_resource(get_config)
audit_logger = lazy_resource(get_audit_logger)
cache_manager = lazy_resource(get_cache_manager)
# DI 一致性（本地 Codex review）：改用 ..context.get_user_manager（runtime 优先，
# 无 runtime 时退回全局单例），与 verify_token（services/transcription.py）、
# users.py、audit.py 三处对齐；此前这里单独 import 了
# utils.accounts.user_manager 的裸 get_user_manager，永远只读那个全局单例，
# 与 verify_token 的鉴权来源不是同一个 UserManager 实例，reload/换配置场景下
# 会各自持有过期状态。
user_manager = lazy_resource(get_user_manager)

router = APIRouter(prefix="/api", tags=["tasks"])


def _normalize_empty_string(value: str | None) -> str | None:
    """将空字符串规范化为 None"""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


# K1 路由站点观察面（CI review 第 3 轮 major）：_fail_task_after_creation
# 的终态清理写入自身失败时，追加到原始 500/503 响应 detail 末尾的明确
# 标记——终态写失败不再是"只记日志、请求方毫无感知"，而是通过响应体这个
# 请求边界暴露出来。这里只是文案标记，不是新的失败通道：既有的 500/503
# 状态码、既有的 finally 名额释放都不变，只是让调用方（客户端/上游告警）
# 能从这次响应本身分辨出"任务状态清理是否确认成功"。
_TERMINAL_WRITE_FAILURE_NOTE = (
    "；任务状态清理失败，task 可能短暂显示为处理中，系统将自动收敛"
)


def _fail_task_after_creation(task_id: str, error_message: str, *, log_context: str) -> bool:
    """任务行已落库、尚未成功交接给队列消费者的窗口内，把任务 CAS 成 failed 终态。

    背景（本地 Codex review 第 14 轮）：transcribe/recalibrate 两条路径都是
    "先创建任务行（queued/processing），再把任务交给队列消费者"——这中间一旦
    出任何异常（队列满/普通入队异常/入队前的数据准备异常），任务行会卡在
    非终态，客户端拿着 task_id 永久轮询一个不会再被任何 worker 处理的任务。
    QueueFull/queue.Full 分支此前已经补了这层收口，但同一窗口内的其余异常
    （非容量类）没有对齐，运行期对账要等 24h 才兜底。此函数把两条路径三处
    "写 failed 收口"的重复逻辑收拢成一份，行为与此前 QueueFull 分支完全一致：
    - error_message 直接传给 update_task_status，terminal_snapshot 由它内部
      按 status='failed' 自动构建（含 status/error_message 等字段），不需要
      调用方重复拼装快照结构。
    - completed_at 由 update_task_status 在写入 success/failed 时无条件带上，
      不依赖这里额外处理。
    - CAS 语义：写入失败（已处于终态）只记 warning，不是需要向上抛出的错误，
      也不算这里说的"清理失败"——任务本身已经确认落在终态（不论是这次写入
      的，还是被其它路径先一步写入的），客户端不会永久轮询一个卡住的任务。

    调用约定：本函数只在 except 块内部调用，写入本身若抛异常在这里被吞掉
    （只记日志），绝不允许掩盖触发这次清理的原始异常——调用方在本函数返回
    后应继续 raise 原始的 HTTPException。

    返回值（K1，CI review 第 3 轮 major）：调用方应据此判断是否需要在
    HTTPException 的 detail 里追加 _TERMINAL_WRITE_FAILURE_NOTE——终态写入
    这一步本身失败（不是"已处于终态"这种 CAS 语义下的正常情况，而是
    update_task_status 调用本身抛出异常）时，任务行的真实状态无法确认，
    必须通过响应体这个请求边界让调用方知晓，不能只留一条只有服务端能看到
    的日志。

    Args:
        task_id: 已创建的任务行 ID。
        error_message: 写入 task_status.error_message 的失败原因。
        log_context: 清理写入自身失败时的日志前缀，用于区分调用场景。

    Returns:
        bool: True 表示任务已确认落在终态（本次写入成功，或已被其它路径
        先一步写成终态）；False 表示写入本身抛出异常，终态未被确认写入。
    """
    try:
        failed_status_written = cache_manager.update_task_status(
            task_id, TaskStatus.FAILED, error_message=error_message,
        )
        if not failed_status_written:
            logger.warning(
                f"任务状态 CAS 写入 failed 失败(可能已处于终态): {task_id}"
            )
        return True
    except Exception as cleanup_exc:
        logger.exception(f"{log_context}: {task_id}, 错误: {cleanup_exc}")
        return False


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

    effective_processing_options = normalize_processing_options(request_body.processing_options)
    logger.info(
        f"收到转录API请求 - URL: {url}, 说话人识别: {request_body.use_speaker_recognition}, "
        f"自定义企微webhook: {request_body.wechat_webhook is not None}, "
        f"download_url: {normalized_download_url}, metadata_override: {normalized_metadata_override}, "
        f"processing_options: {effective_processing_options}"
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
    preparsed_url = None
    url_parse_attempted = True
    try:
        from ...utils.url_parser import URLParser
        preparsed_url = URLParser().parse(url)
        parsed_platform = preparsed_url.platform
        parsed_media_id = preparsed_url.video_id
        logger.info(f"URL预解析成功: platform={parsed_platform}, media_id={parsed_media_id}")
    except Exception as e:
        logger.warning(f"URL预解析失败，降级到精确URL匹配: {e}")

    # 准入即登记（本地 codex review 第 12 轮 P1）：task_id 在落库/入队之前
    # 先生成，try_register 占满时直接 503——此时任务行尚未创建，"满载拒绝"
    # 根本不落库，比"先落库再 CAS 成 failed"更干净。容量语义见
    # RuntimeContext._InflightTaskRegistry（transcription 桶，容量取
    # concurrent.queue_size）。
    task_id = cache_manager.generate_task_id()
    inflight_registry = get_inflight_registry()
    if not inflight_registry.try_register("transcription", task_id):
        logger.warning("在途转录任务已达受理上限，拒绝任务: %s", url)
        raise HTTPException(status_code=503, detail="任务处理已达上限，请稍后重试")

    # 登记表配额从这里开始"归属"这次 HTTP 请求，直到下面 put_nowait 成功
    # 把任务交给消费者（之后由 track_future 的完成回调注销，见
    # process_task_queue::run_and_finalize 提交处 runtime.track_future(
    # future, task_id=task_id) 的调用）——中途任何异常都必须在最下面的
    # finally 里释放，否则这个名额永久占用、无法被后续请求复用。
    registration_owned = True
    try:
        try:
            task_info = cache_manager.create_task(
                task_id=task_id,
                url=url,
                use_speaker_recognition=request_body.use_speaker_recognition,
                download_url=normalized_download_url,
                platform=parsed_platform,
                media_id=parsed_media_id,
                processing_options=effective_processing_options,
                submitted_by=user_id,
            )
            view_token = task_info["view_token"]
            # 状态以 DB 为唯一真相源；create_task 已写入 status='queued'

            try:
                # Build per-channel webhooks dict from user-level config only.
                # Global config webhooks are already embedded in user_info for
                # legacy single-token mode (see user_manager.validate_token),
                # so no additional fallback is needed here.
                notification_webhooks = {}
                wechat_wh = user_info.get("wechat_webhook")
                if wechat_wh:
                    notification_webhooks["wechat"] = wechat_wh
                feishu_wh = user_info.get("feishu_webhook")
                if feishu_wh:
                    notification_webhooks["feishu"] = feishu_wh

                # Per-request overrides (top-level fields > notification_config)
                effective_channel = None
                if request_body.wechat_webhook:
                    notification_webhooks["wechat"] = request_body.wechat_webhook
                if request_body.feishu_webhook:
                    notification_webhooks["feishu"] = request_body.feishu_webhook

                notification_config = getattr(request_body, "notification_config", None)
                if notification_config and notification_config.webhook:
                    effective_channel = notification_config.channel
                    if effective_channel:
                        notification_webhooks[effective_channel] = notification_config.webhook

                task_queue = get_task_queue()
                task = {
                    "id": task_id,
                    "url": url,
                    # API 已经完成一次 URL 解析；worker 必须复用这份事实，避免
                    # b23 等短链在后台再次跳转。原始 url 仍保留给通知和展示。
                    "preparsed_url": preparsed_url,
                    "url_parse_attempted": url_parse_attempted,
                    "use_speaker_recognition": request_body.use_speaker_recognition,
                    "wechat_webhook": notification_webhooks.get("wechat"),
                    "notification_webhooks": notification_webhooks,
                    "notification_channel": effective_channel,
                    "user_info": user_info,
                    "download_url": normalized_download_url,
                    "metadata_override": normalized_metadata_override,
                    "processing_options": effective_processing_options,
                }

                try:
                    # put_nowait（而非 await put）：asyncio.Queue.put 队列满时会无限
                    # 挂起等待空位，下面的 except asyncio.QueueFull 此前永远不可能
                    # 触发——是死代码（M2a，本地 codex review 第 10 轮）。put_nowait
                    # 立即返回，满了就抛 QueueFull，才能真正落到下面的 503 分支。
                    # 正常情况下这个分支现在应当极少触达——上面的 try_register
                    # 已经把准入容量提前到"受理位"，队列自身的 maxsize 只是
                    # 保留的第二道防线（本地 codex review 第 12 轮 P1）。
                    task_queue.put_nowait(task)
                    registration_owned = False
                    logger.info(f"任务已加入队列: {task_id}, URL: {url}")
                except asyncio.QueueFull:
                    logger.warning("任务队列已满，拒绝任务: %s, task_id=%s", url, task_id)
                    # 任务行已经在上面 create_task() 里落库为 queued——队列拒绝后
                    # 不把它 CAS 成 failed 的话，客户端会永久轮询一个永远不会被
                    # 消费的任务（M2c）。error_message/CAS 写法与 worker 侧转录
                    # 异常时的既有失败模式一致（见
                    # transcription.py::transcribe_video 末尾的 except 分支）：
                    # update_task_status 是 compare-and-set，返回 False 只说明
                    # 任务已先一步进入终态，不是需要向上抛出的错误。清理写入自身
                    # 出错不能掩盖"队列已满"这个真正原因——_fail_task_after_creation
                    # 内部已吞掉自身异常，这里继续走到下面的 503；K1（CI review
                    # 第 3 轮 major）：清理写入若失败，返回值为 False，追加
                    # _TERMINAL_WRITE_FAILURE_NOTE 到 detail，把这个不确定性
                    # 通过响应体这个请求边界暴露给调用方。
                    terminal_write_ok = _fail_task_after_creation(
                        task_id, "任务队列已满，提交被拒绝",
                        log_context="队列已满后写入 failed 终态失败",
                    )
                    detail = "任务队列已满，请稍后重试"
                    if not terminal_write_ok:
                        detail += _TERMINAL_WRITE_FAILURE_NOTE
                    raise HTTPException(status_code=503, detail=detail)

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
                        elif "podcasts.apple.com" in display_url:
                            title = "Apple播客转录"
                        elif "xiaohongshu.com" in display_url or "xhslink.com" in display_url:
                            title = "小红书内容转录"
                        elif "douyin.com" in display_url:
                            title = "抖音视频转录"

                    notification_router = get_notification_router()
                    notification_router.send_view_link(
                        title=f"🎬 {title}",
                        view_token=view_token,
                        channel_name=effective_channel,
                        webhooks=notification_webhooks,
                        original_url=display_url,
                    )
                    logger.info(f"已发送任务创建通知: {task_id}，使用URL: {display_url}")
                except Exception as exc:
                    logger.exception("发送任务创建通知失败: %s, 错误: %s", task_id, exc)
            except HTTPException:
                # 上面 QueueFull 分支显式抛出的 503 是最终答案，不能被下面
                # 这条兜底的 except Exception 重新包装成 500（HTTPException 本身
                # 也是 Exception 的子类，不加这条会被无条件吞掉——M2a 的一部分）。
                raise
            except Exception as queue_exc:
                logger.exception("任务加入队列失败: %s, 错误: %s", task_id, queue_exc)
                # 非 QueueFull 的普通入队异常（本地 Codex review 第 14 轮）：任务行
                # 已经落库为 queued，与上面 QueueFull 分支同样必须收口成 failed，
                # 否则客户端会永久轮询一个永远不会被消费的任务。终态写入自身出错
                # 不能掩盖这里真正的原因，仍然继续走到下面的 500（不吞掉原始异常）；
                # K1（CI review 第 3 轮 major）：清理写入若失败，追加
                # _TERMINAL_WRITE_FAILURE_NOTE 到 detail。
                terminal_write_ok = _fail_task_after_creation(
                    task_id, f"任务加入队列失败: {queue_exc}",
                    log_context="任务加入队列失败后写入 failed 终态失败",
                )
                detail = f"任务加入队列失败: {queue_exc}"
                if not terminal_write_ok:
                    detail += _TERMINAL_WRITE_FAILURE_NOTE
                raise HTTPException(status_code=500, detail=detail)

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
                wechat_webhook=notification_webhooks.get("wechat"),
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
    finally:
        if registration_owned:
            inflight_registry.release("transcription", task_id)


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
            logger.warning("任务不存在: %s", task_id)
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

        status = task_info.get("status") or TaskStatus.QUEUED
        code = http_code_for_status(status)

        # 干净的元信息形状 + 显式 status；正文走 view_token 获取
        data = {
            "status": status,
            "view_token": task_info.get("view_token"),
            "title": task_info.get("title"),
            "author": task_info.get("author"),
            "platform": task_info.get("platform"),
            "completed_at": task_info.get("completed_at"),
        }
        if status == TaskStatus.FAILED:
            data["error"] = task_info.get("error_message") or "任务处理失败"

        message_map = {
            TaskStatus.QUEUED: "任务排队中",
            TaskStatus.PROCESSING: "任务处理中",
            TaskStatus.CALIBRATING: "转录完成，校对/总结生成中",
            TaskStatus.SUCCESS: "任务已完成",
            TaskStatus.FAILED: "任务处理失败",
        }
        message = message_map.get(status, "获取任务状态成功")

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

    重新执行校对步骤（跳过下载、转录），需要 recalibrate 权限。
    若原任务缓存里 llm_summary.txt 缺失或为空，会自动补跑总结，
    避免老任务停留在"总结处理中..."状态；其他情况仍保留原总结文件。
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

    # 权限检查：user_manager 走 runtime 优先的 DI 来源，与 verify_token 鉴权
    # 用的是同一个 UserManager 实例（见上方 import 处注释）。
    if not user_manager.check_permission(user_info, "recalibrate"):
        logger.warning(f"用户 {user_id} 无 recalibrate 权限")
        raise HTTPException(status_code=403, detail="无重新校对权限")

    # 通过 view_token 获取缓存数据
    cache_data = ViewTokenResolver(cache_manager).get_cache_by_view_token(view_token)
    if not cache_data:
        logger.warning(f"view_token 对应的缓存不存在: {view_token}")
        raise HTTPException(status_code=404, detail="未找到对应的转录数据")

    # 验证有转录数据
    transcript_data = cache_data.get("transcript_data")
    if not transcript_data:
        logger.warning(f"缓存中无转录数据: {view_token}")
        raise HTTPException(status_code=400, detail="缓存中没有转录数据，无法重新校对")

    task_info = cache_data.get("task_info", {})

    # 归属校验（本地 codex review 第 8 轮 K1）：recalibrate 会创建新任务、
    # 覆盖共享媒体的校对/说话人产物并消耗 LLM 配额，不能像 GET
    # /view/{token} 那样对公开分享的 view_token 一律放行——只有 view_token
    # 关联任务的权威提交者才能触发。复用 audit.py::check_view_token_ownership
    # 与 /api/audit/summary 完全同一套判定逻辑（含 legacy 存量任务的审计行
    # 兜底），fail-closed：判定过程本身抛异常时拒绝而不是放行。
    #
    # to_thread 包装（本地 codex review 第 11 轮 N2）：check_view_token_
    # ownership 内部做多次同步 SQLite 查询（默认 busy timeout ~5s），在
    # async 路由里直接同步调用会整段阻塞事件循环，其它请求、健康检查全部
    # 随之停摆——与 M2b（llm_queue.put_nowait 替代阻塞 put）是同一类问题。
    # 与 audit.py::get_task_summary 对同一个函数的调用方式完全一致（同一套
    # 判定逻辑理应有同一套调度方式，不能一个在线程池跑、一个在事件循环里
    # 裸跑）。
    original_task_id = task_info.get("task_id")
    if original_task_id:
        try:
            owned = await asyncio.to_thread(
                check_view_token_ownership,
                view_token, original_task_id, user_id, cache_manager, audit_logger,
            )
        except Exception as e:
            logger.error(f"recalibrate 归属校验暂时不可用(fail-closed 拒绝): {e}")
            raise HTTPException(status_code=503, detail="归属校验暂时不可用，请稍后重试")
        if not owned:
            logger.warning(
                f"用户 {user_id} 无权对 view_token={view_token} 发起 recalibrate"
                f"（非该任务的权威提交者）"
            )
            raise HTTPException(status_code=403, detail="无权对该任务发起重新校对")

    platform = cache_data.get("platform")
    media_id = cache_data.get("media_id")
    use_speaker_recognition = cache_data.get("use_speaker_recognition", False)
    video_title = cache_data.get("title", "")
    author = cache_data.get("author", "")
    description = cache_data.get("description", "")
    cache_file_path = cache_data.get("file_path")

    # recalibrate 没有对应的请求级 processing_options 字段（RecalibrateRequest
    # 只有 view_token/wechat_webhook）：管线语义固定为"校对必跑"，总结层是否
    # 真正重新生成则由下方 llm_ops._handle_llm_task 里的 _should_backfill_summary
    # 视缓存现状决定（summary_backfill），并非由这里的开关控制。normalize_processing_options(None)
    # 就是 llm_ops.py 对同一个（不携带 processing_options 键的）llm_task 会独立算出的
    # 同一份归一化默认值——取同一个函数结果落库，保证这里持久化的行与任务完成后
    # 写入 terminal_snapshot 的 processing_options 语义一致，不会各说各话。
    recalibrate_processing_options = normalize_processing_options(None)

    # 创建新任务（复用原 view_token）。准入即登记（本地 codex review 第
    # 12 轮 P1）：task_id 提前到 INSERT 之前生成，try_register 占满时直接
    # 503——INSERT 根本不会发生，"满载拒绝"不落库。容量语义见
    # RuntimeContext._InflightTaskRegistry（llm 桶，容量取
    # LLM_QUEUE_MAXSIZE，与下方 llm_queue.put_nowait 共用同一个容量数字
    # 来源）。
    from ..context import get_inflight_registry, get_llm_queue

    task_id = cache_manager.generate_task_id()
    inflight_registry = get_inflight_registry()
    if not inflight_registry.try_register("llm", task_id):
        logger.warning("在途 LLM 任务已达受理上限，拒绝重新校对任务: %s", view_token)
        raise HTTPException(status_code=503, detail="任务处理已达上限，请稍后重试")

    # 登记表配额从这里开始"归属"这次 HTTP 请求，直到下面 llm_queue.
    # put_nowait 成功把任务交给消费者（之后由 track_future 的完成回调
    # 注销，见 llm_ops.process_llm_queue 提交处 runtime.track_future(
    # future, kind="llm", task_id=...) 的调用）——中途任何异常都必须在
    # 最下面的 finally 里释放，否则这个名额永久占用、无法被后续请求复用。
    registration_owned = True
    try:
        try:
            with cache_manager._get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO task_status
                    (task_id, view_token, url, platform, media_id,
                     use_speaker_recognition, status, title, author,
                     processing_options, submitted_by)
                    VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?, ?, ?)
                ''', (
                    task_id, view_token, task_info.get("url", ""),
                    platform, media_id, use_speaker_recognition,
                    video_title, author,
                    json.dumps(recalibrate_processing_options, sort_keys=True),
                    user_id,
                ))
            logger.info(f"重新校对任务创建成功: {task_id}, view_token: {view_token}")
        except Exception as e:
            logger.error(f"创建重新校对任务失败: {e}")
            raise HTTPException(status_code=500, detail=f"创建重新校对任务失败: {e}")

        # 状态以 DB 为唯一真相源；上方 INSERT 已写入 status='processing'

        # 准备转录内容（与 _handle_llm_task 的输入格式一致）
        #
        # try/except 收口（本地 Codex review 第 14 轮）：此前这段没有任何异常
        # 处理——transcript_data 是合法 JSON 但形状损坏时（如 dialog 缺字段），
        # format_transcript_with_speakers 内部的 .get() 链会直接抛异常，未被
        # 捕获则一路冒泡出整个路由函数，成为 FastAPI 默认的 500，而上面
        # INSERT 已经落库为 processing 的任务行永远没有机会被 CAS 成
        # failed——与 QueueFull/普通队列异常同一类"落库成功、交接失败前"缺口，
        # 必须同款收口。
        try:
            transcript_text = ""
            transcription_data_for_llm = None
            if cache_data.get("transcript_type") == "funasr":
                transcription_data_for_llm = transcript_data
                from ...transcriber import FunASRSpeakerClient
                funasr_client = FunASRSpeakerClient()
                transcript_text = funasr_client.format_transcript_with_speakers(transcript_data)
            else:
                transcript_text = transcript_data
        except Exception as format_exc:
            logger.exception(
                f"重新校对任务转录数据格式化失败: {task_id}, 错误: {format_exc}"
            )
            # K1（CI review 第 3 轮 major）：清理写入若失败，追加
            # _TERMINAL_WRITE_FAILURE_NOTE 到 detail。
            terminal_write_ok = _fail_task_after_creation(
                task_id, f"转录数据格式化失败: {format_exc}",
                log_context="转录数据格式化失败后写入 failed 终态失败",
            )
            detail = f"重新校对任务转录数据格式化失败: {format_exc}"
            if not terminal_write_ok:
                detail += _TERMINAL_WRITE_FAILURE_NOTE
            raise HTTPException(status_code=500, detail=detail)

        # Build per-channel webhooks
        recal_webhooks = {}
        wechat_wh = (
            request_body.wechat_webhook
            or user_info.get("wechat_webhook")
            or config.get("wechat", {}).get("webhook")
        )
        if wechat_wh:
            recal_webhooks["wechat"] = wechat_wh
        feishu_wh = (
            user_info.get("feishu_webhook")
            or config.get("feishu", {}).get("webhook")
        )
        if feishu_wh:
            recal_webhooks["feishu"] = feishu_wh

        # 放入 LLM 队列
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
            "wechat_webhook": recal_webhooks.get("wechat"),
            "notification_webhooks": recal_webhooks,
            "calibrate_only": True,
            # 显式传递而非依赖 llm_ops.py 对缺失键的隐式默认——与上面落库到
            # task_status.processing_options 的值同源，杜绝两处各自计算默认值
            # 未来悄悄漂移的风险。
            "processing_options": recalibrate_processing_options,
        }

        try:
            # put_nowait（而非阻塞的 put）：llm_queue 是同步 queue.Queue，在 async
            # 路由里裸调用会阻塞（在没有可用空位前完全占住）整个事件循环——其它
            # 请求、健康检查、优雅关闭全部随之停摆（M2b，本地 codex review 第
            # 10 轮）。put_nowait 立即返回，满了就抛 queue.Full，转成下面的 503，
            # 比在事件循环里 await run_in_executor 带超时更干净。正常情况下这个
            # 分支现在应当极少触达——上面的 try_register 已经把准入容量提前到
            # "受理位"，队列自身的 maxsize 只是保留的第二道防线（本地 codex
            # review 第 12 轮 P1）。
            llm_queue.put_nowait(llm_task)
            registration_owned = False
            logger.info(f"重新校对任务已加入 LLM 队列: {task_id}")
        except queue.Full:
            logger.warning("LLM 队列已满，拒绝重新校对任务: %s", task_id)
            # 上面的 INSERT 已经把这个新任务行落库为 processing——队列拒绝后不
            # CAS 成 failed 的话，客户端会永久轮询一个永远不会被消费的任务
            # (M2c)。写法与 transcribe 路径的等价分支一致：update_task_status
            # 是 compare-and-set，返回 False 只说明任务已先一步进入终态。清理
            # 写入自身出错不能掩盖"队列已满"这个真正原因——
            # _fail_task_after_creation 内部已吞掉自身异常，这里继续走到下面
            # 的 503；K1（CI review 第 3 轮 major）：清理写入若失败，追加
            # _TERMINAL_WRITE_FAILURE_NOTE 到 detail。
            terminal_write_ok = _fail_task_after_creation(
                task_id, "LLM 队列已满，重新校对提交被拒绝",
                log_context="LLM 队列已满后写入 failed 终态失败",
            )
            detail = "LLM 队列已满，请稍后重试"
            if not terminal_write_ok:
                detail += _TERMINAL_WRITE_FAILURE_NOTE
            raise HTTPException(status_code=503, detail=detail)
        except Exception as e:
            logger.error(f"重新校对任务加入队列失败: {e}")
            # 非 queue.Full 的普通入队异常（本地 Codex review 第 14 轮）：任务行
            # 已经落库为 processing，与上面 queue.Full 分支同样必须收口成
            # failed，否则客户端会永久轮询一个永远不会被消费的任务。终态写入
            # 自身出错不能掩盖这里真正的原因，仍然继续走到下面的 500；K1（CI
            # review 第 3 轮 major）：清理写入若失败，追加
            # _TERMINAL_WRITE_FAILURE_NOTE 到 detail。
            terminal_write_ok = _fail_task_after_creation(
                task_id, f"任务加入队列失败: {e}",
                log_context="任务加入队列失败后写入 failed 终态失败",
            )
            detail = f"任务加入队列失败: {e}"
            if not terminal_write_ok:
                detail += _TERMINAL_WRITE_FAILURE_NOTE
            raise HTTPException(status_code=500, detail=detail)

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
            wechat_webhook=recal_webhooks.get("wechat"),
        )

        return TranscribeResponse(
            code=202,
            message="重新校对任务已提交",
            data={"task_id": task_id, "view_token": view_token},
        )
    finally:
        if registration_owned:
            inflight_registry.release("llm", task_id)


@router.post("/resummarize", response_model=TranscribeResponse)
async def resummarize(
    request_body: ResummarizeRequest,
    request: Request,
    user_info: dict = Depends(verify_token),
):
    """重新生成总结接口

    只重跑总结层（跳过下载、转录、校对和章节），复用缓存里已有的
    llm_calibrated 校对文本作为总结输入。权限复用 "recalibrate"——
    两者的爆炸半径同级（消耗 LLM 配额、写共享产物），不新增权限名，
    避免 users.json 迁移。llm_task 构造镜像 transcription.py 分层缓存
    部分命中分支的 else 路径（calibrate=False + transcription_data=None +
    cached_speaker_count 回传），校对层文件/状态均不动，只写总结层。
    """
    view_token = request_body.view_token
    user_id = user_info.get("user_id")
    api_key = user_info.get("api_key")

    start_time = datetime.datetime.now()

    audit_logger.log_api_call(
        api_key=api_key,
        user_id=user_id,
        endpoint="/api/resummarize",
        user_agent=request.headers.get("User-Agent"),
        remote_ip=request.client.host if request.client else None,
    )

    # 权限检查：与 recalibrate 共用同一个权限名（见 docstring），user_manager
    # 同样走 runtime 优先的 DI 来源，与 verify_token 鉴权用同一个实例。
    if not user_manager.check_permission(user_info, "recalibrate"):
        logger.warning(f"用户 {user_id} 无 recalibrate 权限")
        raise HTTPException(status_code=403, detail="无重新生成总结权限")

    # 通过 view_token 获取缓存数据
    cache_data = ViewTokenResolver(cache_manager).get_cache_by_view_token(view_token)
    if not cache_data:
        logger.warning(f"view_token 对应的缓存不存在: {view_token}")
        raise HTTPException(status_code=404, detail="未找到对应的转录数据")

    # 验证有转录数据（llm_calibrated 取不到时要回退到原始转录）
    transcript_data = cache_data.get("transcript_data")
    if not transcript_data:
        logger.warning(f"缓存中无转录数据: {view_token}")
        raise HTTPException(status_code=400, detail="缓存中没有转录数据，无法重新生成总结")

    task_info = cache_data.get("task_info", {})

    # 归属校验：与 recalibrate 完全同一套判定（check_view_token_ownership +
    # asyncio.to_thread，fail-closed）——resummarize 同样会创建新任务、
    # 消耗 LLM 配额并覆盖共享媒体的总结产物，不能对公开分享的 view_token
    # 一律放行。
    original_task_id = task_info.get("task_id")
    if original_task_id:
        try:
            owned = await asyncio.to_thread(
                check_view_token_ownership,
                view_token, original_task_id, user_id, cache_manager, audit_logger,
            )
        except Exception as e:
            logger.error(f"resummarize 归属校验暂时不可用(fail-closed 拒绝): {e}")
            raise HTTPException(status_code=503, detail="归属校验暂时不可用，请稍后重试")
        if not owned:
            logger.warning(
                f"用户 {user_id} 无权对 view_token={view_token} 发起 resummarize"
                f"（非该任务的权威提交者）"
            )
            raise HTTPException(status_code=403, detail="无权对该任务发起重新生成总结")

    # 前置校验（在归属校验之后，避免向非任务归属方泄露总结层状态）：总结
    # 已真实生成过（文件非空且状态为 generated）时直接 400，防止误点重复
    # 消耗 LLM 配额；failed/pending/缺失等情况放行，这正是本接口要补的场景。
    existing_summary = (cache_data.get("llm_summary") or "").strip()
    existing_summary_status = (cache_data.get("llm_status") or {}).get("summary_status")
    if existing_summary and existing_summary_status == SummaryStatus.GENERATED:
        logger.warning(f"总结已存在，拒绝重复生成: {view_token}")
        raise HTTPException(status_code=400, detail="总结已存在，无需重新生成")

    platform = cache_data.get("platform")
    media_id = cache_data.get("media_id")
    use_speaker_recognition = cache_data.get("use_speaker_recognition", False)
    video_title = cache_data.get("title", "")
    author = cache_data.get("author", "")
    description = cache_data.get("description", "")
    cache_file_path = cache_data.get("file_path")

    # resummarize 与 RecalibrateRequest 一样没有请求级 processing_options
    # 字段：管线语义固定为"只跑总结层"——校对层不动（calibrate=False，供
    # llm_ops._save_llm_results 的 suppress_calibration 保护已有校对产物）、
    # 章节不动（chapters=False，llm_status 合并语义保留旧值）、不做二次说话
    # 人姓名推断（infer_speaker_names=False）。INSERT 落库的值与下面交给
    # llm_ops 的 llm_task 里的值同源，保证创建行与终态快照语义一致。
    resummarize_processing_options = {
        "calibrate": False,
        "summarize": True,
        "infer_speaker_names": False,
        "chapters": False,
    }

    # 创建新任务（复用原 view_token）。准入即登记：与 recalibrate 相同的
    # inflight registry 容量语义（llm 桶，容量取 LLM_QUEUE_MAXSIZE），
    # 满载拒绝不落库。
    from ..context import get_inflight_registry, get_llm_queue

    task_id = cache_manager.generate_task_id()
    inflight_registry = get_inflight_registry()
    if not inflight_registry.try_register("llm", task_id):
        logger.warning("在途 LLM 任务已达受理上限，拒绝重新生成总结任务: %s", view_token)
        raise HTTPException(status_code=503, detail="任务处理已达上限，请稍后重试")

    # 登记表配额从这里开始"归属"这次 HTTP 请求，直到下面 llm_queue.
    # put_nowait 成功把任务交给消费者——中途任何异常都必须在最下面的
    # finally 里释放，否则名额永久占用。
    registration_owned = True
    try:
        try:
            with cache_manager._get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO task_status
                    (task_id, view_token, url, platform, media_id,
                     use_speaker_recognition, status, title, author,
                     processing_options, submitted_by)
                    VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?, ?, ?)
                ''', (
                    task_id, view_token, task_info.get("url", ""),
                    platform, media_id, use_speaker_recognition,
                    video_title, author,
                    json.dumps(resummarize_processing_options, sort_keys=True),
                    user_id,
                ))
            logger.info(f"重新生成总结任务创建成功: {task_id}, view_token: {view_token}")
        except Exception as e:
            logger.error(f"创建重新生成总结任务失败: {e}")
            raise HTTPException(status_code=500, detail=f"创建重新生成总结任务失败: {e}")

        # 状态以 DB 为唯一真相源；上方 INSERT 已写入 status='processing'

        # 说话人识别任务：读缓存里已落盘的结构化产物（llm_processed.json）
        # 把真实说话人数回传给协调器，供总结环节选择正确的多/单说话人
        # Prompt——下面强制走纯文本路径（transcription_data=None），协调器
        # 的自动推断必然判成单说话人，必须用这个缓存值覆盖（与
        # transcription.py 分层缓存 else 分支同款逻辑）。
        cached_speaker_count = None
        if use_speaker_recognition:
            cached_structured = cache_data.get("llm_processed") or {}
            cached_speaker_mapping = cached_structured.get("speaker_mapping")
            if cached_speaker_mapping:
                cached_speaker_count = len(cached_speaker_mapping)

        # 总结输入优先取已有校对文本（质量更高）；取不到时回退原始转录
        # （覆盖"原任务未启用校对"的场景），funasr 格式化处理与 recalibrate
        # 同款，且同样必须在"落库成功、交接成功前"的窗口内 try/except 收口
        # 成 failed 终态，否则客户端会永久轮询一个不会被消费的任务。
        try:
            transcript_text = cache_data.get("llm_calibrated")
            if not transcript_text:
                if cache_data.get("transcript_type") == "funasr":
                    from ...transcriber import FunASRSpeakerClient
                    funasr_client = FunASRSpeakerClient()
                    transcript_text = funasr_client.format_transcript_with_speakers(
                        transcript_data
                    )
                else:
                    transcript_text = transcript_data
        except Exception as format_exc:
            logger.exception(
                f"重新生成总结任务转录数据格式化失败: {task_id}, 错误: {format_exc}"
            )
            terminal_write_ok = _fail_task_after_creation(
                task_id, f"转录数据格式化失败: {format_exc}",
                log_context="转录数据格式化失败后写入 failed 终态失败",
            )
            detail = f"重新生成总结任务转录数据格式化失败: {format_exc}"
            if not terminal_write_ok:
                detail += _TERMINAL_WRITE_FAILURE_NOTE
            raise HTTPException(status_code=500, detail=detail)

        # Build per-channel webhooks
        resum_webhooks = {}
        wechat_wh = (
            request_body.wechat_webhook
            or user_info.get("wechat_webhook")
            or config.get("wechat", {}).get("webhook")
        )
        if wechat_wh:
            resum_webhooks["wechat"] = wechat_wh
        feishu_wh = (
            user_info.get("feishu_webhook")
            or config.get("feishu", {}).get("webhook")
        )
        if feishu_wh:
            resum_webhooks["feishu"] = feishu_wh

        # 放入 LLM 队列
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
            # 强制纯文本路由（transcription_data=None），避免二次说话人推断
            # 浪费 LLM 调用；真实说话人数由上面的 cached_speaker_count 回传。
            "transcription_data": None,
            "cached_speaker_count": cached_speaker_count,
            "is_generic": False,
            "wechat_webhook": resum_webhooks.get("wechat"),
            "notification_webhooks": resum_webhooks,
            # 不传 calibrate_only：它不是"重新校对"语义，避免走 llm_ops 的
            # "保留总结"分支；层选择完全由 processing_options 表达。
            "processing_options": resummarize_processing_options,
        }

        try:
            # put_nowait 而非阻塞 put：与 recalibrate 同款收口——同步
            # queue.Queue 在 async 路由里裸 put 会阻塞整个事件循环；满了
            # 抛 queue.Full 转成下面的 503，同时把已落库为 processing 的
            # 任务行 CAS 成 failed。
            llm_queue.put_nowait(llm_task)
            registration_owned = False
            logger.info(f"重新生成总结任务已加入 LLM 队列: {task_id}")
        except queue.Full:
            logger.warning("LLM 队列已满，拒绝重新生成总结任务: %s", task_id)
            terminal_write_ok = _fail_task_after_creation(
                task_id, "LLM 队列已满，重新生成总结提交被拒绝",
                log_context="LLM 队列已满后写入 failed 终态失败",
            )
            detail = "LLM 队列已满，请稍后重试"
            if not terminal_write_ok:
                detail += _TERMINAL_WRITE_FAILURE_NOTE
            raise HTTPException(status_code=503, detail=detail)
        except Exception as e:
            logger.error(f"重新生成总结任务加入队列失败: {e}")
            terminal_write_ok = _fail_task_after_creation(
                task_id, f"任务加入队列失败: {e}",
                log_context="任务加入队列失败后写入 failed 终态失败",
            )
            detail = f"任务加入队列失败: {e}"
            if not terminal_write_ok:
                detail += _TERMINAL_WRITE_FAILURE_NOTE
            raise HTTPException(status_code=500, detail=detail)

        processing_time_ms = int(
            (datetime.datetime.now() - start_time).total_seconds() * 1000
        )
        audit_logger.log_api_call(
            api_key=api_key,
            user_id=user_id,
            endpoint="/api/resummarize",
            processing_time_ms=processing_time_ms,
            status_code=202,
            task_id=task_id,
            user_agent=request.headers.get("User-Agent"),
            remote_ip=request.client.host if request.client else None,
            wechat_webhook=resum_webhooks.get("wechat"),
        )

        return TranscribeResponse(
            code=202,
            message="重新生成总结任务已提交",
            data={"task_id": task_id, "view_token": view_token},
        )
    finally:
        if registration_owned:
            inflight_registry.release("llm", task_id)
