"""LLM 队列处理与任务执行模块

从 transcription.py 拆分而来，负责：
- LLM 队列消费 (process_llm_queue)
- 单个 LLM 任务处理 (_handle_llm_task)
  - 标题生成（通用下载器场景）
  - LLM 协调器调用（校对+总结）
  - 结果缓存保存
  - 企微通知发送
"""

import time
from pathlib import Path
from typing import Optional
from ..context import (
    get_cache_manager,
    get_config,
    get_llm_coordinator,
    get_llm_executor,
    get_llm_queue,
    get_logger,
    task_lock,
)
from ...llm.core.usage_context import bind_task_id, set_context
from ...utils.notifications import (
    WechatNotifier,
    send_long_text_wechat,
    format_llm_config_markdown,
    get_notification_router,
)
from ...utils.notifications.channel import _clean_url, _apply_risk_control_safe
from ...utils.rendering import get_base_url
from ...utils.perf_tracker import PerfTracker
from ...utils.task_status import TaskStatus
from ...utils.llm_status import CalibrationStatus, SummaryStatus

logger = get_logger()
config = get_config()
cache_manager = get_cache_manager()
llm_coordinator = get_llm_coordinator()
llm_task_queue = get_llm_queue()
llm_executor = get_llm_executor()


def process_llm_queue():
    """处理LLM队列的后台任务"""
    logger.info("启动LLM队列处理器")

    while True:
        try:
            llm_task = llm_task_queue.get()
            try:
                logger.info(
                    f"LLM任务出队: {llm_task.get('task_id')}，"
                    f"提交到线程池（当前队列任务完成数: {getattr(llm_task_queue, 'completed', '未知')}）"
                )
                llm_executor.submit(_handle_llm_task, llm_task)
            except Exception as exc:
                logger.exception(f"提交LLM任务失败: {exc}")
                llm_task_queue.task_done()
        except Exception as exc:
            logger.exception(f"LLM队列处理器异常: {exc}")
            time.sleep(1)


