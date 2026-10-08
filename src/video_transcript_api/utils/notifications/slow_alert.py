"""偏慢提醒：任务比预期慢、但还没到硬超时时，推送一条 ⏳（每任务最多一次）。

设计与门槛出处：``docs/sessions/261007-notify-slim/design.md`` 的"偏慢提醒"一节。

- 分阶段计时、排队不计入：
  - 转录阶段：转录 worker 开始处理该任务（``track_transcription``）到转录完成
    交接 LLM 阶段（``finish_transcription``）；拿到媒体时长（``duration_known``）
    后按新值重算门槛，仍从开始处理那一刻起算；
  - 大模型阶段：LLM worker 开始处理该任务（``track_llm``）到任务终态；
    在 LLM 队列里排队的时间不计入（起点是出队之后）。
- 两个阶段共用"已提醒"标记，每个任务最多发一条 ⏳。
- 任务进入终态时由终态唯一入口（``finalize_terminal_status_and_notify``）调用
  ``cancel_all`` 取消全部计时。到点判定与终态取消在同一把锁（``_LOCK``）下
  互斥：谁先拿到锁谁赢，终态之后绝不再发送。
- 计时不持久化，服务重启后不恢复，由现有的恢复扫描兜底。

发送失败只记日志，绝不影响任务主流程；计时线程全部是 daemon，不留常驻线程。
"""

import math
import threading
import time
from typing import Any, Dict, Optional

from ..logging import setup_logger
from . import get_notification_router
from .channel import build_task_notification_heading

logger = setup_logger("slow_alert")

# ---- 门槛（模块常量，不进配置）----
# 数据出处：design.md"偏慢提醒"的生产实测表（n305，2026-10-07，27 个
# CapsWriter 任务，只取聚合值）：语音识别耗时 95 分位 619s / 最大 668s，
# 识别耗时/视频时长 95 分位 0.105 / 最大 3.28（93 秒视频识别 306 秒，
# 疑似服务端排队）；大模型阶段 95 分位 178s / 最大 216s；下载 95 分位
# 5.9s / 最大 12.6s。说话人识别（FunASR）路径无样本。
# 转录门槛 600 + 0.15 × 时长：600s 常数基线取在识别耗时 95 分位量级，
# 覆盖时长未知与短任务的下限；0.15 的斜率在 95 分位比值 0.105 之上——
# 慢于"比实时快约 6.7 倍"才判偏慢，给服务端排队留出余量。时长未知时
# （判法与 capswriter_client._transcription_deadline 一致：None、非有限、
# 非正数一律按未知，issue #190）回退 1800s。大模型阶段耗时与时长基本
# 无关，取单一常数 600s（95 分位 178 / 最大 216，余量充足）。
SLOW_TRANSCRIPTION_BASE_SECONDS = 600.0
SLOW_TRANSCRIPTION_PER_DURATION_SECOND = 0.15
SLOW_TRANSCRIPTION_UNKNOWN_DURATION_SECONDS = 1800.0
SLOW_LLM_SECONDS = 600.0

_STAGE_LABELS = {"transcription": "转录", "llm": "大模型"}


class _TaskTiming:
    """单个任务的进程内计时状态（全部字段由 ``_LOCK`` 守护）。"""

    __slots__ = (
        "alerted", "generation", "transcription_start", "timers",
        "url", "title", "channel_name", "webhooks", "view_url",
    )

    def __init__(self, *, url, title, channel_name, webhooks, view_url):
        self.alerted = False
        # 代数：每次重置/取消某个阶段的计时器都 +1，旧计时器的到点回调
        # 带着注册时的代数，拿锁后发现代数不匹配即放弃——堵住
        # threading.Timer.cancel() 对"已到点、回调已出队"的计时器无效
        # 这个窗口（回调与 cancel 在锁外赛跑时，回调必须重新在锁内验证资格）。
        self.generation = 0
        self.transcription_start = time.monotonic()
        self.timers: Dict[str, threading.Timer] = {}
        self.url = url or ""
        self.title = title
        self.channel_name = channel_name
        self.webhooks = dict(webhooks) if webhooks else None
        self.view_url = view_url


_LOCK = threading.Lock()
_TASKS: Dict[str, _TaskTiming] = {}


