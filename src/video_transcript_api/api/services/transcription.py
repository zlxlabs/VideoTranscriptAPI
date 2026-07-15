import asyncio
import datetime
import os
import threading
import time
from typing import Optional, Dict, Any

from fastapi import HTTPException, Header, Request
from pydantic import BaseModel, Field, StrictBool, field_validator

from ..context import (
    get_audit_logger,
    get_cache_manager,
    get_config,
    get_executor,
    get_llm_queue,
    get_logger,
    get_task_queue,
    get_temp_manager,
    get_user_manager,
)
from ...downloaders import create_downloader
from ...errors import (
    NonVideoContentError,
    ResolverAuthError,
    InvalidURLError,
    ResolverResolveError,
    ResolverResponseError,
)

# P0-1：解析终态异常需直达用户提示，不能被 try/except 吞掉走默认失败路径。
# 这些异常不可重试、有明确用户文案，应冒泡到外层 handler 以 str(exc) 反馈用户。
_TERMINAL_RESOLVER_ERRORS = (
    NonVideoContentError,
    ResolverAuthError,
    InvalidURLError,
    ResolverResolveError,
    ResolverResponseError,
)
from ...transcriber import FunASRSpeakerClient, Transcriber
from ...utils.notifications import (
    WechatNotifier,
    send_long_text_wechat,
    get_notification_router,
)
from ...utils.notifications.channel import _clean_url
from ...utils.rendering import get_base_url
from ...utils.perf_tracker import PerfTracker
from ...utils.task_status import TaskStatus
from ...utils.llm_status import CalibrationStatus, SummaryStatus

logger = get_logger()
config = get_config()
user_manager = get_user_manager()
audit_logger = get_audit_logger()
cache_manager = get_cache_manager()
task_queue = get_task_queue()
llm_task_queue = get_llm_queue()
executor = get_executor()


class MetadataOverride(BaseModel):
    """元数据覆盖模型"""
    title: Optional[str] = Field(None, description="视频标题", max_length=200)
    description: Optional[str] = Field(None, description="视频描述", max_length=2000)
    author: Optional[str] = Field(None, description="视频作者", max_length=200)


class NotificationConfig(BaseModel):
    """通知配置（可选，用于 per-request 指定渠道）"""
    channel: Optional[str] = Field(None, description="通知渠道: wechat / feishu / None(全部)")
    webhook: Optional[str] = Field(None, description="自定义 webhook URL")

    @field_validator("webhook")
    @classmethod
    def validate_webhook_url(cls, v):
        if v is None or v.strip() == "":
            return v
        from ...utils.url_validator import validate_url_safe, URLValidationError
        try:
            validate_url_safe(v)
        except URLValidationError as e:
            raise ValueError(f"webhook URL is not allowed: {e}")
        return v


class ProcessingOptions(BaseModel):
    """处理深度开关：控制本次任务要跑到哪一步（只转录 / 转录+校对 / 全流程）。

    两个字段互相独立，默认均为 True（等价于历史行为：完整跑校对+总结）。
    summarize=True 且 calibrate=False 是合法组合——总结会基于未经 LLM 校对的
    原始转录文本生成，其质量可能受 ASR 识别噪声（错别字、断句错误等）影响，
    但仍然可用；系统不做硬性拦截，由调用方自行权衡。

    用 StrictBool 而非普通 bool（ci-gate review）：Pydantic 的宽松 bool 会
    静默把 "yes"/"1"/"no"/"0" 等字符串转换成布尔值，与本 API 文档声明的
    JSON boolean 类型不符——请求方一个拼写习惯的差异（比如误传字符串
    "false" 而不是布尔 false）就可能意外触发/关闭有真实 token 成本的 LLM
    阶段而不自知。StrictBool 只接受真正的 JSON boolean，其余一律 422。
    """

    calibrate: StrictBool = Field(True, description="是否执行 LLM 校对")
    summarize: StrictBool = Field(
        True,
        description=(
            "是否生成内容总结。若 calibrate=False，总结将基于未经校对的原始转录"
            "文本生成，质量可能受 ASR 识别噪声影响"
        ),
    )


def normalize_processing_options(
    processing_options: Optional["ProcessingOptions"],
) -> dict:
    """将请求里的 processing_options 归一化为 plain dict。

    None（调用方未指定）等价于全部启用（calibrate=True, summarize=True），
    与历史行为保持一致——这是贯穿 task dict / llm_task_queue payload 的统一
    "缺省即全流程"约定，下游（tasks.py/transcription.py/llm_ops.py）都应
    通过本函数或同等的 `.get(...) or DEFAULT` 兜底来读取，不要直接假设键存在。

    Args:
        processing_options: 请求体里的 ProcessingOptions 实例，可能为 None

    Returns:
        dict: {"calibrate": bool, "summarize": bool}
    """
    if processing_options is None:
        return {"calibrate": True, "summarize": True}
    return processing_options.model_dump()


class TranscribeRequest(BaseModel):
    """转录请求数据模型"""

    url: str = Field(..., description="视频URL（平台链接，用于 view_token 和缓存）")
    # StrictBool（同 ProcessingOptions，ci-gate review）：这个开关会切换转录
    # 引擎（FunASR vs CapsWriter）并影响缓存 key，同样不该被 "yes"/"1" 之类
    # 宽松字符串静默触发。
    use_speaker_recognition: StrictBool = Field(False, description="是否使用说话人识别功能")
    wechat_webhook: Optional[str] = Field(
        None, description="企业微信webhook地址"
    )
    feishu_webhook: Optional[str] = Field(
        None, description="飞书webhook地址"
    )
    download_url: Optional[str] = Field(
        None, description="实际下载地址（可选，如果提供则优先使用）"
    )
    metadata_override: Optional[MetadataOverride] = Field(
        None, description="元数据覆盖（用于补充或覆盖解析的元数据）"
    )
    notification_config: Optional[NotificationConfig] = Field(
        None, description="通知配置（可选，指定渠道和自定义 webhook）"
    )
    processing_options: Optional[ProcessingOptions] = Field(
        None,
        description="处理深度开关（只转录/转录+校对/全流程）。None 等价于全部启用",
    )

    @field_validator("wechat_webhook", "feishu_webhook")
    @classmethod
    def validate_webhook_url(cls, v):
        """验证 webhook URL 安全性（防止 SSRF）"""
        if v is None or v.strip() == "":
            return v
        from ...utils.url_validator import validate_url_safe, URLValidationError
        try:
            validate_url_safe(v)
        except URLValidationError as e:
            raise ValueError(f"webhook URL is not allowed: {e}")
        return v


