"""
UsageRecorder unit tests.

Covers:
- Writing a usage row and reading it back from llm_usage table
- usage_missing flag persisted correctly (0/1)
- get_stats() aggregation by stage + overall total
- get_stats() time window filtering (days)
- Recorder never raises on DB errors (fail-open), returns False instead

All console output must be in English only (no emoji, no Chinese).
"""

import sqlite3

import pytest

from video_transcript_api.utils.logging.audit_logger import AuditLogger
from video_transcript_api.utils.logging.usage_recorder import UsageRecorder


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a temporary database path for each test."""
    return str(tmp_path / "test_audit.db")


@pytest.fixture
def audit_logger(tmp_db):
    """Create a fresh AuditLogger instance with a temp database (runs schema migration)."""
    return AuditLogger(db_path=tmp_db)


@pytest.fixture
def recorder(audit_logger):
    """Create a UsageRecorder bound to the temp AuditLogger."""
    return UsageRecorder(audit_logger=audit_logger)


class TestRecordWriteReadback:
    """Verify record() persists a row that can be read back correctly."""

    def test_record_success_writes_row(self, recorder, audit_logger):
        ok = recorder.record(
            task_id="task-1",
            stage="calibration",
            model="gpt-test",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            duration_ms=1234,
            usage_missing=False,
        )
        assert ok is True

        with audit_logger._get_cursor() as cursor:
            cursor.execute(
                "SELECT task_id, stage, model, prompt_tokens, completion_tokens, "
                "total_tokens, duration_ms, usage_missing FROM llm_usage"
            )
            row = cursor.fetchone()

        assert row == ("task-1", "calibration", "gpt-test", 100, 50, 150, 1234, 0)

    def test_record_usage_missing_flag(self, recorder, audit_logger):
        ok = recorder.record(
            task_id="task-2",
            stage="summary",
            model="gpt-test",
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            duration_ms=500,
            usage_missing=True,
        )
        assert ok is True

        with audit_logger._get_cursor() as cursor:
            cursor.execute(
                "SELECT prompt_tokens, completion_tokens, total_tokens, usage_missing "
                "FROM llm_usage WHERE task_id = ?",
                ("task-2",),
            )
            row = cursor.fetchone()

        # provider did not report usage -> tokens recorded as 0, but flagged
        assert row == (0, 0, 0, 1)

    def test_record_missing_task_id_and_stage_default_to_unknown(self, recorder, audit_logger):
        recorder.record(
            task_id=None,
            stage=None,
            model=None,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            duration_ms=10,
            usage_missing=False,
        )

        with audit_logger._get_cursor() as cursor:
            cursor.execute("SELECT task_id, stage, model FROM llm_usage")
            row = cursor.fetchone()

        assert row == ("unknown", "unknown", "unknown")

    def test_created_at_is_utc_formatted(self, recorder, audit_logger):
        recorder.record(
            task_id="task-3", stage="calibration", model="m",
            prompt_tokens=1, completion_tokens=1, total_tokens=2,
            duration_ms=1, usage_missing=False,
        )
        with audit_logger._get_cursor() as cursor:
            cursor.execute("SELECT created_at FROM llm_usage WHERE task_id = ?", ("task-3",))
            created_at = cursor.fetchone()[0]

        # format: "YYYY-MM-DD HH:MM:SS"
        import datetime
        parsed = datetime.datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
        assert parsed is not None


class TestGetStatsAggregation:
    """Verify get_stats() aggregates correctly by stage and overall."""

    def test_aggregates_by_stage(self, recorder):
        recorder.record(
            task_id="t1", stage="calibration", model="m1",
            prompt_tokens=100, completion_tokens=50, total_tokens=150,
            duration_ms=100, usage_missing=False,
        )
        recorder.record(
            task_id="t2", stage="calibration", model="m1",
            prompt_tokens=200, completion_tokens=100, total_tokens=300,
            duration_ms=200, usage_missing=False,
        )
        recorder.record(
            task_id="t3", stage="summary", model="m1",
            prompt_tokens=10, completion_tokens=10, total_tokens=20,
            duration_ms=50, usage_missing=True,
        )

        stats = recorder.get_stats(days=30)

        by_stage = {row["stage"]: row for row in stats["by_stage"]}
        assert by_stage["calibration"]["call_count"] == 2
        assert by_stage["calibration"]["total_tokens"] == 450
        assert by_stage["calibration"]["prompt_tokens"] == 300
        assert by_stage["calibration"]["completion_tokens"] == 150
        assert by_stage["calibration"]["usage_missing_count"] == 0

        assert by_stage["summary"]["call_count"] == 1
        assert by_stage["summary"]["total_tokens"] == 20
        assert by_stage["summary"]["usage_missing_count"] == 1

        assert stats["total"]["call_count"] == 3
        assert stats["total"]["total_tokens"] == 470
        assert stats["total"]["usage_missing_count"] == 1
        assert stats["days"] == 30

    def test_empty_db_returns_zeroed_total(self, recorder):
        stats = recorder.get_stats(days=30)
        assert stats["by_stage"] == []
        assert stats["total"] == {
            "call_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "usage_missing_count": 0,
        }

    def test_days_window_excludes_old_rows(self, recorder, audit_logger):
        # Insert a row with an old created_at directly (bypassing record()'s "now" timestamp)
        with audit_logger._get_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO llm_usage
                (task_id, stage, model, prompt_tokens, completion_tokens,
                 total_tokens, duration_ms, usage_missing, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("old-task", "calibration", "m", 10, 10, 20, 5, 0, "2020-01-01 00:00:00"),
            )

        recorder.record(
            task_id="new-task", stage="calibration", model="m",
            prompt_tokens=1, completion_tokens=1, total_tokens=2,
            duration_ms=1, usage_missing=False,
        )

        stats = recorder.get_stats(days=1)
        # only the freshly-recorded row should count; the 2020 row is outside the window
        assert stats["total"]["call_count"] == 1
        assert stats["total"]["total_tokens"] == 2


class TestRecordFailOpen:
    """Verify record() never raises even when the underlying DB write fails."""

    def test_record_returns_false_on_db_error(self, recorder, monkeypatch):
        def _broken_cursor():
            raise sqlite3.OperationalError("simulated DB failure")

        monkeypatch.setattr(recorder._audit_logger, "_get_cursor", _broken_cursor)

        # Should not raise
        ok = recorder.record(
            task_id="task-x", stage="calibration", model="m",
            prompt_tokens=1, completion_tokens=1, total_tokens=2,
            duration_ms=1, usage_missing=False,
        )
        assert ok is False

    def test_get_stats_returns_empty_on_db_error(self, recorder, monkeypatch):
        def _broken_cursor():
            raise sqlite3.OperationalError("simulated DB failure")

        monkeypatch.setattr(recorder._audit_logger, "_get_cursor", _broken_cursor)

        stats = recorder.get_stats(days=30)
        assert stats["by_stage"] == []
        assert stats["total"]["call_count"] == 0
