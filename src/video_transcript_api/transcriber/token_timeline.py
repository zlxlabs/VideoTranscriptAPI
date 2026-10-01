#!/usr/bin/env python
# coding: utf-8

"""CapsWriter token -> 时间轴映射的唯一权威实现。

背景（issue #109）：CapsWriter 返回的 ``text``（带标点、带空格）与 ``tokens``
（BPE 单元）来自两套不同的书写习惯，历史代码用「去标点去空格的字符游标」去查
「原样累加 tokens 得到的字符位置」，两套坐标系不一致，误差随长度单调累积，
英文内容会把整条时间轴压缩到约 81%（生产实测末段停在 8469.03s，音频 10506.92s）。

本模块的做法：把两侧都投影到**同一把尺子**上（canonical projection），用序列对齐
取代累加游标，并把 canonical 坐标封在类内部——对外只暴露原始 ``text`` 的下标。
调用方拿不到 canonical 坐标，混用在结构上不可能发生。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "TextSpan",
    "TimelineQuality",
    "TokenTimeline",
    "canonical_projection",
    "ANCHOR_SIZE",
]

# 锚点分块对齐的公共子串长度：太短易误锚，太长在中英混排上匹配不到。
ANCHOR_SIZE = 12


# ---------------------------------------------------------------------------
# 规范投影（唯一实现，禁止在别处再写一套）
# ---------------------------------------------------------------------------


def canonical_projection(text: str) -> str:
    """把任意文本投影到规范坐标：小写化后只保留字母/数字（含 CJK）。

    不依赖任何标点集合：标点与空白在新旧 token 风格下的出现与否本身就是未约定
    的，剔除它们才能让两侧坐标系天然重合。
    """
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _project_with_positions(text: str) -> Tuple[str, List[int]]:
    """投影并同时记录每个 canonical 字符在原文中的下标。"""
    chars: List[str] = []
    positions: List[int] = []
    for index, ch in enumerate(text):
        lowered = ch.casefold()
        for piece in lowered:
            if piece.isalnum():
                chars.append(piece)
                positions.append(index)
    return "".join(chars), positions


def clean_token(token: str) -> str:
    """清理 token 的 BPE 续接标记（Paraformer 风格的 ``@@``）。"""
    return token.replace("@@", "")


# ---------------------------------------------------------------------------
# 对外坐标类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextSpan:
    """原始 ``text`` 上的半开区间 ``[start, end)``——唯一对外坐标。"""

    start: int
    end: int

    def __len__(self) -> int:
        return max(0, self.end - self.start)


@dataclass(frozen=True)
class TimelineQuality:
    """对齐质量画像（衡量产物，不衡量输入长度差）。"""

    aligned_chars: int
    unmatched_chars: int
    total_chars: int

    @property
    def aligned_ratio(self) -> float:
        if self.total_chars <= 0:
            return 0.0
        return self.aligned_chars / self.total_chars


def _finite_or_none(value: object) -> Optional[float]:
    """时间值规整：非有限值一律降级为 None（与仓内其余时间口径一致）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


# ---------------------------------------------------------------------------
# TokenTimeline
# ---------------------------------------------------------------------------