class RecalibrateRequest(BaseModel):
    """重新校对请求数据模型"""

    view_token: str = Field(..., description="查看页面的 view_token")
    wechat_webhook: Optional[str] = Field(
        None, description="企业微信webhook地址，用于发送通知"
    )

    @field_validator("wechat_webhook")
    @classmethod
    def validate_webhook_url(cls, v):
        """验证 webhook URL 安全性（防止 SSRF）"""
        if v is None or v.strip() == "":
            return v
        from ...utils.url_validator import validate_url_safe, URLValidationError
        try:
            validate_url_safe(v)
        except URLValidationError as e:
            raise ValueError(f"webhook URL is not allowed: {e}")
        return v


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
        parsed_metadata: 从url解析的元数据（可能为None）
        metadata_override: 用户提供的元数据覆盖（可能为None）
        url: 平台链接（用于生成默认值）

    返回:
        dict: 合并后的完整元数据
    """
    # 步骤1：元数据合并
    if parsed_metadata is not None:
        # 解析成功：metadata_override 作为补充
        # 注意：过滤掉 metadata_override 中的 None 值和空字符串，避免覆盖解析出的有效值
        filtered_override = {
            k: v
            for k, v in (metadata_override or {}).items()
            if v is not None and (not isinstance(v, str) or v.strip())
        }
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
        f"最终元数据: platform={final_metadata['platform']}, "
        f"video_id={final_metadata['video_id']}, "
        f"title={final_metadata['title'][:50]}, "
        f"author={final_metadata['author']}"
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
            notification_channel = task.get("notification_channel")
            notification_webhooks = task.get("notification_webhooks", {})
            download_url = task.get("download_url")
            metadata_override = task.get("metadata_override")
            # 处理深度开关（只转录/转录+校对/全流程）：task dict 里缺失时按全流程兜底，
            # 与 normalize_processing_options(None) 的语义保持一致。
            processing_options = task.get("processing_options") or {
                "calibrate": True,
                "summarize": True,
            }

            try:
                cache_manager.update_task_status(task_id, TaskStatus.PROCESSING, download_url=download_url)

                future = executor.submit(
                    process_transcription,
                    task_id,
                    url,
                    use_speaker_recognition,
                    wechat_webhook,
                    download_url,
                    metadata_override,
                    notification_channel=notification_channel,
                    notification_webhooks=notification_webhooks,
                    processing_options=processing_options,
                )

                def task_completed(future_result):
                    # 状态由 process_transcription / LLM 阶段写入 DB；
                    # 此回调仅兜底 future 意外抛出（未被内部捕获）的情况。
                    try:
                        future_result.result()
                        logger.info(f"任务完成: {task_id}")
                    except Exception as exc:
                        logger.exception(
                            f"任务处理失败: {task_id}, URL: {url}, 错误: {exc}"
                        )
                        cache_manager.update_task_status(
                            task_id, TaskStatus.FAILED,
                            error_message=f"转录任务失败: {exc}",
                        )
                        display_url = url
                        get_notification_router().notify_task_status(
                            url=display_url, status="转录失败", error=str(exc),
                            channel_name=notification_channel, webhooks=notification_webhooks,
                        )

                future.add_done_callback(task_completed)
                logger.info(f"任务已提交到线程池: {task_id}, URL: {url}")
            except Exception as exc:
                logger.exception(
                    f"提交任务到线程池失败: {task_id}, URL: {url}, 错误: {exc}"
                )
                cache_manager.update_task_status(
                    task_id, TaskStatus.FAILED,
                    error_message=f"提交任务失败: {exc}",
                )
            finally:
                task_queue.task_done()
        except Exception as exc:
            logger.exception(f"任务队列处理器异常: {exc}")
            await asyncio.sleep(1)


def process_transcription(
    task_id, url, use_speaker_recognition=False, wechat_webhook=None,
    download_url=None, metadata_override=None, notification_channel=None,
    notification_webhooks=None, processing_options=None,
):
    """
    处理视频转录

    参数:
        task_id: 任务ID
        url: 平台链接（用于元数据解析、view_token 生成、缓存查询）
        use_speaker_recognition: 是否使用说话人识别
        wechat_webhook: 企业微信webhook（向后兼容）
        download_url: 实际下载地址（可选，如果提供则优先使用）
        metadata_override: 元数据覆盖（dict）
        notification_channel: 指定通知渠道（wechat/feishu/None=全部）
        notification_webhooks: per-channel webhook dict {"wechat": "...", "feishu": "..."}
        processing_options: 处理深度开关 dict {"calibrate": bool, "summarize": bool}，
            None 时按全流程兜底（向后兼容旧调用方）
    """
    if notification_webhooks is None:
        notification_webhooks = {}
    if processing_options is None:
        processing_options = {"calibrate": True, "summarize": True}
    # 性能追踪器：记录各阶段耗时
    tracker = PerfTracker(task_id=task_id)

    # ---- 临时文件生命周期（见评审决议 D10/D11/codex#1,#9）----
    # 进入处理：标记活跃（受在途保护）、绑定当前线程任务、建任务专属目录、
    # 顺手做一次节流的惰性清扫兜底孤儿。所有下载产物会落到 data/temp/task_<id>/，
    # 在最外层 finally 里被 clean_up_task 一并删除——覆盖所有 early return 与异常。
    temp_manager = get_temp_manager()
    temp_manager.mark_active(task_id)
    temp_manager.set_current_task(task_id)
    temp_manager.create_task_dir(task_id)
    try:
        temp_manager.maybe_sweep()
    except Exception as sweep_exc:
        logger.warning(f"惰性清扫失败（不影响主流程）: {sweep_exc}")

    try:
        # 规范化 download_url：将空字符串转换为 None
        if download_url is not None and isinstance(download_url, str) and not download_url.strip():
            download_url = None

        # SSRF 防护：验证 download_url 安全性
        if download_url:
            from ...utils.url_validator import validate_url_safe, URLValidationError
            try:
                validate_url_safe(download_url)
            except URLValidationError as e:
                logger.warning(f"download_url SSRF check failed: {download_url}, reason: {e}")
                raise ValueError(f"download_url is not allowed: {e}")

        logger.info(f"开始处理转录任务: {task_id}, URL: {url}, download_url: {download_url}")

        # url 本身就是平台链接，直接使用
        display_url = url
        logger.info(f"通知将使用URL: {display_url}")

        _router = get_notification_router()

        class _TaskNotifier:
            """Bound notifier for this task — wraps router with channel/webhook context."""
            def notify_task_status(self, url, status, error=None, title=None, author=None, transcript=None):
                return _router.notify_task_status(
                    url=url, status=status, error=error, title=title,
                    author=author, transcript=transcript,
                    channel_name=notification_channel, webhooks=notification_webhooks,
                )
            def send_text(self, content, skip_risk_control=False):
                return _router.send_text(
                    content, channel_name=notification_channel, webhooks=notification_webhooks,
                )

        task_notifier = _TaskNotifier()
        engine_info = (
            "说话人识别(FunASR)" if use_speaker_recognition else "普通转录(CapsWriter)"
        )
        task_notifier.notify_task_status(display_url, f"开始处理 - {engine_info}")

        # ==================== 阶段1: URL 解析（提取 platform 和 video_id）====================
        from ...utils.url_parser import URLParser

        # url 本身就是平台链接，直接解析
        check_url = url
        logger.info(f"[URL解析] 开始解析 URL: {check_url[:100]}")

        with tracker.track("url_parse"):
            try:
                # 使用 URLParser 统一解析（支持短链接自动解析）
                url_parser = URLParser()
                parsed_url = url_parser.parse(check_url)

                platform = parsed_url.platform
                video_id = parsed_url.video_id

                logger.info(
                    f"[URL解析] 解析成功: platform={platform}, video_id={video_id}, "
                    f"is_short_url={parsed_url.is_short_url}"
                )

            except Exception as e:
                # URL 解析失败，回退到 generic 模式
                logger.warning(f"[URL解析] 解析失败: {e}，使用 generic 模式")
                platform = 'generic'
                video_id = generate_media_id_from_url(url)
                logger.info(f"[URL解析] 回退到通用标识: platform={platform}, video_id={video_id}")

        # ==================== 阶段2: 缓存检测（在创建下载器之前）====================
        cache_data = None
        is_generic_downloader = platform == 'generic'

        with tracker.track("cache_check"):
            if video_id and platform and not is_generic_downloader:
                logger.info(
                    f"[缓存检测] 检查缓存: platform={platform}, video_id={video_id}, "
                    f"use_speaker_recognition={use_speaker_recognition}"
                )
                cache_data = cache_manager.get_cache(
                    platform=platform,
                    media_id=video_id,
                    use_speaker_recognition=use_speaker_recognition,
                )
            else:
                logger.info(
                    f"[缓存检测] 跳过缓存检查 (platform={platform}, is_generic={is_generic_downloader})"
                )

        if cache_data:
            logger.info("[缓存检测] ✅ 缓存命中，直接返回")
            logger.info("找到已存在的缓存记录，跳过下载和转录步骤")
            video_title = cache_data.get("title", "已缓存视频")
            author = cache_data.get("author", "")
            description = cache_data.get("description", "")
            has_speaker_recognition = cache_data.get("use_speaker_recognition", False)
            # 缓存命中时，is_from_generic 必然是 False（第 365 行条件保证了 generic 不会被缓存）
            is_from_generic = False

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

            # ---- 分层缓存命中判定（相对本次请求的 processing_options）----
            # 缓存产物只增不减：required = {transcript} ∪ (calibrate→calibrated) ∪
            # (summarize→summary)。transcript 已保证存在（否则不会进到这个分支）。
            #
            # calibrated 层的"已满足"判定不能只看文件是否存在——如果上一轮请求
            # calibrate=False，llm_calibrated.txt 仍会被写入（内容是本地格式化的
            # 原文，calibration_status=disabled），此时若本轮请求 calibrate=True，
            # 必须视为"缺失"以触发真实校对，而不是把 disabled 占位文本误当成
            # 已完成的校对结果直接返回。summary 层没有这个问题：disabled/failed
            # 都不落盘 llm_summary.txt（见 llm_ops._save_llm_results），因此文件
            # 存在即代表该层已有确定性产物（generated 或 skipped_short）。
            #
            # NONE 与 disabled 同理需要排除（codex-review R4 #2）：全降级 NONE 现在
            # 也会落盘一份兜底格式化文本（见 llm_ops._save_llm_results 的
            # calibrated_saved），但那是"尝试但完全失败"的产物，不是真正完成的
            # 校对结果——必须允许后续请求（或 /api/recalibrate）把它当作"缺失"
            # 触发真实重试，而不是被当成已满足的层直接短路返回，否则一次失败会
            # 永久锁死在失败状态。
            cached_llm_status = cache_data.get("llm_status") or {}
            cached_calibration_status = cached_llm_status.get("calibration_status")
            calibrated_layer_satisfied = (
                has_llm_calibrated
                and cached_calibration_status != CalibrationStatus.DISABLED
                and cached_calibration_status != CalibrationStatus.NONE
            )
            calibrate_requested = processing_options.get("calibrate", True)
            need_calibrated = calibrate_requested and not calibrated_layer_satisfied
            need_summary = processing_options.get("summarize", True) and not has_llm_summary

            # need_calibrated=False 不代表校对层已经有任何产物——calibrate=False
            # 会无条件把它压成 False，即便 llm_calibrated 压根不存在（比如只有
            # transcript 层的旧缓存，或该媒体第一次请求、LLM 从未跑过一次）。
            # 这种情况下 calibrate_requested=False 是唯一让 need_calibrated=False
            # 的原因，has_llm_calibrated 仍是 False——不能把它和"层已满足"混为
            # 一谈，否则下面会把从未存在的 llm_calibrated 读成空字符串，发出
            # 空校对通知，任务行也不会被标记为 disabled（codex-review R5 #2）。
            # summary 层不需要同样处理：summary 的 disabled/failed 本来就不落盘
            # llm_summary.txt（见上面的分层命中判定注释），has_llm_summary=False
            # 在 calibrate=True 场景下的展示分支已经能正确回退（用 calibrated_text
            # 兜底），不存在"读空字符串"的问题。
            calibration_effectively_missing = not calibrate_requested and not has_llm_calibrated

            if not need_calibrated and not need_summary and not calibration_effectively_missing:
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
                calibrated_text = cache_data.get("llm_calibrated", "")

                # 判断是否跳过了总结：
                # - 真正的"短文本跳过"：summary 与 calibrated 内容相同（见
                #   llm_ops._save_llm_results 的 SKIPPED_SHORT 分支，会把
                #   calibrated 文本原样复制成 summary 落盘）；
                # - 本次/历史请求根本未要求总结（summarize=False，见函数顶部
                #   注释：disabled 时不落盘 llm_summary.txt）：has_llm_summary
                #   为 False，此时 cache_data 里没有 "llm_summary" 键，不能无
                #   条件下标访问，否则 KeyError（这正是本次修复的问题）。
                #   两种情况在展示上等价，都走"未生成总结"文案，与周边已有的
                #   skip_summary 分支保持一致。
                if has_llm_summary:
                    summary_text = cache_data["llm_summary"]
                    skip_summary = summary_text == calibrated_text
                else:
                    summary_text = calibrated_text
                    skip_summary = True

                # 通知里的总结状态文案：与 llm_ops._send_notification 保持一致的
                # 三态区分（failed/disabled/其他一律"未生成"）——这条"缓存全命中"
                # 路径此前硬编码"未生成"，不区分 summary_status，导致用户主动
                # 关闭总结（disabled）时通知误报成"未生成"，与诚实状态模型的
                # 承诺不符（ci-gate review，云端 CI 发现）。
                cached_summary_status = cached_llm_status.get("summary_status")
                if cached_summary_status == SummaryStatus.FAILED:
                    summary_status_label = "生成失败"
                elif cached_summary_status == SummaryStatus.DISABLED:
                    summary_status_label = "未启用"
                else:
                    summary_status_label = "未生成"

                # 构建完整的消息格式
                speaker_info = "（含说话人识别）" if has_speaker_recognition else ""
                # 这条"缓存全命中"路径独立拼接消息，不经过 llm_ops._send_notification/
                # _build_calibration_warning，之前完全没有消费 calibration_status——
                # 但触发这条分支不代表本轮真的做过 LLM 校对：calibrate_requested=False
                # 时即便历史上跑过一轮 disabled（本地格式化占位文本落盘），
                # calibrated_layer_satisfied 仍会因 cached_calibration_status==DISABLED
                # 而判定"层未满足"，need_calibrated 却因 calibrate_requested=False 恒为
                # False，两者结合会让这条分支把"未经 LLM 校对的占位文本"当作
                # calibrated_text/summary_text 发出，且不带任何提示（ci-gate review）。
                calibration_warning = (
                    "\n⚠️ **AI 校对未启用**：当前显示为未经校对的原始语音识别文本"
                    "（可能含错别字、断句错误）。"
                    if cached_calibration_status == CalibrationStatus.DISABLED
                    else ""
                )
                if skip_summary:
                    # 短文本/未启用/失败，均展示校对文本兜底
                    full_message = f"""## 总结和校对