def _handle_llm_task(llm_task: dict):
    """Worker entry for processing a single LLM task.

    Args:
        llm_task: LLM 任务字典，包含 task_id, url, video_title, transcript 等
    """
    task_id = llm_task.get("task_id")
    # 绑定 task_id 到当前 worker 线程的审计上下文（token 用量按任务/阶段落库用），
    # 线程池会复用线程处理后续任务，每次任务入口都重新绑定即可，无需成对 reset
    bind_task_id(task_id)

    # 从 transcription 阶段传递过来的性能追踪器，若无则创建新实例
    tracker: PerfTracker = llm_task.pop("perf_tracker", None) or PerfTracker(task_id=task_id)

    try:
        with task_lock(task_id):
            url = llm_task["url"]
            display_url = llm_task.get("display_url", url)
            platform = llm_task.get("platform")
            media_id = llm_task.get("media_id")
            video_title = llm_task["video_title"]
            transcript = llm_task["transcript"]
            use_speaker_recognition = llm_task.get("use_speaker_recognition", False)
            wechat_webhook = llm_task.get("wechat_webhook")
            notification_channel = llm_task.get("notification_channel")
            notification_webhooks = llm_task.get("notification_webhooks", {})

            _router = get_notification_router()

            class _TaskNotifier:
                def send_text(self, content, skip_risk_control=False):
                    return _router.send_text(
                        content, channel_name=notification_channel,
                        webhooks=notification_webhooks,
                    )

            task_notifier = _TaskNotifier()
            logger.info(f"开始处理LLM任务: {task_id}, 标题: {video_title}")

            try:
                # 通用下载器无标题时使用 LLM 生成
                video_title = _generate_title_if_needed(llm_task, video_title, transcript)
                llm_task["video_title"] = video_title

                # 使用新 LLM 协调器处理任务（用 PerfTracker 记录 LLM 处理耗时）
                logger.info(f"开始使用 LLM 协调器处理任务: {task_id}")

                # 准备内容参数
                content = _prepare_llm_content(llm_task, transcript, use_speaker_recognition)

                # 是否为仅校对模式（重新校对场景）
                calibrate_only = llm_task.get("calibrate_only", False)

                # 处理深度开关（只转录/转录+校对/全流程）：缺失时按全流程兜底，
                # 与 transcription.normalize_processing_options(None) 语义一致。
                # recalibrate 场景不设置该键，天然落到全流程默认，不影响既有行为。
                processing_options = llm_task.get("processing_options") or {
                    "calibrate": True,
                    "summarize": True,
                }
                calibrate_requested = processing_options.get("calibrate", True)
                summarize_requested = processing_options.get("summarize", True)

                # 仅校对模式下，若缓存里 llm_summary.txt 缺失/为空，顺手补跑一次 summary
                # 避免老任务卡在 view 页的 "总结处理中..." 状态
                summary_backfill = False
                if calibrate_only and platform and media_id:
                    cache_snapshot = cache_manager.get_cache(
                        platform, media_id,
                        use_speaker_recognition=use_speaker_recognition,
                    )
                    if _should_backfill_summary(cache_snapshot or {}, calibrate_only=True):
                        summary_backfill = True
                        logger.info(
                            f"recalibrate: llm_summary missing for {task_id}, "
                            f"auto-backfill enabled"
                        )

                # 协调器需要知道是否跳过 summary：backfill 时强制跑 summary；
                # 请求本身关闭 summarize 时同样跳过（两个互不相关的"跳过"原因
                # OR 到同一个协调器参数——协调器不需要关心具体原因，产出的
                # None 状态由 _save_llm_results 按 processing_options 归一化为
                # "保留旧值" 或 "disabled"，见该函数内的三态判定注释）。
                skip_summary_for_coordinator = (
                    (calibrate_only and not summary_backfill) or not summarize_requested
                )
                # 校对是否跳过：仅由本轮 processing_options.calibrate 决定。
                # recalibrate 从不设置 processing_options，默认 calibrate=True，
                # 因此 recalibrate 永远真实执行校对，不受本开关影响。
                skip_calibration_for_coordinator = not calibrate_requested

                # 调用新架构（包含校对和总结）
                with tracker.track("llm_processing"):
                    coordinator_result = llm_coordinator.process(
                        content=content,
                        title=video_title,
                        author=llm_task.get("author", ""),
                        description=llm_task.get("description", ""),
                        platform=platform or "",
                        media_id=media_id or "",
                        skip_summary=skip_summary_for_coordinator,
                        skip_calibration=skip_calibration_for_coordinator,
                    )

                # 适配返回格式
                result_dict = _build_result_dict(coordinator_result)

                logger.info(f"LLM处理完成，开始保存结果和发送微信通知: {task_id}")

                # 保存结果到缓存。返回值是本轮实际落盘的"有效"状态（分层缓存场景下，
                # 本轮未真正处理的层会被抑制为 None，避免污染已有的真实状态）——
                # 用它覆盖 result_dict.stats，确保随后的通知与任务状态更新看到的是
                # 一致的、真实反映落盘结果的状态，而不是协调器这一轮的原始输出。
                effective_status = _save_llm_results(
                    task_id=task_id,
                    platform=platform,
                    media_id=media_id,
                    use_speaker_recognition=use_speaker_recognition,
                    result_dict=result_dict,
                    calibrate_only=calibrate_only,
                    summary_backfill=summary_backfill,
                    processing_options=processing_options,
                )
                if effective_status:
                    # setdefault 而非直接下标：result_dict["stats"] 在真实
                    # _build_result_dict() 产出中恒定存在，这里用 setdefault 只是
                    # 防御测试/未来调用方传入不含 stats 键的精简 result_dict。
                    result_stats = result_dict.setdefault("stats", {})
                    calibration_status = effective_status.get("calibration_status")
                    summary_status = effective_status.get("summary_status")

                    # effective_status 里某一层为 None 表示"本轮未触碰该层，
                    # llm_status.json 按合并语义原样保留旧值"（见 _save_llm_results
                    # 文档）——但 task_status 表这一行是本次请求刚创建的全新任务行
                    # （create_task 建表时 calibration_status/summary_status 即为
                    # NULL），不像 llm_status.json 那样有"旧值"可保留：直接把 None
                    # 传给 update_task_status 只会被它自身的 `is not None` 判断
                    # 跳过更新，导致该列永久留空，与缓存里的真实状态不符（历史 API
                    # 因此会返回 calibration_status: null）。
                    # 修复：某一层为 None 时，从刚写盘的 llm_status.json（本轮
                    # _save_llm_results 内部已调用 save_llm_status 完成合并）里
                    # 把"未触碰层"的合并后真实状态读回来，保证任务行与缓存一致。
                    if (
                        (calibration_status is None or summary_status is None)
                        and platform and media_id
                    ):
                        merged_snapshot = cache_manager.get_cache(
                            platform, media_id,
                            use_speaker_recognition=use_speaker_recognition,
                        )
                        merged_llm_status = (merged_snapshot or {}).get("llm_status") or {}
                        if calibration_status is None:
                            calibration_status = merged_llm_status.get("calibration_status")
                        if summary_status is None:
                            summary_status = merged_llm_status.get("summary_status")

                    result_stats["calibration_status"] = calibration_status
                    result_stats["summary_status"] = summary_status

                # 发送通知（多渠道）
                if not calibrate_only:
                    _send_notification(
                        task_id=task_id,
                        video_title=video_title,
                        display_url=display_url,
                        use_speaker_recognition=use_speaker_recognition,
                        result_dict=result_dict,
                        notification_channel=notification_channel,
                        notification_webhooks=notification_webhooks,
                    )

                logger.info(f"LLM任务处理完成: {task_id}, 标题: {video_title}")

                # 任务成功完成，输出完整性能摘要
                tracker.log_summary()

                # LLM 阶段拥有终态：产物已通过 _save_llm_results 落盘，此时才置 success
                # （对所有任务生效，不再仅限 calibrate_only；终态由本阶段统一写回）
                # 同时把诚实状态模型镜像写入 task_status 表两列，供 /api/audit/history 查询消费。
                done_message = "重新校对完成" if calibrate_only else "校对完成"
                final_stats = result_dict.get("stats", {})
                cache_manager.update_task_status(
                    task_id,
                    TaskStatus.SUCCESS,
                    platform=platform,
                    media_id=media_id,
                    title=video_title,
                    author=llm_task.get("author", ""),
                    calibration_status=final_stats.get("calibration_status"),
                    summary_status=final_stats.get("summary_status"),
                )
                logger.info(f"任务状态已更新为 success: {task_id} ({done_message})")

            except Exception as exc:
                logger.exception(f"LLM任务处理异常: {task_id}, 错误: {exc}")
                # LLM 处理失败时输出已记录的性能摘要
                tracker.log_summary()
                task_notifier.send_text(f"【LLM API调用异常】{exc}")

                # 终态由 LLM 阶段统一写回（对所有任务生效，修复普通任务 LLM 失败被静默的问题）
                fail_message = (
                    f"重新校对失败: {exc}" if llm_task.get("calibrate_only")
                    else f"LLM处理失败: {exc}"
                )
                try:
                    cache_manager.update_task_status(
                        task_id, TaskStatus.FAILED, error_message=fail_message,
                    )
                    logger.info(f"任务状态已更新为 failed: {task_id} ({fail_message})")
                except Exception:
                    pass
    finally:
        llm_task_queue.task_done()


