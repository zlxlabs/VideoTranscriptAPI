"""
字幕解析结果的统一数据结构。

用于在下载器内部（尤其是 YouTube 字幕获取的三条解析路径）传递字幕文本及其
时间戳分段信息，方便后续接入基于时间轴的功能（如字幕对齐、分段展示等）。

设计说明：
- text 字段是当前所有对外接口（get_subtitle 等）已经在用的纯文本字幕，
  必须与历史行为逐字节一致。
- segments 是新增的、可选的时间戳分段信息，解析失败时必须置为 None，
  绝不能因为时间戳解析出错而影响 text 的可用性（容错铁律）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class SubtitleResult:
    """
    字幕解析结果

    属性:
        text: 拼接后的纯文本字幕（与历史行为保持逐字节一致）
        segments: 分段时间戳信息，列表中每个元素形如：
            {"start_time": float|None, "end_time": float|None, "text": str}
            （单位：秒；字幕来源没有说话人信息，因此不含 speaker 字段）
            当没有任何可用分段（如整段字幕都无法识别出时间轴结构）时，
            segments 整体为 None，不影响 text 字段。

            文本永不丢失的不变式：segments 一旦非 None，所有有文本的 cue /
            条目都必须出现在其中——单条时间轴解析失败（格式损坏、属性缺失
            等）只会让该条的 start_time / end_time 置为 None，绝不会把这条
            连同它的文本一起从 segments 中丢弃。下游按 segments 消费文本时
            无需担心因为个别时间戳损坏而静默丢字。
    """

    text: str
    segments: Optional[List[dict]] = None


def sanitize_time_pair(
    start: Optional[float], end: Optional[float]
) -> Tuple[Optional[float], Optional[float]]:
    """
    校验一对时间戳的合理性，摘掉不合逻辑的值，但绝不影响调用方对文本的保留。

    三条 YouTube 字幕解析路径（youtube-transcript-api 分段 / TikHub XML /
    SRT）在各自提取 start_time、end_time 之后，都要经过这同一道校验，统一
    "诚实降级"口径：宁可时间字段是 None，也不能是一个误导下游的负数或
    倒挂区间。校验只做数值合理性判断，不做有限性 (isfinite) 检查——那一层
    容错由各调用方在算出 start / end 之前就已经做过。

    规则（按顺序应用）：
        1. start 为负数 -> start 置 None（负的起始时间没有物理意义，常见
           于上游 start 字段本身就是脏数据）
        2. end 为负数 -> end 置 None（常见于 end = start + duration 中
           duration 为负、且求和结果本身也变成负数的情况）
        3. start、end 均非 None 且 end < start（区间倒挂：duration 为负但
           求和结果仍非负、或时间轴书写顺序颠倒）-> end 置 None

    参数:
        start: 起始时间（秒），None 表示不可用
        end: 结束时间（秒），None 表示不可用

    返回:
        (start, end): 按上述规则校验后的元组；text 字段的保留完全不受
        此函数影响，调用方应始终保留原文本
    """
    if start is not None and start < 0:
        start = None
    if end is not None and end < 0:
        end = None
    if start is not None and end is not None and end < start:
        end = None
    return start, end
