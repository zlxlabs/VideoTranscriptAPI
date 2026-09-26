#!/usr/bin/env python3
"""Read-only, task-deduplicated summary of terminal task observations."""

import argparse
import json
import math
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


STAGES = frozenset(
    {"url_parse", "cache_check", "metadata", "download", "transcription", "llm_processing"}
)
SUMMARY_STATES = frozenset(
    {"generated", "skipped_short", "failed", "pending", "disabled"}
)
CHAPTER_STATES = frozenset(
    {"generated", "skipped_short", "skipped_no_timeline", "failed", "pending", "disabled"}
)
NOTES_STATES = frozenset({"generated", "failed"})
TASK_STATES = frozenset({"success", "failed"})
IN_PROGRESS_STATES = frozenset({"queued", "processing", "calibrating"})
BASE_FIELDS = (
    "task_id",
    "status",
    "platform",
    "created_at",
    "completed_at",
    "calibration_status",
    "summary_status",
    "chapters_status",
)


def _parse_utc(value, option):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"--{option} must be an ISO-8601 timestamp with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"--{option} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _sqlite_utc(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _window_text(value):
    precision = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=precision).replace("+00:00", "Z")


def _open_readonly(path):
    db_path = Path(path).expanduser()
    if not db_path.is_file():
        raise FileNotFoundError(f"database file does not exist: {db_path}")
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _table_columns(connection, table):
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise ValueError(f"unknown database schema: missing {table} table")
    return {row[1] for row in rows}


def _validate_schema(connection, label, table, required):
    columns = _table_columns(connection, table)
    missing = sorted(required - columns)
    if missing:
        raise ValueError(
            f"unknown {label} schema: {table} missing required columns {', '.join(missing)}"
        )
    return columns


def _read_task_rows(connection, table, columns, since, until):
    optional = {"created_at", "terminal_snapshot", "observability_json"}
    selected = [
        field if field in columns else f"NULL AS {field}"
        for field in (*BASE_FIELDS, "terminal_snapshot", "observability_json")
        if field not in optional or field in columns
    ]
    where = ""
    params = ()
    if "created_at" in columns:
        where = (
            "WHERE created_at IS NULL OR julianday(created_at) IS NULL "
            "OR (julianday(created_at) >= julianday(?) "
            "AND julianday(created_at) < julianday(?))"
        )
        params = (since, until)
    return connection.execute(
        f"SELECT {', '.join(selected)} FROM {table} {where}", params
    ).fetchall()


def _json_object(value):
    if value is None or value == "":
        return {}
    if not isinstance(value, str):
        raise ValueError("invalid JSON observation field")
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("observation JSON must be an object")
    return decoded