def _generate_title_if_needed(llm_task: dict, video_title: str, transcript: str) -> str:
    """通用下载器场景下使用 LLM 生成标题

    Args:
        llm_task: LLM 任务字典
        video_title: 当前标题
        transcript: 转录文本

    Returns:
        str: 标题（可能是生成的或原始的）
    """
    if video_title != "":
        return video_title

    is_generic = llm_task.get("is_generic", False)
    if not is_generic:
        return video_title

    logger.info("通用下载器文件没有标题，使用LLM生成")
    title_prompt = (
        "请根据以下音视频转录文本，生成一个简洁的标题（不超过20个字）。\n"
        "只返回标题文本，不要有任何其他说明或标点符号。\n"
        "如果无法从内容中提取有意义的标题，请返回'自定义文件总结'。\n\n"
        "转录文本：\n" + transcript[:1000]
    )

    try:
        config_llm = config.get("llm", {})
        # 复用应用级单例 LLMCoordinator 已配置好的 LLMClient（api_key/base_url/
        # 重试策略均已就绪），而不是直接调用底层 call_llm_api()——避免重复拼装
        # 配置参数，同时天然获得 LLMClient.call() 内置的 token 用量审计记录。
        # set_context(stage="title") 只细化 stage，task_id 沿用 _handle_llm_task
        # 入口处 bind_task_id() 绑定的当前任务 ID。
        with set_context(stage="title"):
            response = llm_coordinator.llm_client.call(
                model=config_llm.get("summary_model"),
                system_prompt="You are a helpful assistant.",
                user_prompt=title_prompt,
                task_type="title",
            )
        generated_title = response.text

        generated_title = (
            generated_title.strip()
            .strip('"')
            .strip("'")
            .strip("。")
            .strip("，")
        )

        if generated_title and len(generated_title) <= 30:
            logger.info(f"LLM生成的标题: {generated_title}")
            return generated_title
        else:
            logger.warning("LLM生成的标题不合规，使用默认标题")
            return "自定义文件总结"
    except Exception as exc:
        # 标题生成失败是合理的降级路径（退默认标题），但仍需 warning 级别留痕，
        # 避免真实故障（如模型配置错误、鉴权失败）被彻底静默、无从排查。
        logger.warning(f"LLM生成标题失败，使用默认标题兜底: {exc}")
        return "自定义文件总结"


