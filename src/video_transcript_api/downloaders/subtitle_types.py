"""
字幕解析结果的统一数据结构。

用于在下载器内部（尤其是 YouTube 字幕获取的三条解析路径）传递字幕文本及其
时间戳分段信息，方便后续接入基于时间轴的功能（如字幕对齐、分段展示等）。

设计说明：
- text 字段是当前所有对外接口（get_subtitle 等）已经在用的纯文本字幕，
  必须与历史行为逐字节一致。
- segments 是新增的、可选的时间戳分段信息，解析失败时必须置为 None，
  绝不能因为时间戳解析出错而影响 text 的可用性（容错铁律）。

`sanitize_time_pair` 的权威实现位于 `transcriber.segments`（时间解析工具的
统一家，`parse_time_to_seconds`、`normalize_segments` 也在那里）；这里只是
re-export，保持本模块既有的 import 路径（`from .subtitle_types import
sanitize_time_pair`）继续可用，调用方（youtube.py / youtube_api_client.py）
无需改动。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..transcriber.segments import sanitize_time_pair

__all__ = ["SubtitleResult", "sanitize_time_pair"]


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
