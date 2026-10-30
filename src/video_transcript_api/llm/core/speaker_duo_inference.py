"""两人访谈的「元数据 + 角色」推测命名通路。

现行证据闸门只认对话里的点名（dialog_address）：标题专场名与作者署名这类
静态候选必须另有对话佐证才被采用。现实里两人访谈/播客的双方常常从不互相
叫名字，于是这类节目全部停在「说话人N 待确认」（生产近 30 天的 15 个两人
任务里有 10 个一个名字都没认出，其中 8 个已有核对通过的标题或作者候选）。

本模块为「恰好两个主要说话人」的低歧义分区开一条窄通路：

- 主要说话人判定、结构前提核对、显示名裁决全部是确定性程序判据；
- LLM 只提供两样东西：角色（host / guest / unknown）与核对通过的静态候选名；
- 任何一条前提不成立，整条通路关闭，说话人保持现行结果（占位符 + 原拒绝原因）。

前提（全部 fail-closed）：

1. 主要说话人恰好两个：按 base dialogs 的发言条数占比，各 ≥15%，两人合计 ≥80%；
2. 两位主要说话人各有 ≥1 条核对通过的静态候选（title_credit / author_host）；
3. 两人用到的名字按 casefold 后恰好两个不同名字（一人一个）；
4. 标题里的 @ 提及不超过一个（提及其以上时分不清谁是嘉宾）；
5. roles 里两人恰好一个 host、一个 guest；
6. 定出的名字不与另一位说话人已采用的名字相同（casefold）。

显示名沿用现行复合名的措辞：``{说话人N}｜标题关联推测:{name}`` /
``{说话人N}｜作者关联推测:{name}``——推测属性必须就地可见，禁止裸名。
"""

import re
from typing import Any, Dict, List, Optional, Tuple

# 主要说话人判定阈值（占比按 base dialogs 的发言条数计算）
DUO_PRIMARY_SHARE = 0.15
DUO_COMBINED_SHARE = 0.80

# 可用于本通路的静态证据类型（对话称呼不在此列：它由现行闸门直接采用）
DUO_STATIC_EVIDENCE_TYPES = ("title_credit", "author_host")

# 角色域值（模型只能给这三个值，其余一律按「角色未定」处理）
ROLE_HOST = "host"
ROLE_GUEST = "guest"
ROLE_UNKNOWN = "unknown"

# meta 里标记「本显示名来自两人通路」的 tier 值
DUO_EVIDENCE_TIER = "duo_metadata_role"

# 显示名标注（与现行复合名同一套措辞）
_MARKERS = {
    "title_credit": "标题关联推测",
    "author_host": "作者关联推测",
}


def _speaker_counts(dialogs: List[Dict]) -> Dict[str, int]:
    """统计每位说话人的发言条数。

    标签解析复用 SpeakerInferencer 的别名链（speaker/spk/speaker_id），
    延迟导入打断循环依赖：speaker_inferencer 在模块级导入本模块。
    """
    from .speaker_inferencer import SpeakerInferencer

    counts: Dict[str, int] = {}
    for dialog in dialogs or []:
        if not isinstance(dialog, dict):
            continue
        speaker = SpeakerInferencer.resolve_dialog_speaker(dialog)
        if speaker is None:
            continue
        key = str(speaker)
        counts[key] = counts.get(key, 0) + 1
    return counts


def select_primary_speakers(dialogs: List[Dict], speakers: List[str]) -> List[str]:
    """选出占比达标的主要说话人；条件不成立时返回空列表。

    判定口径：按 base dialogs 的发言条数算占比，占比 ≥15% 的说话人恰好两个，
    且两人合计占比 ≥80%，才算「低歧义两人分区」。三个及以上主要说话人、
    两人合计不足 80%、以及占比不达标的零散小簇都不进入本通路。
    """
    counts = _speaker_counts(dialogs)
    total = sum(counts.values())
    if not total:
        return []
    primary = [
        speaker
        for speaker in speakers
        if counts.get(str(speaker), 0) / total >= DUO_PRIMARY_SHARE
    ]
    if len(primary) != 2:
        return []
    combined = sum(counts.get(str(speaker), 0) for speaker in primary) / total
    if combined < DUO_COMBINED_SHARE:
        return []
    return primary


def _resolve_roles(roles: Optional[Dict], primary: List[str]) -> Optional[Dict[str, str]]:
    """两位主要说话人必须恰好一个 host、一个 guest；否则返回 None（通路关闭）。

    域外取值（含 unknown）与角色重复都判关闭：角色判错会让两个人的名字
    张冠李戴，比不显示名字更糟。
    """
    if not isinstance(roles, dict):
        return None
    given = {str(key): value for key, value in roles.items()}
    resolved: Dict[str, str] = {}
    for speaker in primary:
        role = given.get(speaker)
        if role not in (ROLE_HOST, ROLE_GUEST):
            return None
        resolved[speaker] = role
    if len(set(resolved.values())) != 2:
        return None
    return resolved


def _static_candidates(meta_entry: Any) -> List[Dict]:
    """取该说话人核对通过的静态候选（标题专场名 / 作者署名）。"""
    if not isinstance(meta_entry, dict):
        return []
    verified = meta_entry.get("verified_candidates")
    if not isinstance(verified, list):
        return []
    return [
        candidate
        for candidate in verified
        if isinstance(candidate, dict)
        and candidate.get("evidence_type") in DUO_STATIC_EVIDENCE_TYPES
        and isinstance(candidate.get("name"), str)
        and candidate["name"].strip()
    ]