def _prepare_llm_content(llm_task: dict, transcript: str, use_speaker_recognition: bool):
    """准备 LLM 协调器的输入内容

    Args:
        llm_task: LLM 任务字典
        transcript: 转录文本
        use_speaker_recognition: 是否使用说话人识别

    Returns:
        内容参数（字符串或列表）
    """
    if use_speaker_recognition and llm_task.get("transcription_data"):
        transcription_data = llm_task.get("transcription_data")
        if isinstance(transcription_data, dict):
            return transcription_data.get("segments", [])
        elif isinstance(transcription_data, list):
            return transcription_data
        else:
            logger.warning(
                f"Unexpected transcription_data type: {type(transcription_data)}, "
                f"falling back to formatted text"
            )
            return transcript
    return transcript


def _build_result_dict(coordinator_result: dict) -> dict:
    """将协调器结果适配为统一格式

    Args:
        coordinator_result: LLM 协调器返回的结果

    Returns:
        dict: 统一格式的结果字典
    """
    calibrated_text = coordinator_result.get("calibrated_text", "")
    summary_text = coordinator_result.get("summary_text")
    should_skip_summary = summary_text is None
    stats = coordinator_result.get("stats", {})

    # calibrate_success 改为派生值（不再硬编码 True）：只有当校对"诚实状态"为
    # NONE（全部内容都降级为原文，LLM 校对完全失败）时才视为失败。
    # calibration_status 缺失（旧/未接入的调用方）时保守视为成功，保持历史行为。
    calibration_status = stats.get("calibration_status")
    calibrate_success = calibration_status != CalibrationStatus.NONE

    result_dict = {
        "校对文本": calibrated_text,
        "内容总结": summary_text,
        "skip_summary": should_skip_summary,
        "stats": stats,
        "models_used": coordinator_result.get("models_used", {}),
        "calibrate_success": calibrate_success,
        "summary_success": summary_text is not None,
        # summary_status 为 None 表示协调器本轮未尝试生成总结（calibrate_only 且
        # 未触发 backfill），_save_llm_results 会据此保留上一轮已落盘的状态，不误覆盖。
        "summary_status": stats.get("summary_status"),
    }

    if "structured_data" in coordinator_result:
        result_dict["structured_data"] = coordinator_result["structured_data"]

    return result_dict


def _should_backfill_summary(cache_data: dict, calibrate_only: bool) -> bool:
    """判断是否需要在 recalibrate 流程里顺手补跑一次 summary。

    触发条件：仅校对模式，且缓存目录里的 llm_summary.txt 缺失或为空字节。
    空文件视为历史遗留占位，同样需要补跑。

    Args:
        cache_data: cache_manager.get_cache(...) 返回的数据字典
        calibrate_only: 是否仅校对（recalibrate）流程

    Returns:
        True 表示应当补跑 summary，False 表示保留现状
    """
    if not calibrate_only:
        return False

    file_path = cache_data.get("file_path") if cache_data else None
    if not file_path:
        return False

    summary_file = Path(file_path) / "llm_summary.txt"
    if not summary_file.exists():
        return True

    try:
        return summary_file.stat().st_size == 0
    except OSError:
        return True


