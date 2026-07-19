"""确定性段落化（deterministic paragraphization, v1）。

把连续的小段（ASR/字幕 segments）拼成阅读段落。**只选边界、不改文本**：
每个输出段落都是连续输入成员的并集，断点只落在成员边界上，全文覆盖
不重不漏。零 LLM 依赖、零 I/O、同输入必同输出。

规格来源：docs/sessions/260719-0513-chapters/TASKS.md T8
「确定性段落化算法规格（v1）」。核心语义——**长度只是预算不是闸刀**：
累计长度达到 target 后开始寻找授权断点，授权点才真的断；一路无授权
点时由 hard_max 逗号级兜底、2*hard_max 终端硬断保证收敛。
"""

import math
from typing import Any, Dict, List, Optional

from ..utils.logging import setup_logger

logger = setup_logger("paragraphize")

# 句末标点：CJK `。！？…` + ASCII `.!?`。
_SENTENCE_END_CHARS = frozenset("。！？….!?")
# 逗号级标点（hard_max 兜底时才获得授权）：`，；：、` + `,;:`。
_COMMA_LEVEL_CHARS = frozenset("，；：、,;:")
# 标点后允许出现的收尾引号/括号：判断句末/逗号收尾时先剥离这层字符，
# 兼容 `。"` `！」` `."` `")` 这类写法。
_TRAILING_CLOSERS = frozenset("\"'”’」』）)]}》")


def _is_cjk_char(ch: str) -> bool:
    """判断字符是否属于 CJK 语境（拼接时两侧任一则为 CJK，不加空格）。

    覆盖 CJK 统一表意文字（含扩展 A/B+ 与兼容表意）、CJK 标点（`。，` 等
    本身也按 CJK 语境处理——`。"` 后不该出现空格）、全角形式、日文假名、
    韩文音节。
    """
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0xF900 <= cp <= 0xFAFF
        or 0x3000 <= cp <= 0x303F
        or 0xFF00 <= cp <= 0xFFEF
        or 0x3040 <= cp <= 0x30FF
        or 0xAC00 <= cp <= 0xD7AF
        or 0x20000 <= cp <= 0x2FA1F
    )


def _ends_with_char_in(text: str, chars: frozenset) -> bool:
    """判断文本是否以指定标点集合中的字符收尾（兼容收尾引号/括号）。

    先剥离尾部空白，再循环剥离收尾引号/括号（`_TRAILING_CLOSERS`），
    最后看剩下的末字符是否落在目标集合里。
    """
    stripped = text.rstrip()
    while stripped and stripped[-1] in _TRAILING_CLOSERS:
        stripped = stripped[:-1].rstrip()
    return bool(stripped) and stripped[-1] in chars


def _smart_join(parts: List[str]) -> str:
    """CJK 感知拼接：左文本以 CJK 结尾或右文本以 CJK 开头时不加空格，
    否则加一个空格。成员原文逐字保留（不改文本），只在边界插入 0/1 个空格。
    """
    if not parts:
        return ""
    out = parts[0]
    for part in parts[1:]:
        left = out.rstrip()
        right = part.lstrip()
        if left and right and not _is_cjk_char(left[-1]) and not _is_cjk_char(right[0]):
            out += " "
        out += part
    return out


def _coerce_seconds(value: Any) -> Optional[float]:
    """把时间字段收敛为 float 秒或 None。

    输入契约允许 float 秒或 None；任何其他形态（字符串、bool、非有限值）
    一律按不可用处理（None），让停顿授权永不因脏数据误触发。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    return seconds if math.isfinite(seconds) else None


def _coerce_number(value: Any) -> Optional[float]:
    """把 duration 字段收敛为有限数值或 None（用于"全员可数值化则求和"）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _boundary_level(
    left: Dict[str, Any], right: Dict[str, Any], pause_threshold_seconds: float
) -> int:
    """评估相邻两成员之间边界的授权级别。

    Returns:
        2 = 强授权（左成员以句末标点收尾，或两侧时间均可用且
            `right.start - left.end >= pause_threshold`）；
        1 = 弱授权（左成员以逗号级标点收尾，仅 hard_max 兜底时生效）；
        0 = 无授权。
    """
    if _ends_with_char_in(left["text"], _SENTENCE_END_CHARS):
        return 2
    left_end = left["end_time"]
    right_start = right["start_time"]
    if (
        left_end is not None
        and right_start is not None
        and right_start - left_end >= pause_threshold_seconds
    ):
        return 2
    if _ends_with_char_in(left["text"], _COMMA_LEVEL_CHARS):
        return 1
    return 0


