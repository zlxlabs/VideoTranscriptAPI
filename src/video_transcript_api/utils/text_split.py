"""Shared text splitting helpers (issue #142).

Leaf module: it imports nothing from this repository, so both the transcriber
segment producer and the LLM chunk producer can depend on it without creating
an import cycle.

The single implementation lives here on purpose. The "a declared length cap
must stay enforceable" rule (#142) is only trustworthy while it has exactly one
implementation: two copies would drift, and a drifting copy silently turns that
chain's cap back into a best-effort hint.
"""

import re
from typing import List, Tuple

_WHITESPACE_RUN = re.compile(r"\s+")


def split_oversized_text(text: str, max_len: int) -> Tuple[List[str], int, int]:
    """把仍超 max_len 的文本兜底切成每片 <= max_len：空白优先，无空白硬切。

    这是「切不开也不放弃」的兜底：调用方已经用更高层级的标点切过一遍，凡是仍
    超上限的片段都走这里，保证任意输入下每片都 <= max_len。

    Args:
        text: 待切文本。
        max_len: 切分宽度。**必须为正整数**，由调用方保证（上限来源见各调用点：
            capswriter 侧是硬编码的 ``max_len=300``；DialogSegmenter 侧来自
            ``LLMConfig.max_chunk_length`` / ``plain_structured_max_chunk_length``，
            那里没有取值校验，所以本函数自己在入口 fail fast）。

    Returns:
        (片段列表, 空白切点数, 硬切点数)。

    Raises:
        ValueError: ``max_len`` 不是正数。必须响亮失败而不是 clamp——clamp 会把
            配置错误静默纠正成另一个宽度，正是「静默出错」那一档；而继续切
            （``cut = max_len``）在 max_len <= 0 时是死循环，会把 worker 挂死。

    切分只移动切点、不删不改任何字符，``"".join(片段)`` 与入参逐字一致；切口
    允许落在空白中间（可能让某片以空白开头/结尾），调用方不要再 strip。
    """
    if max_len <= 0:
        raise ValueError(
            "TEXT_SPLIT_INVALID_MAX_LEN condition=max_len_not_positive "
            f"func=split_oversized_text max_len={max_len}"
        )

    pieces: List[str] = []
    remaining = text
    whitespace_cuts = 0
    hard_cuts = 0

    while len(remaining) > max_len:
        # 只在 [0, max_len] 内找切点：空白串结尾 <= max_len 才可用，
        # 跨过边界的空白串只能硬切，否则片段会超限。
        cut = 0
        for match in _WHITESPACE_RUN.finditer(remaining[: max_len + 1]):
            if match.end() <= max_len:
                cut = match.end()
        if cut:
            whitespace_cuts += 1
        else:
            cut = max_len
            hard_cuts += 1
        pieces.append(remaining[:cut])
        remaining = remaining[cut:]

    if remaining:
        pieces.append(remaining)

    return pieces, whitespace_cuts, hard_cuts
