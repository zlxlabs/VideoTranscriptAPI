"""Read-only report contract tests; console output stays ASCII-only."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "task_observability_report.py"
SINCE = "2026-09-01T00:00:00Z"
UNTIL = "2026-09-02T00:00:00Z"


def _create_databases(tmp_path, *, old_audit=False):
    cache_path = tmp_path / "cache.db"
    audit_path = tmp_path / "audit.db"
    with sqlite3.connect(cache_path) as connection:
        connection.execute(
            """CREATE TABLE task_status (
                task_id TEXT PRIMARY KEY, status TEXT, platform TEXT,
                created_at TEXT, completed_at TEXT, calibration_status TEXT,
                summary_status TEXT, chapters_status TEXT, terminal_snapshot TEXT
            )"""
        )
    with sqlite3.connect(audit_path) as connection:
        if old_audit:
            connection.execute(
                """CREATE TABLE task_audit_snapshots (
                    task_id TEXT PRIMARY KEY, status TEXT, platform TEXT,
                    calibration_status TEXT, summary_status TEXT,
                    chapters_status TEXT, completed_at TEXT
                )"""
            )
        else:
            connection.execute(
                """CREATE TABLE task_audit_snapshots (
                    task_id TEXT PRIMARY KEY, status TEXT, platform TEXT,
                    calibration_status TEXT, summary_status TEXT,
                    chapters_status TEXT, created_at TEXT, completed_at TEXT,
                    observability_json TEXT
                )"""
            )
        connection.execute(
            """CREATE TABLE llm_usage (
                id INTEGER PRIMARY KEY, task_id TEXT, stage TEXT, model TEXT,
                prompt_tokens INTEGER, completion_tokens INTEGER,
                total_tokens INTEGER, duration_ms INTEGER, usage_missing INTEGER,
                created_at TEXT
            )"""
        )
    return cache_path, audit_path


def _insert_task(path, table, values):
    with sqlite3.connect(path) as connection:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )


def _run(cache_path, audit_path, *, since=SINCE, until=UNTIL):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--cache-db",
            str(cache_path),
            "--audit-db",
            str(audit_path),
            "--since",
            since,
            "--until",
            until,
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        preexec_fn=(
            lambda: os.setuid(65534)
            if os.geteuid() == 0 and cache_path.exists()
            and not cache_path.stat().st_mode & 0o444 else None
        ),
    )


def test_report_deduplicates_tasks_and_separates_wall_and_llm_durations(tmp_path):
    cache_path, audit_path = _create_databases(tmp_path)
    _insert_task(
        audit_path,
        "task_audit_snapshots",
        {
            "task_id": "task-a",
            "status": "success",
            "platform": "youtube",
            "calibration_status": "full",
            "summary_status": "generated",
            "chapters_status": "generated",
            "created_at": "2026-09-01 00:00:00",
            "completed_at": "2026-09-01 00:00:10",
            "observability_json": json.dumps(
                {
                    "notes_status": "generated",
                    "stages": {
                        "transcription": {
                            "elapsed_ms": 4000,
                            "count": 1,
                            "successes": 1,
                            "failures": 0,
                        }
                    },
                }
            ),
        },
    )
    _insert_task(
        cache_path,
        "task_status",
        {
            "task_id": "task-a",
            "status": "success",
            "platform": "youtube",
            "created_at": "2026-09-01 00:00:00",
            "completed_at": "2026-09-01 00:00:10",
            "calibration_status": "full",
            "summary_status": "generated",
            "chapters_status": "generated",
            "terminal_snapshot": json.dumps(
                {"result": {"notes_status": "failed"}}
            ),
        },
    )
    _insert_task(
        cache_path,
        "task_status",
        {
            "task_id": "task-b",
            "status": "failed",
            "platform": "bilibili",
            "created_at": "2026-09-01 23:59:55",
            "completed_at": "2026-09-01 23:59:59",
            "calibration_status": None,
            "summary_status": None,
            "chapters_status": None,
            "terminal_snapshot": json.dumps(
                {
                    "url": "https://private.example/?token=secret",
                    "error_message": "RAW-FAILURE-DETAIL",
                    "result": {"notes_status": "failed"},
                        "observability": {
                            "stages": {
                                "metadata": {
                                    "elapsed_ms": 0,
                                    "count": 1,
                                    "successes": 1,
                                    "failures": 0,
                                },
                                "download": {
                                "elapsed_ms": 3000,
                                "count": 1,
                                "successes": 0,
                                "failures": 1,
                            }
                        }
                    },
                }
            ),
        },
    )
    _insert_task(
        cache_path,
        "task_status",
        {
            "task_id": "at-until",
            "status": "success",
            "platform": "youtube",
            "created_at": "2026-09-02 00:00:00",
            "completed_at": "2026-09-02 00:00:01",
            "calibration_status": None,
            "summary_status": None,
            "chapters_status": None,
            "terminal_snapshot": None,
        },
    )
    _insert_task(
        cache_path,
        "task_status",
        {
            "task_id": "in-progress",
            "status": "processing",
            "platform": "youtube",
            "created_at": "2026-09-01 23:59:50",
            "completed_at": None,
            "calibration_status": None,
            "summary_status": None,
            "chapters_status": None,
            "terminal_snapshot": None,
        },
    )
    with sqlite3.connect(audit_path) as connection:
        connection.executemany(
            """INSERT INTO llm_usage
                (task_id, stage, model, prompt_tokens, completion_tokens,
                 total_tokens, duration_ms, usage_missing, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("task-a", "summary", "model-a", 10, 5, 15, 1200, 0, "2026-09-01T00:00:01Z"),
                ("task-a", "summary", "model-a", 8, 7, 15, 1400, 1, "2026-09-01T00:00:02Z"),
                ("task-b", "notes", "model-b", 2, 3, 5, 8000, 0, "2026-09-01T23:59:56Z"),
                ("task-b", "notes", "model-b", 2, 3, 5, 8000, 0, "2026-09-01T23:59:57Z"),
            ],
        )

    result = _run(cache_path, audit_path)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["window"] == {
        "since": SINCE,
        "until": UNTIL,
        "semantics": "UTC [since, until)",
        "selected_by": "created_at",
    }
    assert report["tasks"]["count"] == 3
    assert report["tasks"]["status_counts"] == {
        "success": 1,
        "failed": 1,
        "in_progress": 1,
        "unknown": 0,
    }
    assert report["tasks"]["platform_counts"] == {"bilibili": 1, "youtube": 2}
    assert report["tasks"]["notes_status"] == {
        "states": {"failed": 1, "generated": 1},
        "unknown_count": 1,
    }
    assert report["tasks"]["summary_status"]["unknown_count"] == 2
    assert report["tasks"]["chapters_status"] == {
        "states": {"generated": 1},
        "unknown_count": 2,
    }
    assert report["duration_ms"]["end_to_end"] == {
        "samples": 2,
        "p50": 4000,
        "p95": 10000,
    }
    assert report["duration_ms"]["stages"]["download"] == {
        "successes": 0,
        "failures": 1,
        "duration_ms": {"samples": 1, "p50": 3000, "p95": 3000},
    }
    assert report["duration_ms"]["stages"]["metadata"]["duration_ms"] == {
        "samples": 1,
        "p50": 0,
        "p95": 0,
    }
    assert report["llm_usage"] == {
        "calls": 4,
        "prompt_tokens": 22,
        "completion_tokens": 18,
        "total_tokens": 40,
        "usage_missing_count": 1,
        "duration_sum_ms": 18600,
        "tasks_without_usage_rows": 1,
    }
    assert "secret" not in result.stdout
    assert "RAW-FAILURE-DETAIL" not in result.stdout


