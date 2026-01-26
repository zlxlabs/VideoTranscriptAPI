import asyncio
import datetime
import os
import threading
import time
from typing import Optional, Dict, Any

from fastapi import HTTPException, Header, Request
from pydantic import BaseModel, Field

from ..context import (
    get_audit_logger,
    get_cache_manager,
    get_config,
    get_enhanced_llm_processor,
    get_executor,
    get_llm_executor,
    get_llm_queue,
    get_logger,
    get_task_queue,
    get_task_results,
    get_temp_manager,
    get_user_manager,
    task_lock,
)
from ...downloaders import create_downloader
from ...transcriber import FunASRSpeakerClient, Transcriber
from ...utils.llm import call_llm_api
from ...utils.notifications import (
    WechatNotifier,
    send_long_text_wechat,
    format_llm_config_markdown,
)
from ...utils.rendering import get_base_url

logger = get_logger()
config = get_config()
user_manager = get_user_manager()
audit_logger = get_audit_logger()
cache_manager = get_cache_manager()
enhanced_llm_processor = get_enhanced_llm_processor()
task_results = get_task_results()
task_queue = get_task_queue()
llm_task_queue = get_llm_queue()
llm_executor = get_llm_executor()
executor = get_executor()


class MetadataOverride(BaseModel):
    """元数据覆盖模型"""
    title: Optional[str] = Field(None, description="视频标题")
    description: Optional[str] = Field(None, description="视频描述")
    author: Optional[str] = Field(None, description="视频作者")


class TranscribeRequest(BaseModel):
    """转录请求数据模型"""

    url: str = Field(..., description="视频URL（实际下载地址）")
    use_speaker_recognition: bool = Field(False, description="是否使用说话人识别功能")
    wechat_webhook: Optional[str] = Field(
        None, description="企业微信webhook地址，用于发送通知"
    )
    source_url: Optional[str] = Field(
        None, description="原始视频URL（用于解析平台和元数据）"
    )
    metadata_override: Optional[MetadataOverride] = Field(
        None, description="元数据覆盖（用于补充或覆盖解析的元数据）"
    )


class TranscribeResponse(BaseModel):
    """转录响应数据模型"""

    code: int = Field(200, description="状态码")
    message: str = Field("success", description="状态信息")
    data: Optional[Dict[str, Any]] = Field(None, description="响应数据")


def extract_filename_from_url(url: str) -> str:
    """
    从URL中提取文件名

    参数:
        url: URL地址

    返回:
        str: 提取的文件名，如果无法提取则返回空字符串
    """
    try:
        from urllib.parse import urlparse, unquote
        parsed_url = urlparse(url)
        path = unquote(parsed_url.path)
        filename = os.path.basename(path)
        # 移除扩展名
        if filename:
            return os.path.splitext(filename)[0]
        return ""
    except Exception:
        return ""


def generate_media_id_from_url(url: str) -> str:
    """
    从URL生成唯一的media_id

    参数:
        url: URL地址

    返回:
        str: 16位哈希字符串
    """
    import hashlib
    return hashlib.md5(url.encode()).hexdigest()[:16]


def merge_metadata(parsed_metadata: Optional[dict], metadata_override: Optional[dict], url: str) -> dict:
    """
    合并解析的元数据和用户提供的元数据覆盖

    参数:
        parsed_metadata: 从source_url解析的元数据（可能为None）
        metadata_override: 用户提供的元数据覆盖（可能为None）
        url: 实际下载URL（用于生成默认值）

    返回:
        dict: 合并后的完整元数据
    """
    # 步骤1：元数据合并
    if parsed_metadata is not None:
        # 解析成功：metadata_override 作为补充
        # 注意：过滤掉 metadata_override 中的 None 值，避免覆盖解析出的有效值
        filtered_override = {k: v for k, v in (metadata_override or {}).items() if v is not None}
        final_metadata = {**parsed_metadata, **filtered_override}
        logger.info("元数据解析成功，使用 metadata_override 作为补充")

        # 字段名标准化：将 video_title 映射为 title（如果存在）
        if 'video_title' in final_metadata and 'title' not in final_metadata:
            final_metadata['title'] = final_metadata['video_title']
            logger.debug("已将 video_title 映射为 title")
    else:
        # 解析失败或未提供：metadata_override 作为覆盖
        final_metadata = metadata_override or {}
        logger.info("元数据解析失败或未提供，使用 metadata_override 作为覆盖")

    # 步骤2：填充默认值（如果仍然缺失或为空）
    # 注意：不能用 setdefault，因为它不会覆盖空字符串或 None
    if not (final_metadata.get('title') or '').strip():
        final_metadata['title'] = extract_filename_from_url(url) or "Untitled"
    final_metadata.setdefault('description', "")
    if not (final_metadata.get('author') or '').strip():
        final_metadata['author'] = "Unknown"
    final_metadata.setdefault('platform', 'generic')
    if not final_metadata.get('video_id'):
        final_metadata['video_id'] = generate_media_id_from_url(url)

    logger.info(
        "最终元数据: platform=%s, video_id=%s, title=%s, author=%s",
        final_metadata['platform'],
        final_metadata['video_id'],
        final_metadata['title'][:50],
        final_metadata['author']
    )

    return final_metadata


async def verify_token(authorization: str = Header(None), request: Request = None):
    """
    验证API令牌（支持多用户）
    """
    if not authorization:
        logger.warning("请求未提供Authorization头")
        raise HTTPException(status_code=401, detail="未提供授权令牌")

    token_parts = authorization.split()
    if len(token_parts) != 2 or token_parts[0].lower() != "bearer":
        logger.warning("授权令牌格式错误")
        raise HTTPException(status_code=401, detail="授权令牌格式错误")

    token = token_parts[1]
    user_info = user_manager.validate_token(token)
    if not user_info:
        logger.warning(f"授权令牌无效: {token[:8]}...")
        raise HTTPException(status_code=401, detail="授权令牌无效")

    logger.debug(f"用户认证成功: {user_info.get('user_id')}")
    if request:
        request.state.user_info = user_info
    return user_info


