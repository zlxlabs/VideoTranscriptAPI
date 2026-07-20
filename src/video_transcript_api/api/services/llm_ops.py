"""LLM 队列处理与任务执行模块

从 transcription.py 拆分而来，负责：
- LLM 队列消费 (process_llm_queue)
- 单个 LLM 任务处理 (_handle_llm_task)
  - 标题生成（通用下载器场景）
  - LLM 协调器调用（校对+总结）
  - 结果缓存保存
  - 企微通知发送
"""

import queue
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from ..context import (
    get_cache_manager,
    get_config,
    get_llm_coordinator,
    get_llm_executor,
    get_llm_queue,
    get_logger,
    get_runtime,
    run_with_runtime,
    task_lock,
    lazy_resource,
)
from ..processing_options import normalize_processing_options
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
from ...utils.llm_status import CalibrationStatus, ChaptersStatus, SummaryStatus

logger = lazy_resource(get_logger)
config = lazy_resource(get_config)
cache_manager = lazy_resource(get_cache_manager)
llm_coordinator = lazy_resource(get_llm_coordinator)
llm_task_queue = lazy_resource(get_llm_queue)
llm_executor = lazy_resource(get_llm_executor)


def _requires_llm_title(
    processing_options: dict, *, use_speaker_recognition: bool
) -> bool:
    """Return whether any feature that can actually run for this task needs LLM.

    Chapters alone does NOT trigger title generation (design §5.2 / R7): a
    chapters-only request must not pay an extra title LLM call.
    """
    return bool(
        processing_options.get("calibrate")
        or processing_options.get("summarize")
        or (
            processing_options.get("infer_speaker_names")
            and use_speaker_recognition
        )
    )


def _resolve_chapters_timeline_segments(
    *,
    llm_task: dict,
    platform: Optional[str],
    media_id: Optional[str],
    use_speaker_recognition: bool,
) -> tuple[Optional[list], str]:
    """Resolve cache-side chapters input gradient (§5.1) for coordinator seed.

    This-round structured dialogs are preferred *inside* the coordinator after
    calibration. Here we only resolve what llm_ops can see before process():

      1. Explicit llm_task["timeline_segments"] (layered handoff / tests)
      2. Cached llm_processed.json dialogs
      3. Cached raw segments (get_cache["segments"] or load_segments)
      4. None → coordinator/processor returns SKIPPED_NO_TIMELINE

    Returns:
        (segments_or_none, source_kind) where source_kind is one of
        ``cached_dialogs`` / ``segments`` / ``none``.
    """
    task_segments = llm_task.get("timeline_segments")
    if isinstance(task_segments, list) and task_segments:
        return task_segments, "segments"

    if platform and media_id:
        get_cache = getattr(cache_manager, "get_cache", None)
        if callable(get_cache):
            try:
                cache_snapshot = get_cache(
                    platform,
                    media_id,
                    use_speaker_recognition=use_speaker_recognition,
                )
            except Exception as exc:
                logger.warning(f"get_cache failed for chapters gradient: {exc}")
                cache_snapshot = None
            if cache_snapshot:
                processed = cache_snapshot.get("llm_processed") or {}
                if isinstance(processed, dict):
                    cached_dialogs = processed.get("dialogs")
                    if isinstance(cached_dialogs, list) and cached_dialogs:
                        return cached_dialogs, "cached_dialogs"

                segs = cache_snapshot.get("segments")
                if isinstance(segs, list) and segs:
                    return segs, "segments"

                file_path = cache_snapshot.get("file_path")
                if file_path:
                    try:
                        from ...transcriber.segments import load_segments

                        loaded = load_segments(file_path)
                        if isinstance(loaded, list) and loaded:
                            return loaded, "segments"
                    except Exception as exc:
                        logger.warning(
                            f"load_segments failed for chapters gradient: {exc}"
                        )

    return None, "none"


