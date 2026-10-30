import html
import re
from typing import Any, Dict, List, Optional

from ...i18n import t

_PLACEHOLDER_SPEAKER_RE = re.compile(r"^(?:说话人\d+|Speaker \d+)$")
_SPEAKER_LEVELS = {"high", "medium", "low", "none"}
_EVIDENCE_KEYS = {
    "dialog_address": "render.speaker.basis.dialog_address",
    "title_credit": "render.speaker.basis.title_credit",
    "author_host": "render.speaker.basis.author_host",
    "description_roster": "render.speaker.basis.description_roster",
}
_ROLE_KEYS = {
    "host": "render.speaker.basis.host",
    "guest": "render.speaker.basis.guest",
}


def _speaker_meta_entry(
    speaker_meta: Any,
    dialog: Dict,
    display_speaker: Any,
) -> Optional[Dict]:
    """Return the per-label inference dict, or None for legacy/empty meta."""
    if not isinstance(speaker_meta, dict):
        return None
    keys: List[Any] = []
    for key in (dialog.get("speaker_id"), dialog.get("speaker"), display_speaker):
        if key is None or key == "":
            continue
        keys.append(key)
        key_str = str(key)
        if key_str != key:
            keys.append(key_str)
    seen = set()
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        details = speaker_meta.get(key)
        if isinstance(details, dict) and (
            "applied" in details
            or "confidence" in details
            or "confidence_level" in details
        ):
            return details
    return None


def _is_unconfirmed_speaker(display_speaker: Any, details: Optional[Dict]) -> bool:
    """Pending badge: applied flag first, placeholder regex only for old data."""
    if isinstance(details, dict) and "applied" in details:
        return not bool(details.get("applied"))
    return bool(display_speaker) and bool(
        _PLACEHOLDER_SPEAKER_RE.match(str(display_speaker))
    )


def _confidence_level_label(confidence: Any, unconfirmed: bool) -> str:
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        if confidence >= 0.8:
            return t("render.confidence.reliable")
        if confidence >= 0.6:
            return t("render.confidence.ai")
        return t("render.confidence.pending")
    if unconfirmed:
        return t("render.confidence.pending")
    return t("render.confidence.ai")


def _basis_title(details: Dict, level: str, display_speaker: Any) -> str:
    title_parts = [str(display_speaker)] if level == "medium" else []
    basis = details.get("basis")
    if isinstance(basis, dict):
        evidence_type = basis.get("evidence_type")
        evidence_key = (
            _EVIDENCE_KEYS.get(evidence_type)
            if isinstance(evidence_type, str)
            else None
        )
        if evidence_key:
            evidence = t(evidence_key)
            title_parts.append(t("render.speaker.basis.evidence", evidence=evidence))
            quote = basis.get("quote")
            if quote is not None:
                title_parts[-1] += t(
                    "render.speaker.basis.quote", quote=str(quote)
                )
        role = basis.get("role")
        role_key = _ROLE_KEYS.get(role) if isinstance(role, str) else None
        if role_key:
            role = t(role_key)
            role_text = t("render.speaker.basis.role", role=role)
            if title_parts:
                title_parts[-1] += t("render.speaker.basis.separator") + role_text
            else:
                title_parts.append(role_text)
    if level == "low":
        title_parts.append(t("render.speaker.basis.not_verified"))
    return t("render.speaker.basis.separator").join(title_parts)


def _render_level_tag(
    details: Dict,
    display_speaker: Any,
    color: str,
    override: Optional[Dict],
    level: str,
) -> str:
    safe_display = html.escape(str(display_speaker), quote=True)
    has_override_name = bool(override and override.get("name"))
    guess_name = details.get("guess_name")
    safe_guess = (
        "" if guess_name is None else html.escape(str(guess_name), quote=True)
    )
    if has_override_name:
        label = safe_display
    elif level == "medium":
        label = safe_guess
    elif level == "low":
        label = (
            f"{safe_display} · "
            f"{t('render.speaker.level.possibly', name=safe_guess)}"
        )
    else:
        label = safe_display

    title_level = "override" if has_override_name else level
    title = _basis_title(details, title_level, display_speaker)
    confidence_level = "override" if has_override_name else level
    title_attr = f' title="{html.escape(title, quote=True)}"' if title else ""
    basis = details.get("basis")
    rejection_reason = basis.get("rejection_reason") if isinstance(basis, dict) else None
    rejection_attr = (
        f' data-rejection-reason="{html.escape(str(rejection_reason), quote=True)}"'
        if rejection_reason is not None
        else ""
    )

    override_status = str((override or {}).get("status") or "").lower()
    override_badge = ""
    if override_status in {"confirmed", "suspect"}:
        status_text = (
            t("render.speaker.confirmed")
            if override_status == "confirmed"
            else t("render.speaker.suspect")
        )
        override_badge = (
            f'<span class="speaker-override-status speaker-override-{override_status}" '
            f'data-override-status="{override_status}">{status_text}</span>'
        )

    level_badge = ""
    if not has_override_name and level in {"high", "medium", "low"} and override_status != "confirmed":
        level_badge = (
            '<span class="speaker-status-badge speaker-level-badge" '
            f'data-speaker-level="{level}">{t(f"render.speaker.level.{level}")}</span>'
        )
    pending_badge = ""
    if level == "none" and not has_override_name:
        pending_badge = (
            '<span class="speaker-status-badge speaker-pending" '
            'data-speaker-status="pending">'
            + t("render.speaker.pending")
            + "</span>"
        )

    return f'''<div class="speaker-tag"{title_attr} data-confidence-level="{confidence_level}"{rejection_attr} style="background-color: {color};">
                            {label}
                            {level_badge}{pending_badge}{override_badge}
                        </div>'''


def render_speaker_tag(
    speaker_meta: Any,
    dialog: Dict,
    display_speaker: Any,
    color: str,
    override: Optional[Dict],
) -> str:
    """Render one speaker-tag, preserving exact legacy markup without a valid level."""
    details = _speaker_meta_entry(speaker_meta, dialog, display_speaker)
    level = details.get("confidence_level") if isinstance(details, dict) else None
    if isinstance(level, str) and level in _SPEAKER_LEVELS:
        return _render_level_tag(details, display_speaker, color, override, level)

    unconfirmed = _is_unconfirmed_speaker(display_speaker, details)
    pending_badge = ""
    if unconfirmed:
        pending_badge = (
            '<span class="speaker-status-badge speaker-pending" '
            'data-speaker-status="pending">'
            + t("render.speaker.pending")
            + "</span>"
        )
    override_badge = ""
    if override:
        status = str(override.get("status") or "").lower()
        if status in {"confirmed", "suspect"}:
            status_text = (
                t("render.speaker.confirmed")
                if status == "confirmed"
                else t("render.speaker.suspect")
            )
            override_badge = (
                f'<span class="speaker-override-status speaker-override-{status}" '
                f'data-override-status="{status}">{status_text}</span>'
            )
    confidence = details.get("confidence") if isinstance(details, dict) else None
    level = _confidence_level_label(confidence, unconfirmed)
    safe_speaker = html.escape(str(display_speaker))
    return f'''<div class="speaker-tag" title="{level}" data-confidence-level="{level}" style="background-color: {color};">
                            {safe_speaker}
                            {pending_badge}{override_badge}
                        </div>'''