def test_old_audit_schema_is_read_without_migration_or_cache_status_backfill(tmp_path):
    cache_path, audit_path = _create_databases(tmp_path, old_audit=True)
    _insert_task(
        audit_path,
        "task_audit_snapshots",
        {
            "task_id": "old-task",
            "status": "success",
            "platform": "youtube",
            "calibration_status": "full",
            "summary_status": "generated",
            "chapters_status": "generated",
            "completed_at": "2026-09-01 00:00:10",
        },
    )
    _insert_task(
        cache_path,
        "task_status",
        {
            "task_id": "old-task",
            "status": "success",
            "platform": "youtube",
            "created_at": "2026-09-01 00:00:00",
            "completed_at": "2026-09-01 00:00:10",
            "calibration_status": "full",
            "summary_status": "generated",
            "chapters_status": "generated",
            "terminal_snapshot": None,
        },
    )

    result = _run(cache_path, audit_path)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["tasks"]["count"] == 1
    assert report["tasks"]["notes_status"]["unknown_count"] == 1
    assert report["fields_missing"]["observability_snapshot"] == 1
    with sqlite3.connect(audit_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(task_audit_snapshots)")
        }
    assert "created_at" not in columns
    assert "observability_json" not in columns


def test_empty_window_reports_zero_rows_and_no_duration_samples(tmp_path):
    cache_path, audit_path = _create_databases(tmp_path)

    result = _run(cache_path, audit_path)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["tasks"]["count"] == 0
    assert report["duration_ms"]["end_to_end"] == {
        "samples": 0,
        "p50": None,
        "p95": None,
    }
    assert report["llm_usage"]["calls"] == 0
    assert report["llm_usage"]["duration_sum_ms"] == 0


def test_missing_paths_bad_databases_and_invalid_window_fail_without_creating_files(
    tmp_path,
):
    cache_path, audit_path = _create_databases(tmp_path)
    missing_cache = tmp_path / "does-not-exist.db"

    missing = _run(missing_cache, audit_path)
    malformed = tmp_path / "malformed.db"
    malformed.write_text("not a sqlite database", encoding="ascii")
    bad_database = _run(cache_path, malformed)
    invalid_window = _run(
        cache_path,
        audit_path,
        since="2026-09-01T00:00:00Z' OR 1=1--",
    )
    cache_path.chmod(0)
    try:
        unreadable = _run(cache_path, audit_path)
    finally:
        cache_path.chmod(0o600)
    unknown_cache = tmp_path / "unknown-cache.db"
    unknown_audit = tmp_path / "unknown-audit.db"
    with sqlite3.connect(unknown_cache) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    with sqlite3.connect(unknown_audit) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    unknown_schema = _run(unknown_cache, unknown_audit)

    assert missing.returncode != 0
    assert not missing_cache.exists()
    assert bad_database.returncode != 0
    assert "tasks\": {\"count\": 0" not in bad_database.stdout
    assert invalid_window.returncode != 0
    assert unreadable.returncode != 0
    assert unknown_schema.returncode != 0
    assert "tasks\": {\"count\": 0" not in unknown_schema.stdout
