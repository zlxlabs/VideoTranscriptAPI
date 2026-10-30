"""说话人推测名的证据档位（high / medium / low / none）。

卡 vta-261030-speaker-confidence-tiers：证据不够时现行结果一律是「说话人N」
占位符，参考价值很低。本模块按**程序能核实的证据强弱**给每个说话人补三个
meta 字段（confidence_level / guess_name / basis），由前端自行决定如何呈现；
mapping 的语义不变，仍是导出文本、总结等下游环节的唯一事实源，低档推测名
只通过 meta 新字段送到网页。

档位判据（确定性程序判据，不读 confidence 数值）：
- high：现行规则已采用（applied），且采用依据含核对通过的 dialog_address。
- medium：经两人「元数据+角色」通路采用（evidence_tier=duo_metadata_role）。
- low：未采用，但模型给出了非占位的人名（核对不过、自我介绍复核不过等）。
- none：模型没有给出人名（名字为空 / 等于原始标签 / 占位符「说话人N」）。

guess_name 与 basis 的取值域即卡面接口契约：quote 截断到 80 字符；none 档
basis 四键全 null。档位计算只在 infer() 的真实 LLM 路径（证据裁决 + 两人
通路之后）与 identity fallback 结果上执行；缓存命中路径不重算——规则版本
升级后旧缓存自然失效。
"""

import re
from typing import Any, Dict, Optional, Tuple

from .speaker_duo_inference import DUO_EVIDENCE_TIER

LEVEL_HIGH = "high"
LEVEL_MEDIUM = "medium"
LEVEL_LOW = "low"
LEVEL_NONE = "none"

CONFIDENCE_LEVELS = (LEVEL_HIGH, LEVEL_MEDIUM, LEVEL_LOW, LEVEL_NONE)

# none 档 basis 的固定形状（契约：四个键都是 null）
NONE_BASIS: Dict[str, None] = {
    "evidence_type": None,
    "quote": None,
    "role": None,
    "rejection_reason": None,
}

# 「说话人N」/「Speaker N」占位符（等于原始标签的情形单独判，见 _is_placeholder_name）
_PLACEHOLDER_NAME_RE = re.compile(r"^(?:说话人|speaker)\s*\d+$", re.IGNORECASE)

_QUOTE_MAX_CHARS = 80


def _truncate_quote(text: Any) -> Optional[str]:
    if not isinstance(text, str):
        return None
    return text[:_QUOTE_MAX_CHARS] or None


def _is_placeholder_name(speaker: str, name: Any) -> bool:
    """空名、等于原始标签（如 Speaker1）、占位符都算「没有给出人名」。"""
    if not isinstance(name, str):
        return True
    stripped = name.strip()
    if not stripped or stripped == speaker:
        return True
    return _PLACEHOLDER_NAME_RE.match(stripped) is not None


def _verified(entry: Dict) -> list:
    verified = entry.get("verified_candidates")
    if not isinstance(verified, list):
        return []
    return [item for item in verified if isinstance(item, dict)]


def _first_raw_candidate(entry: Dict) -> Optional[Dict]:
    """模型返回 candidates 原始顺序的第一条（low 档 basis 的依据）。"""
    candidates = entry.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        return candidates[0]
    return None


def _medium_basis(entry: Dict) -> Tuple[Optional[str], Optional[str]]:
    """两人通路所依据的那条核对通过候选（取法与 duo 通路的按角色取一致）。"""
    static = [
        item
        for item in _verified(entry)
        if item.get("evidence_type") in ("title_credit", "author_host")
    ]
    preferred = "author_host" if entry.get("role") == "host" else "title_credit"
    chosen = next(
        (item for item in static if item.get("evidence_type") == preferred),
        static[0] if static else None,
    )
    if chosen is None:
        return None, None
    return chosen.get("evidence_type"), _truncate_quote(chosen.get("evidence_quote"))


def _classify(speaker: str, entry: Dict, display_name: Any) -> Tuple[str, Optional[str], Dict]:
    """单个 meta 条目的档位判定，返回 (level, guess_name, basis)。"""
    verified = _verified(entry)
    if entry.get("applied"):
        if entry.get("evidence_tier") == DUO_EVIDENCE_TIER:
            evidence_type, quote = _medium_basis(entry)
            guess = None
            if isinstance(display_name, str) and ":" in display_name:
                guess = display_name.rsplit(":", 1)[-1].strip() or None
            basis = {
                "evidence_type": evidence_type,
                "quote": quote,
                "role": entry.get("role"),
                "rejection_reason": None,
            }
            return LEVEL_MEDIUM, guess, basis
        dialog = next(
            (item for item in verified if item.get("evidence_type") == "dialog_address"),
            None,
        )
        if dialog is not None:
            basis = {
                "evidence_type": "dialog_address",
                "quote": _truncate_quote(dialog.get("evidence_quote")),
                "role": None,
                "rejection_reason": None,
            }
            return LEVEL_HIGH, dialog.get("name"), basis
    if _is_placeholder_name(speaker, entry.get("name")):
        return LEVEL_NONE, None, dict(NONE_BASIS)
    first = _first_raw_candidate(entry)
    basis = {
        "evidence_type": first.get("evidence_type") if first else None,
        "quote": _truncate_quote(first.get("evidence_quote")) if first else None,
        "role": None,
        "rejection_reason": entry.get("rejection_reason"),
    }
    return LEVEL_LOW, str(entry.get("name")).strip(), basis


def assign_confidence_levels(result: Dict) -> Dict:
    """给结果里每个 meta 条目补 confidence_level / guess_name / basis。

    原地修改后返回同一份结果。对 identity fallback 形态的条目（无候选、
    name 即原始标签）自然落 none 档。
    """
    if not isinstance(result, dict):
        return result
    meta = result.get("meta")
    if not isinstance(meta, dict):
        return result
    mapping = result.get("mapping")
    for speaker, entry in meta.items():
        if not isinstance(entry, dict):
            continue
        display = mapping.get(speaker) if isinstance(mapping, dict) else None
        level, guess, basis = _classify(str(speaker), entry, display)
        entry["confidence_level"] = level
        entry["guess_name"] = guess
        entry["basis"] = basis
    return result