async def process_task_queue():
    """处理任务队列的后台任务"""
    logger.info("启动任务队列处理器")

    while True:
        try:
            task = await task_queue.get()
            task_id = task["id"]
            url = task["url"]
            use_speaker_recognition = task.get("use_speaker_recognition", False)
            wechat_webhook = task.get("wechat_webhook")
            source_url = task.get("source_url")
            metadata_override = task.get("metadata_override")

            try:
                task_results[task_id] = {
                    "status": "processing",
                    "message": "正在处理转录任务",
                }
                cache_manager.update_task_status(task_id, "processing", source_url=source_url)

                future = executor.submit(
                    process_transcription,
                    task_id,
                    url,
                    use_speaker_recognition,
                    wechat_webhook,
                    source_url,
                    metadata_override,
                )

                def task_completed(future_result):
                    try:
                        result = future_result.result()
                        task_results[task_id] = result
                        logger.info(f"任务完成: {task_id}")
                    except Exception as exc:
                        logger.exception(
                            f"任务处理失败: {task_id}, URL: {url}, 错误: {exc}"
                        )
                        task_results[task_id] = {
                            "status": "failed",
                            "message": f"转录任务失败: {exc}",
                            "error": str(exc),
                        }
                        # 优先使用 source_url 用于通知显示
                        display_url = source_url or url
                        WechatNotifier().notify_task_status(display_url, "转录失败", str(exc))

                future.add_done_callback(task_completed)
                logger.info(f"任务已提交到线程池: {task_id}, URL: {url}")
            except Exception as exc:
                logger.exception(
                    f"提交任务到线程池失败: {task_id}, URL: {url}, 错误: {exc}"
                )
                task_results[task_id] = {
                    "status": "failed",
                    "message": f"提交任务失败: {exc}",
                    "error": str(exc),
                }
            finally:
                task_queue.task_done()
        except Exception as exc:
            logger.exception(f"任务队列处理器异常: {exc}")
            await asyncio.sleep(1)