def process_llm_queue():
    """处理LLM队列的后台任务（消费泵，单线程顺序循环）。

    消费泵容量闸门（本地 codex review 第 14 轮，补第 12 轮验收标准的
    遗漏项——"LLM 已提交但尚未完成的工作也计入同一个明确容量限制"）：

    背景：此前出队（llm_task_queue.get()）之后立即 submit 给 llm_executor
    （无界线程池，内部是无界的 SimpleQueue）——"已提交未完成"的这部分
    LLM 工作完全绕开了两个准入点（/api/recalibrate 的 try_register、
    transcription worker 交接的 register_internal）已经做的容量检查：
    那两个准入点只管"能不能进入 inflight_registry 的 'llm' 桶"，管不到
    "进桶之后消费者敢不敢立刻把它转交给执行器"。llm_task_queue 自身的
    maxsize 看起来像是背压闸门，但只要这个循环不停地"出队就提交"，队列
    几乎永远填不满、也就永远不会真正阻塞上游的 put()——持续过载下，
    "已提交未完成"的 LLM 工作总量因此不受任何容量约束（executor 内部
    积压无界）。register_internal 文档"数学上界"一节推导的上界只在
    "瞬时"成立，不构成"持续"意义上的上界（详见该节第 14 轮的订正）。

    修复：出队后、submit 前新增一道闸门——acquire
    RuntimeContext.llm_submit_semaphore（容量 LLM_QUEUE_MAXSIZE，与
    inflight_registry 的 "llm" 桶同一个数字），future 完成时（track_
    future 的完成回调，kind="llm" 分支）release 归还，只统计"已经
    submit、尚未完成"这一类工作。

    没有直接拿 inflight_registry.size("llm") 与它的配置容量比较来做
    这件事——那张登记表同时统计"还在排队、消费者还没出队处理过"和
    "已经 submit、还没完成"两类条目，语义上比这里需要的更宽，且第 14
    轮实测证明拿它当闸门会死锁：持续到达的 register_internal 交接可以
    在消费者提交出第一个 future 之前就把登记总量推过容量，此时没有
    任何 future 存在，谁都无法完成、无法释放名额，闸门永久打不开。
    llm_submit_semaphore 从"空载"状态起步，只在真正调用 submit() 前后
    acquire/release，不看登记表当前积压了多少，因此第一次 acquire
    永远能立刻成功，不存在这个死锁窗口（详见 RuntimeContext.__init__
    里 llm_submit_semaphore 的注释与 register_internal 文档"数学上界"
    一节的订正）。

    acquire 不到名额时原地等待（不消费队列下一项），直到有其他 future
    完成、释放出名额——这段等待期间 llm_task_queue 会被新到达的 put()
    持续填满，触达 maxsize 后真正阻塞，背压才能如实沿既有链路传导回
    "transcription" 桶（进而传导到 /api/transcribe 的 try_register
    准入拒绝）。

    等待用 llm_submit_semaphore.acquire(timeout=0.2)（而非无超时的
    acquire()）：既不忙等，又能在等待中途响应关闭信号——循环内每次
    acquire 超时都重新检查一次 stop_event，与本函数出队阶段
    llm_task_queue.get(timeout=0.2) 的短超时轮询是同一种模式。收到
    stop_event 时，已出队但尚未提交的这一项直接放弃（不 submit、不
    release 它在 inflight_registry 里可能持有的登记——这份登记留给
    下次启动的孤儿恢复兜底，与 RuntimeContext._shutdown_llm_owner 关闭
    路径的既有取舍一致），只补一次 llm_task_queue.task_done() 让队列
    自身的记账（unfinished_tasks，_stop_workers 的 llm_drained 检查
    依赖它归零）如实反映"这一项已经离开队列"，然后直接返回，不留下
    一个永远等在闸门里的线程。
    """
    logger.info("启动LLM队列处理器")
    runtime = get_runtime()
    inflight_registry = runtime.inflight_registry

    while not runtime.llm_stop_event.is_set():
        try:
            try:
                llm_task = llm_task_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if llm_task is None:
                llm_task_queue.task_done()
                return

            # 消费泵容量闸门（本地 codex review 第 14 轮，见本函数
            # docstring）：出队之后、submit 之前限制"已提交未完成"的量。
            acquired = runtime.llm_submit_semaphore.acquire(timeout=0.2)
            while not acquired:
                if runtime.llm_stop_event.is_set():
                    logger.info(
                        "LLM队列处理器在容量闸门等待中收到停止信号，"
                        "放弃提交已出队任务（登记保留，交给下次启动的"
                        f"孤儿恢复兜底）: {llm_task.get('task_id')}"
                    )
                    llm_task_queue.task_done()
                    return
                acquired = runtime.llm_submit_semaphore.acquire(timeout=0.2)

            try:
                logger.info(
                    f"LLM任务出队: {llm_task.get('task_id')}，"
                    f"提交到线程池（当前队列任务完成数: {getattr(llm_task_queue, 'completed', '未知')}）"
                )
                future = llm_executor.submit(
                    run_with_runtime, runtime, _handle_llm_task, llm_task
                )
                runtime.track_future(future, kind="llm", task_id=llm_task.get("task_id"))
            except Exception as exc:
                logger.exception(f"提交LLM任务失败: {exc}")
                # 提交失败意味着 _handle_llm_task 永远不会运行——此前转录阶段
                # 写入的 calibrating 中间态不会再被任何路径推进为终态；不在
                # 此处补写 failed，任务会永久停留在 calibrating（客户端持续
                # 轮询）。终态写入本身也可能失败（如 DB 异常），必须单独兜
                # 住：不能让它抛出后跳过下面的 task_done()、或把整个队列
                # 处理器循环带崩——与该循环既有的外层"LLM队列处理器异常"
                # 语义一致，记录后继续消费下一项。
                #
                # 在途任务登记表释放（本地 codex review 第 12 轮 P1）：submit()
                # 本身抛异常意味着 future 从未真正创建，track_future 的完成
                # 回调（release 的主挂点）永远不会触发——必须在这里显式释放，
                # 否则这个 task_id 占用的 "llm" 桶名额永久无法回收。release
                # 本身幂等，即使这个 task_id 从未在 inflight_registry 里登记过
                # （如任务源自 transcription 的内部 handoff，见
                # _InflightTaskRegistry 类文档）也只是静默 no-op。
                submit_fail_task_id = llm_task.get("task_id")
                inflight_registry.release("llm", submit_fail_task_id)
                # 容量闸门信号量同理必须显式 release（本地 codex review
                # 第 14 轮）：上面 acquire 成功之后 submit() 才抛异常，
                # future 从未真正创建，track_future 的完成回调（release
                # 的主挂点）永远不会触发，不手动释放这个名额会永久无法
                # 回收。
                runtime.llm_submit_semaphore.release()
                try:
                    fail_status_written = cache_manager.update_task_status(
                        submit_fail_task_id, TaskStatus.FAILED,
                        error_message=f"提交LLM任务失败: {exc}",
                    )
                    if fail_status_written:
                        logger.info(
                            f"任务状态已更新为 failed: {submit_fail_task_id} (提交LLM任务失败)"
                        )
                    else:
                        current_task = cache_manager.get_task_by_id(submit_fail_task_id)
                        current_status = current_task.get("status") if current_task else "unknown"
                        logger.warning(
                            f"任务状态 CAS 写入 failed 失败(任务已处于终态 {current_status}，"
                            f"未被本次异常覆盖): {submit_fail_task_id} (提交LLM任务失败)"
                        )
                    terminal_write_failed = False
                except Exception:
                    # G1 修复（CI review 第 2 轮 major）：泵循环站点例外——
                    # 与 _handle_llm_task/_handoff_to_llm_stage/
                    # _fail_task_and_notify 那三类"清理后重新抛出"的站点不
                    # 同，这里是 process_llm_queue 本身（单线程顺序消费泵）
                    # 的循环体，没有 future/worker 线程可以承接一次
                    # re-raise——抛出会被下面 202 行的外层
                    # `except Exception as exc:` 接住、sleep(1) 后继续 while
                    # 循环，行为上等价于"这一轮出的意外都不打断消费"，但会
                    # 让 197-200 行原本要做的 task_done() 被跳过（task_done()
                    # 在 raise 之后才执行不到），导致 llm_task_queue 的
                    # unfinished_tasks 计数永久多算一个——_stop_workers 等待
                    # 队列排空的检查会挂住，比"终态写库失败不可观察"本身更
                    # 严重。因此这里保留 log-only：ERROR 级日志（logger.
                    # exception 默认即 ERROR）即当时（CI review 第 2 轮）
                    # 验收原文"可由调用方观察"的观察面——worker 内层站点
                    # （_handle_llm_task 等）已经承担了"异常需要传播到
                    # future"这一半要求，泵循环这一层只需要保证"泵不能死、
                    # task_done() 必须执行"。
                    #
                    # K1 桶 b 补有界补偿（CI review 第 3 轮 major）：log-only
                    # 只解决了"泵不能死"，没有解决"这个任务永久卡在非终态、
                    # 无人再碰"——运行期对账（reconcile_runtime_orphaned_
                    # tasks）虽然最终会兜底，但那是按 created_at 宽限期猜出
                    # 来的，不是针对这次已知失败的显式动作。现在只记
                    # terminal_write_failed 标记，真正的登记动作放到下面
                    # task_done() 之后（见那里的注释）。
                    logger.exception(
                        f"写入LLM任务提交失败终态时再次异常: {submit_fail_task_id}"
                    )
                    terminal_write_failed = True
                llm_task_queue.task_done()
                if terminal_write_failed:
                    # 登记必须放在 task_done() 之后：task_done() 影响的是
                    # llm_task_queue 自身的记账（unfinished_tasks，
                    # _stop_workers 排空检查依赖它归零），与这里的终态
                    # 补偿登记是两件独立的事，不应该互相牵连——即使
                    # task_done() 本身出意外，也不该连带丢失这次登记（现实
                    # 中 task_done() 内部只是计数器操作，几乎不会失败，这里
                    # 只是保持两件事在时序上互不依赖）。RuntimeContext.
                    # terminal_write_pending 的详细说明见其字段注释与
                    # context._retry_terminal_write_pending 的文档：
                    # _periodic_maintenance 每轮维护会 drain 这个集合并对
                    # 每个 id 重试写 FAILED。
                    runtime.register_terminal_write_pending(submit_fail_task_id)
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
                processing_options = normalize_processing_options(
                    llm_task.get("processing_options")
                )
                calibrate_requested = processing_options.get("calibrate", True)
                summarize_requested = processing_options.get("summarize", True)
                chapters_requested = processing_options.get("chapters", True)
                infer_speaker_names_requested = processing_options.get(
                    "infer_speaker_names", True
                )

                # 通用下载器无标题时使用 LLM 生成
                if _requires_llm_title(
                    processing_options,
                    use_speaker_recognition=use_speaker_recognition,
                ):
                    video_title = _generate_title_if_needed(
                        llm_task, video_title, transcript
                    )
                llm_task["video_title"] = video_title

                # 使用新 LLM 协调器处理任务（用 PerfTracker 记录 LLM 处理耗时）
                logger.info(f"开始使用 LLM 协调器处理任务: {task_id}")

                # 准备内容参数
                content = _prepare_llm_content(llm_task, transcript, use_speaker_recognition)

                # 是否为仅校对模式（重新校对场景）
                calibrate_only = llm_task.get("calibrate_only", False)

                # 处理深度开关（只转录/转录+校对/全流程）：缺失时按全流程兜底，
                # 与 processing_options.normalize_processing_options(None) 语义一致。
                # recalibrate 场景不设置该键，天然落到全流程默认，不影响既有行为。
                # 仅校对模式下，若缓存里 llm_summary.txt 缺失/为空，顺手补跑一次 summary
                # 避免老任务卡在 view 页的 "总结处理中..." 状态
                summary_backfill = False
                force_chapters_recompute = False
                cache_snapshot_for_flags = None
                if calibrate_only and platform and media_id:
                    cache_snapshot_for_flags = cache_manager.get_cache(
                        platform, media_id,
                        use_speaker_recognition=use_speaker_recognition,
                    )
                    if _should_backfill_summary(
                        cache_snapshot_for_flags or {}, calibrate_only=True
                    ):
                        summary_backfill = True
                        logger.info(
                            f"recalibrate: llm_summary missing for {task_id}, "
                            f"auto-backfill enabled"
                        )
                    # R6: recalibrate forces chapters recompute when prior
                    # chapters_status was GENERATED (unlike summary backfill).
                    old_status = (
                        (cache_snapshot_for_flags or {}).get("llm_status") or {}
                    ).get("chapters_status")
                    if old_status == ChaptersStatus.GENERATED:
                        force_chapters_recompute = True
                        logger.info(
                            f"recalibrate: prior chapters GENERATED for {task_id}, "
                            f"force recompute"
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
                # recalibrate 路由显式写入规范化默认值（calibrate=True），
                # 因此 recalibrate 永远真实执行校对，不受本开关影响。
                skip_calibration_for_coordinator = not calibrate_requested

                # chapters 跳过判定（R6）：
                # - calibrate_only（recalibrate）：路由（tasks.py）用
                #   normalize_processing_options(None) 写入默认 processing_options，
                #   chapters_requested 恒为 True，不能据此决定是否跑章节——
                #   仅旧状态为 GENERATED（force_chapters_recompute=True）时强制
                #   重算，SKIPPED_*/FAILED/DISABLED/无状态一律跳过，避免对旧
                #   状态任务产生非预期的章节 LLM 调用费用。
                # - 非 calibrate_only（正常 transcribe 路径）：仅由本轮
                #   processing_options.chapters 决定（此时 force_chapters_recompute
                #   恒为 False——它只在 calibrate_only 分支内置 True）。
                if calibrate_only:
                    skip_chapters_for_coordinator = not force_chapters_recompute
                else:
                    skip_chapters_for_coordinator = not chapters_requested

                # Cache-side chapters seed (this-round dialogs preferred inside
                # coordinator after calibration). Keep llm/ package pure.
                timeline_segments_seed = None
                chapters_seed_kind = "none"
                if not skip_chapters_for_coordinator:
                    timeline_segments_seed, chapters_seed_kind = (
                        _resolve_chapters_timeline_segments(
                            llm_task=llm_task,
                            platform=platform,
                            media_id=media_id,
                            use_speaker_recognition=use_speaker_recognition,
                        )
                    )
                    logger.info(
                        f"chapters timeline seed for {task_id}: kind={chapters_seed_kind}"
                    )

                # 调用新架构（包含校对、总结、章节）
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
                        infer_speaker_names=infer_speaker_names_requested,
                        # 分层缓存"只补总结"场景：transcription.py 把 content 强制
                        # 降级为纯文本（transcription_data=None，避免重跑说话人
                        # 分块校对），协调器自身的说话人数自动推断因此必然判成单
                        # 说话人（0）。cached_speaker_count 是 transcription.py 从
                        # 缓存的 llm_processed.json 里读回的真实说话人数，回传给
                        # 协调器覆盖这个误判，让总结仍然使用多说话人 Prompt
                        # （codex-review R5 #3）。None 表示没有更优信息，协调器按
                        # 自身推断走，不影响其余调用方。
                        speaker_count_hint=llm_task.get("cached_speaker_count"),
                        skip_chapters=skip_chapters_for_coordinator,
                        timeline_segments=timeline_segments_seed,
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
                    chapters_status = effective_status.get("chapters_status")

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
                        (
                            calibration_status is None
                            or summary_status is None
                            or chapters_status is None
                        )
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
                            # 通知误报修复（codex-review R8 #1）：summary_status
                            # 为 None 说明本轮压根没碰总结层（例如分层缓存"只补
                            # 校对"，processing_options.summarize=False），但缓存
                            # 里可能已经有真实落盘的总结——result_dict["内容总结"]
                            # 此时仍是协调器本轮的 None，若不回填，随后的完成通知
                            # 会把"缓存里真实存在的总结"误报成"总结未生成"，丢失
                            # 用户本该看到的内容。
                            _restore_cached_summary_for_notification(
                                result_dict, merged_snapshot,
                            )
                        if chapters_status is None:
                            chapters_status = merged_llm_status.get("chapters_status")

                    result_stats["calibration_status"] = calibration_status
                    result_stats["summary_status"] = summary_status
                    result_stats["chapters_status"] = chapters_status

                logger.info(f"LLM任务处理完成: {task_id}, 标题: {video_title}")

                # 任务成功完成，输出完整性能摘要
                tracker.log_summary()

                # LLM 阶段拥有终态：产物已通过 _save_llm_results 落盘，此时才置 success
                # （对所有任务生效，不再仅限 calibrate_only；终态由本阶段统一写回）
                # 同时把诚实状态模型镜像写入 task_status 表两列，供 /api/audit/history 查询消费。
                #
                # 先写终态、再决定是否通知（本地 codex review 第 7 轮 H2）：
                # update_task_status 是 compare-and-set，终态黏性会拒绝覆盖一个已经
                # 处于 success/failed 的任务行——例如任务已被关闭清算或恢复流程判定
                # 为 failed（超时/异常）。此前的顺序是"先发完成通知、再写 CAS、且忽略
                # 返回值"：CAS 落败时用户已经收到了"任务完成"通知，日志也无条件打印
                # "已更新为 success"，两者都是谎言——数据库和审计快照记录的其实是
                # failed。改为先写 CAS、检查返回值，只有真正赢得这次终态写入时才发送
                # 完成通知；落败时记录 warning（附带当前的真实终态）且不再通知。
                done_message = "重新校对完成" if calibrate_only else "校对完成"
                final_stats = result_dict.get("stats", {})
                status_written = cache_manager.update_task_status(
                    task_id,
                    TaskStatus.SUCCESS,
                    platform=platform,
                    media_id=media_id,
                    title=video_title,
                    author=llm_task.get("author", ""),
                    calibration_status=final_stats.get("calibration_status"),
                    summary_status=final_stats.get("summary_status"),
                    chapters_status=final_stats.get("chapters_status"),
                    terminal_snapshot={
                        "result": result_dict,
                        "processing_options": processing_options,
                    },
                )
                if status_written:
                    logger.info(f"任务状态已更新为 success: {task_id} ({done_message})")
                    # 发送通知（多渠道）——只在真正赢得终态写入时才通知用户"完成"。
                    #
                    # K3 修复（本地 codex review 第 8 轮）：H2 把 CAS 提到通知
                    # 之前是对的，但通知调用此前仍在外层通用失败处理的
                    # try/except（下面的 `except Exception as exc:`）覆盖范围
                    # 内——success 已经落库后，_send_notification 抛出的任何
                    # 异常（通知渠道超时/限流等）都会被那个 except 当成"任务
                    # 失败"处理：返回值上把任务判成失败、发一条误导性的
                    # "【LLM API调用异常】"通知、且无条件声称"已更新为
                    # failed"（即使 FAILED CAS 被终态黏性拒绝，因为任务其实
                    # 已经是 success）。这里用独立的 try/except 兜住通知本身
                    # 的异常：通知失败只记日志，不影响已经写定的任务结果，
                    # 也不会误触发失败通知/失败日志。
                    if not calibrate_only:
                        try:
                            _send_notification(
                                task_id=task_id,
                                video_title=video_title,
                                display_url=display_url,
                                use_speaker_recognition=use_speaker_recognition,
                                result_dict=result_dict,
                                notification_channel=notification_channel,
                                notification_webhooks=notification_webhooks,
                            )
                        except Exception:
                            logger.exception(
                                f"完成通知发送失败（任务已成功落库，不影响任务结果）: {task_id}"
                            )
                else:
                    current_task = cache_manager.get_task_by_id(task_id)
                    current_status = current_task.get("status") if current_task else "unknown"
                    logger.warning(
                        f"任务状态 CAS 写入 success 失败(任务已处于终态 {current_status}，"
                        f"可能已被关闭清算/恢复流程判定)，跳过完成通知: {task_id}"
                    )

            except Exception as exc:
                logger.exception(f"LLM任务处理异常: {task_id}, 错误: {exc}")
                # LLM 处理失败时输出已记录的性能摘要
                tracker.log_summary()

                # 终态由 LLM 阶段统一写回（对所有任务生效，修复普通任务 LLM 失败被静默的问题）
                #
                # R2 修复（PR3 review hardening）：此前失败通知
                # （task_notifier.send_text）排在 FAILED CAS 之前——通知调用
                # 抛出的异常（webhook 超时/限流等）会直接跳出这层 except，
                # 既不会被下面的 try/except 兜住，也不会走到 finally 之外的
                # 任何终态写入，任务永久停在 calibrating（非终态），客户端
                # 一直轮询却再也等不到结果。与 K3（成功侧改为先 CAS、后拿
                # 独立 try/except 包住通知）同一套顺序：先写 FAILED CAS 并
                # 检查返回值，终态落定后再尝试通知；通知异常只记日志，不
                # 影响已经写定的 failed 结果。
                fail_message = (
                    f"重新校对失败: {exc}" if llm_task.get("calibrate_only")
                    else f"LLM处理失败: {exc}"
                )
                try:
                    # K3 修复：FAILED CAS 也是 compare-and-set，终态黏性同样
                    # 可能拒绝这次写入（例如上面的 try 块其实已经成功写过
                    # success，只是 _send_notification 抛了异常——但那条路径
                    # 现在已经被上面的独立 try/except 兜住，不会再走到这里；
                    # 这里检查返回值是为了其它真正在 success CAS 之前就失败
                    # 的路径，不能无条件声称"已更新为 failed"）。
                    fail_status_written = cache_manager.update_task_status(
                        task_id, TaskStatus.FAILED, error_message=fail_message,
                    )
                except Exception:
                    # G1 修复（CI review 第 2 轮 major）：此前这里是
                    # `except Exception: pass`——终态写库异常被完全静默吞掉，
                    # 连日志都没有，函数正常走到 finally 的 task_done() 后
                    # 返回，调用方（run_with_runtime 包装后提交给
                    # llm_executor 的 future）看不到任何异常，任务永久停在
                    # calibrating（非终态），只能靠运行期对账（最长 ~27h）
                    # 才会被发现，是本文件里最彻底的一处"终态写库失败不可
                    # 观察"。改为记日志后重新抛出：本函数是
                    # process_llm_queue 消费泵 submit 给 llm_executor 的
                    # worker 入口（见该函数），异常会传出这个 except 块，
                    # 逐层往外传播（跳过下面 finally 之外的所有代码，但
                    # finally 本身仍会执行 task_done()），最终从
                    # run_with_runtime 传出，被 RuntimeContext.track_future
                    # （kind="llm"）的完成回调观察到——future 完成即释放
                    # inflight_registry 的 "llm" 名额与 llm_submit_semaphore，
                    # 不依赖终态写入是否成功。finally 块保证无论这里是否
                    # 重新抛出，入队失败的通知都会被尝试一次。
                    logger.exception(
                        f"收敛 failed 终态写入异常: {task_id} ({fail_message})"
                    )
                    raise
                finally:
                    try:
                        task_notifier.send_text(f"【LLM API调用异常】{exc}")
                    except Exception:
                        logger.exception(
                            f"失败通知发送失败（任务终态已落库，不影响任务结果）: {task_id}"
                        )

                if fail_status_written:
                    logger.info(f"任务状态已更新为 failed: {task_id} ({fail_message})")
                else:
                    current_task = cache_manager.get_task_by_id(task_id)
                    current_status = current_task.get("status") if current_task else "unknown"
                    logger.warning(
                        f"任务状态 CAS 写入 failed 失败(任务已处于终态 {current_status}，"
                        f"未被本次异常覆盖): {task_id} ({fail_message})"
                    )
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
        # chapters_status 为 None 表示本轮未尝试章节（skip_chapters）；仅 GENERATED
        # 时 chapters 列表非空。
        "chapters_status": stats.get("chapters_status"),
        "chapters": stats.get("chapters") or [],
        "chapters_fingerprint": stats.get("chapters_fingerprint"),
        "chapters_segment_count": stats.get("chapters_segment_count"),
        "chapters_source_kind": stats.get("chapters_source_kind"),
    }

    if "structured_data" in coordinator_result:
        result_dict["structured_data"] = coordinator_result["structured_data"]

    return result_dict


