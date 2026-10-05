#!/usr/bin/env python3
"""Scan audit.db for suspected truncated summary LLM calls.

Heuristic (pre-finish_reason): stage=summary, completion_tokens >= threshold,
created after cutoff. Joins task_audit_snapshots for view_token/title when present.

Usage:
  uv run python scripts/scan_truncated_summaries.py [--db PATH] [--since ISO] [--min-tokens N]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "audit.db"
DEFAULT_SINCE = "2026-08-24 18:00:00"
DEFAULT_MIN_COMPLETION_TOKENS = 2400


def _resolve_db_path(path: str | None) -> Path:
    if path:
        return Path(path)
    repo_db = DEFAULT_DB
    if repo_db.exists():
        return repo_db
    fallback = Path(__file__).resolve().parents[2] / "VideoTranscriptAPI" / "data" / "audit.db"
    return fallback


def scan_truncated_summaries(
    db_path: Path,
    *,
    since: str,
    min_completion_tokens: int,
) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            """
            SELECT
                u.id,
                u.task_id,
                u.model,
                u.prompt_tokens,
                u.completion_tokens,
                u.total_tokens,
                u.duration_ms,
                u.usage_missing,
                u.created_at,
                s.view_token,
                s.title,
                s.platform
            FROM llm_usage u
            LEFT JOIN task_audit_snapshots s ON s.task_id = u.task_id
            WHERE u.stage = 'summary'
              AND u.created_at >= ?
              AND u.completion_tokens >= ?
            ORDER BY u.created_at DESC, u.id DESC
            """,
            (since, min_completion_tokens),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("No suspected truncated summary calls found.")
        return

    headers = (
        "id",
        "created_at",
        "task_id",
        "view_token",
        "model",
        "completion_tokens",
        "title",
    )
    print("\t".join(headers))
    for row in rows:
        print(
            "\t".join(
                str(row.get(column, "") or "")
                for column in headers
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=None,
        help=f"Path to audit.db (default: {DEFAULT_DB} or main repo fallback)",
    )
    parser.add_argument(
        "--since",
        default=DEFAULT_SINCE,
        help=f"Only rows with created_at >= this timestamp (default: {DEFAULT_SINCE})",
    )
    parser.add_argument(
        "--min-tokens",
        type=int,
        default=DEFAULT_MIN_COMPLETION_TOKENS,
        help=(
            "Minimum completion_tokens to flag (default: "
            f"{DEFAULT_MIN_COMPLETION_TOKENS}, S-band min max_tokens)"
        ),
    )
    args = parser.parse_args(argv)

    db_path = _resolve_db_path(args.db)
    if not db_path.exists():
        print(f"audit.db not found: {db_path}", file=sys.stderr)
        return 1

    rows = scan_truncated_summaries(
        db_path,
        since=args.since,
        min_completion_tokens=args.min_tokens,
    )

    for row in rows:
        print(json.dumps(row, ensure_ascii=False))

    _print_table(rows)
    print(f"Total rows: {len(rows)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