def _mention_count(title: str) -> int:
    """标题里的 @ 提及个数；提及其以上时无法确定谁是嘉宾。"""
    return len(re.findall(r"@\w+", str(title or "")))


def _pick_candidate(candidates: List[Dict], role: str) -> Dict:
    """同一说话人同时有标题名与作者名时按角色取。

    主持人优先作者署名（发布账号通常就是主持人），嘉宾优先标题名（标题
    专场名通常写的是被访谈的人）。
    """
    preferred = "author_host" if role == ROLE_HOST else "title_credit"
    for candidate in candidates:
        if candidate.get("evidence_type") == preferred:
            return candidate
    return candidates[0]


def _fallback_label(speaker: str, speakers: List[str]) -> str:
    """占位符编号（「说话人N」），与现行降级标签同一实现。"""
    from .speaker_inferencer import SpeakerInferencer

    return SpeakerInferencer._build_fallback_label(speaker, speakers)


def _adopted_names(meta: Dict, mapping: Dict, exclude: str) -> set:
    """已被现行规则采用的名字集合（排除 exclude 那位说话人）。

    既取显示名本身，也取其核对通过候选里的名字：复合显示名
    （「说话人2｜标题关联推测:青柠｜对话称:柠姐」）整体不等于任何裸名，
    只有拆出候选名才拦得住「同一个人的两种写法分给两个人」。
    """
    names = set()
    for label, entry in meta.items():
        if label == exclude or not isinstance(entry, dict):
            continue
        if not entry.get("applied"):
            continue
        adopted = mapping.get(label)
        if isinstance(adopted, str) and adopted.strip():
            names.add(adopted.strip().casefold())
        for candidate in entry.get("verified_candidates") or []:
            if not isinstance(candidate, dict):
                continue
            name = candidate.get("name")
            if isinstance(name, str) and name.strip():
                names.add(name.strip().casefold())
    return names


def plan_duo_names(
    meta: Dict,
    mapping: Dict,
    *,
    dialogs: List[Dict],
    title: str,
    speakers: List[str],
    roles: Optional[Dict],
) -> Dict[str, Tuple[str, str]]:
    """算出两位主要说话人的推测显示名；前提不成立时返回空字典。

    返回 {speaker: (显示名, 角色)}。显示名带「推测」标注并保留原编号。
    """
    primary = select_primary_speakers(dialogs, speakers)
    if len(primary) != 2:
        return {}
    role_by_speaker = _resolve_roles(roles, primary)
    if role_by_speaker is None:
        return {}
    if _mention_count(title) > 1:
        return {}

    candidates_by_speaker: Dict[str, List[Dict]] = {}
    distinct_names: List[str] = []
    for speaker in primary:
        candidates = _static_candidates(meta.get(speaker))
        names = {candidate["name"].strip().casefold() for candidate in candidates}
        if len(names) != 1:
            # 没有静态候选，或同一人名下挂了 ≥2 个不同名字：分不清是谁。
            return {}
        candidates_by_speaker[speaker] = candidates
        distinct_names.append(next(iter(names)))
    if distinct_names[0] == distinct_names[1]:
        return {}

    plan: Dict[str, Tuple[str, str]] = {}
    for speaker in primary:
        entry = meta.get(speaker)
        if not isinstance(entry, dict) or entry.get("applied"):
            # 现行规则已能定名的人保持现行结果，本通路不插手。
            continue
        role = role_by_speaker[speaker]
        candidate = _pick_candidate(candidates_by_speaker[speaker], role)
        name = candidate["name"].strip()
        if name.casefold() in _adopted_names(meta, mapping, speaker):
            # 与另一位说话人已采用的名字撞名：整条通路关闭。
            return {}
        label = _fallback_label(speaker, speakers)
        marker = _MARKERS[candidate["evidence_type"]]
        plan[speaker] = (f"{label}｜{marker}:{name}", role)
    return plan


def apply_duo_metadata_roles(
    result: Dict,
    dialogs: List[Dict],
    title: str,
    speakers: List[str],
    roles: Optional[Dict],
) -> Dict:
    """把两人访谈的推测显示名写进闸门结果（原地修改后返回同一份结果）。

    ``result`` 是 ``SpeakerInferencer._apply_evidence_gate`` 的产物：mapping /
    meta / low_confidence。本函数只在该说话人按现行规则会被拒绝时改写它，
    并在 meta 里记 evidence_tier=duo_metadata_role 与角色。
    """
    if not isinstance(result, dict):
        return result
    mapping = result.get("mapping")
    meta = result.get("meta")
    if not isinstance(mapping, dict) or not isinstance(meta, dict):
        return result

    plan = plan_duo_names(
        meta,
        mapping,
        dialogs=dialogs,
        title=title,
        speakers=speakers,
        roles=roles,
    )
    if not plan:
        return result

    low_confidence = result.get("low_confidence")
    for speaker, (display_name, role) in plan.items():
        mapping[speaker] = display_name
        entry = meta[speaker]
        entry["applied"] = True
        entry["evidence_tier"] = DUO_EVIDENCE_TIER
        entry["role"] = role
        entry.pop("rejection_reason", None)
        if isinstance(low_confidence, list) and speaker in low_confidence:
            low_confidence.remove(speaker)
    return result
