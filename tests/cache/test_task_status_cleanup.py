"""Unit tests for CacheManager.cleanup_task_status.

Verifies the periodic task_status retention cleanup: only terminal
(success/failed) records older than the retention window are deleted;
non-terminal records (queued/processing/calibrating) are always kept
regardless of age, since they may still be in-flight or waiting for the
startup orphan-recovery scan.

Fixture pattern (tmp_path-based isolated CacheManager) mirrors
tests/unit/test_cache_manager.py, this repo's established convention for
isolated CacheManager pytest tests. tests/cache/ itself only contains
legacy print-based manual scripts with no reusable pytest fixture, so the
fixture is borrowed from tests/unit/ instead of reinvented here.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import src.video_transcript_api.cache.cache_manager as cache_manager_module
from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.task_status import TaskStatus


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def cm(tmp_path):
    """Create a CacheManager backed by a temporary directory/db."""
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _days_ago(days: int) -> str:
    """UTC timestamp string `days` before now, in the same format SQLite's
    CURRENT_TIMESTAMP writes ("YYYY-MM-DD HH:MM:SS", no timezone suffix)."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _insert_task(cm, status, completed_at, created_at, media_id="vid"):
    """Create a task_status row through the public API, then force its
    status/timestamps directly with SQL.

    create_task() always produces a fresh 'queued' row stamped with
    CURRENT_TIMESTAMP; to exercise retention boundaries we need explicit
    control over status and created_at/completed_at, so we backdate them
    afterwards. This mirrors the raw-SQL backdating technique used in
    tests/unit/test_cache_manager.py
    (TestLLMConfigFallback._create_task_at_time).

    Returns the generated task_id.
    """
    unique = uuid.uuid4().hex[:8]
    task_info = cm.create_task(
        url=f"https://example.com/{media_id}-{unique}",
        platform="youtube",
        media_id=f"{media_id}-{unique}",
    )
    task_id = task_info["task_id"]
    with cm._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET status = ?, completed_at = ?, created_at = ? WHERE task_id = ?",
            (status, completed_at, created_at, task_id),
        )
    return task_id


# ---------------------------------------------------------------------------
# cleanup_task_status
# ---------------------------------------------------------------------------

class TestCleanupTaskStatus:
    """Tests for CacheManager.cleanup_task_status."""

    def test_deletes_only_old_terminal_records(self, cm):
        """Old success/failed rows are deleted; new terminal and any
        non-terminal rows (regardless of age) are kept."""
        old_success = _insert_task(
            cm, TaskStatus.SUCCESS, completed_at=_days_ago(400), created_at=_days_ago(400)
        )
        old_failed = _insert_task(
            cm, TaskStatus.FAILED, completed_at=_days_ago(400), created_at=_days_ago(400)
        )
        new_success = _insert_task(
            cm, TaskStatus.SUCCESS, completed_at=_days_ago(5), created_at=_days_ago(5)
        )
        old_queued = _insert_task(
            cm, TaskStatus.QUEUED, completed_at=None, created_at=_days_ago(400)
        )
        old_processing = _insert_task(
            cm, TaskStatus.PROCESSING, completed_at=None, created_at=_days_ago(400)
        )

        deleted = cm.cleanup_task_status(retention_days=180)

        assert deleted == 2
        assert cm.get_task_by_id(old_success) is None
        assert cm.get_task_by_id(old_failed) is None
        assert cm.get_task_by_id(new_success) is not None
        assert cm.get_task_by_id(old_queued) is not None, "non-terminal rows must never be deleted"
        assert cm.get_task_by_id(old_processing) is not None, "non-terminal rows must never be deleted"

    def test_return_value_equals_deleted_count(self, cm):
        """Return value must exactly match the number of rows removed."""
        for _ in range(3):
            _insert_task(
                cm, TaskStatus.SUCCESS, completed_at=_days_ago(400), created_at=_days_ago(400)
            )
        _insert_task(
            cm, TaskStatus.FAILED, completed_at=_days_ago(5), created_at=_days_ago(5)
        )

        deleted = cm.cleanup_task_status(retention_days=180)

        assert deleted == 3

    def test_empty_table_returns_zero(self, cm):
        """Calling cleanup on an empty task_status table must not raise
        and must return 0."""
        assert cm.cleanup_task_status(retention_days=180) == 0

    def test_null_completed_at_falls_back_to_created_at(self, cm):
        """Defensive fallback: a terminal row with NULL completed_at (should
        not normally happen, since update_task_status always stamps
        completed_at when writing a terminal status) is still reclaimed
        using created_at, so such edge-case rows are not stuck forever."""
        task_id = _insert_task(
            cm, TaskStatus.FAILED, completed_at=None, created_at=_days_ago(400)
        )

        deleted = cm.cleanup_task_status(retention_days=180)

        assert deleted == 1
        assert cm.get_task_by_id(task_id) is None

    def test_boundary_exact_cutoff_kept_one_second_earlier_deleted(self, cm, monkeypatch):
        """Retention boundary is a strict `<`: a row whose timestamp equals
        the cutoff exactly is kept; a row one second older is deleted.

        The system clock is frozen (via monkeypatching the `datetime.datetime`
        class referenced inside cache_manager) so the comparison is exact and
        not subject to a wall-clock race between test setup and the call to
        cleanup_task_status.
        """
        frozen_now = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

        class _FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen_now if tz is not None else frozen_now.replace(tzinfo=None)

        # cache_manager.py does `import datetime` and calls
        # `datetime.datetime.now(...)`; patch the `datetime` class attribute
        # on that module reference so only this test's call to
        # cleanup_task_status observes the frozen clock. monkeypatch restores
        # the original class automatically at teardown.
        monkeypatch.setattr(cache_manager_module.datetime, "datetime", _FrozenDateTime)

        retention_days = 30
        cutoff = frozen_now - timedelta(days=retention_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        one_second_before_str = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

        at_cutoff = _insert_task(
            cm, TaskStatus.SUCCESS, completed_at=cutoff_str, created_at=cutoff_str
        )
        before_cutoff = _insert_task(
            cm, TaskStatus.SUCCESS, completed_at=one_second_before_str, created_at=one_second_before_str
        )

        deleted = cm.cleanup_task_status(retention_days=retention_days)

        assert deleted == 1
        assert cm.get_task_by_id(at_cutoff) is not None, "row exactly at cutoff must be kept (strict <)"
        assert cm.get_task_by_id(before_cutoff) is None, "row older than cutoff must be deleted"