def _restore_cached_summary_for_notification(result_dict: dict, merged_snapshot: Optional[dict]) -> None:
    """分层缓存"只补校对"场景下，用缓存里已有的总结文本回填通知用的结果字典。

    背景（codex-review R8 #1）：coordinator 本轮若因
    processing_options.summarize=False 而跳过总结（例如缓存已有
    llm_summary.txt，本轮只补校对层），_build_result_dict() 产出的
    result_dict["内容总结"] 仍是 None、skip_summary=True——这只反映"本轮
    是否重新生成"，不反映"总结是否存在"。_save_llm_results 内部对
    llm_status.json 的合并语义是对的（保留旧值），但随后 _send_notification
    直接消费这份内存态的 result_dict，会把缓存里真实存在的总结误报成
    "总结未生成"。

    只在"本轮确实没有产出总结文本 + 缓存里确实有真实落盘的总结"两个条件都
    满足时才回填：调用方（_handle_llm_task）已经把本次调用限定在
    summary_status 合并前为 None（本轮未触碰该层）的分支里，因此这里不会
    把"本轮真实尝试但失败"（FAILED，缓存不会有 llm_summary.txt）误伪装成
    成功。

    修复（codex-review R9 P2）：仅"llm_summary.txt 存在"不足以证明它是一份
    真正的总结——诚实状态模型里 summary_status=SKIPPED_SHORT 时，
    llm_summary.txt 存的是"文本过短，保存校对文本作为总结"的兜底内容（完整
    校对文本，不是真总结，见 _save_llm_results 的 SKIPPED_SHORT 分支）。
    若不区分，只补校对的请求会把这份兜底内容当"内容总结"回填进通知，既误导
    用户，又绕开了校对文本通知路径自身的 5000 字截断（"总结"分支不做长度
    截断）。因此这里额外核实合并后（llm_status.json）的 summary_status
    必须是 GENERATED 才回填；SKIPPED_SHORT/FAILED/DISABLED/PENDING 均维持
    "未生成"语义不变，交由 _send_notification 按各自既有文案处理。

    Args:
        result_dict: _build_result_dict() 的输出，原地修改（补回总结文本、
            skip_summary 与 stats.summary_length）
        merged_snapshot: cache_manager.get_cache(...) 返回的合并后缓存快照，
            可能为 None（缓存未命中）
    """
    if result_dict.get("内容总结") is not None:
        return
    if not merged_snapshot or "llm_summary" not in merged_snapshot:
        return

    merged_llm_status = merged_snapshot.get("llm_status") or {}
    if merged_llm_status.get("summary_status") != SummaryStatus.GENERATED:
        return

    cached_summary_text = merged_snapshot.get("llm_summary") or ""
    if not cached_summary_text:
        return

    result_dict["内容总结"] = cached_summary_text
    result_dict["skip_summary"] = False
    result_dict.setdefault("stats", {})["summary_length"] = len(cached_summary_text)
    logger.info("本轮未生成总结，已从缓存回填真实总结文本用于通知")


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


def structured_dialogs_consistent_with_mapping(
    structured: Any, mapping: Optional[dict]
) -> bool:
    """判断一份 llm_processed.json 结构化产物（dialogs）里展示的说话人姓名，
    是否与给定的权威 speaker_mapping 一致。

    与下面 _refresh_speaker_names_in_existing_structured_artifact"第三步：
    分叉判定"同一套逐条 speaker_id 比对 idiom（该函数 docstring 里 K5 修复
    (a) 的论证——mapping 整体相等不能替代逐条比对，因为 dialogs 与
    speaker_mapping 是两份独立持久化的产物——同样适用于这里）。提取为独立
    的只读判断函数，供 transcription.py 的"完整命中"判定复用，避免针对
    同一个问题重新发明第二套一致性判断（本地 codex review 第 16 轮 Q3）。

    Args:
        structured: cache_data.get("llm_processed")，可能为 None/非 dict/
            形状不符（一律视为不一致，调用方据此判定需要重建）。
        mapping: 当前权威映射（例如 cache_manager.get_speaker_mapping(...)
            返回结果的 "mapping" 字段）。None 表示调用方自己都拿不到权威
            映射，无法核验——一律视为不一致，交由调用方决定是否需要重建
            （不強行放行一份无法验证的产物）。

    Returns:
        bool: True 表示可信一致，或因旧格式（dialogs 完全没有 speaker_id）
            无法逐条核验而按既有"旧格式兼容"立场放行；False 表示确认存在
            分叉，或产物形状不符/无权威映射可比对。
    """
    if not isinstance(structured, dict) or not isinstance(mapping, dict):
        return False
    dialogs = structured.get("dialogs")
    if not isinstance(dialogs, list) or not dialogs:
        return False
    relevant_dialogs = [d for d in dialogs if isinstance(d, dict)]
    if not relevant_dialogs:
        return False

    # 旧格式兼容：与 _refresh_speaker_names_in_existing_structured_artifact
    # 同一立场——完全没有 speaker_id 的旧 schema 没有原始标签可以精确核验，
    # 不算"不一致"，按既有产物可信处理（避免旧 schema 数据被无谓地反复
    # 判定为需要重建）。
    has_raw_labels = any(d.get("speaker_id") is not None for d in relevant_dialogs)
    if not has_raw_labels:
        return True

    for dialog in relevant_dialogs:
        speaker_id = dialog.get("speaker_id")
        if speaker_id is None or speaker_id not in mapping:
            return False
        if mapping.get(speaker_id) != dialog.get("speaker"):
            return False
    return True


