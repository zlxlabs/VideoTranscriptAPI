"""Structural and language validation for generated chapters."""

import bisect
from typing import Any, Dict, List, Optional

from ...utils.logging import setup_logger
from ..utils.language_detector import detect_language

logger = setup_logger(__name__)

_RETRY_HINT_VALUE_MAX_CHARS = 200
_RETRY_HINT_VALUE_TRUNCATE_TO = 199
_RETRY_HINT_MAX_CHARS = 1000
_RETRY_HINT_TRUNCATE_TO = 999
CHAPTERS_LANGUAGE_MISMATCH = "CHAPTERS-LANGUAGE-MISMATCH"


def _truncate_repr_for_hint(
    value: Any,
    max_chars: int = _RETRY_HINT_VALUE_MAX_CHARS,
    truncate_to: int = _RETRY_HINT_VALUE_TRUNCATE_TO,
) -> str:
    """Bound the repr of untrusted values before including them in errors/logs."""
    text = repr(value)
    if len(text) <= max_chars:
        return text
    return text[:truncate_to] + "…"


def _count_moved_entries(chapters: List[Dict[str, Any]]) -> int:
    """Count entries whose positions change when sorted by start_seg."""
    sorted_segs = sorted(c["start_seg"] for c in chapters)
    return sum(
        1 for c, target in zip(chapters, sorted_segs) if c["start_seg"] != target
    )


def _longest_increasing_subsequence(values: List[int]) -> int:
    """Return the strict LIS length using the O(n log n) patience algorithm."""
    tails: List[int] = []
    for value in values:
        pos = bisect.bisect_left(tails, value)
        if pos == len(tails):
            tails.append(value)
        else:
            tails[pos] = value
    return len(tails)


def _validate_and_normalize_start_segs(
    raw_chapters: Any,
    survived_indices: List[int],
    expected_language: Optional[str] = None,
) -> tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Validate chapter fields, anchors, ordering, and (when requested) language.

    Returns normalized chapters and no error on success. A language mismatch
    is terminal to the caller and is logged here; structural errors retain the
    existing semantic retry behavior.
    """
    if not isinstance(raw_chapters, list) or not raw_chapters:
        return None, "chapters field is missing, not a list, or empty"

    survived_set = set(survived_indices)
    min_survived = survived_indices[0]

    items: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_chapters):
        if not isinstance(item, dict):
            return None, f"chapters[{idx}] is not an object"

        start_seg = item.get("start_seg")
        if isinstance(start_seg, bool):
            coerced = None
        elif isinstance(start_seg, int):
            coerced = start_seg
        elif isinstance(start_seg, float) and start_seg.is_integer():
            coerced = int(start_seg)
        elif isinstance(start_seg, str):
            try:
                coerced = int(start_seg.strip())
            except ValueError:
                coerced = None
        else:
            coerced = None
        if coerced is None:
            return None, (
                f"chapters[{idx}].start_seg is not an int: "
                f"{_truncate_repr_for_hint(start_seg)}"
            )
        start_seg = coerced
        if start_seg not in survived_set:
            return None, (
                f"chapters[{idx}].start_seg={start_seg} out of range (not among the "
                f"{len(survived_indices)} surviving segment indices in "
                f"[{survived_indices[0]}, {survived_indices[-1]}])"
            )

        raw_title = item.get("title")
        if not isinstance(raw_title, str) or not raw_title.strip():
            return None, (
                f"chapters[{idx}].title is missing, not a string, or blank: "
                f"{_truncate_repr_for_hint(raw_title)}"
            )

        raw_gist = item.get("gist")
        if not isinstance(raw_gist, str) or not raw_gist.strip():
            return None, (
                f"chapters[{idx}].gist is missing, not a string, or blank: "
                f"{_truncate_repr_for_hint(raw_gist)}"
            )

        items.append({
            "title": raw_title.strip(),
            "gist": raw_gist.strip(),
            "start_seg": start_seg,
        })

    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for item in items:
        if item["start_seg"] in seen:
            continue
        seen.add(item["start_seg"])
        deduped.append(item)

    is_sorted = all(
        curr["start_seg"] > prev["start_seg"]
        for prev, curr in zip(deduped, deduped[1:])
    )
    if not is_sorted:
        moved = _count_moved_entries(deduped)
        lis_len = _longest_increasing_subsequence([d["start_seg"] for d in deduped])
        logger.warning(
            f"[CHAPTERS] start_seg sequence not strictly increasing after dedup; "
            f"normalizing by sorting: moved {moved}/{len(deduped)} chapters, "
            f"LIS {lis_len}/{len(deduped)}, "
            f"before {[d['start_seg'] for d in deduped]}"
        )
        deduped.sort(key=lambda c: c["start_seg"])

    if deduped[0]["start_seg"] > min_survived:
        logger.info(
            f"[CHAPTERS] Clamping first chapter start_seg from "
            f"{deduped[0]['start_seg']} to {min_survived} "
            "(smallest surviving segment index)"
        )
        deduped[0] = {**deduped[0], "start_seg": min_survived}

    if expected_language is not None:
        output_text = " ".join(
            f"{chapter['title']} {chapter['gist']}" for chapter in deduped
        )
        detected_language = detect_language(output_text)
        if detected_language != expected_language:
            error = (
                f"{CHAPTERS_LANGUAGE_MISMATCH}: "
                f"expected={expected_language} detected={detected_language}"
            )
            logger.error(f"[CHAPTERS] {error}")
            return None, error

    return deduped, None
