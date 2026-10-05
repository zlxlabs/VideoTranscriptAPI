"""Summary output budget: single source of truth for prompt, max_tokens, and post-hoc checks."""

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class SummaryBudget:
    """Budget envelope for one summary generation call."""

    target_min: int
    target_max: int
    hard_cap: int
    max_tokens: int


@dataclass(frozen=True)
class SummaryBudgetConfig:
    """Configurable band parameters; defaults match production locked decision #4."""

    s_target_min: int = 500
    s_target_max: int = 3000
    s_hard_cap_max: int = 4500
    m_target_min: int = 2000
    m_target_max: int = 4000
    m_hard_cap: int = 5000
    l_target_min: int = 4000
    l_target_max: int = 6000
    l_hard_cap: int = 8000
    max_tokens_multiplier: float = 1.5

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SummaryBudgetConfig":
        if not data:
            return cls()
        return cls(
            s_target_min=int(data.get("s_target_min", 500)),
            s_target_max=int(data.get("s_target_max", 3000)),
            s_hard_cap_max=int(data.get("s_hard_cap_max", 4500)),
            m_target_min=int(data.get("m_target_min", 2000)),
            m_target_max=int(data.get("m_target_max", 4000)),
            m_hard_cap=int(data.get("m_hard_cap", 5000)),
            l_target_min=int(data.get("l_target_min", 4000)),
            l_target_max=int(data.get("l_target_max", 6000)),
            l_hard_cap=int(data.get("l_hard_cap", 8000)),
            max_tokens_multiplier=float(data.get("max_tokens_multiplier", 1.5)),
        )


def compute_summary_budget(
    original_length: int,
    config: Optional[SummaryBudgetConfig] = None,
) -> SummaryBudget:
    """Compute summary budget from calibrated transcript length L (characters).

    Bands (locked decision #4):
      S: 800 <= L < 8000  -> target 500-3000, hard cap min(2*L, 4500)
      M: 8000 <= L < 30000 -> target 2000-4000, hard cap 5000
      L: L >= 30000       -> target 4000-6000, hard cap 8000

    For L < 800 (possible when min_summary_threshold < 800), S-band targets apply
    with the same hard-cap formula as S.
    """
    cfg = config or SummaryBudgetConfig()
    length = max(0, int(original_length))

    if length >= 30000:
        target_min = cfg.l_target_min
        target_max = cfg.l_target_max
        hard_cap = cfg.l_hard_cap
    elif length >= 8000:
        target_min = cfg.m_target_min
        target_max = cfg.m_target_max
        hard_cap = cfg.m_hard_cap
    else:
        target_min = cfg.s_target_min
        target_max = cfg.s_target_max
        hard_cap = min(2 * length, cfg.s_hard_cap_max)

    # Prompt 注入的 target_max 不得超过 hard_cap（S 带 L∈[800,1500) 时 2L < s_target_max）。
    target_max = min(target_max, hard_cap)

    max_tokens = int(hard_cap * cfg.max_tokens_multiplier)
    return SummaryBudget(
        target_min=target_min,
        target_max=target_max,
        hard_cap=hard_cap,
        max_tokens=max_tokens,
    )


def should_send_max_tokens(reasoning_effort: Optional[str]) -> bool:
    """Whether to send max_tokens on summary LLM calls.

    Only non-reasoning models (reasoning_effort == \"disabled\") receive an
    explicit token ceiling. Reasoning models consume hidden reasoning tokens
    against the same completion budget, so max_tokens must not be sent.
    """
    return reasoning_effort == "disabled"


def resolve_summary_max_tokens(
    budget: SummaryBudget,
    reasoning_effort: Optional[str],
) -> Optional[int]:
    """Return max_tokens for a summary call, or None for reasoning models."""
    if should_send_max_tokens(reasoning_effort):
        return budget.max_tokens
    return None


def classify_original_length_band(original_length: int) -> str:
    """Return S/M/L band label for monitoring (same L thresholds as compute_summary_budget)."""
    length = max(0, int(original_length))
    if length >= 30000:
        return "L"
    if length >= 8000:
        return "M"
    if length >= 800:
        return "S"
    return "below_S"