def structured_artifact_is_refreshable(
    structured: Any, mapping: Optional[dict] = None
) -> bool:
    """判断一份 llm_processed.json 结构化产物是否具备"被说话人姓名补层刷新"
    的资格——J2 修复（本地增量复核第 3 轮）。

    此前 transcription.py 排队侧只用 isinstance(structured, dict) 判断"可
    刷新"，但 _refresh_speaker_names_in_existing_structured_artifact 真正
    尝试写入前还有一整套更严格的前置校验（speaker_mapping/dialogs 字段
    完整、dialogs 非空、每条 dialog 都带非空 speaker_id）——排队侧因此会
    对"存在但不可刷新"的产物（空 dict、缺字段、旧 schema、混合 schema、
    空 dialogs）误判为可刷新，排队后白烧一次 LLM 推断，helper 侧再静默
    跳过，结果永远不可见。这里把两处的判定前置抽成同一份实现，从根上
    堵住口径分裂。

    判定分两层：

    - schema 层（结构形状，与具体映射内容无关）：structured 是 dict，
      speaker_mapping 字段是 dict，dialogs 是非空 list，且每一条 dialog
      都是 dict 且带非空 speaker_id——任意一条 dialog 不是 dict（K3，CI
      review 第 3 轮 minor：此前会先过滤掉这类畸形条目、再只校验剩下的，
      混合 dialogs 因此会被误判为可刷新）都直接判定整体不可刷新，不再
      过滤后各自为政。这一层不依赖"新映射"是否已经推断出来，排队时点
      （当前媒体的映射可能尚未解析，甚至压根不需要真实推断）就能判定，
      因此排队侧只需要调用这一层（不传 mapping）。

    - mapping 层（可选）：在 schema 层通过的前提下，进一步要求每条
      dialog 的 speaker_id 都能在给定的 mapping 里解析出一个展示名。只
      有真正持有本轮权威映射的 helper 侧才具备判定这一层的条件，调用时
      显式传入 mapping；排队侧此时点通常拿不到这份映射（本轮可能就是
      要去推断它），因此不传（mapping=None 时这一层直接跳过，只看
      schema 层）。

    Args:
        structured: 待判定的结构化产物（如 cache_data.get("llm_processed")
            / existing_snapshot.get("llm_processed")）。
        mapping: 可选，权威 speaker_mapping（{speaker_id: 展示名}）。为
            None 时只做 schema 层判定；传入 dict 时叠加 mapping 层判定。

    Returns:
        bool: True 表示（在已提供的判定层级下）具备刷新资格。
    """
    if not isinstance(structured, dict):
        return False
    if not isinstance(structured.get("speaker_mapping"), dict):
        return False
    dialogs = structured.get("dialogs")
    if not isinstance(dialogs, list) or not dialogs:
        return False
    # K3（CI review 第 3 轮 minor）：此前先过滤掉非 dict 的畸形 dialog 条目、
    # 再只对剩下的"看起来正常"的条目做校验——混合 dialogs（部分合法 dict、
    # 部分畸形非 dict 条目）因此会被误判为可刷新，实际却只覆盖了其中一部分
    # 内容，且畸形条目本身的存在恰恰说明这份产物形状不可信。现在任意一条
    # 非 dict 条目直接判定整体不可刷新（不再过滤后各自为政），与"结构形状
    # 完整"这条前提保持一致。
    if any(not isinstance(d, dict) for d in dialogs):
        return False
    if any(d.get("speaker_id") is None for d in dialogs):
        return False
    if mapping is not None:
        if any(d.get("speaker_id") not in mapping for d in dialogs):
            return False
    return True