def _save_llm_results(
    task_id: str,
    platform: str,
    media_id: str,
    use_speaker_recognition: bool,
    result_dict: dict,
    calibrate_only: bool,
    summary_backfill: bool = False,
    processing_options: Optional[dict] = None,
):
    """保存 LLM 处理结果到缓存

    Args:
        task_id: 任务 ID
        platform: 平台标识
        media_id: 媒体 ID
        use_speaker_recognition: 是否使用说话人识别
        result_dict: LLM 处理结果字典
        calibrate_only: 是否仅校对模式
        summary_backfill: 仅校对模式下是否需要补写 summary 文件
            （原任务缺失 llm_summary.txt 时由 _handle_llm_task 置为 True）
        processing_options: 本轮处理深度开关 {"calibrate": bool, "summarize": bool}，
            None 时按全流程兜底（向后兼容旧调用方，包括本模块所有既有单测）。
            用于分层缓存场景下的"已存在层不覆盖"保护：仅当某一层在本轮开始前
            已经存在、且本轮 processing_options 未请求该层时才会抑制写入。

    Returns:
        Optional[dict]: 本次实际落盘的"有效"状态
            {"calibration_status": ..., "summary_status": ...}（值可能为 None，
            表示该层本轮未触碰、旧值原样保留）。platform/media_id 缺失时返回
            None（沿用早退语义，调用方应据此跳过覆盖 result_dict）。
    """
    calibrated_text = result_dict.get("校对文本", "")
    summary_text = result_dict.get("内容总结")
    skip_summary = result_dict.get("skip_summary", False)
    stats = result_dict.get("stats", {})
    models_used = result_dict.get("models_used", {})
    calibrate_success = result_dict.get("calibrate_success", True)
    summary_success = result_dict.get("summary_success", True)

    # summary_status："诚实状态模型"三态 + None（"本轮未尝试"）。
    # 用 sentinel 区分"调用方没传这个键"（旧/手工构造的 result_dict，例如
    # test_recalibrate.py 里手工拼的 dict）和"调用方传了 None"（协调器在
    # calibrate_only 且未 backfill 时显式给出的"本轮未尝试生成总结"信号）——
    # 二者语义完全不同，前者要从旧的 skip_summary/summary_success 派生等价状态
    # 以保持向后兼容；后者必须原样保留 None，交给下面的 save_llm_status 走
    # "不覆盖旧值"的合并语义，否则会把已有的 GENERATED 状态误伪装成新状态。
    _MISSING = object()
    summary_status = result_dict.get("summary_status", _MISSING)
    if summary_status is _MISSING:
        if skip_summary:
            summary_status = SummaryStatus.SKIPPED_SHORT
        elif summary_success:
            summary_status = SummaryStatus.GENERATED
        else:
            summary_status = SummaryStatus.FAILED

    # 保存 LLM 模型配置到数据库
    if models_used:
        cache_manager.update_task_llm_config(task_id, models_used)
        logger.info(
            f"LLM模型配置已保存: {task_id}"
        )

    if not (platform and media_id):
        return None

    # ---- 分层缓存保护：判断本轮是否需要"已存在层不覆盖"，整段持媒体锁 ----
    # processing_options 缺省按全流程兜底，此时 calibrate_requested/summarize_requested
    # 均为 True，need_snapshot 恒为 False，不会发起额外的 get_cache 查询——保证本函数
    # 现有全部调用方（包括不传 processing_options 的旧测试）行为完全不变。
    #
    # 加锁范围覆盖"判断层是否已存在 -> 写入产物 -> 合并落盘状态"整段
    # （codex-review R3 #1）：同一媒体的两个并发任务（例如任务 A 只要
    # summarize、任务 B 只要 calibrate）如果不共享这把锁，A 在判断阶段拍下
    # 的"校对层不存在"快照会在 B 写入真实校对产物之后过期——A 恢复执行后
    # 仍会用这份过期快照，把自己的格式化原文/占位内容当作"层不存在时的
    # 兜底"覆盖到 B 刚写好的真实产物之上，破坏"缓存产物只增不减"的分层
    # 缓存不变式。cache_manager.media_lock 是 RLock，本函数末尾调用的
    # save_llm_status 内部也会请求同一把锁，同线程可重入，不会死锁。
    with cache_manager.media_lock(platform, media_id):
        if processing_options is None:
            processing_options = {"calibrate": True, "summarize": True}
        calibrate_requested = processing_options.get("calibrate", True)
        summarize_requested = processing_options.get("summarize", True)

        calibrated_exists_before = False
        summary_exists_before = False
        need_snapshot = (not calibrate_requested) or (not summarize_requested)
        if need_snapshot:
            existing_snapshot = cache_manager.get_cache(
                platform, media_id, use_speaker_recognition=use_speaker_recognition,
            )
            if existing_snapshot:
                calibrated_exists_before = "llm_calibrated" in existing_snapshot
                summary_exists_before = "llm_summary" in existing_snapshot

        # 校对层已存在、且本轮未请求（重新）校对 -> 抑制写入，保护已有的真实产物
        # 不被本轮 skip_calibration=True 产出的占位内容覆盖。
        suppress_calibration = calibrated_exists_before and not calibrate_requested

        # 总结层的"关闭"语义二次判定：
        # - 已有真实产物（GENERATED/SKIPPED_SHORT 都会落盘文件）-> 本轮未触碰，
        #   保留旧值（None，走 save_llm_status 的合并语义）。
        # - 尚无产物 -> 用户首次显式关闭总结，记为 DISABLED（区别于"文本过短跳过"）。
        # calibrate_only 且未 backfill 的 recalibrate 分支维持原样：summary_status
        # 此时已是协调器给出的 None（"本轮未尝试"），不需要在这里重新判定。
        if not (calibrate_only and not summary_backfill) and not summarize_requested:
            summary_status = None if summary_exists_before else SummaryStatus.DISABLED

        # 保存校对文本
        if calibrate_success and not suppress_calibration:
            cache_manager.save_llm_result(
                platform=platform,
                media_id=media_id,
                use_speaker_recognition=use_speaker_recognition,
                llm_type="calibrated",
                content=calibrated_text,
            )
            logger.info(f"校对文本已保存到缓存: {task_id}")
        elif suppress_calibration:
            logger.info(f"校对层已存在且本轮未请求重新校对，跳过覆盖: {task_id}")
        else:
            logger.warning(f"校对失败，跳过保存校对文件: {task_id}")

        # 保存总结文本：按 summary_status 三态（+DISABLED/保留）分支（不再用
        # skip_summary/summary_success 二元判定——旧逻辑里 skip_summary 与
        # summary_success 永远互补，导致"文本过短"分支实际不可达，"生成失败"
        # 被悄悄吞掉，既不落盘校对文本兜底也不报错）
        if calibrate_only and not summary_backfill:
            logger.info(f"仅校对模式，保留原有总结文件: {task_id}")
        elif summary_status == SummaryStatus.GENERATED:
            if summary_text is not None:
                logger.info(f"保存LLM总结到缓存: {task_id}")
                cache_manager.save_llm_result(
                    platform=platform,
                    media_id=media_id,
                    use_speaker_recognition=use_speaker_recognition,
                    llm_type="summary",
                    content=summary_text,
                )
            else:
                logger.warning(f"总结状态为 generated 但文本为空，跳过保存: {task_id}")
        elif summary_status == SummaryStatus.SKIPPED_SHORT:
            if calibrate_success:
                logger.info(f"文本过短，保存校对文本作为总结: {task_id}")
                cache_manager.save_llm_result(
                    platform=platform,
                    media_id=media_id,
                    use_speaker_recognition=use_speaker_recognition,
                    llm_type="summary",
                    content=calibrated_text,
                )
        elif summary_status == SummaryStatus.FAILED:
            # 关键修复：总结失败不再把校对文本复制成总结文件，避免"生成失败"被
            # 伪装成"文本过短"的正常路径（诚实状态模型的核心诉求）
            logger.warning(f"总结生成失败，不落盘复制校对文本: {task_id}")
        elif summary_status == SummaryStatus.DISABLED:
            logger.info(f"总结已禁用（本任务未启用内容总结），不生成总结文件: {task_id}")
        elif summary_status is None and not summarize_requested:
            logger.info(f"总结层已存在且本轮未请求生成总结，保留原有总结文件: {task_id}")
        else:
            logger.warning(f"总结状态未知或仍在处理中({summary_status})，跳过保存总结文件: {task_id}")

        # 保存结构化数据（同样受校对层抑制保护，避免覆盖已有的真实说话人校对结果）
        if (
            use_speaker_recognition
            and calibrate_success
            and not suppress_calibration
            and "structured_data" in result_dict
        ):
            structured_data = result_dict["structured_data"]
            cal_stats_for_save = stats.get("calibration_stats")
            if cal_stats_for_save:
                structured_data["calibration_stats"] = cal_stats_for_save
            save_ok = cache_manager.save_llm_result(
                platform=platform,
                media_id=media_id,
                use_speaker_recognition=use_speaker_recognition,
                llm_type="structured",
                content=structured_data,
            )
            if save_ok:
                logger.info(f"结构化数据已保存到缓存: {platform}/{media_id}/llm_processed.json")
            else:
                logger.warning(f"结构化数据保存失败: {task_id}")

        # 写入统一的诚实状态落盘文件 llm_status.json（两条路径都写）。
        # calibration_status/summary_status 为 None 时（本轮未触碰该层）传 None 给
        # save_llm_status，其合并语义会保留旧值，不会把已有的真实状态误覆盖。
        effective_calibration_status = None if suppress_calibration else stats.get("calibration_status")
        effective_calibration_stats = None if suppress_calibration else stats.get("calibration_stats")
        cache_manager.save_llm_status(
            platform=platform,
            media_id=media_id,
            use_speaker_recognition=use_speaker_recognition,
            calibration_status=effective_calibration_status,
            calibration_stats=effective_calibration_stats,
            summary_status=summary_status,
        )

    if calibrate_success or summary_success:
        logger.info(f"LLM结果已保存到缓存: {platform}/{media_id}")
    else:
        logger.warning(f"LLM处理全部失败，未保存任何结果文件: {task_id}")

    return {
        "calibration_status": effective_calibration_status,
        "summary_status": summary_status,
    }


