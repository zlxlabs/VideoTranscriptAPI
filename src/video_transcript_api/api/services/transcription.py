import asyncio
import datetime
import os
import threading
import time
from typing import Optional, Dict, Any

from fastapi import HTTPException, Header, Request
from pydantic import BaseModel, Field, StrictBool, field_validator
from ..processing_options import ProcessingOptions, normalize_processing_options

from ..context import (
    get_audit_logger,
    get_cache_manager,
    get_config,
    get_executor,
    get_inflight_registry,
    get_llm_queue,
    get_logger,
    get_runtime,
    get_task_queue,
    get_temp_manager,
    get_user_manager,
    lazy_resource,
    run_with_runtime,
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


def _extract_speaker_labels(dialogs) -> list[str]:
    """Extract stable speaker labels without dropping numeric ID zero,
    skipping empty-text dialogs.

    委托给 SpeakerInferencer.extract_speaker_labels（本地 codex review
    第 7 轮 H7）：读侧（本函数，供分层缓存预检计算 input_fingerprint 用）
    与写侧（SpeakerAwareProcessor.process() 基于 _coerce_dialogs 结果推导
    speakers 列表）必须使用同一份说话人标签提取逻辑——否则同一份 dialogs
    在"某个说话人只在空文本 dialog 里出现"这种边界输入上，两侧算出的
    说话人集合会不一致，进而让 input_fingerprint 永久不同：预检误判为
    "从未处理过"，明明已经缓存的说话人映射每次都被当作缓存未命中重新
    触发一轮 LLM 推断，并用这轮结果覆写已经落盘的产物。局部 import
    沿用本函数原有的懒加载风格（与下方 process_transcription 内部对
    同一个类的局部 import 保持一致，避免为一次纯计算在模块导入期就拉起
    整条 llm.core 依赖链）。
    """
    from ...llm.core.speaker_inferencer import SpeakerInferencer
    return SpeakerInferencer.extract_speaker_labels(dialogs)
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

logger = lazy_resource(get_logger)
config = lazy_resource(get_config)
user_manager = lazy_resource(get_user_manager)
audit_logger = lazy_resource(get_audit_logger)
cache_manager = lazy_resource(get_cache_manager)
task_queue = lazy_resource(get_task_queue)
llm_task_queue = lazy_resource(get_llm_queue)
executor = lazy_resource(get_executor)


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
        logger.warning("Authorization token rejected")
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
            notification_webhooks = task.get("notification_webhooks") or {}
            download_url = task.get("download_url")
            metadata_override = task.get("metadata_override")
            # 处理深度开关（只转录/转录+校对/全流程）：task dict 里缺失时按全流程兜底，
            # 与 normalize_processing_options(None) 的语义保持一致。
            processing_options = normalize_processing_options(task.get("processing_options"))

            try:
                cache_manager.update_task_status(task_id, TaskStatus.PROCESSING, download_url=download_url)

                runtime = get_runtime()

                def run_and_finalize(
                    task_id=task_id,
                    url=url,
                    use_speaker_recognition=use_speaker_recognition,
                    wechat_webhook=wechat_webhook,
                    download_url=download_url,
                    metadata_override=metadata_override,
                    notification_channel=notification_channel,
                    notification_webhooks=dict(notification_webhooks),
                    processing_options=dict(processing_options),
                ):
                    try:
                        process_transcription(
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
                        logger.info(f"任务完成: {task_id}")
                    except Exception as exc:
                        logger.exception(
                            f"任务处理失败: {task_id}, URL: {url}, 错误: {exc}"
                        )
                        cache_manager.update_task_status(
                            task_id, TaskStatus.FAILED,
                            error_message=f"转录任务失败: {exc}",
                        )
                        get_notification_router().notify_task_status(
                            url=url, status="转录失败", error=str(exc),
                            channel_name=notification_channel,
                            webhooks=notification_webhooks,
                        )

                future = executor.submit(
                    run_with_runtime,
                    runtime,
                    run_and_finalize,
                )
                runtime.track_future(future, task_id=task_id)
                logger.info(f"任务已提交到线程池: {task_id}, URL: {url}")
            except Exception as exc:
                logger.exception(
                    f"提交任务到线程池失败: {task_id}, URL: {url}, 错误: {exc}"
                )
                # Y1 修复（PR3 review hardening 加固轮）：在途任务登记表释放提到
                # update_task_status 之前——与 R3 先例同一套顺序原则（见
                # _handoff_to_llm_stage 里 put() 失败分支的注释：release 提到
                # 通知之前）：release() 本身不会抛异常（InflightRegistry 内部
                # 一次加锁的 dict.pop，幂等，见其文档），会抛异常的只有
                # get_runtime() 这一步（进程内 contextvar 读取，理论上不该在
                # 这条路径失败，仍用 try/except 兜底），因此整体排在下面的
                # update_task_status 之前。旧顺序里 update_task_status 排在
                # release 之前：这是一次会触达数据库的 CAS 写入，一旦抛出未
                # 预期的异常（如 DB 层故障），会直接跳出这个 except 块，下面
                # 的 release 永远不会执行——这个 task_id 占用的
                # "transcription" 桶名额永久无法回收；此时 future 从未真正
                # 创建，track_future 的完成回调（release 的另一个主挂点）也
                # 不会触发补救，连续故障会让可用配额持续缩水，最终耗尽到
                # /api/transcribe 恒 503，直到进程重启。release 提前执行不会
                # 引入新的失败面。
                try:
                    get_runtime().inflight_registry.release("transcription", task_id)
                except Exception:
                    logger.exception(f"释放在途任务登记表名额失败: {task_id}")
                # G1 修复（CI review 第 2 轮 major）：此前这里认为
                # "update_task_status 自身的异常也不能向上传播"——重新核实后
                # 发现这个理由不成立：下面的 task_queue.task_done() 是外层
                # try(305) 的 finally（见下方 403 行），不在这个内层
                # try/except 的保护范围内，重新抛出不会跳过它；原始异常 exc
                # 也已经在上面（370 行）被 logger.exception 记录过，不会被
                # "掩盖"。此前只记日志、不重新抛出，函数就此吞掉终态写库
                # 异常（这个 except 已经是提交失败分支的最后一步）——任务
                # 实际停在 PROCESSING 之前的非终态，只能靠运行期对账（最长
                # ~27h）才会被发现，违反"repository 清理/保存失败必须抛错"
                # 的设计条款。改为记日志后重新抛出：异常传出这个 except 块，
                # 被下面的 finally 放行后，传播到本函数最外层的 `except
                # Exception as exc:`（process_task_queue 自身的泵循环兜底，
                # 本来就设计成容忍单次异常、sleep(1) 后继续消费下一项）。
                # 与 llm_ops.py 的 process_llm_queue 泵循环不同：那里的
                # task_done() 不在 finally 里，重新抛出会跳过它、造成队列
                # 记账永久泄漏，因此保留 log-only；这里 task_done() 由
                # finally 保护，重新抛出不会破坏队列记账，可以采用与其它
                # 站点一致的"清理后重抛"。
                try:
                    cache_manager.update_task_status(
                        task_id, TaskStatus.FAILED,
                        error_message=f"提交任务失败: {exc}",
                    )
                except Exception:
                    logger.exception(f"提交失败后写入 failed 终态异常: {task_id}")
                    raise
            finally:
                task_queue.task_done()
        except Exception as exc:
            logger.exception(f"任务队列处理器异常: {exc}")
            await asyncio.sleep(1)


def _register_llm_handoff(task_id: str) -> None:
    """转录 worker 把任务通过 llm_task_queue.put() 交给 LLM 阶段前，先登记
    进 inflight_registry 的 "llm" 桶（本地 codex review 第 13 轮唯一发现，
    详见 _InflightTaskRegistry.register_internal 的方法文档）。

    调用时机是这个函数存在的唯一理由：必须在 put() 之前调用，
    且仍在 process_transcription 所在的 worker 线程内——此时该 task_id 仍占着
    "transcription" 桶的名额（future 尚未完成），两桶之间因此无缝
    衔接，不会出现"transcription 名额已释放、llm 名额尚未登记"的真
    空窗口，运行期对账（app.py::_periodic_maintenance 的 all_task_ids()
    排除名单）任何时刻查询都能看到这个任务。

    用 get_inflight_registry() 而非 get_runtime().inflight_registry：
    process_transcription 除了生产环境的 run_with_runtime worker 线程，
    也被大量既有单测（如 tests/integration/test_layered_cache.py、
    tests/features/test_transcription_flow_regression.py）在未绑定
    runtime 的主线程里直接调用——get_runtime() 在这种环境下会抛
    RuntimeError，get_inflight_registry() 则按设计优雅降级为一次性的
    空登记表（不缓存单例，不影响任何真实背压/对账逻
    辑），保证这里的登记调用在测试环境下是安全的纯
    粹 no-op，不会连带让下面真正重要的 llm_task_queue.put() 交接失败。
    """
    get_inflight_registry().register_internal("llm", task_id)


def _handoff_to_llm_stage(
    task_id: str,
    llm_payload: dict,
    *,
    calibrating_status_kwargs: dict,
    task_notifier,
    log_context: str,
) -> Optional[dict]:
    """把已完成转录、待补 LLM 层的任务交给 LLM 阶段——转录到 LLM 的五处内部
    交接（process_transcription 的缓存复用分支、YouTube API Server 的两条快速路
    径、平台字幕分支、常规下载转录分支）共用同一份顺序与失败处理
    （本地 codex review 第 15 轮唯一发现）。

    调用约定：转录 worker 已经拿到可以喂给 LLM 阶段的产物（transcript/
    transcription_data 等），装好 llm_payload 后调用本函数。返回 None 表示交接
    完全成功（CALIBRATING 已落库、任务已入队）——调用方据此继续构造并返回自己的
    "success" 响应；返回非 None 的 dict 表示交接未完成——调用方必须原样 return
    这个 dict，绝不能再假装成功继续往下走（这正是重排前第一处分支的 bug：
    CALIBRATING 写入异常被当成入队失败处理、写了 failed，函数却仍然 return 了
    "status": "success"）。

    重排后的顺序（本次修复的核心）：先写 CALIBRATING、检查 CAS 返回值，赢了才
    register_internal + put()——不是旧版"先 put 入队、再写 CALIBRATING"。旧版的
    问题：queue.Queue.put() 一旦成功，队列消费者立刻可能取走任务开始处理，不可
    撤回；这之后才发现 CALIBRATING 写入失败或被终态黏性拒绝（CAS False）的话，
    任务对外已经呈现某个终态（通常是 failed 或维持原有 success/failed），LLM
    worker 却仍会继续处理这个已经被放弃的任务——烧 token、写共享产物，最终 LLM
    阶段的 success CAS 因终态黏性被静默拒绝，一次不可观测的重复劳动。重排后
    CALIBRATING 写入是唯一"是否交给 LLM 阶段"的关卡，赢了才有资格入队。

    三种结果分支：
    1. CALIBRATING 写入本身抛异常：不注册 llm 名额（还没走到那一步）、不入队；
       尝试收敛写 failed；这次写入成功则返回 failed 响应。这次写入若也失败
       （G1 修复，CI review 第 2 轮 major）：记日志后重新抛出，不再静默返回
       failed 响应假装已经收敛——异常沿 process_transcription 的外层兜底继续
       传播，最终可被 worker future 观察到（详见下面 except 分支的注释）。
    2. CALIBRATING 写入返回 False（终态黏性：任务已被运行期对账/关闭清算/另一
       次并发写入判定为 success/failed）：不入队、不注册 llm 名额、不覆盖已有
       终态；按任务当前实际终态如实返回，而不是硬编码 failed。
    3. CALIBRATING 写入成功（True）：register_internal 登记 llm 名额，随后
       put()。put() 抛异常：释放 llm 名额、收敛写 failed。这次写入若也失败
       （G1 修复同上）：记日志后重新抛出，不返回 failed 响应；写入成功则
       返回 failed 响应。put() 成功：返回 None，交接完成。

    Args:
        task_id: 任务 ID。
        llm_payload: 传给 llm_task_queue.put() 的完整任务字典，由调用方按各自
            分支的产物组装（结构因分支而异，本函数不关心内容）。
        calibrating_status_kwargs: 透传给 update_task_status(...,
            TaskStatus.CALIBRATING, **kwargs) 的关键字参数（platform/media_id/
            title/author/download_url），由调用方按各自分支的变量名组装。
        task_notifier: 绑定了通知渠道的任务通知器，put() 失败时用于
            task_notifier.send_text 告警（与重排前的既有行为一致）。
        log_context: 日志里标识调用分支的简短中文标签（如"缓存""youtube-api"
            "平台字幕""常规转录"），拼进本函数内部的日志文案，方便按分支定位
            问题。

    Returns:
        None 表示交接成功；否则返回调用方应直接 return 的 failed 响应 dict
        （含 "status"/"message" 两个键）。
    """
    try:
        calibrating_written = cache_manager.update_task_status(
            task_id, TaskStatus.CALIBRATING, **calibrating_status_kwargs,
        )
    except Exception as status_exc:
        logger.exception(
            f"CALIBRATING 状态写入异常，放弃 LLM 交接（{log_context}）: "
            f"{task_id}, 错误: {status_exc}"
        )
        try:
            cache_manager.update_task_status(
                task_id, TaskStatus.FAILED,
                error_message=f"任务状态写入异常: {status_exc}",
            )
        except Exception:
            # G1 修复（CI review 第 2 轮 major）：此前这里只记日志、不重新
            # 抛出，函数继续往下 return failed 字典——调用方（process_
            # transcription 的各分支）会把这当成"已经妥善收敛"，任务实际
            # 停在 CALIBRATING 之前的非终态（多为 PROCESSING），只能靠运行
            # 期对账（最长 ~27h）才会被发现，违反"repository 清理/保存失败
            # 必须抛错"的设计条款。这里还没有走到 register_internal（还未
            # 注册 llm 名额），没有需要额外释放的配额；改为记日志后重新
            # 抛出：异常会传出这个 except 块（不会被本函数其它 except 再次
            # 捕获），一路传播到 process_transcription 最外层的 `except
            # Exception as exc:`——那里会再尝试一次 FAILED 写入，如果同样
            # 失败会再抛出，最终传播到 run_and_finalize/线程池 future，被
            # RuntimeContext.track_future 的完成回调观察到（future 完成即
            # 释放 inflight_registry 名额，不依赖终态写入是否成功）。
            logger.exception(f"收敛 failed 终态写入也失败（{log_context}）: {task_id}")
            raise
        return {"status": "failed", "message": f"任务状态写入异常: {status_exc}"}

    if not calibrating_written:
        current_task = cache_manager.get_task_by_id(task_id)
        current_status = (current_task or {}).get("status") or "unknown"
        logger.warning(
            f"CALIBRATING 写入被终态黏性拦截，任务已被外部终态化，跳过 LLM 交接"
            f"（{log_context}）: {task_id}, 当前状态: {current_status}"
        )
        reported_status = (
            current_status
            if current_status in (TaskStatus.SUCCESS, TaskStatus.FAILED)
            else "failed"
        )
        return {
            "status": reported_status,
            "message": f"任务已被并发流程终态化（当前状态: {current_status}），跳过 LLM 交接",
        }

    _register_llm_handoff(task_id)
    try:
        llm_task_queue.put(llm_payload)
    except Exception as exc:
        logger.exception(f"将LLM任务加入队列失败（{log_context}）: {exc}")
        # R3 修复（PR3 review hardening）：release 提到通知之前——release()
        # 是 InflightRegistry 内部一次加锁的 dict.pop，幂等且不会抛异常
        # （见 api/context.py::InflightRegistry.release 的文档），而
        # task_notifier.send_text 是外部 webhook 调用，可能因超时/限流抛
        # 异常。旧顺序里通知排在 release 之前：通知一旦抛异常，会直接跳出
        # 这个 except 块，release 和下面的 FAILED 收敛写入都不会执行——
        # "llm" 桶里的登记条目永久漏掉一个名额（且没有 future 完成回调能
        # 补救，因为 put() 从未成功、根本没有对应的 future），每次这种
        # 组合故障都会让可用配额缩水一个，最终耗尽到 recalibrate 恒 503，
        # 直到进程重启。release 不会抛异常，提前执行不会引入新的失败面。
        get_inflight_registry().release("llm", task_id)

        # W5 修复（PR3 review hardening 二轮）：FAILED 终态写入提到通知之前——
        # 与 R2/K3（llm_ops.py 里成功/失败两侧的同一顺序重排，见该文件
        # _handle_llm_task 的注释）同一套顺序原则：先写终态 CAS 并检查返回值，
        # 终态落定后再尝试通知；通知放进独立 try/except 兜住，异常只记日志，
        # 不影响已经写定的终态。旧顺序里通知（task_notifier.send_text，外部
        # webhook 调用，可能因超时/限流抛异常）排在终态写入之前，一旦抛异常会
        # 直接跳出这个 except 块，下面的 FAILED 写入永远不会执行——任务永久停在
        # CALIBRATING（非终态），客户端只能一直轮询、再也等不到结果；既有测试
        # 还反过来锁死了"通知异常会传播"这个错误行为，本次一并修正（见
        # tests/features/test_transcription_flow_regression.py）。
        try:
            fail_status_written = cache_manager.update_task_status(
                task_id, TaskStatus.FAILED,
                error_message=f"LLM任务加入队列失败: {exc}",
            )
        except Exception:
            # G1 修复（CI review 第 2 轮 major）：此前这里只记日志、不重新
            # 抛出，函数继续往下发通知 + return failed 字典——调用方会把
            # 这当成"已经妥善收敛"直接 return，任务实际停在 CALIBRATING
            # （已经在上面成功写入过一次），既不是 failed 也不会再被任何
            # 路径推进，只能靠运行期对账（最长 ~27h）才会被发现。llm 名额
            # （release("llm", task_id)）已经在上面完成，这里不需要额外
            # 清理；改为记日志后重新抛出，异常会传出这个 except 块，一路
            # 传播到 process_transcription 最外层的 `except Exception as
            # exc:`——那里会再尝试一次 FAILED 写入，如果同样失败会再抛出，
            # 最终传播到 run_and_finalize/线程池 future，被 RuntimeContext.
            # track_future 的完成回调观察到（future 完成即释放
            # inflight_registry 名额，不依赖终态写入是否成功）。L1 修复
            # （CI review 第 5 轮 P1）：不再在 finally 里无条件发通知——写入
            # 本身抛异常时终态未落定，这里直接向上抛出，不发任何确定性的
            # 失败通知。
            logger.exception(f"收敛 failed 终态写入异常（{log_context}）: {task_id}")
            raise

        if fail_status_written:
            # M1 修复（PR3 review hardening 收尾轮）：CAS==True 才是本次调用的
            # 真正胜者——只有这个分支能确定"是本次把任务写成 failed 的"，失败
            # 通知严格只挂在这里。
            logger.info(f"任务状态已更新为 failed（{log_context}）: {task_id}")
            try:
                task_notifier.send_text(f"【LLM任务加入队列失败】{exc}")
            except Exception:
                logger.exception(
                    f"入队失败通知发送失败（任务终态已落库，不影响任务结果，"
                    f"{log_context}）: {task_id}"
                )
            return {"status": "failed", "message": f"LLM任务加入队列失败: {exc}"}

        # M1 修复（PR3 review hardening 收尾轮）：CAS 返回 False 只代表"这次
        # 没有写入"，无法区分"任务行已是 success/failed 终态"与"任务行根本
        # 不存在"——两种情况下 update_task_status 的 UPDATE ... WHERE task_id=?
        # AND status NOT IN ('success','failed') rowcount 都是 0。因此除
        # success 分支要如实改写返回值外，其余一律不再发失败通知：已是 failed
        # 的话，真正的 CAS 胜者早已发过一次；行不存在/unknown 的话，不为不存在
        # 的终态编造通知（上一轮 L1 修复遗留的口子：曾经对"非 success"一律继续
        # 发通知，未做这个区分）。
        current_task = cache_manager.get_task_by_id(task_id)
        current_status = (current_task or {}).get("status") or "unknown"
        logger.warning(
            f"任务状态 CAS 写入 failed 失败（任务已处于终态 "
            f"{current_status}，未被本次异常覆盖，跳过失败通知，{log_context}）: {task_id}"
        )
        if current_status == TaskStatus.SUCCESS:
            return {
                "status": TaskStatus.SUCCESS,
                "message": f"任务已被并发流程标记为 success，跳过失败通知（{log_context}）",
            }
        return {"status": "failed", "message": f"LLM任务加入队列失败: {exc}"}

    return None


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
    processing_options = normalize_processing_options(processing_options)
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

        def _fail_task_and_notify(
            error_msg: str, *, notify_status: str = "下载失败",
            title: Optional[str] = None, author_name: Optional[str] = None,
        ) -> dict:
            """终态写入 + 失败通知的统一顺序（W5 修复，PR3 review hardening
            二轮，顺手排查全仓同类站点后一并修复的 4 处下载/字幕失败分支之一）：
            先写 FAILED 终态并检查 CAS 返回值，终态落定后再尝试通知，通知放进
            独立 try/except 兜住——通知（task_notifier.notify_task_status，最终
            经 NotificationRouter 转发到各渠道，理论上可能因超时/限流失败）绝不
            能因为自身抛异常而跳过终态写入，否则任务会永久停在非终态（此前
            PROCESSING/尚未进入 CALIBRATING），客户端只能一直轮询、永远等不到
            结果，与 _handoff_to_llm_stage 内 put() 失败分支同一根因、同一处
            修复。闭包读取本函数作用域内的 task_id/cache_manager/task_notifier/
            display_url/download_url，避免每个下载失败分支重复抄一遍这段
            顺序 + 日志样板代码。

            G1 修复（CI review 第 2 轮 major）：FAILED 终态写入本身若抛异常，
            此前只记日志、不重新抛出，函数正常返回——调用方（本文件十来处
            下载/字幕失败分支）会把这当成"已经妥善收敛"，任务实际停在写入
            FAILED 之前的非终态（多为 PROCESSING），只能靠运行期对账（最长
            ~27h）才会被发现。现在改为记日志后重新抛出：本函数的全部调用点
            都是 process_transcription 内部的裸调用（无局部 try/except 吞掉
            普通 Exception），异常会一路传播到 process_transcription 最外层
            的 `except Exception as exc:`——那里会再尝试一次 FAILED 写入，
            如果同样失败会再抛出，最终传播到 run_and_finalize/线程池
            future，被 RuntimeContext.track_future 的完成回调观察到（future
            完成即释放 inflight_registry 名额，不依赖终态写入是否成功）。

            L1 修复（CI review 第 5 轮 P1）：通知不再放在无条件执行的
            finally 里——旧版无论 FAILED 写入抛异常还是 CAS 返回 False（任务
            已被并发流程终态化，例如已经写成功），都会照发一条确定性的失败
            通知，与权威终态自相矛盾。现在通知只在 CAS 明确返回 True（终态
            真的落定为 failed）之后发送；写入抛异常直接向上抛出、不发任何
            通知；CAS 返回 False 时读取既有终态，如果已经是 success 则同样不
            发失败通知，并把返回值如实改成 success（不再硬编码
            failed）——调用方从 `_fail_task_and_notify(...); return
            {"status": "failed", ...}` 两行硬编码，改为直接
            `return _fail_task_and_notify(...)`，复用这里算出的真实终态。

            Returns:
                调用方应直接 return 的响应 dict（含 "status"/"message" 两个
                键）：CAS 写入成功时为 {"status": "failed", ...}；CAS 返回
                False 且既有终态已是 success 时为 {"status": "success",
                ...}；CAS 写入抛异常则不返回，异常向上传播。"""
            try:
                fail_status_written = cache_manager.update_task_status(
                    task_id, TaskStatus.FAILED,
                    download_url=download_url, error_message=error_msg,
                )
            except Exception:
                logger.exception(f"收敛 failed 终态写入异常: {task_id} ({error_msg})")
                raise

            if fail_status_written:
                # M1 修复（PR3 review hardening 收尾轮）：CAS==True 才是本次调用
                # 的真正胜者，失败通知严格只挂在这里。
                logger.info(f"任务状态已更新为 failed: {task_id} ({error_msg})")
                try:
                    task_notifier.notify_task_status(
                        display_url, notify_status, error_msg,
                        title=title, author=author_name,
                    )
                except Exception:
                    logger.exception(
                        f"失败通知发送失败（任务终态已落库，不影响任务结果）: {task_id}"
                    )
                return {"status": "failed", "message": error_msg}

            # M1 修复（PR3 review hardening 收尾轮）：CAS 返回 False 无法区分
            # "任务行已是 success/failed 终态"与"任务行根本不存在"——两种情况
            # 下 update_task_status 的 UPDATE ... WHERE task_id=? AND status
            # NOT IN ('success','failed') rowcount 都是 0。因此除 success 分支
            # 要如实改写返回值外，其余一律不再发失败通知：已是 failed 的话，
            # 真正的 CAS 胜者早已发过一次；行不存在/unknown 的话，不为不存在的
            # 终态编造通知（上一轮 L1 修复遗留的口子：曾经对"非 success"一律
            # 继续发通知，未做这个区分）。
            current_task = cache_manager.get_task_by_id(task_id)
            current_status = current_task.get("status") if current_task else "unknown"
            logger.warning(
                f"任务状态 CAS 写入 failed 失败(任务已处于终态 {current_status}，"
                f"未被本次异常覆盖，跳过失败通知): {task_id} ({error_msg})"
            )
            if current_status == TaskStatus.SUCCESS:
                return {
                    "status": TaskStatus.SUCCESS,
                    "message": f"任务已被并发流程标记为 success，跳过失败通知（当前状态: {current_status}）",
                }
            return {"status": "failed", "message": error_msg}

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
            # llm_status.json 是 _save_llm_results 整个多文件落盘序列里最后
            # 才写入的那一个（校对文本 -> 总结 -> 结构化数据 -> 状态文件），
            # 天然就是这次 LLM 处理“已完整提交”的标记。中途失败（例如状态
            # 文件写入本身失败，或更早的某个产物写入失败直接中止了整段
            # 保存）都会让本轮的 llm_calibrated.txt 停留在磁盘上，却永远等
            # 不到这份状态文件——has_llm_calibrated=True 但
            # cached_calibration_status 仍是 None。此前的判定只看文件是否
            # 存在，会把这种半提交产物（甚至可能是全降级 NONE 的兜底格式化
            # 原文）误判为“层已满足”，永久跳过重试（本地 codex review 第 16
            # 轮 Q2）。
            #
            # 兼容性说明：llm_status.json（诚实状态模型）晚于
            # llm_calibrated.txt 落地——在它上线之前保存成功的旧缓存也会
            # 呈现“calibrated 存在、status 缺失”这同一种文件形状，与半提交
            # 在磁盘上完全无法区分（没有时间戳/版本标记可用；但上线前的旧
            # 代码从不会在校对全降级为 NONE 时落盘任何文件，所以旧数据只
            # 可能是真实的 full/partial 结果，不会是伪装成成功的失败产物）。
            # 这里没有引入按 llm_processed.json 等尾部产物反推“是不是旧
            # 数据”的第二套判定——那套推断本身也不可靠（比如从未启用过说话
            # 人识别的普通任务压根不会有 llm_processed.json 可供参照），
            # 刻意选择统一保守处理：状态文件缺失一律视为未确认完成，触发
            # 一次真实重新校对。旧缓存因此最多被重新校对一次，且这一次会
            # 自然补写 llm_status.json，之后同一媒体的请求即可正常命中——
            # 是一次性代价，不是数据损坏。
            calibrated_layer_satisfied = (
                has_llm_calibrated
                and cached_calibration_status is not None
                and cached_calibration_status != CalibrationStatus.DISABLED
                and cached_calibration_status != CalibrationStatus.NONE
            )
            calibrate_requested = processing_options.get("calibrate", True)
            need_calibrated = calibrate_requested and not calibrated_layer_satisfied
            need_summary = processing_options.get("summarize", True) and not has_llm_summary
            infer_speaker_names_requested = processing_options.get(
                "infer_speaker_names", True
            )
            need_speaker_names = False
            if infer_speaker_names_requested and has_speaker_recognition:
                from ...llm.core.speaker_inferencer import SpeakerInferencer
                from .llm_ops import (
                    structured_artifact_is_refreshable,
                    structured_dialogs_consistent_with_mapping,
                )

                dialogs = (
                    transcription_data.get("segments", [])
                    if isinstance(transcription_data, dict)
                    else (transcription_data or [])
                )
                speakers = _extract_speaker_labels(dialogs)
                if speakers:
                    fingerprint = SpeakerInferencer.input_fingerprint(speakers, dialogs)
                    current_speaker_mapping = cache_manager.get_speaker_mapping(
                        cache_data.get("platform"),
                        cache_data.get("media_id"),
                        input_fingerprint=fingerprint,
                        speakers=speakers,
                    )
                    cached_structured_for_names = cache_data.get("llm_processed")
                    structured_refreshable = structured_artifact_is_refreshable(
                        cached_structured_for_names
                    )
                    # need_speaker_names 判定条件表（映射状态 × 结构化产物可刷新性 ×
                    # 校对完成状态 → 是否排队）。这个判定改了很多轮
                    # （X1/G2/H2/J1/J2……），历史论证已归档进各自的
                    # commit/复盘文档，这里只保留对下一个读者有用的最终结论：
                    #
                    # structured_refreshable 现在由
                    # llm_ops.structured_artifact_is_refreshable 的 schema 层计算
                    # （J2 修复，本地增量复核第 3 轮）：不再是简单的
                    # isinstance(x, dict)——还要求 speaker_mapping/
                    # dialogs 字段完整、dialogs 非空、且每条 dialog 都带非空
                    # speaker_id。与 llm_ops._refresh_speaker_names_in_existing_
                    # structured_artifact 真正尝试写入前的判定共用同一份实现，排队
                    # 侧因此不会再对"存在但不可刷新"的产物（空
                    # dict、缺字段、旧 schema、混合 schema、空 dialogs）误判为
                    # 可刷新——那种误判此前会排队烧一次 LLM 推断，
                    # helper 侧再静默跳过，结果永远不可见。
                    # mapping 层校验（speaker_id 是否能在新映射里解析出姓名）依赖
                    # 本轮尚未推断出的映射，只有 helper 侧真正拿到映射后
                    # 才判定得了，排队侧不传 mapping，只做 schema 层判定。
                    #
                    # 1. 映射缺失（current_speaker_mapping is None，本轮需要真实
                    #    LLM 推断新映射）：
                    #    a. 结构化产物可刷新（structured_refreshable）→ 排队。新
                    #       推断出的映射有落点可写（下游
                    #       _refresh_speaker_names_in_existing_structured_artifact
                    #       会原子刷新 dialogs 展示名），值得花这次 LLM 调用。
                    #    b. 结构化产物不可刷新（缺失/空/旧格式/混合 schema）→ 不
                    #       排队（J1 修复：不管校对是否确认 FULL，继续排队都只会
                    #       换来一次没有任何落点的说话人推断——白烧 LLM token 且
                    #       结果永远不可见，合并成同一条"无落点不排队"规则）。
                    #       J2 修复进一步把"结构化产物存在"的判据从 naive 的
                    #       isinstance(x, dict) 收紧为 structured_refreshable
                    #       （schema 层），"存在但不可刷新"的产物现在也会落进这
                    #       条不排队分支，不再需要排队后靠 helper 侧静默跳过来
                    #       兜底。legacy 缺口交给用户显式 recalibrate 触发全流程
                    #       处理，不在这里自动垫付。
                    #
                    # 2. 映射存在（fingerprint 命中，本轮不需要真实
                    #    LLM 推断，零成本）：
                    #    a. 结构化产物可刷新但与映射不一致（分叉）→ 排队，K5 刷新
                    #       分支只改展示名、不动已校对内容，零成本安全。
                    #    b. 结构化产物可刷新且一致 → 不排队，无事可做。
                    #    c. 结构化产物不可刷新 + 校对层未满足（calibrated_layer_
                    #       satisfied 为 False，即 DISABLED/NONE/状态缺失）→ 排队，
                    #       Q3 原有保护，未变。这条腿本身零 LLM 成本（映射已知，
                    #       cache_hit 来源，不受 J1 删除隐式升级影响）；
                    #       queued_calibrate 现在纯由 need_calibrated 决定，
                    #       calibrate_requested=True 时 need_calibrated 天然为 True
                    #       （校对层未满足），照样触发一次真实完整校对自愈缺口；
                    #       用户显式 calibrate=false 时按其意图保留 False，这一轮
                    #       说话人补层因为仍无落点会在 helper 侧被
                    #       structured_artifact_is_refreshable 挡下、不写入，但也
                    #       不产生额外 LLM 成本，无需在这里单独收紧。
                    #    d. 结构化产物不可刷新 + 校对层已满足（FULL 或 PARTIAL）→
                    #       不排队（X1，未变）。映射本身没变，缺口只是历史展示层
                    #       空洞，渲染层本有平文本回退，不值得为一个纯展示问题
                    #       触发重建。
                    #
                    #    1b 与 2c/2d 的判定口径故意不同（前者只看
                    #    structured_refreshable，后者仍用更宽松的
                    #    calibrated_layer_satisfied，含 PARTIAL）：1 这条腿一旦
                    #    排队必然真烧一次 LLM 推断，只有确认有落点
                    #    （structured_refreshable）才划算；2 这条腿排队与否都不
                    #    产生新的 LLM 推断（映射已就位，只差展示层重建），沿用 X1
                    #    已验证过的更宽松判定即可，不需要收紧。
                    if current_speaker_mapping is None:
                        if structured_refreshable:
                            need_speaker_names = True
                    elif structured_refreshable:
                        if not structured_dialogs_consistent_with_mapping(
                            cached_structured_for_names,
                            current_speaker_mapping.get("mapping"),
                        ):
                            need_speaker_names = True
                    elif not calibrated_layer_satisfied:
                        need_speaker_names = True

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

            if (
                not need_calibrated
                and not need_summary
                and not need_speaker_names
                and not calibration_effectively_missing
            ):
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
                # calibrated_layer_satisfied 现在已经把“状态文件缺失”纳入
                # 判定（见上方定义处的注释），意味着只要走到这条“完全命中”
                # 分支，cached_calibration_status 就不可能再是 None——不再
                # 需要（也不应该）在这里把“缺失”悄悄补成 FULL：那正是本地
                # codex review 第 16 轮 Q2 指出的“缺失状态被推断为 FULL”
                # 问题本身。直接镜像状态文件里的真实值，缺失时保持 None
                # （保守值，如实反映“未确认”），不再编造。
                mirrored_calibration_status = cached_calibration_status
                mirrored_summary_status = cached_llm_status.get("summary_status")

                # 先写终态、检查 CAS 返回值，赢了才发送通知（本地 codex review
                # 第 7 轮 H2）：update_task_status 是 compare-and-set，终态黏性会
                # 拒绝覆盖一个已经处于 success/failed 的任务行——例如任务已被
                # 关闭清算或恢复流程判定为 failed。此前这里先发"任务完成"
                # 通知、再写 CAS 且忽略返回值——CAS 落败时用户已经收到了通知，
                # 日志却从不提示矛盾。改为先写、检查结果，只有真正赢得
                # 终态写入时才发送。
                status_written = cache_manager.update_task_status(
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
                    terminal_snapshot={
                        "title": video_title,
                        "author": author,
                        "platform": cache_data.get("platform"),
                        "media_id": cache_data.get("media_id"),
                        "calibration_status": mirrored_calibration_status,
                        "summary_status": mirrored_summary_status,
                        "processing_options": processing_options,
                    },
                )

                if not status_written:
                    current_task = cache_manager.get_task_by_id(task_id)
                    current_status = current_task.get("status") if current_task else "unknown"
                    logger.warning(
                        f"任务状态 CAS 写入 success 失败(任务已处于终态 {current_status}，"
                        f"可能已被关闭清算/恢复流程判定)，跳过完成通知: {task_id}"
                    )
                else:
                    # K3 修复（本地 codex review 第 8 轮）：完成通知与其辅助
                    # 逻辑（发送校对/总结正文、拼装查看链接、发送完成通知）
                    # 此前仍在最外层通用失败处理的 try/except（本函数末尾的
                    # `except Exception as exc:`）覆盖范围内——success 已经
                    # 落库后，这里任意一步抛异常都会被那个 except 当成
                    # "转录处理异常"：把返回值改成 failed、发一条误导性的
                    # "转录异常"通知，且原本没有检查 FAILED CAS 的返回值就
                    # 声称处理完毕。改为独立 try/except 兜住这段通知逻辑
                    # 本身的异常：通知失败只记日志，不影响已经写定的
                    # success 任务结果，也不会误触发失败通知。
                    try:
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
                    except Exception:
                        logger.exception(
                            f"完成通知发送失败（任务已成功落库，不影响任务结果）: {task_id}"
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

            if need_calibrated or need_speaker_names:
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

            # J1 修复（本地增量复核第 3 轮）：此前这里还会在
            # need_speaker_names=True 且校对未确认 FULL 时（G2 修复）把
            # queued_calibrate 强制升级为 True——即便用户本次请求显式
            # 传了 calibrate=false，也会被系统偷偷改写成一次真实付费
            # 校对，违反 ProcessingOptions 每个开关都必须相互独立
            # 、用户显式传值必须被尊重的设计合同。
            #
            # G2 当初这么做是为了避免"生肉发布"：need_speaker_names 排
            # 队后若仍以 calibrate=False 运行，SpeakerAwareProcessor 会
            # skip_calibration=True 产出未经校对的原始 dialogs，一旦被
            # _refresh_speaker_names_in_existing_structured_artifact 当作
            # 结构化产物首次落盘，会被 DialogRenderer 无条件优先渲
            # 染，用生肉冒充/覆盖已有的（哪怕只是部分）真实校
            # 对文本。但这条首次落盘路径已经被 H2（本地增量
            # 复核）整段删除——没有旧产物可刷新时，helper 现在只
            # 记日志、不做任何写入，生肉不再有机会伪装成结构化
            # 产物。原本"校对未确认 FULL + 结构化产物缺失 + 要
            # 姓名"的场景，也已经并入上面 need_speaker_names 条件表 1b
            # 分支的"无落点不排队"：结构化产物缺失时压根不会
            # 排队 need_speaker_names，queued_calibrate 也就不再
            # 需要为它兜底升级。
            #
            # 现在 queued_calibrate 纯粹反映用户意图 + 缓存层状态
            # （need_calibrated 的既有计算已经如实综合了 calibrate_requested
            # 与 calibrated_layer_satisfied），不再有任何隐式升级。
            queued_calibrate = need_calibrated
            queued_processing_options = {
                "calibrate": queued_calibrate,
                "summarize": need_summary,
                "infer_speaker_names": need_speaker_names,
            }

            handoff_failure = _handoff_to_llm_stage(
                task_id,
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
                },
                calibrating_status_kwargs={
                    "platform": cache_data.get("platform"),
                    "media_id": cache_data.get("media_id"),
                    "title": video_title,
                    "author": author,
                    "download_url": download_url,
                },
                task_notifier=task_notifier,
                log_context="缓存",
            )
            if handoff_failure is not None:
                return handoff_failure

            logger.info(
                f"将LLM任务加入队列: {task_id}, 标题: {video_title}, "
                f"说话人识别: {has_speaker_recognition}, "
                f"需补层: calibrate={need_calibrated}, summarize={need_summary}, "
                f"infer_speaker_names={need_speaker_names}"
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
                        # fetch_for_transcription 在解析 SRT 时尽力保留
                        # segments；缺省时诚实降级（extra_json_data=None）
                        yt_api_segments = api_result.get("transcript_segments")
                        yt_api_extra_json = (
                            {"segments": yt_api_segments}
                            if yt_api_segments
                            else None
                        )
                        logger.info(
                            f"[youtube-api] Using platform transcript, length={len(transcript)}, "
                            f"segments={len(yt_api_segments) if yt_api_segments else 0}"
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
                            extra_json_data=yt_api_extra_json,
                        )
                        if not cache_result:
                            error_msg = "[youtube-api] 转录结果保存到缓存失败"
                            logger.error(error_msg)
                            # Y4 修复（PR3 review hardening 加固轮）：save_cache 失败
                            # 不能只记日志继续走 handoff/通知/success——转录产物根本
                            # 没有落盘，任务却仍会报告成功，进程重启后正文永久不可
                            # 恢复（没有缓存文件、没有可供 /view 页面读取的产物），
                            # 终态快照与实际缓存状态不一致。改走既有的失败收口
                            # （_fail_task_and_notify）：写 FAILED 终态 + 快照、发
                            # 失败通知、不再继续下面的 LLM handoff。
                            return _fail_task_and_notify(
                                error_msg, notify_status="转录结果保存失败",
                                title=video_title, author_name=author,
                            )

                        # 加入 LLM 处理队列
                        handoff_failure = _handoff_to_llm_stage(
                            task_id,
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
                            },
                            calibrating_status_kwargs={
                                "platform": platform,
                                "media_id": media_id,
                                "title": video_title,
                                "author": author,
                                "download_url": download_url,
                            },
                            task_notifier=task_notifier,
                            log_context="youtube-api",
                        )
                        if handoff_failure is not None:
                            return handoff_failure
                        logger.info(f"[youtube-api] LLM task queued: {task_id}")

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

                                # CapsWriter timeline：Transcriber 已从
                                # *_funasr.json 读入 funasr_json_data，接通
                                # extra_json_data 落盘为 transcript_capswriter.json
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
                                    extra_json_data=transcription_result.get(
                                        "funasr_json_data"
                                    ),
                                )

                        if not cache_result:
                            error_msg = "[youtube-api] 转录结果保存到缓存失败"
                            logger.error(error_msg)
                            # Y4 修复（PR3 review hardening 加固轮，同 [youtube-api]
                            # 平台字幕分支的收口原则）：转录产物未真正落盘，任务却
                            # 仍会报告成功，改走既有失败收口 _fail_task_and_notify。
                            return _fail_task_and_notify(
                                error_msg, notify_status="转录结果保存失败",
                                title=video_title, author_name=author,
                            )

                        task_notifier.notify_task_status(
                            display_url,
                            f"转录完成 - {engine_info}",
                            title=video_title,
                            author=author,
                            transcript=transcript,
                        )

                        # 加入 LLM 处理队列
                        handoff_failure = _handoff_to_llm_stage(
                            task_id,
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
                            },
                            calibrating_status_kwargs={
                                "platform": platform,
                                "media_id": media_id,
                                "title": video_title,
                                "author": author,
                                "download_url": download_url,
                            },
                            task_notifier=task_notifier,
                            log_context="youtube-api",
                        )
                        if handoff_failure is not None:
                            return handoff_failure
                        logger.info(f"[youtube-api] LLM task queued: {task_id}")

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
                    return _fail_task_and_notify(error_msg)

                except Exception as exc:
                    # 其他异常也不降级
                    error_msg = f"YouTube API Server unexpected error: {exc}"
                    logger.exception(f"[youtube-api] {error_msg}")
                    return _fail_task_and_notify(error_msg)

            # ========== 原有逻辑（非 YouTube API Server 路径）==========
            # 已在前面完成元数据解析与下载器准备
            original_downloader = None
            if not download_url:
                original_downloader = metadata_downloader or create_downloader(url)
            else:
                logger.info("已提供 download_url，使用解析的元数据，跳过传统下载器的 get_video_info")
                is_from_generic = (platform == 'generic')

            # 根据 use_speaker_recognition 参数决定处理优先级
            # YouTube 平台字幕走 get_subtitle_result 保留 timeline segments；
            # 其它路径仍用纯文本 subtitle（兼容非 YouTube 下载器）。
            subtitle = None
            subtitle_extra_json = None

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
                yt_downloader = None
                if metadata_downloader and metadata_downloader.__class__.__name__ == "YoutubeDownloader":
                    yt_downloader = metadata_downloader
                elif not download_url and original_downloader:
                    if original_downloader.__class__.__name__ == "YoutubeDownloader":
                        yt_downloader = original_downloader

                if yt_downloader is not None:
                    logger.info(f"不需要说话人识别，尝试获取YouTube平台字幕: {url}")
                    # 权威入口：保留 segments 时间戳；无字幕时返回 None
                    subtitle_result = yt_downloader.get_subtitle_result(url)
                    if subtitle_result and (
                        subtitle_result.text.strip() or subtitle_result.segments
                    ):
                        subtitle = subtitle_result.text
                        if subtitle_result.segments:
                            # FunASR 兼容形态：{"segments": [...]}
                            subtitle_extra_json = {
                                "segments": subtitle_result.segments
                            }
                        logger.info(
                            f"YouTube subtitle result: "
                            f"text_len={len(subtitle)}, "
                            f"segments={len(subtitle_result.segments) if subtitle_result.segments else 0}"
                        )

            if subtitle is not None and (
                (isinstance(subtitle, str) and subtitle.strip())
                or subtitle_extra_json is not None
            ):
                # 如果有字幕（文本或仅有 segments），直接使用
                logger.info(f"使用平台提供的字幕: {url}")

                task_notifier.notify_task_status(
                    display_url,
                    "平台字幕获取成功 - 直接使用平台字幕",
                    title=video_title,
                    author=author,
                )

                # 使用新的缓存系统保存平台字幕（有 segments 则写侧车 JSON）
                cache_result = cache_manager.save_cache(
                    platform=platform,
                    url=url,
                    media_id=video_id,
                    use_speaker_recognition=False,  # 平台字幕没有说话人识别
                    transcript_data=subtitle if subtitle is not None else "",
                    transcript_type="capswriter",  # 平台字幕按文本格式保存
                    title=video_title,
                    author=author,
                    description=description,
                    extra_json_data=subtitle_extra_json,
                )

                if not cache_result:
                    error_msg = "保存平台字幕到缓存失败"
                    logger.error(error_msg)
                    # Y4 修复（PR3 review hardening 加固轮，同上游 [youtube-api]
                    # 分支的收口原则）：字幕产物未真正落盘，任务却仍会报告成功，
                    # 改走既有失败收口 _fail_task_and_notify。
                    return _fail_task_and_notify(
                        error_msg, notify_status="转录结果保存失败",
                        title=video_title, author_name=author,
                    )

                # 将LLM处理任务加入队列
                handoff_failure = _handoff_to_llm_stage(
                    task_id,
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
                    },
                    calibrating_status_kwargs={
                        "platform": platform,
                        "media_id": video_id,
                        "title": video_title,
                        "author": author,
                        "download_url": download_url,
                    },
                    task_notifier=task_notifier,
                    log_context="平台字幕",
                )
                if handoff_failure is not None:
                    return handoff_failure
                logger.info(
                    f"将LLM任务加入队列（平台字幕）: {task_id}, 标题: {video_title}"
                )

                return {
                    "status": "success",
                    "message": "使用平台字幕成功",
                    "data": {
                        "video_title": video_title,
                        "author": author,
                        "transcript": subtitle,
                    },
                }
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
                            return _fail_task_and_notify(
                                error_msg, title=video_title, author_name=author,
                            )

                if not local_file:
                    error_msg = f"下载文件失败: {url}"
                    logger.error(error_msg)
                    return _fail_task_and_notify(
                        error_msg, title=video_title, author_name=author,
                    )

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
                                error_msg = "保存FunASR转录结果到缓存失败"
                                logger.error(error_msg)
                                # Y4 修复（PR3 review hardening 加固轮，同上游各
                                # save_cache 站点的收口原则）：转录产物未真正落盘，
                                # 任务却仍会报告成功，改走既有失败收口
                                # _fail_task_and_notify；外层 with tracker.track(...)
                                # / try 均以 finally: pass 收尾，这里 return 不会跳过
                                # 任何清理逻辑（与本函数上方"下载文件失败"分支同款
                                # 写法）。
                                return _fail_task_and_notify(
                                    error_msg, notify_status="转录结果保存失败",
                                    title=video_title, author_name=author,
                                )

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

                            # CapsWriter timeline：接通 funasr_json_data 落盘
                            # 为 transcript_capswriter.json（缺省时 None 诚实降级）
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
                                extra_json_data=transcription_result.get(
                                    "funasr_json_data"
                                ),
                            )

                            if not cache_result:
                                error_msg = "保存CapsWriter转录结果到缓存失败"
                                logger.error(error_msg)
                                # Y4 修复（PR3 review hardening 加固轮，同上面 FunASR
                                # 分支的收口原则，理由同注释）。
                                return _fail_task_and_notify(
                                    error_msg, notify_status="转录结果保存失败",
                                    title=video_title, author_name=author,
                                )

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
                    handoff_failure = _handoff_to_llm_stage(
                        task_id,
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
                        },
                        calibrating_status_kwargs={
                            "platform": platform,
                            "media_id": video_id,
                            "title": video_title,
                            "author": author,
                            "download_url": download_url,
                        },
                        task_notifier=task_notifier,
                        log_context="常规转录",
                    )
                    if handoff_failure is not None:
                        return handoff_failure
                    logger.info(
                        f"将LLM任务加入队列（常规转录）: {task_id}, 标题: {video_title}"
                    )

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

        return result
    except Exception as exc:
        logger.exception(f"转录处理异常: {exc}")
        # 任务失败时输出已记录的性能摘要
        tracker.log_summary()
        display_url = url
        # 先写终态再通知：与队列处理器（run_and_finalize
        # 的 except 分支）保持一致的顺序：若先 notify 再 update，
        # 通知渠道悬挂/失败会延迟终态写入，让
        # GET /api/task 在此期间仍显示过时的 in-flight 状态。
        # K3 修复（本地 codex review 第 8 轮）：update_task_status 是
        # compare-and-set，终态黏性可能拒绝这次 FAILED 写入（例如缓存全
        # 命中分支其实已经成功写过 success，只是完成通知逻辑抛了异常——但
        # 那条路径现在已经被独立 try/except 兜住，不会再走到这里；这里检
        # 查返回值是为了其它真正在 success CAS 之前就失败的路径，不能对
        # 写入结果不闻不问）。
        # L1 修复（CI review 第 5 轮 P1）：这是最后一道防线——写入本身若抛
        # 异常，此前这里没有 try/except，异常会直接原样传播（恰好没有先发
        # 通知，但缺少收敛日志）；现在显式 try/except 记日志后重新抛出，
        # 行为不变、可观测性补齐，且与 _handoff_to_llm_stage /
        # _fail_task_and_notify 两处站点统一。
        try:
            failed_status_written = cache_manager.update_task_status(
                task_id, TaskStatus.FAILED, download_url=download_url,
                error_message=f"转录任务异常: {exc}",
            )
        except Exception:
            logger.exception(f"收敛 failed 终态写入异常: {task_id}")
            raise

        if failed_status_written:
            # M1 修复（PR3 review hardening 收尾轮）：CAS==True 才是本次调用的
            # 真正胜者，失败通知严格只挂在这里。
            get_notification_router().notify_task_status(
                url=display_url, status="转录异常", error=str(exc),
                channel_name=notification_channel, webhooks=notification_webhooks,
            )
            return {
                "status": "failed",
                "message": f"转录任务异常: {exc}",
                "error": str(exc),
            }

        # M1 修复（PR3 review hardening 收尾轮）：CAS 返回 False 无法区分"任务
        # 行已是 success/failed 终态"与"任务行根本不存在"——两种情况下
        # update_task_status 的 UPDATE ... WHERE task_id=? AND status NOT IN
        # ('success','failed') rowcount 都是 0。因此除 success 分支要如实改写
        # 返回值外，其余一律不再发失败通知：已是 failed 的话，真正的 CAS 胜者
        # 早已发过一次；行不存在/unknown 的话，不为不存在的终态编造通知（上一
        # 轮 L1 修复遗留的口子：曾经对"非 success"一律继续发通知，未做这个
        # 区分）。
        current_task = cache_manager.get_task_by_id(task_id)
        current_status = current_task.get("status") if current_task else "unknown"
        logger.warning(
            f"任务状态 CAS 写入 failed 失败(任务已处于终态 {current_status}，"
            f"未被本次异常覆盖，跳过失败通知): {task_id}"
        )
        if current_status == TaskStatus.SUCCESS:
            return {
                "status": TaskStatus.SUCCESS,
                "message": "任务已被并发流程标记为 success，跳过失败通知",
            }
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