def _refresh_speaker_names_in_existing_structured_artifact(
    *,
    task_id: str,
    platform: str,
    media_id: str,
    use_speaker_recognition: bool,
    existing_snapshot: Optional[dict],
    new_speaker_mapping: Any,
    speaker_inference_source: Optional[str] = None,
) -> None:
    """补层刷新：calibrate=False 但 infer_speaker_names=True 时，说话人姓名
    推断这一轮会产出全新的 speaker_mapping（并已经由 SpeakerInferencer.infer()
    在本函数运行之前，就先行落盘到 speaker_mapping.json——见该函数的
    save_speaker_mapping 调用），但 _save_llm_results 的 suppress_calibration
    保护为了不让本轮未经校对的占位文本覆盖已有的、真正校对过的
    llm_processed.json，把整个 structured_data 保存都跳过了——这导致查看页
    实际渲染用的 dialogs[i]["speaker"]（渲染时直接读这个字段，见
    utils/rendering/dialog_renderer.py::_render_from_structured_data，不会
    动态回查 speaker_mapping）永远停留在上一次真实校对时解析出的旧姓名，
    "仅补姓名"这一轮的推断结果对用户来说等于没生效（本地 Codex review
    发现，第 3 轮）。

    本轮（第 4 轮）重做的一处：旧实现按"旧 speaker_mapping 反查显示名"
    （{display_name: raw_label}）为 key 定位每条 dialog 对应的原始标签，
    两个不同原始标签共享同一个旧显示名时（如都无法识别、退化成同一个
    通用占位名，或两位嘉宾恰好同名）会用 dict 覆盖语义丢掉其中一个，导致
    该原始标签名下的全部 dialog 被按另一个原始标签的新姓名错误覆盖——
    真实发生过的"张冠李戴"数据损坏，而不只是理论风险。改为直接读取每条
    dialog 自带的 "speaker_id" 字段（SpeakerAwareProcessor._normalize_dialog
    现在会随 "speaker"（显示名）一起保留这个原始标签，schema 演进见该处
    改动），完全不需要反查，天然不会碰撞。

    旧格式兼容：若既有 dialogs 是本次 schema 演进之前产出的（不带
    speaker_id），没有原始标签可以精确定位，不做有损猜测——跳过刷新，
    仅记录一条 warning 说明限制（展示名会在下次完整处理/重新校对、产出
    新schema 的 structured_data 时自然更新）。这种情况不算"刷新失败"：
    重试也无法让一份已经不带 speaker_id 的旧产物凭空长出这个字段，继续
    保留（已经由 infer() 写盘的）新 mapping 不会有任何害处，回滚只会让
    每次请求都白白重烧一次推断 token。

    本轮（第 6 轮，local codex review G4）新增 speaker_inference_source
    门槛：旧实现只按"新旧 mapping 是否相等"判断是否刷新——LLM 调用的瞬时
    故障（网络抖动/限流/超时）在 SpeakerInferencer.infer() 里会退化为
    identity fallback（{label: label}），这个 identity mapping 几乎总是
    不同于已有的、真正推断过的好 mapping，会被"新旧不等就刷新"误判成
    合法更新，直接把已经展示的真名覆盖成"说话人N"占位符，而任务本身仍以
    success 收尾（调用方 _save_llm_results 不知道这次刷新其实是在拿一份
    没有任何真实推断依据的结果搞破坏）。"identity_fallback"（LLM 异常/无
    有效样本/allow_llm=False/说话人列表为空）在这里直接跳过，不触碰既有
    产物；缺省 None（未显式传入）视为放行——生产环境唯一调用方（下方
    _save_llm_results）现在总会显式传入这个字段，宽松默认只是为了兼容
    手工构造调用参数的既有单测，它们本身模拟的就是"本轮真实推断成功"
    这一场景。

    第 7 轮（H6）修正 "cache_hit" 的处理：此前 "cache_hit"（命中已持久化的
    speaker_mapping.json，本轮未发起新的 LLM 调用）与 identity_fallback
    一样被无条件跳过，理由是"既有产物理论上早已与它一致"——但这个假设
    并不总成立：speaker_mapping.json（映射的权威存储）与
    llm_processed.json 内嵌的 dialogs 展示姓名，是两份独立持久化的产物，
    可能因历史部分写入、推断阈值变化等原因各自独立漂移，一旦分叉，旧的
    "无条件跳过"会让这个分叉永久存在、没有任何自愈机会。现在 cache_hit
    仍然零 LLM 成本（本轮确实没有发起新的推断调用），但会往下走到与
    "llm" 来源完全相同的按 speaker_id 逐条比对逻辑：姓名一致则不产生
    多余写入，一旦发现某条 dialog 的展示姓名与刚读到的权威映射不一致，
    照样执行刷新——这正是 cache_hit 命中的映射本该反映的真实状态。

    第 8 轮（K5，本地 codex review）：把决策逻辑整体重构为单一的"是否可
    刷新 -> 是否已经一致 -> 全量原子写入"三段判定，修正三个同源缺陷（都
    是决策逻辑碎片化的产物，此前分散在函数各处、各自独立判断）：

    (a) 此前 "整份 mapping 相等即跳过" 这条捷径只对非 cache_hit 来源
        （即 "llm"/None）保留，理由是"本轮确实刚推断，mapping 不变蕴含
        逐条比对结果也不会变"——但这个假设同样不成立：dialogs 与
        speaker_mapping 是两份独立持久化的产物，即便本轮 llm 推断出的
        新 mapping 恰好与旧 mapping 相等，也不能反推 dialogs 展示名此刻
        已经与它一致（可能是更早一轮 llm 推断成功但展示刷新失败后，
        mapping 通过其它路径被重新写成同一个值）。现在彻底删除这条捷径，
        所有来源统一走下面的逐条 speaker_id 比对来判定是否需要刷新，不
        再看 mapping 整体相等性。

    (b) 此前"是否刷新"由 any()（任意一条 dialog 的姓名变了就整体判定为
        "changed"）驱动，但换算 refreshed_dialogs 时，缺 speaker_id 或
        speaker_id 不在新映射里的 dialog 会被跳过、保留旧姓名——而顶层
        speaker_mapping 仍会整份替换成新映射。这会造成部分提交：产物里
        一部分 dialog 的展示名对应新映射，另一部分仍对应已被替换掉的旧
        映射，内部自相矛盾。现在改为先做一次前置校验：只有当"所有相关
        dialog 都带有非空 speaker_id，且该 speaker_id 都能在新映射里解析
        出姓名"时才认为整体可刷新（refreshable）；只要有一条不满足，就
        整体跳过、不做任何改动（不是失败，语义等同旧格式兼容分支：新
        speaker_mapping 仍会保留，展示名留到下次完整处理时随新 schema 一
        并更新）。可刷新时才继续判断是否存在展示名分叉（divergent），
        分叉时才发生一次全量原子写入——不会再出现只改一部分 dialog、
        mapping 却整份替换的中间态。

    (c) 此前刷新写入失败时，不分来源一律 cache_manager.invalidate_
        speaker_mapping() 回滚——但 "cache_hit" 来源读到的 mapping 是历史
        上已经真实推断、成功持久化过的有效资产，本轮并未产生新的 LLM
        计算；回滚（删除 speaker_mapping.json）会让下一次相同
        input_fingerprint 的请求被错误地判定为缓存未命中，被迫重新真实
        调用一次 LLM，只为找回一份其实一直有效的旧结果。现在回滚只保留
        给 "llm"（含默认 None，视为本轮新持久化）来源；"cache_hit" 来源
        写入失败时保留原有效 mapping 不动，只如实 raise 上报"这次展示
        刷新失败了"，不静默声称成功，也不牵连本该继续有效的历史资产——
        与上面 (b) 的"跳过不算失败，不回滚"要严格区分：这里是"确实尝试
        写入且真正失败"，仍然要 raise，只是不删 mapping。

    第 9-10 轮（V5/G2，PR3 review hardening 二轮）曾在这里加过"无既有产物
    可刷新时，把本轮 structured_data 当首份产物首次落盘"的分支，并给它配
    过一道"既有校对必须确认 FULL 才允许落盘"的门槛（G2）。本轮（H2，增量
    复核）整段移除，原因：

    (a) G2 的 FULL 门槛判错了对象——它核验的是旧 llm_calibrated 文本是否
        完整，不是本轮 structured_data 的来源是否经过校对；suppress_
        calibration=True 恒等于"本轮走 skip_calibration=True 的未校对
        原文"，即便旧校对确认 FULL，把这份原文当结构化产物落盘仍会被
        DialogRenderer 无条件优先渲染，用生肉覆盖/伪装已经完整的校对
        成果，FULL 门槛没能挡住这个问题。

    (b) V5 引入这个分支要解的"结构化产物永远缺失、每次请求都重新排队
        推断、纯烧 LLM token"死循环，已经由 transcription.py 的 X1 修复
        （PR3 review hardening 三轮）在源头解决：当校对层已经真正满足
        （calibrated_layer_satisfied）时，结构化产物单纯缺失不再触发
        need_speaker_names 重排队（见 transcription.py 对应注释），
        所以这里不需要靠"首次落盘"自愈。

    (c) 结构化产物缺失时，DialogRenderer 本就会回退到 llm_calibrated.txt
        的平文本渲染（见 utils/rendering/dialog_renderer.py 的
        "structured" -> "normal" 策略回退），用户仍能看到已完成的校对
        内容，不是功能缺失，只是这一轮"仅补姓名"的推断结果这次不落盘。

    现在没有旧产物可"刷新"时，统一记一条 info 日志说明跳过，不做任何
    写入——与"structured_data 非 dict"的既有跳过语义完全一致，也不再需要
    额外的 structured_data 参数。真正的"这份媒体的第一份结构化产物"，
    仍然只能通过 `not suppress_calibration`（真实完整校对轮）那条完整
    保存分支产生。

    第 11 轮（J2，本地增量复核第 3 轮）：把"是否可刷新"的前置校验（此前
    分散在这里的 has_raw_labels/unresolvable 两段几乎重复的判定）抽成
    独立的模块级函数 structured_artifact_is_refreshable，与
    transcription.py 排队侧判断"要不要排队 need_speaker_names"时用的
    schema 层判定共用同一份实现——此前排队侧只查
    isinstance(cached_structured_for_names, dict)，比这里真正尝试写入前
    的判定宽松得多，"存在但不可刷新"（空 dict、缺字段、旧 schema、混合
    schema、空 dialogs）的产物会被排队侧误判为可刷新，排队后真烧一次
    LLM 推断，这里再静默跳过、结果永远不可见。这个函数把校验拆成
    schema 层（结构形状，排队侧可判）与 mapping 层（speaker_id 能否在
    权威映射里解析出姓名，只有真正持有映射的这里才判得了）两层，本函数
    调用时传入 new_speaker_mapping，两层都会被校验。

    Args:
        existing_snapshot: cache_manager.get_cache(...) 的既有缓存快照
            （调用方已确认非空，因为 suppress_calibration=True 蕴含
            calibrated_exists_before=True，即 existing_snapshot 曾经非空）。
        new_speaker_mapping: 本轮协调器产出的 structured_data["speaker_mapping"]，
            类型未经调用方校验，可能不是 dict（防御性处理）。
        speaker_inference_source: SpeakerAwareProcessor.process() 透传的
            SpeakerInferencer.infer() 返回值里的 "source" 字段
            （"llm"/"cache_hit"/"identity_fallback"/None），见上方说明。

    Raises:
        OSError: 展示产物刷新真正尝试写入且失败
            （cache_manager.save_llm_result 返回 False）。调用方
            （_save_llm_results）没有包 try/except，异常会照原样传播到
            _handle_llm_task 的外层异常处理，把本次任务标记为 failed。
    """
    if speaker_inference_source == "identity_fallback":
        logger.info(
            f"说话人姓名补层：本轮映射来源为 {speaker_inference_source}"
            f"（非本轮真实 LLM 推断），跳过刷新展示产物，避免用占位映射"
            f"覆盖已有好名字: {platform}/{media_id}"
        )
        return

    def _write_structured_artifact(content: dict, *, log_verb: str) -> None:
        """全量原子写入 + 失败回滚（K5 修复 (c) 的落地位置），供"按新映射
        刷新既有产物"分支调用（H2 移除了曾经共用这份逻辑的"无旧产物、
        首次落盘"分支，见本函数 docstring）。写盘失败时，"llm"（含默认
        None）来源要回滚早于本函数已经落盘的 speaker_mapping.json，让
        下一次请求视为缓存未命中、自然重新触发完整推断；"cache_hit" 来源
        读到的是历史上已经真实有效的旧映射，回滚只会让下次相同指纹被迫
        重新调用一次 LLM，因此只如实上报失败、不动它。"""
        save_ok = cache_manager.save_llm_result(
            platform=platform,
            media_id=media_id,
            use_speaker_recognition=use_speaker_recognition,
            llm_type="structured",
            content=content,
        )
        if save_ok:
            logger.info(
                f"说话人姓名补层已{log_verb}展示产物: "
                f"{platform}/{media_id}/llm_processed.json"
            )
            return
        if speaker_inference_source == "cache_hit":
            logger.error(
                f"说话人姓名补层{log_verb}展示产物失败（cache_hit 来源，保留"
                f"原有效 mapping 不回滚，避免下次相同指纹被迫重新调用 LLM）: "
                f"{platform}/{media_id}"
            )
        else:
            cache_manager.invalidate_speaker_mapping(platform, media_id)
        raise OSError(f"说话人姓名补层{log_verb}展示产物失败: {task_id}")

    old_structured = (existing_snapshot or {}).get("llm_processed")
    if not isinstance(old_structured, dict):
        # H2（增量复核）：没有旧产物可"刷新"时不再把本轮 skip_calibration=True
        # 产出的未校对原文当首份产物落盘（此前的 V5/G2 分支，已整段移除，
        # 理由详见本函数 docstring）。结构化产物缺失时 DialogRenderer 本就
        # 回退到 llm_calibrated.txt 的平文本渲染，用户不会因此丢失已完成的
        # 校对内容；这份媒体真正的首份结构化产物，只能来自真实完整校对轮
        # （not suppress_calibration）的完整保存分支。
        logger.info(f"说话人姓名补层：无既有结构化产物可刷新，跳过: {platform}/{media_id}")
        return
    if not isinstance(new_speaker_mapping, dict):
        return  # 本轮没有可用的新映射，无需刷新

    # J2 修复（本地增量复核第 3 轮）：可刷新性前置校验统一收口到
    # structured_artifact_is_refreshable——与 transcription.py 排队侧的
    # schema 层判定共用同一份实现（该函数 docstring 有完整分层说明），不
    # 再各自维护一套"存在但不可刷新"的校验逻辑（旧格式缺 speaker_id、
    # 混合 schema、空 dialogs 等，此前分散成 has_raw_labels/unresolvable
    # 两段几乎重复的判定）。这里额外传入 new_speaker_mapping，叠加排队侧
    # 看不到的 mapping 层校验（speaker_id 必须能在本轮新映射里解析出
    # 姓名）。任何一层没通过都整体跳过、不做任何改动——不是失败，不回
    # 滚、不 raise：新 speaker_mapping 仍会保留，展示名留到下次完整处理
    # （重新校对）时随新 schema 一并更新。
    if not structured_artifact_is_refreshable(old_structured, new_speaker_mapping):
        logger.warning(
            f"说话人姓名补层：既有结构化产物不满足刷新前置条件（缺 "
            f"speaker_mapping/dialogs 字段、dialogs 为空、存在缺失或未被"
            f"本轮新映射覆盖的 speaker_id，或是 schema 演进前的旧格式），"
            f"跳过刷新展示产物: {platform}/{media_id}"
        )
        return

    # 只在非 dict 的畸形条目上原样透传（理论防御，正常产物不会出现）；
    # 分叉判定只看这些结构完整的候选。
    old_dialogs = old_structured["dialogs"]
    relevant_dialogs = [d for d in old_dialogs if isinstance(d, dict)]

    # 分叉判定（K5 修复 (a)）——不再看"新旧整份 mapping 是否相等"这个
    # 捷径（所有来源统一走这里，不再区分 llm/cache_hit）：mapping 相等
    # 不能反推 dialogs 展示名已经与它一致，必须逐条按 speaker_id 比对
    # 展示名与新映射，只要有一条不一致就判定为分叉，需要刷新。
    divergent = any(
        new_speaker_mapping.get(d["speaker_id"]) != d.get("speaker")
        for d in relevant_dialogs
    )
    if not divergent:
        logger.info(f"说话人姓名补层：新旧姓名一致，无需刷新展示产物: {platform}/{media_id}")
        return

    # 全量原子写入：既然已确认全部可解析，直接按新映射重写每一条相关
    # dialog 的展示名，不再是"部分改、部分不改"。
    refreshed_dialogs = [
        {**d, "speaker": new_speaker_mapping[d["speaker_id"]]} if isinstance(d, dict) else d
        for d in old_dialogs
    ]
    refreshed_structured = {
        **old_structured,
        "dialogs": refreshed_dialogs,
        "speaker_mapping": new_speaker_mapping,
    }
    # 刷新真正尝试写入且失败时的回滚语义（K5 修复 (c)，按来源区分，与上面
    # 三处"跳过不算失败"的 return 严格区分——这里是真正尝试写入、真正
    # 失败）统一走 _write_structured_artifact。
    _write_structured_artifact(refreshed_structured, log_verb="刷新")