def _send_notification(
    task_id: str,
    video_title: str,
    display_url: str,
    use_speaker_recognition: bool,
    result_dict: dict,
    notification_channel: str = None,
    notification_webhooks: dict = None,
):
    """Send LLM results notification via router (multi-channel).

    Args:
        task_id: task ID
        video_title: video title
        display_url: display URL
        use_speaker_recognition: speaker recognition flag
        result_dict: LLM result dict
        notification_channel: target channel (wechat/feishu/None=all)
        notification_webhooks: per-channel webhook dict
    """
    if notification_webhooks is None:
        notification_webhooks = {}
    router = get_notification_router()

    calibrated_text = result_dict.get("校对文本", "")
    summary_text = result_dict.get("内容总结")
    skip_summary = result_dict.get("skip_summary", False)
    stats = result_dict.get("stats", {})
    models_used = result_dict.get("models_used", {})

    task_info = cache_manager.get_task_by_id(task_id)
    view_url = ""
    if task_info and task_info.get("view_token"):
        base_url = get_base_url()
        view_url = f"{base_url}/view/{task_info['view_token']}"

    original_length = stats.get("original_length", 0)
    calibrated_length = stats.get("calibrated_length", 0)
    summary_length = stats.get("summary_length", 0)

    calibration_warning = _build_calibration_warning(stats)

    speaker_info = "（含说话人识别）" if use_speaker_recognition else ""
    model_config_text = format_llm_config_markdown(models_used)

    # 通知里的总结状态文案：failed 时明确写"生成失败"，disabled 时明确写
    # "未启用"（用户主动关闭，不是失败）——避免和"文本过短未生成"这种正常路径
    # 混为一谈（诚实状态模型的一部分）。缺失/其他 summary_status 时（legacy
    # 调用方或分层缓存"本轮未触碰、保留旧值"的 None）保持历史文案"未生成"不变。
    summary_status = stats.get("summary_status")
    if summary_status == SummaryStatus.FAILED:
        summary_status_label = "生成失败"
    elif summary_status == SummaryStatus.DISABLED:
        summary_status_label = "未启用"
    else:
        summary_status_label = "未生成"

    # 校对文本超过此阈值时，不发送全文到通知渠道（避免刷屏）
    NOTIFICATION_TEXT_THRESHOLD = 5000

    if skip_summary:
        if len(calibrated_text) <= NOTIFICATION_TEXT_THRESHOLD:
            full_message = f"""## 总结和校对
🌐 网页查看：{view_url}
📄 直接获取：{view_url}?raw=calibrated

## 转录统计
原始 {original_length:,} 字 | 校对 {calibrated_length:,} 字 | 总结 {summary_status_label}{calibration_warning}

{model_config_text}

## 校对文本{speaker_info}
{calibrated_text}"""
            logger.info(f"发送校对文本（总结{summary_status_label}，文本较短直接发送）: {task_id}")
        else:
            full_message = f"""## 总结和校对
🌐 网页查看：{view_url}
📄 直接获取：{view_url}?raw=calibrated

## 转录统计
原始 {original_length:,} 字 | 校对 {calibrated_length:,} 字 | 总结 {summary_status_label}{calibration_warning}

{model_config_text}

⚠️ 校对文本过长（{calibrated_length:,} 字），请点击上方链接在网页中查看完整内容。"""
            logger.info(
                f"校对文本过长（{calibrated_length} 字 > {NOTIFICATION_TEXT_THRESHOLD}），"
                f"仅发送链接: {task_id}"
            )
    else:
        full_message = f"""## 总结和校对
🌐 网页查看：{view_url}
📄 直接获取：{view_url}?raw=calibrated

## 转录统计
原始 {original_length:,} 字 | 校对 {calibrated_length:,} 字 | 总结 {summary_length:,} 字{calibration_warning}

{model_config_text}

## 总结{speaker_info}
{summary_text}"""
        logger.info(f"发送总结文本: {task_id}")

    router.send_long_text(
        title=video_title,
        url=display_url,
        text=full_message,
        is_summary=not skip_summary,
        has_speaker_recognition=use_speaker_recognition,
        channel_name=notification_channel,
        webhooks=notification_webhooks,
        skip_content_type_header=True,
    )

    time.sleep(0.1)

    task_info = cache_manager.get_task_by_id(task_id)
    if task_info and task_info.get("view_token"):
        base_url = get_base_url()
        view_url = f"{base_url}/view/{task_info['view_token']}"
        clean = _clean_url(display_url)
        sanitized_title = _sanitize_title(video_title)

        completion_message = f"# {sanitized_title}\n\n{clean}\n\n🔗 总结和校对：\n{view_url}\n\n✅ **【任务完成】**"
        router.send_text(
            completion_message,
            channel_name=notification_channel,
            webhooks=notification_webhooks,
        )
        logger.info(f"任务完成通知已加入限流队列: {task_id}")


