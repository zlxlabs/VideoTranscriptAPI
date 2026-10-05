"""Compute summary-to-original length ratio stats for audit monitoring."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...transcriber import FunASRSpeakerClient
from ...utils.logging import setup_logger
from ...llm.core.summary_budget import (
    classify_original_length_band,
    compute_summary_budget,
    SummaryBudgetConfig,
)

logger = setup_logger(__name__)

_BAND_ORDER = ("S", "M", "L")


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _p90(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(0.9 * (len(ordered) - 1))
    return ordered[index]


def _read_original_length(cache_dir: Path) -> Optional[int]:
    funasr_file = cache_dir / "transcript_funasr.json"
    capswriter_file = cache_dir / "transcript_capswriter.txt"
    try:
        if funasr_file.exists():
            with funasr_file.open("r", encoding="utf-8") as handle:
                funasr_data = json.load(handle)
            client = FunASRSpeakerClient()
            transcript_text = client.format_transcript_with_speakers(funasr_data)
            return len(transcript_text)
        if capswriter_file.exists():
            with capswriter_file.open("r", encoding="utf-8") as handle:
                return len(handle.read())
    except Exception as exc:
        logger.warning(f"Failed to read original transcript length from {cache_dir}: {exc}")
    return None


def _read_summary_length(cache_dir: Path) -> Optional[int]:
    summary_file = cache_dir / "llm_summary.txt"
    if not summary_file.exists():
        return None
    try:
        with summary_file.open("r", encoding="utf-8") as handle:
            return len(handle.read())
    except OSError as exc:
        logger.warning(f"Failed to read summary length from {cache_dir}: {exc}")
        return None


def _resolve_cache_dir(
    cache_conn: sqlite3.Connection,
    cache_root: Path,
    *,
    cache_id: Optional[int],
    platform: str,
    media_id: str,
    use_speaker_recognition: bool,
) -> Optional[Path]:
    """Resolve artifact directory for the task's actual cache variant."""
    cache_root_resolved = cache_root.resolve()
    cache_row = None

    if cache_id is not None:
        cache_row = cache_conn.execute(
            "SELECT files_loc FROM video_cache WHERE id = ?",
            (cache_id,),
        ).fetchone()

    if not cache_row:
        cache_row = cache_conn.execute(
            """
            SELECT files_loc FROM video_cache
            WHERE platform = ? AND media_id = ? AND use_speaker_recognition = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (platform, media_id, 1 if use_speaker_recognition else 0),
        ).fetchone()

    if not cache_row:
        return None

    cache_dir = (cache_root / Path(cache_row["files_loc"])).resolve()
    if not cache_dir.is_relative_to(cache_root_resolved):
        logger.warning(
            f"files_loc escapes cache_root, skipping: {cache_row['files_loc']}"
        )
        return None
    return cache_dir


def _aggregate_band(ratios: List[float], over_100: int, over_hardcap: int) -> Dict[str, Any]:
    return {
        "n": len(ratios),
        "median_ratio": round(_median(ratios), 4) if ratios else 0.0,
        "p90_ratio": round(_p90(ratios), 4) if ratios else 0.0,
        "over_100pct": over_100,
        "over_hardcap": over_hardcap,
    }


def compute_summary_ratio_stats(
    *,
    audit_db_path: str,
    cache_db_path: str,
    cache_root: Path,
    days: int,
    budget_config: Optional[SummaryBudgetConfig] = None,
) -> Dict[str, Any]:
    """Join audit snapshots to cache artifacts and compute S/M/L band ratios."""
    budget_config = budget_config or SummaryBudgetConfig()
    cutoff = (datetime.now() - timedelta(days=max(1, days))).strftime("%Y-%m-%d %H:%M:%S")

    audit_conn = sqlite3.connect(f"file:{audit_db_path}?mode=ro", uri=True)
    audit_conn.row_factory = sqlite3.Row
    cache_conn = sqlite3.connect(f"file:{cache_db_path}?mode=ro", uri=True)
    cache_conn.row_factory = sqlite3.Row

    try:
        snapshot_rows = audit_conn.execute(
            """
            SELECT task_id, platform, summary_status, archived_at
            FROM task_audit_snapshots
            WHERE summary_status = 'generated'
              AND status = 'success'
              AND COALESCE(content_expired, 0) = 0
              AND archived_at >= ?
            """,
            (cutoff,),
        ).fetchall()

        band_ratios: Dict[str, List[float]] = {band: [] for band in _BAND_ORDER}
        band_over_100: Dict[str, int] = {band: 0 for band in _BAND_ORDER}
        band_over_hardcap: Dict[str, int] = {band: 0 for band in _BAND_ORDER}
        skipped = 0

        for row in snapshot_rows:
            task_id = row["task_id"]
            platform = row["platform"]
            task_row = cache_conn.execute(
                """
                SELECT platform, media_id, use_speaker_recognition, cache_id
                FROM task_status
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if not task_row:
                skipped += 1
                continue

            resolved_platform = task_row["platform"] or platform
            media_id = task_row["media_id"]
            if not resolved_platform or not media_id:
                skipped += 1
                continue

            use_speaker = bool(task_row["use_speaker_recognition"])
            cache_dir = _resolve_cache_dir(
                cache_conn,
                cache_root,
                cache_id=task_row["cache_id"],
                platform=resolved_platform,
                media_id=media_id,
                use_speaker_recognition=use_speaker,
            )
            if not cache_dir:
                skipped += 1
                continue

            original_length = _read_original_length(cache_dir)
            summary_length = _read_summary_length(cache_dir)
            if original_length is None or summary_length is None or original_length <= 0:
                skipped += 1
                continue

            band = classify_original_length_band(original_length)
            if band not in band_ratios:
                skipped += 1
                continue

            ratio = summary_length / original_length
            band_ratios[band].append(ratio)
            if ratio > 1.0:
                band_over_100[band] += 1
            hard_cap = compute_summary_budget(original_length, budget_config).hard_cap
            if summary_length > hard_cap:
                band_over_hardcap[band] += 1

        bands = {
            band: _aggregate_band(
                band_ratios[band],
                band_over_100[band],
                band_over_hardcap[band],
            )
            for band in _BAND_ORDER
        }
        return {
            "days": days,
            "cutoff": cutoff,
            "bands": bands,
            "skipped_tasks": skipped,
            "sampled_tasks": sum(bands[b]["n"] for b in _BAND_ORDER),
        }
    finally:
        audit_conn.close()
        cache_conn.close()