def _replace_speaker_labels_in_text(text: str, name_replacements: Dict[str, str]) -> str:
    """按行首说话人标签替换文本中的占位名（V2 修复，PR3 review hardening）。

    calibrated_text（"校对文本"）由 SpeakerAwareProcessor._build_text_from_dialogs
    按 `f"{speaker}：{text}"`、以 "\n\n" 连接每条对话拼接而成——说话人名
    永远只出现在每条对话文本的行首、紧跟一个全角冒号"："。按"行首 + 冒号"
    这个固定形状做正则替换，不会误伤正文里偶然出现的同名字样（比如某位
    嘉宾在发言内容里恰好提到了另一位说话人的名字）。

    identity_fallback 场景下 speaker_mapping 是恒等映射（{label: label}），
    本轮 calibrated_text 里每行的占位名因此就是原始 speaker_id 本身，与
    _restore_real_names_after_identity_fallback() 按 speaker_id 生成的
    name_replacements 的 key 天然对应，不存在多个 speaker_id 共享同一占位
    文本、替换互相冲突的可能。

    Args:
        text: 待替换的 calibrated_text（或同样按该格式拼接的其它文本，如
            "文本过短"分支用作总结兜底的校对文本副本）。
        name_replacements: {占位标签: 真实姓名}，通常来自
            _restore_real_names_after_identity_fallback() 的返回值。

    Returns:
        str: 替换后的文本；text 或 name_replacements 为空时原样返回。
    """
    if not text or not name_replacements:
        return text
    # 过滤掉空 key、以及新旧同名的无效映射（替换了也没有可观察变化）。
    mapping = {
        old_label: new_name
        for old_label, new_name in name_replacements.items()
        if old_label and old_label != new_name
    }
    if not mapping:
        return text

    # Y2 修复（PR3 review hardening 加固轮）：单趟替换，而不是对每个
    # (old_label, new_name) 逐项迭代替换。
    #
    # 级联问题：旧写法里每一轮 pattern.sub() 都作用在上一轮已经被替换过的
    # `replaced` 字符串上——如果某个 new_name 恰好等于后续某一轮要匹配的
    # old_label（例如映射里同时有 "Speaker2" -> "Alice" 和 "S1" ->
    # "Speaker2"），处理顺序恰好先处理 S1 时，"S1：" 先被换成 "Speaker2："，
    # 紧接着下一轮 "Speaker2" -> "Alice" 又把这行刚生成的 "Speaker2：" 二次
    # 替换成 "Alice："——这一行本该保留的是"Speaker2"这个真实姓名，却被误
    # 伤成了"Alice"。改为把全部旧标签一次性编译进同一个 alternation 正则，
    # 一趟 re.sub() 扫描原始文本、按位置逐个匹配替换，每个匹配位置只被
    # 处理一次，不存在"用上一轮结果喂给下一轮"的机会。
    # alternation 分支按标签长度降序排列：避免短标签恰好是另一个长标签前缀
    # 时，正则引擎优先匹配到较短分支导致边界匹配错误（例如 "S1" 与
    # "S10" 同时存在时，必须优先尝试匹配 "S10"）。
    #
    # 转义问题：re.sub 的 replacement 参数如果是字符串，会解释其中的反
    # 斜杠转义与分组引用（如 "\1"、"\g<name>"）——如果姓名恰好包含这类
    # 字符（LLM 生成的姓名理论上什么字符都可能出现），会导致输出被破坏
    # 甚至直接抛 re.error。用 lambda（可调用对象）作为 replacement 时，
    # re.sub 会把其返回值当纯字面量直接拼接，不做任何转义解释，天然避免
    # 这个问题。
    ordered_labels = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(
        r"^(" + "|".join(re.escape(label) for label in ordered_labels) + r")：",
        re.MULTILINE,
    )
    return pattern.sub(lambda m: mapping[m.group(1)] + "：", text)