def _state(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def _stage_observations(row):
    audit = _json_object(row.get("observability_json"))
    cache_snapshot = _json_object(row.get("terminal_snapshot"))
    cache_observation = cache_snapshot.get("observability")
    observation = audit or (
        cache_observation if isinstance(cache_observation, dict) else {}
    )
    result = cache_snapshot.get("result")
    if not isinstance(result, dict):
        result = {}
    notes_status = _state(audit.get("notes_status"), NOTES_STATES)
    if notes_status is None:
        notes_status = _state(result.get("notes_status"), NOTES_STATES)
    raw_stages = observation.get("stages")
    if not isinstance(raw_stages, dict) and isinstance(cache_observation, dict):
        raw_stages = cache_observation.get("stages")
    raw_counters = observation.get("counters")
    if not isinstance(raw_counters, dict) and isinstance(cache_observation, dict):
        raw_counters = cache_observation.get("counters")
    counters = {
        name: value
        for name, value in (raw_counters.items() if isinstance(raw_counters, dict) else ())
        if name in {"cache_hit", "cache_hit_partial"}
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }
    stages = {}
    if isinstance(raw_stages, dict):
        for name, raw in raw_stages.items():
            if name not in STAGES or not isinstance(raw, dict):
                continue
            elapsed = raw.get("elapsed_ms")
            count = raw.get("count")
            failures = raw.get("failures")
            if (
                isinstance(elapsed, bool)
                or not isinstance(elapsed, (int, float))
                or not math.isfinite(elapsed)
                or elapsed < 0
                or not isinstance(count, int)
                or isinstance(count, bool)
                or count < 1
                or not isinstance(failures, int)
                or isinstance(failures, bool)
                or failures < 0
                or failures > count
            ):
                continue
            successes = raw.get("successes", count - failures)
            if (
                not isinstance(successes, int)
                or isinstance(successes, bool)
                or successes < 0
                or successes + failures != count
            ):
                continue
            stages[name] = {
                "elapsed_ms": round(elapsed, 2),
                "count": count,
                "successes": successes,
                "failures": failures,
            }
    return notes_status, stages, counters, bool(audit or cache_observation)


def _merge_task_rows(audit_rows, cache_rows, since, until):
    merged = {}
    for source_rows in (cache_rows, audit_rows):
        for raw in source_rows:
            row = dict(raw)
            task_id = row.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            existing = merged.setdefault(task_id, {})
            for field, value in row.items():
                if value is not None:
                    existing[field] = value

    selected = {}
    missing_created = 0
    for task_id, row in merged.items():
        created = _sqlite_utc(row.get("created_at"))
        if created is None:
            missing_created += 1
        elif since <= created < until:
            selected[task_id] = row
    return selected, missing_created


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    value = round(ordered[index], 2)
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _duration_summary(values):
    return {
        "samples": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
    }


def _usage_summary(connection, task_ids):
    values = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
              "total_tokens": 0, "usage_missing_count": 0, "duration_sum_ms": 0}
    tasks_with_usage = set()
    ids = sorted(task_ids)
    for start in range(0, len(ids), 500):
        batch = ids[start : start + 500]
        placeholders = ", ".join("?" for _ in batch)
        rows = connection.execute(
            "SELECT task_id, COUNT(*) AS calls, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
            "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
            "COALESCE(SUM(usage_missing), 0) AS usage_missing_count, "
            "COALESCE(SUM(duration_ms), 0) AS duration_sum_ms "
            f"FROM llm_usage WHERE task_id IN ({placeholders}) GROUP BY task_id",
            batch,
        ).fetchall()
        for row in rows:
            tasks_with_usage.add(row["task_id"])
            values["calls"] += row["calls"]
            for field in (
                "prompt_tokens", "completion_tokens", "total_tokens",
                "usage_missing_count", "duration_sum_ms",
            ):
                values[field] += row[field]
    values["tasks_without_usage_rows"] = len(task_ids - tasks_with_usage)
    return values