🌐 网页查看：{view_url}
📄 直接获取：{view_url}?raw=calibrated

## 转录统计
原始 {original_length:,} 字 | 校对 {calibrated_length:,} 字 | 总结 {summary_status_label}{calibration_warning}

## 校对文本{speaker_info}
{summary_text}"""
                    logger.info(f"缓存模式 - 发送校对文本（总结{summary_status_label}）")
                else:
                    # 长文本，有总结
                    summary_length = len(summary_text)
                    full_message = f"""## 总结和校对
🌐 网页查看：{view_url}
📄 直接获取：{view_url}?raw=calibrated

## 转录统计
原始 {original_length:,} 字 | 校对 {calibrated_length:,} 字 | 总结 {summary_length:,} 字{calibration_warning}

## 总结{speaker_info}
{summary_text}"""
                    logger.info("缓存模式 - 发送总结文本")

                # 发送（跳过自动添加的内容类型标题）
                _router.send_long_text(
                    title=video_title,
                    url=display_url,
                    text=full_message,
                    is_summary=not skip_summary,
                    has_speaker_recognition=has_speaker_recognition,
                    channel_name=notification_channel,
                    webhooks=notification_webhooks,
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

                    from ...utils.notifications.channel import _apply_risk_control_safe
                    clean = _clean_url(display_url)
                    sanitized_title = _apply_risk_control_safe(video_title, text_type="title")

                    completion_message = f"# {sanitized_title}\n\n{clean}\n\n🔗 总结和校对：\n{view_url}\n\n✅ **【任务完成】**"
                    logger.info(f"[缓存模式] 准备发送任务完成通知: {sanitized_title}")
                    task_notifier.send_text(completion_message, skip_risk_control=True)
                    logger.info(f"[缓存模式] 任务完成通知已加入限流队列: {task_id}")

                logger.info(f"已发送缓存的 LLM 结果: {video_title}")

                # 缓存全命中（含 LLM 结果）：无后续 LLM 工作，直接置终态 success。
                # 把本次已读到的 llm_status.json 快照（cached_llm_status，见上方
                # 分层缓存命中判定）镜像进这条新建的 task_status 行——否则
                # create_task 建出的行 calibration_status/summary_status 默认为
                # NULL，/api/audit/history 对全命中任务会返回空状态，与缓存目录
                # 里的真实状态不一致（codex-review R2）。沿用 llm_ops 里"从
                # llm_status.json 读回未触碰层"的同一模式：优先用 llm_status.json
                # 里的显式值；缺失时（早于诚实状态模型上线的旧缓存）按现有产物
                # 文件推导合理默认——calibrated_layer_satisfied 已排除 disabled
                # 占位符，此时真实存在校对文件即可推断为 full；summary 无法
                # 可靠区分 generated 与 skipped_short，缺失时保持 None，不瞎编。
                mirrored_calibration_status = cached_calibration_status
                if mirrored_calibration_status is None and calibrated_layer_satisfied:
                    mirrored_calibration_status = CalibrationStatus.FULL
                mirrored_summary_status = cached_llm_status.get("summary_status")

                cache_manager.update_task_status(
                    task_id,
                    TaskStatus.SUCCESS,
                    platform=cache_data.get("platform"),
                    media_id=cache_data.get("media_id"),
                    title=video_title,
                    author=author,
                    cache_id=cache_data.get("cache_id"),
                    download_url=download_url,
                    calibration_status=mirrored_calibration_status,
                    summary_status=mirrored_summary_status,
                )

                # 缓存完全命中（含 LLM 结果），记录计数并输出性能摘要
                tracker.count("cache_hit")
                tracker.log_summary()

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

            # 缓存部分命中（transcript 已在，但请求的层里至少一层缺失），记录计数
            tracker.count("cache_hit_partial")

            # 只在"强制降级为纯文本"的 else 分支里才需要：读缓存里已落盘的说话人
            # 结构化数据（llm_processed.json，见 cache_manager.get_cache），在不
            # 重跑说话人分块校对的前提下把真实说话人数回传给协调器，供总结环节
            # 选择正确的多/单说话人 Prompt（codex-review R5 #3，详见下方 else
            # 分支注释）。没有说话人识别、或缓存里还没有结构化产物（比如说话人
            # 识别但从未真正过一次 LLM 校对）时保持 None——协调器会退回自己的
            # 自动推断，不引入新的失败模式。
            cached_speaker_count = None
            if has_speaker_recognition:
                cached_structured = cache_data.get("llm_processed") or {}
                cached_speaker_mapping = cached_structured.get("speaker_mapping")
                if cached_speaker_mapping:
                    cached_speaker_count = len(cached_speaker_mapping)

            if need_calibrated:
                # 校对层缺失，需要真实（重新）校对：沿用原始转录内容
                queued_transcript = transcript
                queued_transcription_data = transcription_data if has_speaker_recognition else None
                queued_use_speaker_recognition = has_speaker_recognition
            else:
                # need_calibrated=False 到这里有两种情况：
                # 1) 校对层已满足，只缺总结：复用已有校对文本作为总结输入（而非
                #    原始转录，质量更高），并强制走纯文本路径
                #    （transcription_data=None）避免二次说话人推断浪费 LLM 调用
                #    ——_prepare_llm_content 在 transcription_data 为空时会回退
                #    到纯文本分支，与 use_speaker_recognition 是否为 True 无关。
                #    强制纯文本会让协调器的说话人数自动推断失真（纯文本必然判
                #    成单说话人），所以上面读出 cached_speaker_count 随任务一起
                #    传下去，由协调器覆盖这个误判（而不是重新推断一次说话人，
                #    那样就违背了"避免二次说话人推断"的初衷）。
                # 2) calibration_effectively_missing 命中（calibrate=False 且
                #    校对层从未存在过，need_summary 可能同时为 False——两层都
                #    从未处理过；也可能为 True——只是校对层缺失、总结层本身
                #    还需要真实生成）：cache_data.get("llm_calibrated") 取不到
                #    值，回退到原始转录——这个分支本身就是把请求交给 llm_ops
                #    的 skip_calibration 本地格式化路径去产出 disabled 占位
                #    产物，语义上仍然正确。
                queued_transcript = cache_data.get("llm_calibrated") or transcript
                queued_transcription_data = None
                queued_use_speaker_recognition = has_speaker_recognition

            queued_processing_options = {
                "calibrate": need_calibrated,
                "summarize": need_summary,
            }

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
                        "transcript": queued_transcript,
                        "use_speaker_recognition": queued_use_speaker_recognition,
                        "transcription_data": queued_transcription_data,
                        "cached_speaker_count": cached_speaker_count,
                        "is_generic": is_generic_downloader or is_from_generic,
                        "wechat_webhook": wechat_webhook,
                        "notification_channel": notification_channel,
                        "notification_webhooks": notification_webhooks,
                        "perf_tracker": tracker,
                        "processing_options": queued_processing_options,
                    }
                )
                logger.info(
                    f"将LLM任务加入队列: {task_id}, 标题: {video_title}, "
                    f"说话人识别: {has_speaker_recognition}, "
                    f"需补层: calibrate={need_calibrated}, summarize={need_summary}"
                )
                # 转录已就绪、LLM 校对/总结进行中 → calibrating（终态由 LLM 阶段写）
                cache_manager.update_task_status(
                    task_id,
                    TaskStatus.CALIBRATING,
                    platform=cache_data.get("platform"),
                    media_id=cache_data.get("media_id"),
                    title=video_title,
                    author=author,
                    download_url=download_url,
                )
            except Exception as exc:
                logger.exception(f"将LLM任务加入队列失败（缓存）: {exc}")
                task_notifier.send_text(f"【LLM任务加入队列失败】{exc}")
                cache_manager.update_task_status(
                    task_id, TaskStatus.FAILED,
                    error_message=f"LLM任务加入队列失败: {exc}",
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
        else:
            logger.info("[缓存检测] ❌ 缓存未命中，准备下载和转录")

            # ==================== 阶段3: 元数据获取（创建下载器实例）====================
            parsed_metadata = None
            metadata_downloader = None
            metadata_obj = None
            download_info_obj = None
            parse_url = url

            # url 可能是外部系统提供的不透明标识符（如 recorder://...），并非真实
            # 可访问的 http/https 地址。这种情况下对 url 发起元数据请求必然会被
            # GenericDownloader 的 SSRF 校验以 "Unsupported URL scheme" 拒绝并抛出
            # InvalidURLError（属于 _TERMINAL_RESOLVER_ERRORS，会被下方 except 直接
            # 透传给用户），导致任务终态失败——即使下方已有 metadata_override 兜底
            # 路径可用。因此在真正发起请求前先判断 url 的 scheme：非 http/https 时
            # 直接跳过元数据探测，让 parsed_metadata 保持 None，自然落入下面的
            # metadata_override（或默认值）兜底分支。
            from urllib.parse import urlparse
            url_scheme = urlparse(url).scheme.lower()

            with tracker.track("metadata"):
                if url_scheme not in ("http", "https"):
                    logger.info(
                        f"[元数据获取] URL scheme 非 http/https（{url_scheme or '空'}），"
                        f"跳过元数据探测，直接使用 metadata_override 兜底: {parse_url}"
                    )
                else:
                    try:
                        logger.info(f"[元数据获取] 创建下载器实例: {parse_url}")
                        metadata_downloader = create_downloader(parse_url)
                        logger.info(
                            f"[元数据获取] 下载器类型: {metadata_downloader.__class__.__name__}"
                        )

                        metadata_obj = metadata_downloader.get_metadata(parse_url)
                        parsed_metadata = {
                            "video_id": metadata_obj.video_id,
                            "video_title": metadata_obj.title,
                            "title": metadata_obj.title,
                            "author": metadata_obj.author,
                            "description": metadata_obj.description,
                            "platform": metadata_obj.platform,
                        }
                        logger.info(
                            f"[元数据获取] 成功: platform={metadata_obj.platform}, "
                            f"video_id={metadata_obj.video_id}, "
                            f"title={metadata_obj.title[:50]}"
                        )
                    except _TERMINAL_RESOLVER_ERRORS:
                        # P0-1：解析终态异常直达用户，不走默认失败路径
                        logger.error("[元数据获取] 解析终态异常，向用户透传")
                        raise
                    except Exception as e:
                        logger.warning(f"[元数据获取] 失败: {e}")
                        parsed_metadata = None
                        metadata_obj = None

            # 合并元数据（metadata_override 作为补充或覆盖）
            if parsed_metadata:
                final_metadata = merge_metadata(parsed_metadata, metadata_override, url)
                video_title = final_metadata.get('title') or final_metadata.get('video_title', '')
                author = final_metadata.get('author', '')
                description = final_metadata.get('description', '')
                # 更新 platform 和 video_id（用完整数据覆盖 URLParser 提取的值）
                platform = final_metadata.get('platform', platform)
                video_id = final_metadata.get('video_id', video_id)
                logger.info(f"[元数据合并] 元数据解析成功，metadata_override 作为补充")
            else:
                # 元数据获取失败，使用 metadata_override 或默认值
                final_metadata = metadata_override or {}
                video_title = final_metadata.get('title') or extract_filename_from_url(url) or "Untitled"
                author = final_metadata.get('author', 'Unknown')
                description = final_metadata.get('description', '')
                logger.info(f"[元数据合并] 元数据解析失败，使用 metadata_override 或默认值")

            media_id = video_id
            is_from_generic = (platform == 'generic')
            logger.info(
                f"[元数据合并] 最终元数据: platform={platform}, video_id={video_id}, "
                f"title={video_title[:50]}, author={author}"
            )

            # 判断是否提供了 download_url
            # 如果提供，说明需要从 download_url 下载，而 url 仅用于元数据解析
            has_separate_download_url = (
                download_url is not None and
                download_url.strip() != ""
            )

            # 下载器准备
            from ...downloaders.generic import GenericDownloader
            download_downloader = None
            if has_separate_download_url:
                download_downloader = GenericDownloader()
            elif metadata_downloader:
                download_downloader = metadata_downloader
            else:
                download_downloader = create_downloader(url)

            # 获取下载信息（仅在需要使用解析URL下载时）
            if not has_separate_download_url and download_downloader:
                try:
                    download_info_obj = download_downloader.get_download_info(parse_url)
                    logger.info(
                        f"[下载信息] 获取成功: platform={platform}, video_id={video_id}"
                    )
                except _TERMINAL_RESOLVER_ERRORS:
                    # P0-1：解析终态异常直达用户，不走默认失败路径
                    logger.error("[下载信息] 解析终态异常，向用户透传")
                    raise
                except Exception as e:
                    logger.warning(f"[下载信息] 获取失败: {e}")
                    download_info_obj = None

            # ========== YouTube API Server 快速路径 ==========
            # 如果提供了 download_url，则跳过 API Server，强制使用 download_url 下载
            if has_separate_download_url:
                logger.info("[youtube-api] download_url provided; skip API Server fast path")
            # 如果是 YouTube URL 且启用了 API Server，使用一次请求获取所有资源
            elif (
                metadata_downloader
                and metadata_downloader.__class__.__name__ == "YoutubeDownloader"
                and hasattr(metadata_downloader, "use_api_server")
                and metadata_downloader.use_api_server
            ):
                logger.info(f"[youtube-api] Using API Server for: {url}")
                try:
                    from ...downloaders.youtube_api_errors import YouTubeApiError

                    # 一次 API 请求获取所有信息（含下载）
                    with tracker.track("download"):
                        api_result = metadata_downloader.fetch_for_transcription(
                            url, use_speaker_recognition
                        )

                    # 将 API 返回的数据作为 parsed_metadata，与 metadata_override 合并
                    api_metadata = {
                        'video_id': api_result["video_id"],
                        'video_title': api_result["video_title"],
                        'title': api_result["video_title"],  # 字段名标准化
                        'author': api_result["author"],
                        'description': api_result["description"],
                        'platform': api_result["platform"],
                    }
                    youtube_merged = merge_metadata(api_metadata, metadata_override, url)

                    video_title = youtube_merged.get('title', '')
                    author = youtube_merged.get('author', '')
                    description = youtube_merged.get('description', '')
                    platform = youtube_merged.get('platform', 'youtube')
                    media_id = youtube_merged.get('video_id', '')

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
                                    "notification_channel": notification_channel,
                                    "notification_webhooks": notification_webhooks,
                                    "perf_tracker": tracker,
                                    "processing_options": processing_options,
                                }
                            )
                            logger.info(f"[youtube-api] LLM task queued: {task_id}")
                        except Exception as exc:
                            logger.exception(
                                f"[youtube-api] Failed to queue LLM task: {exc}"
                            )
                            task_notifier.send_text(f"【LLM任务加入队列失败】{exc}")
                            cache_manager.update_task_status(
                                task_id, TaskStatus.FAILED,
                                error_message=f"LLM任务加入队列失败: {exc}",
                            )
                            return {"status": "failed", "message": f"LLM任务加入队列失败: {exc}"}

                        # 转录就绪、LLM 校对/总结进行中 → calibrating（终态由 LLM 阶段写）
                        cache_manager.update_task_status(
                            task_id,
                            TaskStatus.CALIBRATING,
                            platform=platform,
                            media_id=media_id,
                            title=video_title,
                            author=author,
                            download_url=download_url,
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
                        with tracker.track("transcription"):
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
                                    "notification_channel": notification_channel,
                                    "notification_webhooks": notification_webhooks,
                                    "perf_tracker": tracker,
                                    "processing_options": processing_options,
                                }
                            )
                            logger.info(f"[youtube-api] LLM task queued: {task_id}")
                        except Exception as exc:
                            logger.exception(
                                f"[youtube-api] Failed to queue LLM task: {exc}"
                            )
                            task_notifier.send_text(f"【LLM任务加入队列失败】{exc}")
                            cache_manager.update_task_status(
                                task_id, TaskStatus.FAILED,
                                error_message=f"LLM任务加入队列失败: {exc}",
                            )
                            return {"status": "failed", "message": f"LLM任务加入队列失败: {exc}"}

                        # 转录就绪、LLM 校对/总结进行中 → calibrating（终态由 LLM 阶段写）
                        cache_manager.update_task_status(
                            task_id,
                            TaskStatus.CALIBRATING,
                            platform=platform,
                            media_id=media_id,
                            title=video_title,
                            author=author,
                            download_url=download_url,
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
                    cache_manager.update_task_status(
                        task_id, TaskStatus.FAILED,
                        download_url=download_url, error_message=error_msg,
                    )
                    return {"status": "failed", "message": error_msg}

                except Exception as exc:
                    # 其他异常也不降级
                    error_msg = f"YouTube API Server unexpected error: {exc}"
                    logger.exception(f"[youtube-api] {error_msg}")
                    task_notifier.notify_task_status(display_url, "下载失败", error_msg)
                    cache_manager.update_task_status(
                        task_id, TaskStatus.FAILED,
                        download_url=download_url, error_message=error_msg,
                    )
                    return {"status": "failed", "message": error_msg}

            # ========== 原有逻辑（非 YouTube API Server 路径）==========
            # 已在前面完成元数据解析与下载器准备
            original_downloader = None
            if not download_url:
                original_downloader = metadata_downloader or create_downloader(url)
            else:
                logger.info("已提供 download_url，使用解析的元数据，跳过传统下载器的 get_video_info")
                is_from_generic = (platform == 'generic')

            # 根据 use_speaker_recognition 参数决定处理优先级
            subtitle = None

            if has_separate_download_url:
                # 提供了 download_url，说明用户已有下载地址
                # 跳过字幕获取，直接使用 download_url 进行下载和转录
                logger.info(
                    f"检测到提供了独立的下载地址，跳过字幕获取，直接使用 download_url 进行转录: "
                    f"url={url}, download_url={download_url}"
                )
                subtitle = None
            elif use_speaker_recognition:
                # 如果需要说话人识别，强制跳过平台字幕，直接进行下载转录
                logger.info(f"需要说话人识别，跳过平台字幕获取，强制下载转录: {url}")
                subtitle = None
            else:
                # 只有在不需要说话人识别时，才尝试获取平台字幕
                if metadata_downloader and metadata_downloader.__class__.__name__ == "YoutubeDownloader":
                    logger.info(f"不需要说话人识别，尝试获取YouTube平台字幕: {url}")
                    subtitle = metadata_downloader.get_subtitle(url)
                elif not download_url and original_downloader:
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
                    platform=platform,
                    url=url,
                    media_id=video_id,
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
                            "platform": platform,
                            "media_id": video_id,
                            "video_title": video_title,
                            "author": author,
                            "description": description,
                            "transcript": subtitle,
                            "use_speaker_recognition": False,  # 平台字幕没有说话人信息
                            "is_generic": is_generic_downloader or is_from_generic,
                            "wechat_webhook": wechat_webhook,
                            "notification_channel": notification_channel,
                            "notification_webhooks": notification_webhooks,
                            "perf_tracker": tracker,
                            "processing_options": processing_options,
                        }
                    )
                    logger.info(
                        f"将LLM任务加入队列（平台字幕）: {task_id}, 标题: {video_title}"
                    )
                except Exception as exc:
                    logger.exception(f"将LLM任务加入队列失败（平台字幕）: {exc}")
                    task_notifier.send_text(f"【LLM任务加入队列失败】{exc}")
                    cache_manager.update_task_status(
                        task_id, TaskStatus.FAILED,
                        error_message=f"LLM任务加入队列失败: {exc}",
                    )
                    return {"status": "failed", "message": f"LLM任务加入队列失败: {exc}"}

                result = {
                    "status": "success",
                    "message": "使用平台字幕成功",
                    "data": {
                        "video_title": video_title,
                        "author": author,
                        "transcript": subtitle,
                    },
                }
                # 转录就绪、LLM 校对/总结进行中 → calibrating（终态由 LLM 阶段写）
                cache_manager.update_task_status(
                    task_id,
                    TaskStatus.CALIBRATING,
                    platform=platform,
                    media_id=video_id,
                    title=video_title,
                    author=author,
                    download_url=download_url,
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

                # 下载文件
                local_file = None
                if has_separate_download_url:
                    actual_download_url = download_url or url
                    logger.info(f"使用 GenericDownloader 下载文件: {actual_download_url}")
                    # 从 URL 提取文件名
                    from urllib.parse import urlparse, unquote
                    parsed_url = urlparse(actual_download_url)
                    path = unquote(parsed_url.path)
                    filename = os.path.basename(path)
                    if not filename:
                        filename = f"{platform}_{video_id}.mp4"

                    if download_info_obj and download_info_obj.filename:
                        filename = download_info_obj.filename

                    with tracker.track("download"):
                        local_file = download_downloader.download_file(actual_download_url, filename)
                else:
                    # 确保下载信息已获取
                    if download_info_obj is None and download_downloader:
                        try:
                            download_info_obj = download_downloader.get_download_info(parse_url)
                        except _TERMINAL_RESOLVER_ERRORS:
                            # P0-1：解析终态异常直达用户，不走默认失败路径
                            logger.error("[下载信息] 解析终态异常，向用户透传")
                            raise
                        except Exception as e:
                            logger.warning(f"[下载信息] 获取失败: {e}")

                    # 检查是否已有本地文件
                    if download_info_obj and download_info_obj.downloaded and download_info_obj.local_file:
                        local_file = download_info_obj.local_file
                        logger.info(f"使用已下载的本地文件: {local_file}")
                    else:
                        download_info_url = download_info_obj.download_url if download_info_obj else None
                        filename = download_info_obj.filename if download_info_obj else None

                        original_downloader = download_downloader or create_downloader(url)
                        if hasattr(original_downloader, "download_video_with_priority") and (
                            "youtube.com" in url or "youtu.be" in url
                        ):
                            logger.info(f"YouTube视频，使用优先级下载（yt-dlp优先）: {url}")
                            legacy_video_info = {
                                "video_id": video_id,
                                "video_title": video_title,
                                "author": author,
                                "description": description,
                                "platform": platform,
                                "download_url": download_info_url,
                                "filename": filename,
                            }
                            with tracker.track("download"):
                                local_file = original_downloader.download_video_with_priority(
                                    url, legacy_video_info
                                )
                        elif download_info_url and filename:
                            with tracker.track("download"):
                                local_file = original_downloader.download_file(download_info_url, filename)
                        else:
                            error_msg = f"无法获取下载信息: {url}"
                            logger.error(error_msg)
                            task_notifier.notify_task_status(
                                display_url, "下载失败", error_msg, title=video_title, author=author
                            )
                            cache_manager.update_task_status(
                                task_id, TaskStatus.FAILED,
                                download_url=download_url, error_message=error_msg,
                            )
                            return {"status": "failed", "message": error_msg}

                if not local_file:
                    error_msg = f"下载文件失败: {url}"
                    logger.error(error_msg)
                    task_notifier.notify_task_status(
                        display_url, "下载失败", error_msg, title=video_title, author=author
                    )
                    cache_manager.update_task_status(
                        task_id, TaskStatus.FAILED,
                        download_url=download_url, error_message=error_msg,
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

                    # 根据是否需要说话人识别选择转录器（用 PerfTracker 记录转录阶段耗时）
                    with tracker.track("transcription"):
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
                                "notification_channel": notification_channel,
                                "notification_webhooks": notification_webhooks,
                                "perf_tracker": tracker,
                                "processing_options": processing_options,
                            }
                        )
                        logger.info(
                            f"将LLM任务加入队列（常规转录）: {task_id}, 标题: {video_title}"
                        )
                    except Exception as exc:
                        logger.exception(f"将LLM任务加入队列失败（常规转录）: {exc}")
                        task_notifier.send_text(f"【LLM任务加入队列失败】{exc}")
                        cache_manager.update_task_status(
                            task_id, TaskStatus.FAILED,
                            error_message=f"LLM任务加入队列失败: {exc}",
                        )
                        return {"status": "failed", "message": f"LLM任务加入队列失败: {exc}"}

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

                # 转录就绪、LLM 校对/总结进行中 → calibrating（终态由 LLM 阶段写）
                cache_manager.update_task_status(
                    task_id,
                    TaskStatus.CALIBRATING,
                    platform=platform,
                    media_id=video_id,
                    title=video_title,
                    author=author,
                    download_url=download_url,
                )

        return result
    except Exception as exc:
        logger.exception(f"转录处理异常: {exc}")
        # 任务失败时输出已记录的性能摘要
        tracker.log_summary()
        display_url = url
        get_notification_router().notify_task_status(
            url=display_url, status="转录异常", error=str(exc),
            channel_name=notification_channel, webhooks=notification_webhooks,
        )
        cache_manager.update_task_status(
            task_id, TaskStatus.FAILED, download_url=download_url,
            error_message=f"转录任务异常: {exc}",
        )
        return {
            "status": "failed",
            "message": f"转录任务异常: {exc}",
            "error": str(exc),
        }
    finally:
        # 终态清理：删除本任务在 temp 下的全部文件（源视频 + 提取音频 + 中间件）。
        # 临时文件只是转录的输入，转录段结束即可清，不依赖 LLM 阶段终态（codex#9）。
        # 失败打 WARNING 不中断主流程；务必 clear_current_task 避免线程复用串号（codex#1）。
        try:
            temp_manager.clean_up_task(task_id)
        except Exception as cleanup_exc:
            logger.warning(
                f"清理任务临时文件失败（不影响主流程）: {task_id}, 错误: {cleanup_exc}"
            )
        finally:
            temp_manager.clear_current_task()
            temp_manager.mark_done(task_id)


def process_llm_queue():
    """处理LLM队列的后台任务（委托给 llm_ops 模块）"""
    from .llm_ops import process_llm_queue as _process_llm_queue
    _process_llm_queue()


def _handle_llm_task(llm_task: dict):
    """Worker entry for processing a single LLM task（委托给 llm_ops 模块）"""
    from .llm_ops import _handle_llm_task as _handle
    _handle(llm_task)