def _restore_real_names_after_identity_fallback(
    *,
    platform: str,
    media_id: str,
    existing_snapshot: Optional[dict],
    structured_data: dict,
) -> Tuple[dict, Dict[str, str]]:
    """本轮说话人推断退化为 identity_fallback 时，用既有产物里真实推断过的
    姓名修补本轮刚生成的 dialogs/speaker_mapping（按 speaker_id 精确匹配），
    不触碰本轮新产出的文本/时间戳等其它字段。

    R4 修复（PR3 review hardening）：_save_llm_results 的"完整结构化保存"
    分支（calibrate=True 这一轮，即 not suppress_calibration）此前无条件
    把本轮 structured_data 整体写盘，不检查 speaker_inference_source——
    "fallback 不覆盖既有好姓名"这条保护（G4）只接入了 suppress_calibration
    分支调用的 _refresh_speaker_names_in_existing_structured_artifact。一次
    临时 LLM 故障（网络抖动/限流/超时）就足以让 SpeakerInferencer.infer()
    退化为 identity fallback（{label: label}），而这条完整保存路径会把它
    原样落盘，把历史上已经真实推断出的姓名覆盖成"Speaker1"之类的占位符，
    任务仍以 success 收尾。

    与 _refresh_speaker_names_in_existing_structured_artifact 的"全量原子
    写入"步骤同一思路、应用方向相反：那边是 calibrate=False 这一轮"用新
    mapping 刷新旧 dialogs"（校对文本继续沿用旧的，只补姓名），这里是
    calibrate=True 这一轮"用旧 mapping 修补新 dialogs"——本轮同时做了真实
    校对，校对产出的文本必须落盘，但说话人推断这一步本身失败了，不能让
    占位名盖掉已经验证过的真实姓名。

    找不到可比对的既有产物/旧 mapping 时原样返回 structured_data，不做
    任何改动——identity_fallback 的占位名此时是唯一可用的结果，不算回归。

    V2 修复（PR3 review hardening）：此前只修补了 structured_data，
    calibrated_text（"校对文本"）在调用本函数之前已经从 result_dict 提取
    成局部变量并落盘，通知（_send_notification）与任务终态快照
    （terminal_snapshot）也都在 _save_llm_results 返回之后直接消费调用方
    那份未经修补的 result_dict——查看页的结构化数据显示的是修补后的真名，
    主文本/通知/快照却仍是 identity_fallback 的占位名，自相矛盾。调用方
    （_save_llm_results）现在把本函数的调用挪到了 calibrated_text 被提取
    为局部变量之前，并消费下面新增的 name_replacements 返回值对
    calibrated_text 做同步的占位名替换（见 _replace_speaker_labels_in_text）；
    本函数自身只负责生成这份"占位标签 -> 真实姓名"映射，不关心它具体被
    用在哪些消费点。

    V3 修复（PR3 review hardening）：此前恢复只按 speaker_id 字符串匹配
    old_mapping，不验证 old_mapping 所在的 llm_processed.json 对应的
    diarization 输入是否与本轮相同——diarization 重跑后，同一个
    speaker_id（如 "SPEAKER_00"）完全可能对应不同的物理说话人，仅按
    字符串相等直接复用旧姓名会把历史身份错配给本轮的陌生人并持久化。
    llm_processed.json（old_structured 的来源）本身不携带
    input_fingerprint；携带它的是独立持久化的 speaker_mapping.json（见
    cache_manager.save_speaker_mapping/get_speaker_mapping）。这里复用
    既有指纹设施：从 existing_snapshot 里与 old_structured 同批落盘、
    LLM 阶段不会重写的原始转录（transcript_data）反推本轮说话人集合与
    SpeakerInferencer.input_fingerprint，再调用
    cache_manager.get_speaker_mapping() 校验它是否与磁盘上的
    speaker_mapping.json 精确匹配（fingerprint + speakers 集合 +
    source="llm" 三者都吻合，这也是该方法自身一贯的既有语义，不是本次
    新增的校验口径）——不匹配（含完全没有可比对的指纹信息，比如旧格式
    历史数据从未写过 speaker_mapping.json）一律跳过恢复，保留本轮的
    raw 标签，如实反映"这一轮没有可信的姓名依据"，不再靠 speaker_id
    字符串巧合去赌是不是同一个人。

    V4 修复（PR3 review hardening 二轮）：V3 引入的指纹校验此前形同虚设——
    verified_mapping 只用来做 None 判断（"要不要恢复"），恢复循环实际取名字
    仍然读未经校验的 old_mapping，二者是两个独立来源，指纹对得上也不代表
    old_mapping 里的具体姓名跟 verified_mapping 一致。现在恢复循环里
    `speaker_id in old_mapping`/`old_mapping[speaker_id]` 统一改成读
    verified_mapping，old_mapping 只保留作为"是否存在任何历史映射值得
    尝试恢复"的前置判断，不再是姓名的实际来源。

    Args:
        existing_snapshot: cache_manager.get_cache(...) 的既有缓存快照，
            可能为 None（全新任务，没有可比对的历史姓名）。同时提供
            V3 指纹校验所需的 transcript_data 字段。
        structured_data: 本轮协调器产出的
            {"dialogs": [...], "speaker_mapping": {...}}。

    Returns:
        Tuple[dict, Dict[str, str]]:
            - structured_data：姓名已按可能情况修补过的结构化数据；未发生
              修补时原样返回传入的对象。
            - name_replacements：{本轮占位标签: 恢复后的真实姓名}，仅包含
              真正发生了替换的条目；调用方用它对 calibrated_text 做同步的
              行首说话人标签替换。未发生修补（含指纹校验未通过被跳过）时
              为空 dict。
    """
    old_structured = (existing_snapshot or {}).get("llm_processed")
    if not isinstance(old_structured, dict):
        return structured_data, {}
    old_mapping = old_structured.get("speaker_mapping")
    if not isinstance(old_mapping, dict) or not old_mapping:
        return structured_data, {}

    dialogs = structured_data.get("dialogs")
    if not isinstance(dialogs, list):
        return structured_data, {}

    # ---- V3：指纹边界校验，理清两个文件谁携带指纹 ----
    # llm_processed.json（old_structured）不携带 input_fingerprint；
    # speaker_mapping.json（cache_manager.get_speaker_mapping 的读侧）才
    # 携带。本轮"真实"指纹从当前 transcript_data 现算，与
    # transcription.py 分层缓存预检、SpeakerInferencer.infer() 内部缓存
    # 命中判断用的是同一份既有指纹设施（SpeakerInferencer.
    # extract_speaker_labels/input_fingerprint），不新建第二套算法。
    from ...llm.core.speaker_inferencer import SpeakerInferencer

    current_transcript_data = (existing_snapshot or {}).get("transcript_data")
    current_dialogs = (
        current_transcript_data.get("segments", [])
        if isinstance(current_transcript_data, dict)
        else (current_transcript_data or [])
    )
    current_speakers = SpeakerInferencer.extract_speaker_labels(current_dialogs)
    verified_mapping = (
        cache_manager.get_speaker_mapping(
            platform,
            media_id,
            input_fingerprint=SpeakerInferencer.input_fingerprint(
                current_speakers, current_dialogs
            ),
            speakers=current_speakers,
        )
        if current_speakers
        else None
    )
    if verified_mapping is None:
        logger.info(
            f"identity_fallback 姓名恢复：本轮输入指纹与既有 "
            f"speaker_mapping.json 不一致（或无法确定指纹），跳过恢复、"
            f"保留本轮 raw 标签，避免跨 diarization 错配真实身份: "
            f"{platform}/{media_id}"
        )
        return structured_data, {}

    # V4 修复（PR3 review hardening 二轮）：恢复的姓名来源必须是上面刚做过
    # 指纹校验的 verified_mapping，不能是未经校验的 old_mapping——此前这里
    # 仍从 old_mapping（llm_processed.json 内嵌、不携带 input_fingerprint
    # 的旧映射）取名字，verified_mapping 只被用来决定"要不要恢复"（None
    # 检查），却从未被用作恢复内容的实际来源，V3 的指纹校验因此形同虚设：
    # 只要 old_mapping 存在且指纹恰好也验证通过，取到的名字仍然可能是
    # old_mapping 里过期/不一致的值。old_mapping 在这里只保留作为"是否
    # 值得尝试恢复"的前置存在性判断（避免没有任何历史映射时白算一次指纹），
    # 具体姓名一律读 verified_mapping。
    #
    # verified_mapping 是 cache_manager.get_speaker_mapping() 的返回值，
    # 形状是 {"mapping": {speaker_id: 展示名}, "meta": {...},
    # "low_confidence": [...]}（get_speaker_mapping 内部经
    # _speaker_mapping_result_is_valid 校验过，非 None 时 "mapping" 一定是
    # dict 且覆盖 current_speakers 全集），真正的姓名表在它的 "mapping" 键
    # 下，不是 verified_mapping 本身。
    verified_names_by_speaker = verified_mapping.get("mapping") or {}

    restored_mapping = dict(structured_data.get("speaker_mapping") or {})
    restored_dialogs = []
    changed = False
    name_replacements: Dict[str, str] = {}
    for dialog in dialogs:
        if isinstance(dialog, dict):
            speaker_id = dialog.get("speaker_id")
            if speaker_id in verified_names_by_speaker:
                old_name = verified_names_by_speaker[speaker_id]
                current_label = dialog.get("speaker")
                if current_label != old_name:
                    dialog = {**dialog, "speaker": old_name}
                    changed = True
                    if isinstance(current_label, str) and current_label:
                        name_replacements[current_label] = old_name
                restored_mapping[speaker_id] = old_name
        restored_dialogs.append(dialog)

    if not changed:
        return structured_data, {}

    logger.info(
        f"说话人推断本轮退化为 identity_fallback，已用既有真实姓名修补本轮"
        f"新产物的说话人标签（校对文本等其它字段不受影响）: {platform}/{media_id}"
    )
    return (
        {**structured_data, "dialogs": restored_dialogs, "speaker_mapping": restored_mapping},
        name_replacements,
    )


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
            {"calibration_status": ..., "summary_status": ...,
             "chapters_status": ...}（值可能为 None，表示该层本轮未触碰、
            旧值原样保留）。platform/media_id 缺失时返回 None（沿用早退语义，
            调用方应据此跳过覆盖 result_dict）。
    """
    # 注意：这里不提前把"校对文本"取成局部变量——calibrated_text 的赋值
    # 挪到了下面 media_lock 内、identity_fallback 姓名恢复完成之后（V2
    # 修复，PR3 review hardening）。提前在这里 result_dict.get() 出来的
    # 局部变量是不可变字符串的一份快照，不会随后续对 result_dict 的原地
    # 修改而更新，会绕开姓名恢复直接引用修补前的旧文本。
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

    # chapters_status：协调器显式 None = 本轮未尝试；缺键时视为未尝试（兼容
    # 旧测试/调用方），不伪造状态。
    chapters_status = result_dict.get("chapters_status", _MISSING)
    if chapters_status is _MISSING:
        chapters_status = stats.get("chapters_status")
    chapters_payload_list = result_dict.get("chapters") or stats.get("chapters") or []
    chapters_fingerprint = (
        result_dict.get("chapters_fingerprint")
        or stats.get("chapters_fingerprint")
    )
    chapters_segment_count = (
        result_dict.get("chapters_segment_count")
        if result_dict.get("chapters_segment_count") is not None
        else stats.get("chapters_segment_count")
    )
    chapters_source_kind = (
        result_dict.get("chapters_source_kind")
        or stats.get("chapters_source_kind")
        or "none"
    )

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
        processing_options = normalize_processing_options(processing_options)
        calibrate_requested = processing_options.get("calibrate", True)
        summarize_requested = processing_options.get("summarize", True)
        chapters_requested = processing_options.get("chapters", True)

        calibrated_exists_before = False
        summary_exists_before = False
        chapters_generated_before = False
        existing_snapshot = None
        need_snapshot = (
            (not calibrate_requested)
            or (not summarize_requested)
            or (not chapters_requested)
        )
        if need_snapshot:
            existing_snapshot = cache_manager.get_cache(
                platform, media_id, use_speaker_recognition=use_speaker_recognition,
            )
            if existing_snapshot:
                calibrated_exists_before = "llm_calibrated" in existing_snapshot
                summary_exists_before = "llm_summary" in existing_snapshot
                prior_chapters_status = (
                    (existing_snapshot.get("llm_status") or {}).get("chapters_status")
                )
                chapters_generated_before = (
                    prior_chapters_status == ChaptersStatus.GENERATED
                    and "llm_chapters" in existing_snapshot
                )

        # 校对层已存在、且本轮未请求（重新）校对 -> 抑制写入，保护已有的真实产物
        # 不被本轮 skip_calibration=True 产出的占位内容覆盖。
        suppress_calibration = calibrated_exists_before and not calibrate_requested

        # ---- V2 修复（PR3 review hardening）：identity_fallback 姓名恢复
        # 提前到所有消费点之前 ----
        # 背景：R4 修复（_restore_real_names_after_identity_fallback）此前
        # 只修补了下面"完整结构化保存"分支里的 structured_data，但
        # calibrated_text（"校对文本"）在函数最顶部就已经从 result_dict
        # 提取成局部变量、通知（_send_notification）与任务终态快照
        # （terminal_snapshot）也都在本函数返回之后直接消费调用方那份
        # 未经修补的 result_dict——查看页的结构化数据显示的是修补后的
        # 真名，主文本/通知/快照却仍是 identity_fallback 的占位名，自相
        # 矛盾。
        #
        # 修法：把恢复动作挪到本函数最早可行的位置（suppress_calibration
        # 刚算出来、calibrated_text 尚未被提取为局部变量之前），原地修改
        # result_dict 这个"单一权威副本"——_handle_llm_task 里
        # terminal_snapshot/_send_notification 消费的正是同一个 dict
        # 对象，原地修改自动对它们可见，沿用
        # _restore_cached_summary_for_notification 已经在用的同一种
        # "调用方传入的 dict 原地修改"写法。下面 calibrated_text 的提取
        # 相应挪到这里之后，读到的就是修补后的值。
        #
        # 仅在 not suppress_calibration 时才尝试（与 R4 原有的生效范围
        # 保持一致）：suppress_calibration 分支走的是另一套 G4 保护机制
        # _refresh_speaker_names_in_existing_structured_artifact，那里
        # 自己会按 speaker_inference_source 跳过 identity_fallback，不需要
        # 也不应该被这里重复处理。
        if (
            use_speaker_recognition
            and not suppress_calibration
            and isinstance(result_dict.get("structured_data"), dict)
            and stats.get("speaker_inference_source") == "identity_fallback"
        ):
            snapshot_for_names = existing_snapshot
            if snapshot_for_names is None:
                snapshot_for_names = cache_manager.get_cache(
                    platform, media_id, use_speaker_recognition=use_speaker_recognition,
                )
            restored_structured, name_replacements = _restore_real_names_after_identity_fallback(
                platform=platform,
                media_id=media_id,
                existing_snapshot=snapshot_for_names,
                structured_data=result_dict["structured_data"],
            )
            if name_replacements:
                result_dict["structured_data"] = restored_structured
                raw_calibrated_text = result_dict.get("校对文本")
                if isinstance(raw_calibrated_text, str):
                    result_dict["校对文本"] = _replace_speaker_labels_in_text(
                        raw_calibrated_text, name_replacements,
                    )

        calibrated_text = result_dict.get("校对文本", "")

        # 总结层的"关闭"语义二次判定：
        # - 已有真实产物（GENERATED/SKIPPED_SHORT 都会落盘文件）-> 本轮未触碰，
        #   保留旧值（None，走 save_llm_status 的合并语义）。
        # - 尚无产物 -> 用户首次显式关闭总结，记为 DISABLED（区别于"文本过短跳过"）。
        # calibrate_only 且未 backfill 的 recalibrate 分支维持原样：summary_status
        # 此时已是协调器给出的 None（"本轮未尝试"），不需要在这里重新判定。
        if not (calibrate_only and not summary_backfill) and not summarize_requested:
            summary_status = None if summary_exists_before else SummaryStatus.DISABLED

        # chapters 层 suppress / DISABLED（R5）：
        # - 本轮未请求 chapters 且协调器给出 None（未尝试）：若已有任意真实状态
        #   （含 GENERATED / SKIPPED_* / FAILED）则保留；尚无状态则记 DISABLED。
        # - 本轮请求了 chapters 时用协调器输出原样（含 force recompute）。
        # - 已有 GENERATED 且本轮未请求 -> 绝不覆盖章节文件（只增不减）。
        suppress_chapters_artifact = (
            not chapters_requested and chapters_generated_before
        )

        # 保存校对文本：即便校对全降级为 NONE（诚实状态模型里"尝试但完全失败"），
        # 处理器返回的 calibrated_text 仍是一份可用的兜底产物（分段/分块降级后的
        # 格式化原文，见 plain_text_processor/speaker_aware_processor 的 fallback
        # 分支），必须落盘——此前 calibrate_success=False 时整段跳过保存，导致
        # 查看页只能 str() 原始 dict 展示，且相同请求会因为"缓存里没有
        # llm_calibrated.txt"反复重跑 LLM（codex-review R4 #2）。calibration_status
        # 仍如实记为 NONE（下面 save_llm_status 那行不变）——"产物已落盘"和
        # "校对未成功"是两件事，不再互相矛盾。

        # ---- write-ahead 撤销 llm_status.json（S1，PR3 review hardening）----
        # 必须在下面任何一次产物文件重写之前完成：本函数只有在"全部产物写入
        # 都成功"时才会执行到末尾的 save_llm_status 调用（下面任意一次
        # save_llm_result 失败都会直接 raise，跳过末尾那次调用）。修复前的
        # bug 正是：中途失败后，旧的 llm_status.json 原样留在磁盘，继续为
        # "新校对文本 + 旧总结/旧结构化"这份此刻并不存在过的混合产物背书；
        # 下一次请求把它当完整缓存直接返回，静默不一致且跳过重试。
        #
        # 撤销后返回的旧内容（old_llm_status）供下面"层未触碰、按合并语义
        # 保留旧值"的位置显式回填——不能继续依赖 save_llm_status 自己读磁盘
        # 做 merge：那次读取此刻只会读到刚被撤销的空文件（见
        # cache_manager.invalidate_llm_status 文档）。
        #
        # 并发读取窗口：撤销 -> 重写 -> 写新状态整段仍在本函数顶部
        # `with cache_manager.media_lock(...)` 持锁范围内；另一个请求的并发
        # 读取（如 transcription.py 的分层缓存命中判定）不经过这把锁，如果
        # 恰好落在撤销之后、新状态写完之前，会读到"状态缺失"，按既有判定
        # 逻辑视为未确认完成、触发一次真实重算——是可接受的保守行为（多算
        # 一次，不会返回错误产物）。并发写入则被这把 media lock 完全阻塞，
        # 不会有两个写者同时撤销/重写同一份状态文件。
        old_llm_status = cache_manager.invalidate_llm_status(
            platform, media_id, use_speaker_recognition=use_speaker_recognition,
        )
        if not isinstance(old_llm_status, dict):
            old_llm_status = {}

        calibrated_saved = not suppress_calibration
        if calibrated_saved:
            if not cache_manager.save_llm_result(
                platform=platform,
                media_id=media_id,
                use_speaker_recognition=use_speaker_recognition,
                llm_type="calibrated",
                content=calibrated_text,
            ):
                raise OSError("failed to persist calibrated artifact")
            if calibrate_success:
                logger.info(f"校对文本已保存到缓存: {task_id}")
            else:
                logger.warning(f"校对全部降级为原文，仍落盘兜底格式化文本: {task_id}")
        else:
            logger.info(f"校对层已存在且本轮未请求重新校对，跳过覆盖: {task_id}")

        # 保存总结文本：按 summary_status 三态（+DISABLED/保留）分支（不再用
        # skip_summary/summary_success 二元判定——旧逻辑里 skip_summary 与
        # summary_success 永远互补，导致"文本过短"分支实际不可达，"生成失败"
        # 被悄悄吞掉，既不落盘校对文本兜底也不报错）
        summary_saved = False
        chapters_saved = False
        effective_chapters_status = chapters_status
        if calibrate_only and not summary_backfill:
            logger.info(f"仅校对模式，保留原有总结文件: {task_id}")
        elif summary_status == SummaryStatus.GENERATED:
            if summary_text is not None:
                logger.info(f"保存LLM总结到缓存: {task_id}")
                if not cache_manager.save_llm_result(
                    platform=platform,
                    media_id=media_id,
                    use_speaker_recognition=use_speaker_recognition,
                    llm_type="summary",
                    content=summary_text,
                ):
                    raise OSError("failed to persist summary artifact")
                summary_saved = True
            else:
                logger.warning(f"总结状态为 generated 但文本为空，跳过保存: {task_id}")
        elif summary_status == SummaryStatus.SKIPPED_SHORT:
            # 文本过短、用校对文本作为总结的兜底展示：与上面 calibrated_saved 同理，
            # 即便校对全降级为 NONE，calibrated_text 也是可用的兜底格式化文本，
            # 不再要求 calibrate_success——否则该组合下 llm_summary.txt 永久缺失，
            # 复现同一类"产物缺失导致反复重跑"的问题（codex-review R4 #2 的自洽延伸）。
            logger.info(f"文本过短，保存校对文本作为总结: {task_id}")
            if not cache_manager.save_llm_result(
                platform=platform,
                media_id=media_id,
                use_speaker_recognition=use_speaker_recognition,
                llm_type="summary",
                content=calibrated_text,
            ):
                raise OSError("failed to persist short-summary artifact")
            summary_saved = True
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

        # 保存结构化数据（同样受校对层抑制保护，避免覆盖已有的真实说话人校对结果）。
        # 不再要求 calibrate_success：校对全降级为 NONE 时，speaker_aware_processor
        # 依然会返回一份 dict（chunk 级 fallback 把原文合并回 dialogs，见该处理器的
        # _apply_structured_fallback），是与 calibrated_text 同等地位的兜底产物，
        # 理应一并落盘（codex-review R4 #2），而不是随 calibrate_success=False 被
        # 整体跳过。
        # structured_data 只有内容按"对话列表"路径（speaker_aware_processor）真实处理
        # 时才是 dict——一旦本轮走了纯文本路由（_prepare_llm_content 在
        # transcription_data 缺失/类型异常时会回退纯文本，例如 calibrate_only=True
        # 的 recalibrate 从不设置 processing_options、天然绕过上面的
        # suppress_calibration 保护），协调器返回的 structured_data 就是 None。
        # 不能无条件下标赋值，否则 TypeError 会让本应成功落盘的总结也被判定为
        # 任务失败（codex-review R4 #1）。isinstance 校验兜底：非 dict 时跳过保存，
        # 缓存里已有的结构化产物原样保留（诚实状态模型下"本轮没有新产物"不等于
        # "清空旧产物"）。
        if (
            use_speaker_recognition
            and not suppress_calibration
            and "structured_data" in result_dict
        ):
            structured_data = result_dict["structured_data"]
            if isinstance(structured_data, dict):
                # 说话人推断退化为 identity_fallback 时的姓名恢复（R4/V2
                # 修复，PR3 review hardening）已经在上面 suppress_calibration
                # 刚算出来时统一处理过，并原地写回了
                # result_dict["structured_data"]——这里读到的 structured_data
                # 已经是恢复后的版本，不需要也不应该再调用一次
                # _restore_real_names_after_identity_fallback（重复调用只会
                # 用同一份 existing_snapshot 再比对一遍，此时 dialog.get
                # ("speaker") 已经等于恢复后的真名，不会再触发任何变化，
                # 纯粹浪费一次判断，还容易让人误以为这里还有独立的保护
                # 逻辑）。
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
                    raise OSError("failed to persist structured artifact")
            else:
                logger.info(
                    f"本轮结构化数据非 dict（{type(structured_data).__name__}，"
                    f"通常因走了纯文本路由），跳过保存，保留缓存中已有的结构化产物: {task_id}"
                )
        elif (
            use_speaker_recognition
            and suppress_calibration
            and isinstance(result_dict.get("structured_data"), dict)
        ):
            # calibrate=False 但 infer_speaker_names=True：上面的分支为了保护
            # 已校对文本跳过了整段 structured_data 保存，这里单独把姓名标签
            # 的更新补回已有产物（见 _refresh_speaker_names_in_existing_
            # structured_artifact 的说明），不然本轮姓名推断结果永远不会
            # 反映到查看页。speaker_inference_source 透传本轮映射的真实来源
            # （SpeakerAwareProcessor.process() 写进 stats，见该函数），由
            # 被调用方决定是否允许刷新——非真实 LLM 推断（identity_fallback/
            # cache_hit）时必须跳过，避免瞬时故障覆盖已有好名字（本地 codex
            # review 第 6 轮 G4）。
            _refresh_speaker_names_in_existing_structured_artifact(
                task_id=task_id,
                platform=platform,
                media_id=media_id,
                use_speaker_recognition=use_speaker_recognition,
                existing_snapshot=existing_snapshot,
                new_speaker_mapping=result_dict["structured_data"].get("speaker_mapping"),
                speaker_inference_source=stats.get("speaker_inference_source"),
            )

        # ---- chapters 落盘（仅 GENERATED 写 llm_chapters.json；R4/R5）----
        # 契约见 cache_manager.save_llm_result(llm_type="chapters") 文档注释：
        # start_seg/end_seg = 原始输入列表下标；fingerprint 来自 ChaptersResult。
        if suppress_chapters_artifact or (
            chapters_status is None and not chapters_requested
        ):
            # 本轮未请求：若旧状态存在则保留（effective=None）；否则 DISABLED。
            old_ch = old_llm_status.get("chapters_status")
            if old_ch is not None:
                effective_chapters_status = None
                logger.info(
                    f"chapters layer exists (status={old_ch}) and not requested; "
                    f"preserve: {task_id}"
                )
            else:
                effective_chapters_status = ChaptersStatus.DISABLED
                logger.info(
                    f"chapters disabled for first time (no prior status): {task_id}"
                )
        elif chapters_status == ChaptersStatus.GENERATED:
            from datetime import datetime, timezone

            chapters_file_payload = {
                "format_version": "v1",
                "source": {
                    "kind": chapters_source_kind,
                    "segment_count": chapters_segment_count or len(chapters_payload_list),
                    "fingerprint": chapters_fingerprint,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
                "chapters": chapters_payload_list,
            }
            if not cache_manager.save_llm_result(
                platform=platform,
                media_id=media_id,
                use_speaker_recognition=use_speaker_recognition,
                llm_type="chapters",
                content=chapters_file_payload,
            ):
                raise OSError("failed to persist chapters artifact")
            chapters_saved = True
            logger.info(f"chapters artifact saved: {task_id}")
        elif chapters_status in (
            ChaptersStatus.SKIPPED_SHORT,
            ChaptersStatus.SKIPPED_NO_TIMELINE,
            ChaptersStatus.FAILED,
            ChaptersStatus.DISABLED,
        ):
            logger.info(
                f"chapters status={chapters_status}, skip file write: {task_id}"
            )
        elif chapters_status is None:
            logger.info(f"chapters not attempted this round, preserve status: {task_id}")
        else:
            logger.warning(
                f"unknown chapters_status={chapters_status}, skip file write: {task_id}"
            )

        # 写入统一的诚实状态落盘文件 llm_status.json（两条路径都写）。
        # calibration_status/summary_status 为 None 时（本轮未触碰该层）传 None 给
        # save_llm_status，其合并语义会保留旧值，不会把已有的真实状态误覆盖。
        effective_calibration_status = None if suppress_calibration else stats.get("calibration_status")
        effective_calibration_stats = None if suppress_calibration else stats.get("calibration_stats")
        # 上面撤销动作删除了旧状态文件，save_llm_status 内部"读磁盘做合并"
        # 这次只能读到空文件——用撤销前捕获的 old_llm_status 显式顶替 None，
        # 把"层未触碰、保留旧值"的语义从"隐式依赖磁盘合并"改成"显式传值"，
        # 落盘结果与撤销之前完全一致（只有 old_llm_status 本身也缺该字段时
        # 才会退化为 None，等价于历史上从未有过这份状态文件的场景）。
        final_calibration_status = (
            effective_calibration_status
            if effective_calibration_status is not None
            else old_llm_status.get("calibration_status")
        )
        final_calibration_stats = (
            effective_calibration_stats
            if effective_calibration_stats is not None
            else old_llm_status.get("calibration_stats")
        )
        final_summary_status = (
            summary_status
            if summary_status is not None
            else old_llm_status.get("summary_status")
        )
        final_chapters_status = (
            effective_chapters_status
            if effective_chapters_status is not None
            else old_llm_status.get("chapters_status")
        )
        cache_manager.save_llm_status(
            platform=platform,
            media_id=media_id,
            use_speaker_recognition=use_speaker_recognition,
            calibration_status=final_calibration_status,
            calibration_stats=final_calibration_stats,
            summary_status=final_summary_status,
            chapters_status=final_chapters_status,
        )

    # calibrated_saved 覆盖了"即便 calibrate_success=False（NONE 全降级）仍落盘
    # 兜底文本"的新语义（codex-review R4 #2）——不能再单纯用 calibrate_success
    # 判断"是否保存了任何文件"，否则 NONE 场景会被误报为"未保存任何结果文件"。
    if calibrated_saved or summary_saved or chapters_saved:
        logger.info(f"LLM结果已保存到缓存: {platform}/{media_id}")
    else:
        logger.warning(f"LLM处理全部失败，未保存任何结果文件: {task_id}")

    return {
        "calibration_status": effective_calibration_status,
        "summary_status": summary_status,
        "chapters_status": effective_chapters_status,
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

    优先判断 calibration_status == DISABLED（用户通过 processing_options
    主动关闭校对）：这种情况下根本不会产生 chunk/segment 级别的详细统计，
    但通知里如果对此毫无提示，用户会把"未经 AI 校对的原始语音识别文本"
    误当成已校对结果查看——尤其是 calibrate=False 但 summarize=True 时，
    总结本身是基于这份未校对原文生成的，缺了这条提示用户完全无从得知
    （ci-gate review：只处理了 summary_status 维度，漏了 calibration_status
    维度，与 docs/guides/notification.md 里"关闭校对或总结都会体现真实
    状态"的承诺不符）。

    其余情况下，两条校对路径统计口径不同：结构化路径（说话人识别）按 chunk
    计数（total_chunks/success_count/fallback_count/failed_count），纯文本
    路径按 segment 计数（total_segments/calibrated_segments/fallback_segments/
    low_quality_segments）。这里按 cal_stats 里出现的字段名分辨路径，
    分别生成对应的详情文案，保证纯文本路径的降级也能像结构化路径一样出警告。

    Args:
        stats: 统计信息字典（coordinator.process() 返回的 stats，
            stats["calibration_stats"] 为 None 或缺失时视为无统计可用）

    Returns:
        str: 警告文本（空字符串表示无警告）
    """
    if stats.get("calibration_status") == CalibrationStatus.DISABLED:
        return (
            "\n⚠️ **AI 校对未启用**：当前显示为未经校对的原始语音识别文本"
            "（可能含错别字、断句错误）。"
        )

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