def _build_paragraph(members: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把一组连续成员组装成一个段落 dict。

    - `text`：smart join 拼接（不改原文，只插入 0/1 个空格）；
    - `start_time` = 首成员 start、`end_time` = 末成员 end（None 原样保留，
      绝不编造）；
    - `original_text`：仅当全组成员都携带时拼接输出；
    - `duration`：仅当全员可数值化时求和输出。
    """
    paragraph: Dict[str, Any] = {
        "text": _smart_join([m["text"] for m in members]),
        "start_time": members[0]["start_time"],
        "end_time": members[-1]["end_time"],
    }
    if all(m["original_text"] is not None for m in members):
        paragraph["original_text"] = _smart_join([m["original_text"] for m in members])
    durations = [_coerce_number(m["duration"]) for m in members]
    if all(d is not None for d in durations):
        paragraph["duration"] = sum(d for d in durations if d is not None)
    return paragraph


def paragraphize_segments(
    segments: List[Dict[str, Any]],
    *,
    target_chars: int = 300,
    hard_max_chars: int = 600,
    pause_threshold_seconds: float = 2.0,
) -> List[Dict[str, Any]]:
    """把连续 segments 确定性拼成阅读段落（只选边界、不改文本）。

    前提（调用方保证）：每条 segment 必有 `"text": str` 且非空；
    `"start_time"`/`"end_time"` 为 float 秒或 None（缺失/其他类型按 None
    处理）；`"original_text"`（str）与 `"duration"`（数值）可选。

    规则（cur_len = 当前段落累计字符数，只算 text 长度，不算拼接空格）：
        1. 句末授权：cur_len >= target 且左成员以句末标点收尾 → 断；
        2. 停顿授权：cur_len >= target 且两侧时间均可用、gap >=
           pause_threshold → 断（无需句末标点）；
        3. target 之前任何授权点都不提前断；
        4. 硬上限兜底：cur_len >= hard_max 且组内尚无断点 → 逗号级也获得
           授权，断点取 hard_max 之前最后一个授权点（句末/停顿/逗号均可，
           回溯不再受 target 下限约束——宁可短也不腰斩语义），取不到则取
           hard_max 之后第一个授权点；
        5. 终端规则：到 2*hard_max 仍无任何授权点 → 在保持组长度
           <= 2*hard_max 的最后一个成员边界硬断并记 warning（保证收敛）；
        6. 病理兜底：单成员 len(text) > hard_max → 自成一段并记 warning，
           不影响相邻段；
        7. 时间缺失：任一侧时间为 None → 该边界停顿信号不可用，仅用标点。

    Args:
        segments: 输入 segment dict 列表（见上方前提）。
        target_chars: 长度预算（达到后开始寻找授权断点）。
        hard_max_chars: 硬上限（达到后逗号级授权、触发回溯选点）。
        pause_threshold_seconds: 停顿授权阈值（秒）。

    Returns:
        段落 dict 列表，每段 `{"text", "start_time", "end_time"}` 必有，
        `original_text`/`duration` 按全员规则条件性输出。段落是连续成员
        的并集，覆盖全部输入、不重不漏。
    """
    # 归一化为内部记录：时间收敛为 float|None，可选字段统一成显式 None，
    # 后续组装与授权判断都不必再防御脏形态。
    records: List[Dict[str, Any]] = []
    for seg in segments:
        original_text = seg.get("original_text")
        records.append(
            {
                "text": seg["text"],
                "start_time": _coerce_seconds(seg.get("start_time")),
                "end_time": _coerce_seconds(seg.get("end_time")),
                "original_text": original_text if isinstance(original_text, str) else None,
                "duration": seg.get("duration"),
            }
        )

    paragraphs: List[Dict[str, Any]] = []
    n = len(records)
    group_start = 0  # 当前组首成员下标（含）
    cur_len = 0  # 当前组累计字符数（只算 text，不含拼接空格）
    # 组内已见的授权边界：(右成员下标, 级别 2/1)。全部记录（含 target 之前
    # 的），供 hard_max 回溯取"最后一个授权点"。
    boundaries: List[tuple] = []

    i = 0
    while i < n:
        rec = records[i]
        text_len = len(rec["text"])

        # 规则 6（病理兜底）：单成员超 hard_max → 自成一段，相邻组照常输出。
        if text_len > hard_max_chars:
            if cur_len > 0:
                paragraphs.append(_build_paragraph(records[group_start:i]))
            paragraphs.append(_build_paragraph(records[i : i + 1]))
            logger.warning(
                f"paragraphize: single segment length {text_len} exceeds "
                f"hard_max_chars {hard_max_chars}, kept as its own paragraph"
            )
            i += 1
            group_start = i
            cur_len = 0
            boundaries = []
            continue

        if cur_len > 0:
            # 组非空：评估边界 (i-1, i) 是否断开。回溯断点后组变短，授权
            # 状态随之变化，因此用循环重判直到"不断"为止。
            while True:
                level = _boundary_level(records[i - 1], rec, pause_threshold_seconds)
                brk: Optional[int] = None
                force_break = False
                if cur_len >= target_chars and level == 2:
                    # 规则 1/2：句末/停顿授权（target 之后才生效）。
                    brk = i
                elif cur_len >= hard_max_chars:
                    if boundaries:
                        # 规则 4 前半：回溯到 hard_max 之前最后一个授权点。
                        brk = boundaries[-1][0]
                    elif level == 1:
                        # 规则 4 后半：hard_max 之后第一个逗号级授权点。
                        brk = i
                    elif cur_len + text_len > 2 * hard_max_chars:
                        # 规则 5（终端）：无任何授权点且再加就超 2*hard_max。
                        brk = i
                        force_break = True
                if brk is None:
                    # 不断：把当前边界授权级别登记进组内记录，供后续回溯。
                    if level:
                        boundaries.append((i, level))
                    break
                if force_break:
                    logger.warning(
                        f"paragraphize: no authorized breakpoint before "
                        f"2x hard_max_chars ({2 * hard_max_chars}), force break "
                        f"at segment boundary (group_chars={cur_len})"
                    )
                paragraphs.append(_build_paragraph(records[group_start:brk]))
                group_start = brk
                cur_len = sum(len(records[k]["text"]) for k in range(brk, i))
                boundaries = [(r, lv) for (r, lv) in boundaries if r > brk]
                if brk == i:
                    # 断在当前边界：新组为空，直接落成员 i。
                    break

        cur_len += text_len
        i += 1

    if group_start < n:
        paragraphs.append(_build_paragraph(records[group_start:n]))

    return paragraphs