def _finite_positive_or_none(value: Any) -> Optional[float]:
    """与 capswriter_client._transcription_deadline 同判据的时长规整。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def transcription_threshold(duration_s: Any) -> float:
    """转录阶段偏慢门槛（秒）。

    时长已知（正的有限数）时为 ``600 + 0.15 × 时长``；未知时回退
    ``SLOW_TRANSCRIPTION_UNKNOWN_DURATION_SECONDS``。
    """
    duration = _finite_positive_or_none(duration_s)
    if duration is None:
        return SLOW_TRANSCRIPTION_UNKNOWN_DURATION_SECONDS
    return SLOW_TRANSCRIPTION_BASE_SECONDS + SLOW_TRANSCRIPTION_PER_DURATION_SECOND * duration


def _start_timer_locked(task_id, stage, stage_start, threshold, generation):
    remaining = threshold - (time.monotonic() - stage_start)
    timer = threading.Timer(
        max(0.0, remaining),
        _on_deadline,
        args=(task_id, stage, stage_start, threshold, generation),
    )
    timer.daemon = True
    timer.start()
    return timer


def track_transcription(
    task_id: str,
    *,
    url: str = "",
    title: Optional[str] = None,
    channel_name: Optional[str] = None,
    webhooks: Optional[Dict[str, str]] = None,
    view_url: Optional[str] = None,
) -> None:
    """转录阶段开始计时：转录 worker 开始处理该任务时调用（幂等）。"""
    with _LOCK:
        if task_id in _TASKS:
            return
        timing = _TaskTiming(
            url=url, title=title, channel_name=channel_name,
            webhooks=webhooks, view_url=view_url,
        )
        timing.timers["transcription"] = _start_timer_locked(
            task_id, "transcription", timing.transcription_start,
            SLOW_TRANSCRIPTION_UNKNOWN_DURATION_SECONDS, timing.generation,
        )
        _TASKS[task_id] = timing


def duration_known(task_id: str, duration_s: Any, *, title: Optional[str] = None) -> None:
    """拿到媒体时长后调用：门槛按新值重算，仍从开始处理那一刻起算。

    时长未知（None / 非正 / 非有限）时保持未知时长的门槛不变。
    """
    with _LOCK:
        timing = _TASKS.get(task_id)
        if timing is None:
            return
        if title:
            timing.title = title
        if timing.alerted or _finite_positive_or_none(duration_s) is None:
            return
        threshold = transcription_threshold(duration_s)
        timing.generation += 1
        stale = timing.timers.pop("transcription", None)
        if stale is not None:
            stale.cancel()
        timing.timers["transcription"] = _start_timer_locked(
            task_id, "transcription", timing.transcription_start,
            threshold, timing.generation,
        )


def finish_transcription(task_id: str) -> None:
    """转录阶段结束（交接 LLM 阶段）时调用：取消转录阶段计时。"""
    with _LOCK:
        timing = _TASKS.get(task_id)
        if timing is None:
            return
        timing.generation += 1
        stale = timing.timers.pop("transcription", None)
        if stale is not None:
            stale.cancel()


def track_llm(task_id: str, *, title: Optional[str] = None) -> None:
    """大模型阶段开始计时：LLM worker 开始处理该任务时调用。

    排队时间不计入——起点就是本次调用。仅转录流水线交接来的任务会走到
    这里（recalibrate / resummarize / notes 在接线点已被排除）；没有登记
    状态时本调用为 no-op。
    """
    with _LOCK:
        timing = _TASKS.get(task_id)
        if timing is None or timing.alerted:
            return
        if title:
            timing.title = title
        timing.timers["llm"] = _start_timer_locked(
            task_id, "llm", time.monotonic(), SLOW_LLM_SECONDS, timing.generation,
        )


def cancel_all(task_id: str) -> None:
    """任务进入终态时取消全部计时并释放状态（幂等）。"""
    with _LOCK:
        timing = _TASKS.pop(task_id, None)
        timers = list(timing.timers.values()) if timing is not None else []
    for timer in timers:
        timer.cancel()


def _on_deadline(task_id, stage, stage_start, threshold, generation):
    """计时器到点回调：在锁内重新验证资格，赢了才发送（发送在锁外）。"""
    with _LOCK:
        timing = _TASKS.get(task_id)
        if timing is None or timing.alerted or generation != timing.generation:
            return
        timing.alerted = True
        timing.timers.pop(stage, None)
        payload = (
            task_id, stage, time.monotonic() - stage_start, threshold,
            timing.url, timing.title, timing.channel_name,
            timing.webhooks, timing.view_url,
        )
    try:
        _send_alert(*payload)
    except Exception:
        logger.exception(f"slow-task alert send failed: {task_id}")


def _send_alert(
    task_id, stage, elapsed_s, threshold_s,
    url, title, channel_name, webhooks, view_url,
) -> None:
    """按该任务 ✅/❌ 相同的渠道与 webhook 选择规则发送 ⏳（两个渠道都发）。"""
    heading = build_task_notification_heading(task_id, title, url, icon="⏳")
    lines = [
        heading,
        "",
        "任务处理比预期慢，仍在运行",
        f"当前阶段：{_STAGE_LABELS.get(stage, stage)}",
        f"已用 {elapsed_s / 60:.0f} 分钟，超过门槛 {threshold_s / 60:.0f} 分钟",
    ]
    if view_url:
        lines += ["", f"🔗 查看：{view_url}"]
    router = get_notification_router()
    router.send_rich(
        "\n".join(lines),
        channel_name=channel_name,
        webhooks=webhooks,
        title=heading,
    )