def process_transcription(
    task_id, url, use_speaker_recognition=False, wechat_webhook=None,
    source_url=None, metadata_override=None
):
    """
    处理视频转录

    参数:
        task_id: 任务ID
        url: 实际下载地址
        use_speaker_recognition: 是否使用说话人识别
        wechat_webhook: 企业微信webhook
        source_url: 原始视频URL（用于解析平台和元数据）
        metadata_override: 元数据覆盖（dict）
    """
    try:
        logger.info(f"开始处理转录任务: {task_id}, URL: {url}, source_url: {source_url}")

        # 优先使用 source_url 用于通知显示（保持原始平台链接的可读性）
        display_url = source_url or url
        logger.info(f"企业微信通知将使用URL: {display_url}")

        task_notifier = (
            WechatNotifier(wechat_webhook) if wechat_webhook else WechatNotifier()
        )
        engine_info = (
            "说话人识别(FunASR)" if use_speaker_recognition else "普通转录(CapsWriter)"
        )
        task_notifier.notify_task_status(display_url, f"开始处理 - {engine_info}")

        # === 新增：元数据解析和合并逻辑 ===
        parsed_metadata = None
        metadata_downloader = None

        if source_url:
            try:
                # 使用 source_url 解析元数据
                metadata_downloader = create_downloader(source_url)
                logger.info(f"使用 source_url 解析元数据: {source_url}, 下载器类型: {metadata_downloader.__class__.__name__}")

                # 只调用 get_video_info 获取元数据，不执行下载
                parsed_metadata = metadata_downloader.get_video_info(source_url)
                logger.info(
                    f"成功从 source_url 解析元数据: platform={parsed_metadata.get('platform')}, "
                    f"media_id={parsed_metadata.get('video_id')}, title={parsed_metadata.get('video_title', '')[:50]}"
                )
            except Exception as e:
                logger.warning(f"解析 source_url 失败: {e}，将使用 metadata_override 作为兜底")
                parsed_metadata = None

        # 合并元数据
        final_metadata = merge_metadata(parsed_metadata, metadata_override, url)

        # 从合并后的元数据中提取字段（兼容 title 和 video_title 两种字段名）
        video_title = final_metadata.get('title') or final_metadata.get('video_title', '')
        author = final_metadata.get('author', '')
        description = final_metadata.get('description', '')
        platform = final_metadata.get('platform', 'generic')
        video_id = final_metadata.get('video_id', generate_media_id_from_url(url))
        media_id = video_id  # media_id 是 video_id 的别名，保持兼容性

        # 实际下载使用 GenericDownloader（统一处理）
        from ...downloaders.generic import GenericDownloader
        downloader = GenericDownloader()

        # 标记是否使用了通用下载器（用于后续逻辑）
        is_generic_downloader = platform == 'generic'
        is_from_generic = is_generic_downloader

        # 检查缓存
        cache_data = None
        if video_id and platform and not is_generic_downloader:
            logger.info(f"从URL中解析出平台: {platform}，视频ID: {video_id}")
            cache_data = cache_manager.get_cache(
                platform=platform,
                media_id=video_id,
                use_speaker_recognition=use_speaker_recognition,
            )

        if cache_data:
            logger.info("找到已存在的缓存记录，跳过下载和转录步骤")
            video_title = cache_data.get("title", "已缓存视频")
            author = cache_data.get("author", "")
            description = cache_data.get("description", "")
            has_speaker_recognition = cache_data.get("use_speaker_recognition", False)

            transcript = ""
            transcription_data = None
            if cache_data["transcript_type"] == "funasr":
                transcription_data = cache_data["transcript_data"]
                funasr_client = FunASRSpeakerClient()
                transcript = funasr_client.format_transcript_with_speakers(
                    transcription_data
                )
                logger.info("使用 FunASR 缓存，包含说话人信息")
            else:
                transcript = cache_data["transcript_data"]
                logger.info("使用 CapsWriter 缓存文本")

            has_llm_calibrated = "llm_calibrated" in cache_data
            has_llm_summary = "llm_summary" in cache_data

            if has_llm_calibrated and has_llm_summary:
                logger.info("缓存中已有 LLM 结果，直接使用")
                cache_type = "含说话人识别" if has_speaker_recognition else "普通转录"
                engine_info = "FunASR" if has_speaker_recognition else "CapsWriter"
                task_notifier.notify_task_status(
                    display_url,
                    f"使用已有缓存({cache_type}-{engine_info}，含LLM结果)",
                    title=video_title,
                    author=author,
                    transcript="使用缓存的校对和总结文本...",
                )

                # 直接发送缓存的 LLM 结果（仅发送总结文本）
                logger.info("缓存模式 - 发送总结文本")

                # 获取查看链接
                task_info = cache_manager.get_task_by_id(task_id)
                view_url = ""
                if task_info and task_info.get("view_token"):
                    base_url = get_base_url()
                    view_url = f"{base_url}/view/{task_info['view_token']}"

                # 计算统计信息
                original_length = len(transcript)
                calibrated_length = len(cache_data.get("llm_calibrated", ""))
                summary_text = cache_data["llm_summary"]
                calibrated_text = cache_data.get("llm_calibrated", "")

                # 判断是否跳过了总结（总结文本和校对文本相同）
                skip_summary = summary_text == calibrated_text

                # 构建完整的消息格式
                speaker_info = "（含说话人识别）" if has_speaker_recognition else ""
                if skip_summary:
                    # 短文本，未生成总结
                    full_message = f"""## 总结和校对
🌐 网页查看：{view_url}
📄 直接获取：{view_url}?raw=calibrated

## 转录统计
原始 {original_length:,} 字 | 校对 {calibrated_length:,} 字 | 总结 未生成

## 校对文本{speaker_info}
{summary_text}"""
                    logger.info("缓存模式 - 发送校对文本（未总结）")
                else:
                    # 长文本，有总结
                    summary_length = len(summary_text)
                    full_message = f"""## 总结和校对
🌐 网页查看：{view_url}
📄 直接获取：{view_url}?raw=calibrated

## 转录统计
原始 {original_length:,} 字 | 校对 {calibrated_length:,} 字 | 总结 {summary_length:,} 字

## 总结{speaker_info}
{summary_text}"""
                    logger.info("缓存模式 - 发送总结文本")

                # 发送（跳过自动添加的内容类型标题）
                send_long_text_wechat(
                    title=video_title,
                    url=display_url,
                    text=full_message,
                    is_summary=not skip_summary,
                    has_speaker_recognition=has_speaker_recognition,
                    webhook=wechat_webhook,
                    skip_content_type_header=True,
                )

                # 确保总结文本完全加入队列后再发送完成通知
                logger.info("[缓存模式] 总结文本发送完成，延迟100ms后发送完成通知")
                time.sleep(0.1)

                # 发送任务完成通知，包含查看链接
                task_info = cache_manager.get_task_by_id(task_id)
                if task_info and task_info.get("view_token"):
                    base_url = get_base_url()
                    view_url = f"{base_url}/view/{task_info['view_token']}"

                    # 使用限流系统发送完成通知，确保顺序正确
                    clean_url = WechatNotifier()._clean_url(display_url)

                    # 对标题进行风控处理
                    sanitized_title = video_title
                    try:
                        from ...utils.risk_control import is_enabled, sanitize_text

                        if is_enabled():
                            title_result = sanitize_text(video_title, text_type="title")
                            if title_result["has_sensitive"]:
                                logger.info(
                                    f"[风控] 完成通知标题包含 {len(title_result['sensitive_words'])} 个敏感词，已处理"
                                )
                                sanitized_title = title_result["sanitized_text"]
                    except Exception as risk_exc:
                        logger.exception(f"完成通知标题风控处理失败: {risk_exc}")

                    completion_message = f"# {sanitized_title}\n\n{clean_url}\n\n🔗 总结和校对：\n{view_url}\n\n✅ **【任务完成】**"
                    logger.info(f"[缓存模式] 准备发送任务完成通知: {sanitized_title}")
                    task_notifier = WechatNotifier(wechat_webhook)
                    task_notifier.send_text(completion_message, skip_risk_control=True)
                    logger.info(f"[缓存模式] 任务完成通知已加入限流队列: {task_id}")

                logger.info(f"已发送缓存的 LLM 结果: {video_title}")

                cache_manager.update_task_status(
                    task_id,
                    "success",
                    platform=cache_data.get("platform"),
                    media_id=cache_data.get("media_id"),
                    title=video_title,
                    author=author,
                    cache_id=cache_data.get("cache_id"),
                    source_url=source_url,
                )

                return {
                    "status": "success",
                    "message": "使用已有缓存成功",
                    "data": {
                        "video_title": video_title,
                        "author": author,
                        "transcript": transcript,
                        "cached": True,
                        "speaker_recognition": has_speaker_recognition,
                    },
                }

            task_notifier.notify_task_status(
                display_url,
                "使用已有缓存",
                title=video_title,
                author=author,
                transcript="正在处理已存在的转录文本...",
            )

            try:
                llm_task_queue.put(
                    {
                        "task_id": task_id,
                        "url": url,
                        "display_url": display_url,
                        "platform": cache_data.get("platform"),
                        "media_id": cache_data.get("media_id"),
                        "video_title": video_title,
                        "author": author,
                        "description": description,
                        "transcript": transcript,
                        "use_speaker_recognition": has_speaker_recognition,
                        "transcription_data": transcription_data
                        if has_speaker_recognition
                        else None,
                        "is_generic": is_generic_downloader or is_from_generic,
                        "wechat_webhook": wechat_webhook,
                    }
                )
                logger.info(
                    f"将LLM任务加入队列: {task_id}, 标题: {video_title}, 说话人识别: {has_speaker_recognition}"
                )
            except Exception as exc:
                logger.exception(f"将LLM任务加入队列失败（缓存）: {exc}")
                task_notifier.send_text(f"【LLM任务加入队列失败】{exc}")

            return {
                "status": "success",
                "message": "使用已有缓存成功",
                "data": {
                    "video_title": video_title,
                    "author": author,
                    "transcript": transcript,
                    "cached": True,
                    "speaker_recognition": has_speaker_recognition,
                },
            }
        else:
            logger.info("未找到缓存，准备下载和转录")

            # ========== YouTube API Server 快速路径 ==========
            # 如果是 YouTube URL 且启用了 API Server，使用一次请求获取所有资源
            if (
                downloader.__class__.__name__ == "YoutubeDownloader"
                and hasattr(downloader, "use_api_server")
                and downloader.use_api_server
            ):
                logger.info(f"[youtube-api] Using API Server for: {url}")
                try:
                    from ...downloaders.youtube_api_errors import YouTubeApiError

                    # 一次 API 请求获取所有信息
                    api_result = downloader.fetch_for_transcription(
                        url, use_speaker_recognition
                    )

                    video_title = api_result["video_title"]
                    author = api_result["author"]
                    description = api_result["description"]
                    platform = api_result["platform"]
                    media_id = api_result["video_id"]

                    if not api_result["need_transcription"]:
                        # 有平台字幕，直接使用
                        transcript = api_result["transcript"]
                        logger.info(
                            f"[youtube-api] Using platform transcript, length={len(transcript)}"
                        )

                        task_notifier.notify_task_status(
                            display_url,
                            "平台字幕获取成功 - 使用 YouTube API Server",
                            title=video_title,
                            author=author,
                        )

                        # 保存到缓存
                        cache_result = cache_manager.save_cache(
                            platform=platform,
                            url=url,
                            media_id=media_id,
                            use_speaker_recognition=False,
                            transcript_data=transcript,
                            transcript_type="capswriter",
                            title=video_title,
                            author=author,
                            description=description,
                        )
                        if not cache_result:
                            logger.error(
                                "[youtube-api] Failed to save transcript cache"
                            )

                        # 加入 LLM 处理队列
                        try:
                            llm_task_queue.put(
                                {
                                    "task_id": task_id,
                                    "url": url,
                                    "display_url": display_url,
                                    "platform": platform,
                                    "media_id": media_id,
                                    "video_title": video_title,
                                    "author": author,
                                    "description": description,
                                    "transcript": transcript,
                                    "use_speaker_recognition": False,
                                    "is_generic": False,
                                    "wechat_webhook": wechat_webhook,
                                }
                            )
                            logger.info(f"[youtube-api] LLM task queued: {task_id}")
                        except Exception as exc:
                            logger.exception(
                                f"[youtube-api] Failed to queue LLM task: {exc}"
                            )
                            task_notifier.send_text(f"【LLM任务加入队列失败】{exc}")

                        cache_manager.update_task_status(
                            task_id,
                            "success",
                            platform=platform,
                            media_id=media_id,
                            title=video_title,
                            author=author,
                            source_url=source_url,
                        )
                        return {
                            "status": "success",
                            "message": "使用 YouTube API Server 获取字幕成功",
                            "data": {
                                "video_title": video_title,
                                "author": author,
                                "transcript": transcript,
                            },
                        }
                    else:
                        # 需要转录，使用已下载的音频
                        local_file = api_result["audio_path"]
                        logger.info(
                            f"[youtube-api] Audio downloaded, need transcription: {local_file}"
                        )

                        task_notifier.notify_task_status(
                            display_url,
                            f"正在转录音视频 - {engine_info}",
                            title=video_title,
                            author=author,
                        )

                        # 根据是否需要说话人识别选择转录器
                        if use_speaker_recognition:
                            logger.info("[youtube-api] Using FunASR for transcription")
                            funasr_client = FunASRSpeakerClient()
                            funasr_result = funasr_client.transcribe_sync(local_file)
                            transcript = funasr_result["formatted_text"]
                            transcription_data = funasr_result["transcription_result"]

                            cache_result = cache_manager.save_cache(
                                platform=platform,
                                url=url,
                                media_id=media_id,
                                use_speaker_recognition=True,
                                transcript_data=transcription_data,
                                transcript_type="funasr",
                                title=video_title,
                                author=author,
                                description=description,
                            )
                            transcription_result = {
                                "transcript": transcript,
                                "speaker_recognition": True,
                                "transcription_data": transcription_data,
                            }
                        else:
                            logger.info(
                                "[youtube-api] Using CapsWriter for transcription"
                            )
                            transcriber = Transcriber()
                            temp_output_base = datetime.datetime.now().strftime(
                                "%y%m%d-%H%M%S"
                            )
                            transcription_result = transcriber.transcribe(
                                local_file, temp_output_base
                            )
                            transcript = transcription_result.get("transcript", "")

                            cache_result = cache_manager.save_cache(
                                platform=platform,
                                url=url,
                                media_id=media_id,
                                use_speaker_recognition=False,
                                transcript_data=transcript,
                                transcript_type="capswriter",
                                title=video_title,
                                author=author,
                                description=description,
                            )

                        if not cache_result:
                            logger.error(
                                "[youtube-api] Failed to save transcription cache"
                            )

                        task_notifier.notify_task_status(
                            display_url,
                            f"转录完成 - {engine_info}",
                            title=video_title,
                            author=author,
                            transcript=transcript,
                        )

                        # 加入 LLM 处理队列
                        try:
                            llm_task_queue.put(
                                {
                                    "task_id": task_id,
                                    "url": url,
                                    "display_url": display_url,
                                    "platform": platform,
                                    "media_id": media_id,
                                    "video_title": video_title,
                                    "author": author,
                                    "description": description,
                                    "transcript": transcript,
                                    "use_speaker_recognition": use_speaker_recognition,
                                    "transcription_data": transcription_result.get(
                                        "transcription_data"
                                    )
                                    if use_speaker_recognition
                                    else None,
                                    "is_generic": False,
                                    "wechat_webhook": wechat_webhook,
                                }
                            )
                            logger.info(f"[youtube-api] LLM task queued: {task_id}")
                        except Exception as exc:
                            logger.exception(
                                f"[youtube-api] Failed to queue LLM task: {exc}"
                            )
                            task_notifier.send_text(f"【LLM任务加入队列失败】{exc}")

                        cache_manager.update_task_status(
                            task_id,
                            "success",
                            platform=platform,
                            media_id=media_id,
                            title=video_title,
                            author=author,
                            source_url=source_url,
                        )
                        return {
                            "status": "success",
                            "message": "使用 YouTube API Server 下载并转录成功",
                            "data": {
                                "video_title": video_title,
                                "author": author,
                                "transcript": transcript,
                                "speaker_recognition": use_speaker_recognition,
                            },
                        }

                except YouTubeApiError as api_error:
                    # API Server 失败，不降级，直接返回错误
                    error_msg = f"YouTube API Server error: [{api_error.code}] {api_error.message}"
                    logger.error(f"[youtube-api] {error_msg}")
                    task_notifier.notify_task_status(display_url, "下载失败", error_msg)
                    return {"status": "failed", "message": error_msg}

                except Exception as exc:
                    # 其他异常也不降级
                    error_msg = f"YouTube API Server unexpected error: {exc}"
                    logger.exception(f"[youtube-api] {error_msg}")
                    task_notifier.notify_task_status(display_url, "下载失败", error_msg)
                    return {"status": "failed", "message": error_msg}

            # ========== 原有逻辑（非 YouTube API Server 路径）==========
            # 如果没有提供 source_url，则使用传统方式获取视频信息
            if not source_url:
                logger.info("未提供 source_url，使用传统下载器获取视频信息")
                original_downloader = create_downloader(url)
                video_info = original_downloader.get_video_info(url)
                if not video_info:
                    raise ValueError("下载器未返回视频信息")

                video_title = video_info.get("video_title", "")
                author = video_info.get("author", "")
                description = video_info.get("description", "")
                is_from_generic = video_info.get("is_generic", False)
                platform = video_info.get("platform")
                video_id = video_info.get("video_id")
                media_id = video_id  # 保持兼容性
            else:
                logger.info("已提供 source_url，使用解析的元数据，跳过传统下载器的 get_video_info")
                # video_title, author, description, platform, video_id, media_id 已在前面设置

            # 根据 use_speaker_recognition 参数决定处理优先级
            subtitle = None

            # 如果提供了 source_url 且需要获取字幕，则使用 metadata_downloader
            if use_speaker_recognition:
                # 如果需要说话人识别，强制跳过平台字幕，直接进行下载转录
                logger.info(f"需要说话人识别，跳过平台字幕获取，强制下载转录: {url}")
            else:
                # 只有在不需要说话人识别时，才尝试获取平台字幕
                if metadata_downloader and metadata_downloader.__class__.__name__ == "YoutubeDownloader" and source_url:
                    logger.info(f"不需要说话人识别，尝试从 source_url 获取YouTube平台字幕: {source_url}")
                    subtitle = metadata_downloader.get_subtitle(source_url)
                elif not source_url:
                    # 如果没有 source_url，使用原有逻辑
                    original_downloader = create_downloader(url)
                    if original_downloader.__class__.__name__ == "YoutubeDownloader":
                        logger.info(f"不需要说话人识别，尝试获取YouTube平台字幕: {url}")
                        subtitle = original_downloader.get_subtitle(url)

            if subtitle:
                # 如果有字幕，直接使用
                logger.info(f"使用平台提供的字幕: {url}")

                task_notifier.notify_task_status(
                    display_url,
                    "平台字幕获取成功 - 直接使用平台字幕",
                    title=video_title,
                    author=author,
                )

                # 使用新的缓存系统保存平台字幕
                cache_result = cache_manager.save_cache(
                    platform=video_info.get("platform"),
                    url=url,
                    media_id=video_info.get("video_id"),
                    use_speaker_recognition=False,  # 平台字幕没有说话人识别
                    transcript_data=subtitle,
                    transcript_type="capswriter",  # 平台字幕按文本格式保存
                    title=video_title,
                    author=author,
                    description=description,
                )

                if not cache_result:
                    logger.error("保存平台字幕到缓存失败")

                # 将LLM处理任务加入队列
                try:
                    llm_task_queue.put(
                        {
                            "task_id": task_id,
                            "url": url,
                            "display_url": display_url,
                            "platform": video_info.get("platform"),
                            "media_id": video_info.get("video_id"),
                            "video_title": video_title,
                            "author": author,
                            "description": description,
                            "transcript": subtitle,
                            "use_speaker_recognition": False,  # 平台字幕没有说话人信息
                            "is_generic": is_generic_downloader or is_from_generic,
                            "wechat_webhook": wechat_webhook,
                        }
                    )
                    logger.info(
                        f"将LLM任务加入队列（平台字幕）: {task_id}, 标题: {video_title}"
                    )
                except Exception as exc:
                    logger.exception(f"将LLM任务加入队列失败（平台字幕）: {exc}")
                    task_notifier.send_text(f"【LLM任务加入队列失败】{exc}")

                result = {
                    "status": "success",
                    "message": "使用平台字幕成功",
                    "data": {
                        "video_title": video_title,
                        "author": author,
                        "transcript": subtitle,
                    },
                }
                cache_manager.update_task_status(
                    task_id,
                    "success",
                    platform=platform,
                    media_id=video_id,
                    title=video_title,
                    author=author,
                    source_url=source_url,
                )
                return result
            else:
                # 没有字幕，需要下载音视频并转录
                logger.info(f"下载视频进行转录: {url}")
                task_notifier.notify_task_status(
                    display_url,
                    f"正在下载视频 - {engine_info}",
                    title=video_title,
                    author=author,
                )

                # 如果提供了 source_url，说明使用本地文件，直接下载
                local_file = None
                if source_url:
                    # 使用 GenericDownloader 下载文件
                    logger.info(f"使用 GenericDownloader 下载文件: {url}")
                    # 从 URL 提取文件名
                    from urllib.parse import urlparse, unquote
                    parsed_url = urlparse(url)
                    path = unquote(parsed_url.path)
                    filename = os.path.basename(path)
                    if not filename:
                        # 如果无法提取文件名，生成一个
                        filename = f"{platform}_{video_id}.mp4"

                    local_file = downloader.download_file(url, filename)
                else:
                    # 传统逻辑：检查是否已通过BBDown下载
                    if video_info.get("downloaded") and video_info.get("local_file"):
                        # 使用BBDown已下载的文件
                        local_file = video_info.get("local_file")
                        logger.info(f"使用BBDown已下载的文件: {local_file}")
                    else:
                        # 常规下载流程
                        download_url = video_info.get("download_url")
                        filename = video_info.get("filename")

                        # 如果是YouTube链接，使用优先级下载方式（yt-dlp优先，TikHub备用）
                        original_downloader = create_downloader(url)
                        if hasattr(original_downloader, "download_video_with_priority") and (
                            "youtube.com" in url or "youtu.be" in url
                        ):
                            logger.info(f"YouTube视频，使用优先级下载（yt-dlp优先）: {url}")
                            local_file = original_downloader.download_video_with_priority(
                                url, video_info
                            )
                        elif download_url and filename:
                            # 其他平台使用常规下载流程
                            local_file = original_downloader.download_file(download_url, filename)
                        else:
                            error_msg = f"无法获取下载信息: {url}"
                            logger.error(error_msg)
                            task_notifier.notify_task_status(
                                display_url, "下载失败", error_msg, title=video_title, author=author
                            )
                            return {"status": "failed", "message": error_msg}

                if not local_file:
                    error_msg = f"下载文件失败: {url}"
                    logger.error(error_msg)
                    task_notifier.notify_task_status(
                        display_url, "下载失败", error_msg, title=video_title, author=author
                    )
                    return {"status": "failed", "message": error_msg}

                try:
                    # 开始转录
                    logger.info(f"开始转录音视频: {local_file}")
                    task_notifier.notify_task_status(
                        display_url,
                        f"正在转录音视频 - {engine_info}",
                        title=video_title,
                        author=author,
                    )

                    # platform 和 video_id 已在前面设置

                    # 根据是否需要说话人识别选择转录器
                    if use_speaker_recognition:
                        # 使用 FunASR 说话人识别服务器
                        logger.info("使用 FunASR 说话人识别服务器进行转录")
                        funasr_client = FunASRSpeakerClient()
                        funasr_result = funasr_client.transcribe_sync(local_file)

                        # 获取格式化的转录文本
                        transcript = funasr_result["formatted_text"]
                        transcription_data = funasr_result["transcription_result"]

                        # 使用新缓存系统保存
                        cache_result = cache_manager.save_cache(
                            platform=platform,
                            url=url,
                            media_id=media_id,
                            use_speaker_recognition=True,
                            transcript_data=transcription_data,
                            transcript_type="funasr",
                            title=video_title,
                            author=author,
                            description=description,
                        )

                        if not cache_result:
                            logger.error("保存FunASR转录结果到缓存失败")

                        # 构造与普通转录器兼容的结果
                        transcription_result = {
                            "transcript": transcript,
                            "speaker_recognition": True,
                            "transcription_data": transcription_data,
                        }
                    else:
                        # 使用普通 CapsWriter 转录器
                        transcriber = Transcriber()
                        # 使用时间戳作为临时输出基础名
                        temp_output_base = datetime.datetime.now().strftime(
                            "%y%m%d-%H%M%S"
                        )
                        transcription_result = transcriber.transcribe(
                            local_file, temp_output_base
                        )
                        transcript = transcription_result.get("transcript", "")

                        # 使用新缓存系统保存
                        cache_result = cache_manager.save_cache(
                            platform=platform,
                            url=url,
                            media_id=media_id,
                            use_speaker_recognition=False,
                            transcript_data=transcript,
                            transcript_type="capswriter",
                            title=video_title,
                            author=author,
                            description=description,
                        )

                        if not cache_result:
                            logger.error("保存CapsWriter转录结果到缓存失败")

                    # 获取转录文本
                    transcript = transcription_result.get("transcript", "")

                    # 通知转录完成，包含转录文本预览和服务器类型信息
                    task_notifier.notify_task_status(
                        display_url,
                        f"转录完成 - {engine_info}",
                        title=video_title,
                        author=author,
                        transcript=transcript,
                    )

                    # 将LLM处理任务加入队列
                    try:
                        llm_task_queue.put(
                            {
                                "task_id": task_id,
                                "url": url,
                                "display_url": display_url,
                                "platform": platform,
                                "media_id": media_id,
                                "video_title": video_title,
                                "author": author,
                                "description": description,
                                "transcript": transcript,
                                "use_speaker_recognition": use_speaker_recognition,
                                "transcription_data": transcription_result.get(
                                    "transcription_data"
                                )
                                if use_speaker_recognition
                                else None,
                                "is_generic": is_generic_downloader or is_from_generic,
                                "wechat_webhook": wechat_webhook,
                            }
                        )
                        logger.info(
                            f"将LLM任务加入队列（常规转录）: {task_id}, 标题: {video_title}"
                        )
                    except Exception as exc:
                        logger.exception(f"将LLM任务加入队列失败（常规转录）: {exc}")
                        task_notifier.send_text(f"【LLM任务加入队列失败】{exc}")

                    # 返回结果
                    result = {
                        "status": "success",
                        "message": "转录成功",
                        "data": {
                            "video_title": video_title,
                            "author": author,
                            "transcript": transcript,
                            "speaker_recognition": use_speaker_recognition,
                        },
                    }
                finally:
                    pass

                # 更新任务状态为成功
                cache_manager.update_task_status(
                    task_id,
                    "success",
                    platform=platform,
                    media_id=video_id,
                    title=video_title,
                    author=author,
                    source_url=source_url,
                )

        return result
    except Exception as exc:
        logger.exception(f"转录处理异常: {exc}")
        # 优先使用 source_url 用于通知显示
        display_url = source_url or url
        task_notifier = (
            WechatNotifier(wechat_webhook) if wechat_webhook else WechatNotifier()
        )
        task_notifier.notify_task_status(display_url, "转录异常", str(exc))
        cache_manager.update_task_status(task_id, "failed", source_url=source_url)
        return {
            "status": "failed",
            "message": f"转录任务异常: {exc}",
            "error": str(exc),
        }