def build_report(cache_connection, audit_connection, since_value, until_value):
    since = _parse_utc(since_value, "since")
    until = _parse_utc(until_value, "until")
    if since >= until:
        raise ValueError("--since must be earlier than --until")
    since_sql = _window_text(since)
    until_sql = _window_text(until)

    cache_columns = _validate_schema(
        cache_connection,
        "cache",
        "task_status",
        {"task_id", "status", "platform", "created_at", "completed_at"},
    )
    audit_columns = _validate_schema(
        audit_connection,
        "audit",
        "task_audit_snapshots",
        {
            "task_id", "status", "platform", "completed_at",
            "calibration_status", "summary_status", "chapters_status",
        },
    )
    usage_columns = _validate_schema(
        audit_connection,
        "audit",
        "llm_usage",
        {
            "task_id", "prompt_tokens", "completion_tokens", "total_tokens",
            "duration_ms", "usage_missing",
        },
    )
    audit_version_columns = _table_columns(audit_connection, "schema_version")
    if "version" not in audit_version_columns:
        raise ValueError("unknown audit schema: schema_version missing version column")
    versions = audit_connection.execute("SELECT version FROM schema_version").fetchall()
    if (
        len(versions) != 1
        or type(versions[0][0]) is not int
        or versions[0][0] not in (5, 6)
    ):
        raise ValueError("unknown audit schema version")
    if versions[0][0] == 6:
        _validate_schema(
            audit_connection,
            "audit",
            "task_audit_snapshots",
            {"created_at", "observability_json"},
        )

    cache_rows = _read_task_rows(
        cache_connection, "task_status", cache_columns, since_sql, until_sql
    )
    audit_rows = _read_task_rows(
        audit_connection, "task_audit_snapshots", audit_columns, since_sql, until_sql
    )
    tasks, unassigned_created = _merge_task_rows(audit_rows, cache_rows, since, until)

    status_counts = {"success": 0, "failed": 0, "in_progress": 0, "unknown": 0}
    platform_counts = {}
    state_counts = {key: {} for key in ("notes_status", "summary_status", "chapters_status")}
    unknown_states = {key: 0 for key in state_counts}
    end_to_end = []
    stage_values = {}
    cache_hits = {"full": 0, "partial": 0, "unknown": 0}
    missing = {
        "completed_at": 0,
        "observability_snapshot": 0,
        "notes_status": 0,
        "summary_status": 0,
        "chapters_status": 0,
        "end_to_end_duration": 0,
        "stages": 0,
    }
    for row in tasks.values():
        status = _state(row.get("status"), TASK_STATES)
        if status is not None:
            status_counts[status] += 1
        elif row.get("status") in IN_PROGRESS_STATES:
            status_counts["in_progress"] += 1
        else:
            status_counts["unknown"] += 1
        platform = row.get("platform")
        if not isinstance(platform, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", platform):
            platform = "unknown"
        platform_counts[platform] = platform_counts.get(platform, 0) + 1

        notes, stages, counters, has_observation = _stage_observations(row)
        full_hit = counters.get("cache_hit", 0) > 0
        partial_hit = counters.get("cache_hit_partial", 0) > 0
        cache_hits["full"] += int(full_hit)
        cache_hits["partial"] += int(partial_hit)
        if not counters:
            cache_hits["unknown"] += 1
        states = {
            "notes_status": notes,
            "summary_status": _state(row.get("summary_status"), SUMMARY_STATES),
            "chapters_status": _state(row.get("chapters_status"), CHAPTER_STATES),
        }
        for field, value in states.items():
            if value is None:
                unknown_states[field] += 1
                missing[field] += 1
            else:
                counts = state_counts[field]
                counts[value] = counts.get(value, 0) + 1
        if not has_observation:
            missing["observability_snapshot"] += 1
        if not stages:
            missing["stages"] += 1
        for name, stage in stages.items():
            totals = stage_values.setdefault(name, {"durations": [], "successes": 0, "failures": 0})
            totals["durations"].append(stage["elapsed_ms"])
            totals["successes"] += stage["successes"]
            totals["failures"] += stage["failures"]

        completed = _sqlite_utc(row.get("completed_at"))
        created = _sqlite_utc(row.get("created_at"))
        if completed is None:
            missing["completed_at"] += 1
            missing["end_to_end_duration"] += 1
        elif created is None or completed < created:
            missing["end_to_end_duration"] += 1
        else:
            end_to_end.append((completed - created).total_seconds() * 1000)

    duration_stages = {}
    for name, values in sorted(stage_values.items()):
        duration_stages[name] = {
            "successes": values["successes"],
            "failures": values["failures"],
            "duration_ms": _duration_summary(values["durations"]),
        }
    task_ids = set(tasks)
    usage = _usage_summary(audit_connection, task_ids)
    missing["task_status"] = status_counts["unknown"]
    return {
        "window": {
            "since": _window_text(since),
            "until": _window_text(until),
            "semantics": "UTC [since, until)",
            "selected_by": "created_at",
        },
        "tasks": {
            "count": len(tasks),
            "status_counts": status_counts,
            "platform_counts": dict(sorted(platform_counts.items())),
            "cache_hits": cache_hits,
            "notes_status": {
                "states": dict(sorted(state_counts["notes_status"].items())),
                "unknown_count": unknown_states["notes_status"],
            },
            "summary_status": {
                "states": dict(sorted(state_counts["summary_status"].items())),
                "unknown_count": unknown_states["summary_status"],
            },
            "chapters_status": {
                "states": dict(sorted(state_counts["chapters_status"].items())),
                "unknown_count": unknown_states["chapters_status"],
            },
        },
        "duration_ms": {
            "end_to_end": _duration_summary(end_to_end),
            "stages": duration_stages,
        },
        "llm_usage": usage,
        "fields_missing": missing,
        "unassigned_created_at_tasks": unassigned_created,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-db", required=True)
    parser.add_argument("--audit-db", required=True)
    parser.add_argument("--since", required=True, help="ISO-8601 timestamp with timezone")
    parser.add_argument("--until", required=True, help="ISO-8601 timestamp with timezone")
    parser.add_argument("--format", choices=("json",), default="json")
    args = parser.parse_args(argv)
    try:
        with closing(_open_readonly(args.cache_db)) as cache_connection:
            with closing(_open_readonly(args.audit_db)) as audit_connection:
                report = build_report(
                    cache_connection, audit_connection, args.since, args.until
                )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"task observability report failed: {exc}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, ensure_ascii=True, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