class TokenTimeline:
    """``text`` 区间 -> token 时间戳的查询器。

    构造请用 :meth:`align`。canonical 坐标（投影后的字符下标）是私有实现细节，
    外部拿不到；每个 span 独立查询时间，误差不随长度累积。
    """

    def __init__(
        self,
        text: str,
        tokens: Sequence[str],
        timestamps: Sequence[float],
    ) -> None:
        if not text:
            raise ValueError("text must not be empty")
        count = min(len(tokens), len(timestamps))
        if count == 0:
            raise ValueError("tokens/timestamps must not be empty")

        self._text = text
        self.duration: Optional[float] = None
        self._timestamps = list(timestamps[:count])
        self._tokens = [clean_token(tok) for tok in list(tokens)[:count]]
        self._token_count = count

        self._canonical_text, text_positions = _project_with_positions(text)

        # text 下标 -> canonical 下标（-1 表示该字符被投影剔除）
        self._text_to_canonical = [-1] * len(text)
        for canonical_index, text_index in enumerate(text_positions):
            self._text_to_canonical[text_index] = canonical_index

        token_offsets = [0]
        token_parts: List[str] = []
        for token in self._tokens:
            token_parts.append(canonical_projection(token))
            token_offsets.append(token_offsets[-1] + len(token_parts[-1]))
        self._token_offsets = token_offsets
        canonical_tokens = "".join(token_parts)

        self._canonical_to_token, anchored = self._align(
            self._canonical_text, canonical_tokens, token_offsets
        )

        self._quality = TimelineQuality(
            aligned_chars=anchored,
            unmatched_chars=len(self._canonical_text) - anchored,
            total_chars=len(self._canonical_text),
        )

    # -- 构造 ----------------------------------------------------------

    @classmethod
    def align(
        cls,
        text: str,
        tokens: Sequence[str],
        timestamps: Sequence[float],
        duration: Optional[float] = None,
    ) -> "TokenTimeline":
        """从服务端原始三元组构造时间轴。

        ``duration`` 仅用于调用方记忆，不参与任何映射计算（保留参数是为了让调用
        点的意图显式：产物覆盖率要拿它当分母）。
        """
        timeline = cls(text, tokens, timestamps)
        timeline.duration = _finite_or_none(duration)
        return timeline

    # -- 对齐 ----------------------------------------------------------

    def _token_index_at(self, canonical_token_index: int) -> int:
        offsets = self._token_offsets
        index = bisect_right(offsets, canonical_token_index) - 1
        if index < 0:
            index = 0
        if index >= self._token_count:
            index = self._token_count - 1
        return index

    def _align(
        self,
        canonical_text: str,
        canonical_tokens: str,
        token_offsets: Sequence[int],
    ) -> Tuple[List[int], int]:
        """canonical text 下标 -> token 下标（-1 表示未锚定）。

        返回 (映射, 被**锚点证据**覆盖的字符数)。锚点之间的空隙按最近的锚点顺延
        （映射单调不减，保证时间轴无空洞），但这些字符**不计入** aligned_chars——
        质量画像只认证据，不认推断。

        实测依据（125,699 字符 / 26,400 token 的合成英文样本）：
          * 投影后完全相同 -> 恒等快速路径，0.044s；
          * ``difflib.SequenceMatcher(autojunk=False)`` 字符级对齐在两侧真的分叉时
            是平方级：丢掉 20% token 需 **999.978s**（实测），不可接受，故不采用；
          * 锚点分块（k=12 公共子串）同规模 0.05s 量级，且映射严格单调。
        """
        mapping = [-1] * len(canonical_text)
        if not canonical_text:
            return mapping, 0

        if canonical_text == canonical_tokens:
            # 恒等映射快速路径：两侧投影后逐字相同。
            for position in range(len(canonical_text)):
                mapping[position] = self._token_index_at(position)
            return mapping, len(canonical_text)

        anchors = self._find_anchors(canonical_text, canonical_tokens)
        if not anchors:
            return mapping, 0

        k = ANCHOR_SIZE
        for text_start, token_start in anchors:
            for offset in range(k):
                mapping[text_start + offset] = self._token_index_at(token_start + offset)
        anchored = sum(1 for value in mapping if value >= 0)
        return self._carry_forward(mapping), anchored

    @staticmethod
    def _find_anchors(canonical_text: str, canonical_tokens: str) -> List[Tuple[int, int]]:
        """按 12 字符公共子串找单调锚点链（O(n)）。"""
        text_length = len(canonical_text)
        token_length = len(canonical_tokens)
        k = ANCHOR_SIZE
        if text_length < k or token_length < k:
            return []

        index: Dict[str, List[int]] = {}
        for start in range(token_length - k + 1):
            index.setdefault(canonical_tokens[start:start + k], []).append(start)

        anchors: List[Tuple[int, int]] = []
        last_token_start = -1
        position = 0
        while position <= text_length - k:
            bucket = index.get(canonical_text[position:position + k])
            if bucket:
                slot = bisect_right(bucket, last_token_start)
                if slot < len(bucket):
                    token_start = bucket[slot]
                    anchors.append((position, token_start))
                    last_token_start = token_start
                    position += k
                    continue
            position += 1
        return anchors

    @staticmethod
    def _carry_forward(mapping: List[int]) -> List[int]:
        """把锚点之间/两端的空隙按最近的锚点顺延，保持单调不减。"""
        filled = list(mapping)
        last = -1
        for index, value in enumerate(filled):
            if value >= 0:
                last = value
            elif last >= 0:
                filled[index] = last
        following = -1
        for index in range(len(filled) - 1, -1, -1):
            if filled[index] >= 0:
                following = filled[index]
            elif following >= 0:
                filled[index] = following
        return filled

    # -- 查询 ----------------------------------------------------------

    @property
    def quality(self) -> TimelineQuality:
        return self._quality

    def _canonical_bounds(self, span: TextSpan) -> Tuple[Optional[int], Optional[int]]:
        start_index: Optional[int] = None
        for index in range(span.start, span.end):
            canonical = self._text_to_canonical[index]
            if canonical >= 0:
                start_index = canonical
                break
        end_index: Optional[int] = None
        for index in range(span.end - 1, span.start - 1, -1):
            canonical = self._text_to_canonical[index]
            if canonical >= 0:
                end_index = canonical
                break
        return start_index, end_index

    def _time_at(self, canonical_index: Optional[int]) -> Tuple[Optional[float], bool]:
        if canonical_index is None:
            return None, False
        if canonical_index < 0 or canonical_index >= len(self._canonical_to_token):
            return None, False
        token_index = self._canonical_to_token[canonical_index]
        if token_index < 0:
            return None, False
        value = _finite_or_none(self._timestamps[token_index])
        if value is None:
            return None, False
        return value, True

    def start_of(self, span: TextSpan) -> Tuple[Optional[float], bool]:
        """span 起点的 ``(时间, 是否锚定)``。"""
        start_index, _ = self._canonical_bounds(span)
        return self._time_at(start_index)

    def end_of(self, span: TextSpan) -> Tuple[Optional[float], bool]:
        """span 终点的 ``(时间, 是否锚定)``。

        句尾标点/空白 token 的 canonical 投影为空，落在句界之后。它们属于本句（而
        不是下一句），因此终点向前吸收这些空投影 token —— 否则末段终点会停在最后一个
        实词上，小于服务端给出的 ``timestamps[-1]``。
        """
        _, end_index = self._canonical_bounds(span)
        if end_index is None:
            return None, False
        token_index = self._canonical_to_token[end_index]
        if token_index < 0:
            return None, False
        next_canonical = self._next_canonical_after(span.end)
        offsets = self._token_offsets
        cursor = token_index
        while (
            cursor + 1 < self._token_count
            # token cursor+1 的 canonical 投影为空（标点/纯空白），且不属于下一句
            and offsets[cursor + 1] == offsets[cursor + 2]
            and offsets[cursor + 1] <= next_canonical
        ):
            cursor += 1
        value = _finite_or_none(self._timestamps[cursor])
        if value is None:
            return None, False
        return value, True

    def _next_canonical_after(self, text_index: int) -> int:
        """text 下标 ``text_index`` 之后的第一个 canonical 下标（没有则取总数）。"""
        for index in range(text_index, len(self._text_to_canonical)):
            canonical = self._text_to_canonical[index]
            if canonical >= 0:
                return canonical
        return len(self._canonical_to_token)

    # -- 兼容旧调用方（历史遗留测试导入，仓内生产路径不再使用） ----------

    def legacy_token_positions(self) -> Tuple[List[int], str]:
        """返回 (token 起始字符下标, 原样累加的 token 文本)。

        仅供历史遗留测试 ``tests/transcript/test_funasr_conversion.py`` 使用；
        新代码请用 :class:`TokenTimeline`。
        """
        positions: List[int] = []
        reconstructed = ""
        for token in self._tokens:
            positions.append(len(reconstructed))
            reconstructed += token
        positions.append(len(reconstructed))
        return positions, reconstructed
