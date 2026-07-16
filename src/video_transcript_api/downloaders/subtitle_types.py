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
from typing import List, Optional


@dataclass
class SubtitleResult:
    """
    字幕解析结果

    属性:
        text: 拼接后的纯文本字幕（与历史行为保持逐字节一致）
        segments: 分段时间戳信息，列表中每个元素形如：
            {"start_time": float, "end_time": float, "text": str}
            （单位：秒；字幕来源没有说话人信息，因此不含 speaker 字段）
            当时间戳解析失败或没有可用分段时为 None，不影响 text 字段。
    """

    text: str
    segments: Optional[List[dict]] = None