def _build_calibration_warning(stats: dict) -> str:
    """构建校准质量警告文本

    两条校对路径统计口径不同：结构化路径（说话人识别）按 chunk 计数
    （total_chunks/success_count/fallback_count/failed_count），纯文本路径按
    segment 计数（total_segments/calibrated_segments/fallback_segments/
    low_quality_segments）。这里按 cal_stats 里出现的字段名分辨路径，
    分别生成对应的详情文案，保证纯文本路径的降级也能像结构化路径一样出警告。

    Args:
        stats: 统计信息字典（coordinator.process() 返回的 stats，
            stats["calibration_stats"] 为 None 或缺失时视为无统计可用）

    Returns:
        str: 警告文本（空字符串表示无警告）
    """
    cal_stats = stats.get("calibration_stats")
    if not cal_stats:
        return ""

    if "total_chunks" in cal_stats:
        # 结构化路径（说话人识别）：chunk 口径
        failed = cal_stats.get("failed_count", 0)
        fallback = cal_stats.get("fallback_count", 0)
        total = cal_stats.get("total_chunks", 0)
        success = cal_stats.get("success_count", 0)

        if failed == total and total > 0:
            return (
                "\n⚠️ **校准完全失败**：LLM API 超时，"
                "当前显示为未校准的原始语音识别文本，质量较低。"
                "建议稍后重新提交。"
            )
        if failed > 0 or fallback > 0:
            return (
                f"\n⚠️ **校准部分异常**：{success}/{total} 段校准成功，"
                f"{fallback} 段降级，{failed} 段失败。"
                "部分内容为未校准文本。"
            )
        return ""

    if "total_segments" in cal_stats:
        # 纯文本路径：segment 口径
        total = cal_stats.get("total_segments", 0)
        calibrated = cal_stats.get("calibrated_segments", 0)
        fallback = cal_stats.get("fallback_segments", 0)
        low_quality = cal_stats.get("low_quality_segments", 0)

        if calibrated == 0 and total > 0:
            return (
                "\n⚠️ **校准完全失败**：LLM API 超时，"
                "当前显示为未校准的原始语音识别文本，质量较低。"
                "建议稍后重新提交。"
            )
        if fallback > 0 or low_quality > 0:
            detail = f"{calibrated}/{total} 段校准成功，{fallback} 段降级为原文"
            if low_quality:
                detail += f"，其中 {low_quality} 段质量存疑"
            return f"\n⚠️ **校准部分异常**：{detail}。部分内容为未校准文本。"
        return ""

    return ""


def _sanitize_title(video_title: str) -> str:
    """对标题进行风控处理

    Args:
        video_title: 原始标题

    Returns:
        str: 处理后的标题
    """
    try:
        from ...risk_control import is_enabled, sanitize_text

        if is_enabled():
            title_result = sanitize_text(video_title, text_type="title")
            if title_result["has_sensitive"]:
                logger.info(
                    f"[风控] 完成通知标题包含 {len(title_result['sensitive_words'])} 个敏感词，已处理"
                )
                return title_result["sanitized_text"]
    except Exception as risk_exc:
        logger.exception(f"完成通知标题风控处理失败: {risk_exc}")

    return video_title