def process_llm_queue():
    """处理LLM队列的后台任务"""
    logger.info("启动LLM队列处理器")

    while True:
        try:
            llm_task = llm_task_queue.get()
            try:
                logger.info(
                    "LLM任务出队: %s，提交到线程池（当前队列任务完成数: %s）",
                    llm_task.get("task_id"),
                    getattr(llm_task_queue, "completed", "未知"),
                )
                llm_executor.submit(_handle_llm_task, llm_task)
            except Exception as exc:
                logger.exception(f"提交LLM任务失败: {exc}")
                llm_task_queue.task_done()
        except Exception as exc:
            logger.exception(f"LLM队列处理器异常: {exc}")
            import time

            time.sleep(1)


def _handle_llm_task(llm_task: dict):
    """Worker entry for processing a single LLM task."""
    task_id = llm_task.get("task_id")

    try:
        with task_lock(task_id):
            url = llm_task["url"]
            # 优先使用 display_url 用于通知显示
            display_url = llm_task.get("display_url", url)
            platform = llm_task.get("platform")
            media_id = llm_task.get("media_id")
            video_title = llm_task["video_title"]
            transcript = llm_task["transcript"]
            use_speaker_recognition = llm_task.get("use_speaker_recognition", False)
            wechat_webhook = llm_task.get("wechat_webhook")

            task_notifier = (
                WechatNotifier(wechat_webhook) if wechat_webhook else WechatNotifier()
            )
            logger.info(f"开始处理LLM任务: {task_id}, 标题: {video_title}")

            try:
                if video_title == "":
                    is_generic = llm_task.get("is_generic", False)
                    if is_generic:
                        logger.info("通用下载器文件没有标题，使用LLM生成")
                        title_prompt = (
                            "请根据以下音视频转录文本，生成一个简洁的标题（不超过20个字）。\n"
                            "只返回标题文本，不要有任何其他说明或标点符号。\n"
                            "如果无法从内容中提取有意义的标题，请返回'自定义文件总结'。\n\n"
                            "转录文本：\n" + transcript[:1000]
                        )

                        try:
                            config_llm = config.get("llm", {})
                            api_key = config_llm.get("api_key")
                            base_url = config_llm.get("base_url")
                            summary_model = config_llm.get("summary_model")
                            max_retries = config_llm.get("max_retries", 2)
                            retry_delay = config_llm.get("retry_delay", 5)
                            generated_title = call_llm_api(
                                summary_model,
                                title_prompt,
                                api_key,
                                base_url,
                                max_retries,
                                retry_delay,
                            )

                            generated_title = (
                                generated_title.strip()
                                .strip('"')
                                .strip("'")
                                .strip("。")
                                .strip("，")
                            )
                            if generated_title and len(generated_title) <= 30:
                                video_title = generated_title
                                logger.info(f"LLM生成的标题: {video_title}")
                            else:
                                video_title = "自定义文件总结"
                                logger.warning("LLM生成的标题不合规，使用默认标题")
                        except Exception as exc:
                            logger.error(f"LLM生成标题失败: {exc}")
                            video_title = "自定义文件总结"

                llm_task["video_title"] = video_title

                # 使用增强LLM处理器处理任务（自动判断是否需要分段）
                logger.info(f"开始使用增强LLM处理器处理任务: {task_id}")
                result_dict = enhanced_llm_processor.process_llm_task(llm_task)

                logger.info(f"LLM处理完成，开始保存结果和发送微信通知: {task_id}")

                # 提取结果和统计信息
                calibrated_text = result_dict.get("校对文本", "")
                summary_text = result_dict.get("内容总结")
                skip_summary = result_dict.get("skip_summary", False)
                stats = result_dict.get("stats", {})
                models_used = result_dict.get("models_used", {})

                # 提取成功标记（B1方案：失败时不写文件）
                calibrate_success = result_dict.get("calibrate_success", True)
                summary_success = result_dict.get("summary_success", True)

                # 保存 LLM 模型配置到数据库
                if models_used:
                    cache_manager.update_task_llm_config(task_id, models_used)
                    logger.info(
                        f"LLM模型配置已保存: {task_id}, risk_detected={models_used.get('has_risk', False)}"
                    )

                # 保存校对文本到缓存（仅在成功时保存）
                if platform and media_id:
                    if calibrate_success:
                        cache_manager.save_llm_result(
                            platform=platform,
                            media_id=media_id,
                            use_speaker_recognition=use_speaker_recognition,
                            llm_type="calibrated",
                            content=calibrated_text,
                        )
                        logger.info(f"校对文本已保存到缓存: {task_id}")
                    else:
                        logger.warning(f"校对失败，跳过保存校对文件: {task_id}")

                    # 保存总结文本到缓存（仅在成功时保存）
                    if summary_success:
                        if skip_summary:
                            # 跳过总结时，只有校对成功才保存
                            if calibrate_success:
                                summary_content = calibrated_text
                                logger.info(f"文本过短，保存校对文本作为总结: {task_id}")
                                cache_manager.save_llm_result(
                                    platform=platform,
                                    media_id=media_id,
                                    use_speaker_recognition=use_speaker_recognition,
                                    llm_type="summary",
                                    content=summary_content,
                                )
                        else:
                            summary_content = summary_text
                            logger.info(f"保存LLM总结到缓存: {task_id}")
                            cache_manager.save_llm_result(
                                platform=platform,
                                media_id=media_id,
                                use_speaker_recognition=use_speaker_recognition,
                                llm_type="summary",
                                content=summary_content,
                            )
                    else:
                        logger.warning(f"总结失败，跳过保存总结文件: {task_id}")

                    if calibrate_success or summary_success:
                        logger.info(f"LLM结果已保存到缓存: {platform}/{media_id}")
                    else:
                        logger.warning(f"LLM处理全部失败，未保存任何结果文件: {task_id}")

                # 获取查看链接
                task_info = cache_manager.get_task_by_id(task_id)
                view_url = ""
                if task_info and task_info.get("view_token"):
                    base_url = get_base_url()
                    view_url = f"{base_url}/view/{task_info['view_token']}"

                # 构建统计信息文本
                original_length = stats.get("original_length", 0)
                calibrated_length = stats.get("calibrated_length", 0)
                summary_length = stats.get("summary_length", 0)

                # 构建完整的消息格式
                speaker_info = "（含说话人识别）" if use_speaker_recognition else ""

                # 格式化模型配置信息
                model_config_text = format_llm_config_markdown(models_used)

                if skip_summary:
                    full_message = f"""## 总结和校对
🌐 网页查看：{view_url}
📄 直接获取：{view_url}?raw=calibrated

## 转录统计
原始 {original_length:,} 字 | 校对 {calibrated_length:,} 字 | 总结 未生成

{model_config_text}

## 校对文本{speaker_info}
{calibrated_text}"""
                    logger.info(f"发送校对文本（文本过短，未总结）: {task_id}")
                else:
                    full_message = f"""## 总结和校对
🌐 网页查看：{view_url}
📄 直接获取：{view_url}?raw=calibrated

## 转录统计
原始 {original_length:,} 字 | 校对 {calibrated_length:,} 字 | 总结 {summary_length:,} 字

{model_config_text}

## 总结{speaker_info}
{summary_text}"""
                    logger.info(f"发送总结文本: {task_id}")

                send_long_text_wechat(
                    title=video_title,
                    url=display_url,
                    text=full_message,
                    is_summary=not skip_summary,
                    has_speaker_recognition=use_speaker_recognition,
                    webhook=wechat_webhook,
                    skip_content_type_header=True,
                )

                import time

                time.sleep(0.1)  # 100ms延迟，确保总结文本已加入队列

                task_info = cache_manager.get_task_by_id(task_id)
                if task_info and task_info.get("view_token"):
                    base_url = get_base_url()
                    view_url = f"{base_url}/view/{task_info['view_token']}"

                    clean_url = WechatNotifier()._clean_url(display_url)

                    sanitized_title = video_title
                    try:
                        from ...utils.risk_control import is_enabled, sanitize_text

                        if is_enabled():
                            title_result = sanitize_text(video_title, text_type="title")
                            if title_result["has_sensitive"]:
                                logger.info(
                                    f"[风控] 完成通知标题包含 {len(title_result['sensitive_words'])} 个敏感词，已处理"
                                )
                                sanitized_title = title_result["sanitized_text"]
                    except Exception as risk_exc:
                        logger.exception(f"完成通知标题风控处理失败: {risk_exc}")

                    completion_message = f"# {sanitized_title}\n\n{clean_url}\n\n🔗 总结和校对：\n{view_url}\n\n✅ **【任务完成】**"
                    task_notifier = WechatNotifier(wechat_webhook)
                    task_notifier.send_text(completion_message, skip_risk_control=True)
                    logger.info(f"任务完成通知已加入限流队列: {task_id}")

                logger.info(f"LLM任务处理完成: {task_id}, 标题: {video_title}")
            except Exception as exc:
                logger.exception(f"LLM任务处理异常: {task_id}, 错误: {exc}")
                task_notifier.send_text(f"【LLM API调用异常】{exc}")
    finally:
        llm_task_queue.task_done()
