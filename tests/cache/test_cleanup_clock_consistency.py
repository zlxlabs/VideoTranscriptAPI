"""Regression test: CacheManager.cleanup_old_cache and .cleanup_task_status
must judge retention against the exact same UTC clock basis.

Both video_cache.updated_at and task_status.completed_at/created_at are
written by SQLite's own `CURRENT_TIMESTAMP`, which is always UTC (naive,
no timezone suffix, "YYYY-MM-DD HH:MM:SS"). cleanup_task_status() already
computed its cutoff from `datetime.now(timezone.utc)`. cleanup_old_cache()
used to compute its cutoff from the naive, timezone-unaware
`datetime.now()` -- on a host whose local wall clock differs from UTC,
this shifts the effective cutoff away from the true UTC cutoff, so the two
cleanup functions can disagree on whether a record at the same age should
be deleted. That breaks the "task_status retention is clamped to at least
cache_retention_days" invariant added earlier: the two cleanups are
supposed to share one clock, or a window opens where a task_status row
(and therefore its /view/{view_token} link) is deleted while the matching
cache record is still considered fresh, or vice versa.

This test freezes the module's `datetime.datetime.now` so that:
- `now(tz=timezone.utc)` returns a fixed instant (what cleanup_task_status
  calls), and
- `now()` (no args, naive) returns that same instant shifted by an
  artificial, extreme local/UTC skew (what the pre-fix cleanup_old_cache
  called) -- simulating a host far west of UTC.

Fixture/backdating pattern mirrors tests/cache/test_task_status_cleanup.py
(itself borrowed from tests/unit/test_cache_manager.py's tmp_path-based
isolated CacheManager convention).

Console output: English only, no emoji.
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


def _insert_cache_row(cm, updated_at: str, media_id: str = "vid") -> str:
    """Insert a video_cache row via raw SQL so `updated_at` can be
    backdated directly -- save_cache() always stamps CURRENT_TIMESTAMP and
    writes real files, neither of which this boundary test needs.

    Returns the generated (unique) media_id.
    """
    unique = uuid.uuid4().hex[:8]
    media_id = f"{media_id}-{unique}"
    with cm._get_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO video_cache
                (platform, url, media_id, use_speaker_recognition, files_loc, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "youtube",
                f"https://example.com/{media_id}",
                media_id,
                0,
                f"youtube/2026/202601/{media_id}",
                updated_at,
            ),
        )
    return media_id


def _cache_row_exists(cm, media_id: str) -> bool:
    with cm._get_cursor() as cursor:
        cursor.execute("SELECT 1 FROM video_cache WHERE media_id = ?", (media_id,))
        return cursor.fetchone() is not None


def _insert_task_row(cm, status, completed_at, created_at, media_id: str = "vid") -> str:
    """Same backdating technique as
    tests/cache/test_task_status_cleanup.py::_insert_task."""
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


def _task_row_exists(cm, task_id: str) -> bool:
    return cm.get_task_by_id(task_id) is not None


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

class TestCleanupClockBasisConsistency:
    """codex-review P2: cleanup_old_cache() and cleanup_task_status() must
    agree on retention decisions for records of the same age, because both
    tables are stamped by the same UTC CURRENT_TIMESTAMP clock."""

    def test_same_boundary_timestamps_same_deletion_verdict_under_local_utc_skew(
        self, cm, monkeypatch
    ):
        frozen_utc_now = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        # Extreme, deliberately implausible skew (12h) so the test fails
        # loudly if cleanup_old_cache ever again reads the naive local
        # clock instead of UTC -- regardless of the CI host's real TZ.
        local_skew = timedelta(hours=-12)

        class _SkewedLocalDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is not None:
                    return frozen_utc_now
                return (frozen_utc_now + local_skew).replace(tzinfo=None)

        # cache_manager.py does `import datetime` and calls
        # `datetime.datetime.now(...)`; patch the `datetime` class
        # attribute on that module reference so both cleanup_old_cache and
        # cleanup_task_status (which live in the same module) observe the
        # same frozen/skewed clock.
        monkeypatch.setattr(cache_manager_module.datetime, "datetime", _SkewedLocalDateTime)

        retention_days = 30
        true_utc_cutoff = frozen_utc_now - timedelta(days=retention_days)

        one_second_before = (true_utc_cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        exactly_at_cutoff = true_utc_cutoff.strftime("%Y-%m-%d %H:%M:%S")
        well_within_retention = (true_utc_cutoff + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")

        # video_cache rows
        cache_before = _insert_cache_row(cm, one_second_before)
        cache_at = _insert_cache_row(cm, exactly_at_cutoff)
        cache_within = _insert_cache_row(cm, well_within_retention)

        # task_status rows (terminal, same three boundary timestamps)
        task_before = _insert_task_row(cm, TaskStatus.SUCCESS, one_second_before, one_second_before)
        task_at = _insert_task_row(cm, TaskStatus.SUCCESS, exactly_at_cutoff, exactly_at_cutoff)
        task_within = _insert_task_row(cm, TaskStatus.SUCCESS, well_within_retention, well_within_retention)

        cache_deleted = cm.cleanup_old_cache(days=retention_days)
        task_deleted = cm.cleanup_task_status(retention_days=retention_days)

        assert cache_deleted == 1, "only the row older than the true UTC cutoff should be reclaimed"
        assert task_deleted == 1, "only the row older than the true UTC cutoff should be reclaimed"

        # Strict '<' boundary: exactly-at-cutoff is kept, one-second-older is deleted.
        assert not _cache_row_exists(cm, cache_before), "video_cache: row older than UTC cutoff must be deleted"
        assert _cache_row_exists(cm, cache_at), "video_cache: row exactly at cutoff must be kept (strict <)"
        assert _cache_row_exists(cm, cache_within), "video_cache: row within retention must be kept"

        assert not _task_row_exists(cm, task_before), "task_status: row older than UTC cutoff must be deleted"
        assert _task_row_exists(cm, task_at), "task_status: row exactly at cutoff must be kept (strict <)"
        assert _task_row_exists(cm, task_within), "task_status: row within retention must be kept"

        # The two tables' verdicts must agree record-for-record at every
        # boundary timestamp -- this is the actual invariant under test:
        # cleanup_old_cache and cleanup_task_status share one clock.
        assert _cache_row_exists(cm, cache_before) == _task_row_exists(cm, task_before)
        assert _cache_row_exists(cm, cache_at) == _task_row_exists(cm, task_at)
        assert _cache_row_exists(cm, cache_within) == _task_row_exists(cm, task_within)


class TestCleanupSharedNowParameter:
    """codex-review R10 #1: within a single _periodic_maintenance pass,
    cleanup_old_cache() walks and deletes files (can take seconds or
    longer), so if cleanup_task_status() is then called and independently
    reads now() again, the two cutoffs drift apart and open a race window:
    a record whose timestamp falls between the two cutoffs is judged "not
    yet expired" by one cleanup and "expired" by the other, breaking the
    "task_status lives at least as long as cache" invariant even though
    both functions already agree on the UTC clock basis (the case covered
    by TestCleanupClockBasisConsistency above, which is about *which*
    clock, not *when* it's read).

    The fix: both functions accept an optional `now` (tz-aware UTC
    datetime); the caller computes it once and passes the identical value
    to both, removing the window entirely. These tests lock two things:
    passing `now=` is honored verbatim (not merely used as a hint), and
    omitting it keeps reading the real clock as before (backward
    compatibility for existing callers/tests, including the frozen-clock
    test above and tests/cache/test_task_status_cleanup.py)."""

    def test_shared_now_produces_identical_verdict_without_touching_the_real_clock(self, cm):
        """A deliberately stale `now` (weeks before the actual wall clock)
        is passed to both functions; if either ignored it and read the
        real clock instead, the boundary rows below would land on the
        wrong side of the cutoff and this test would fail."""
        shared_now = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        retention_days = 30
        cutoff = shared_now - timedelta(days=retention_days)

        one_second_before = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        exactly_at_cutoff = cutoff.strftime("%Y-%m-%d %H:%M:%S")

        cache_before = _insert_cache_row(cm, one_second_before)
        cache_at = _insert_cache_row(cm, exactly_at_cutoff)
        task_before = _insert_task_row(cm, TaskStatus.SUCCESS, one_second_before, one_second_before)
        task_at = _insert_task_row(cm, TaskStatus.SUCCESS, exactly_at_cutoff, exactly_at_cutoff)

        cache_deleted = cm.cleanup_old_cache(days=retention_days, now=shared_now)
        task_deleted = cm.cleanup_task_status(retention_days=retention_days, now=shared_now)

        assert cache_deleted == 1, "only the row older than the injected now's cutoff should be reclaimed"
        assert task_deleted == 1, "only the row older than the injected now's cutoff should be reclaimed"

        assert not _cache_row_exists(cm, cache_before), "video_cache: row older than injected cutoff must be deleted"
        assert _cache_row_exists(cm, cache_at), "video_cache: row exactly at cutoff must be kept (strict <)"
        assert not _task_row_exists(cm, task_before), "task_status: row older than injected cutoff must be deleted"
        assert _task_row_exists(cm, task_at), "task_status: row exactly at cutoff must be kept (strict <)"

    def test_omitting_now_keeps_reading_the_real_clock(self, cm, monkeypatch):
        """Callers that do not pass now= (legacy callers/tests) must get the
        exact pre-fix behavior: the function reads
        datetime.datetime.now(timezone.utc) internally itself. Verified the
        same way as test_boundary_exact_cutoff_kept_one_second_earlier_deleted
        in test_task_status_cleanup.py: freeze the module clock and confirm
        it is still consulted when now= is omitted."""
        frozen_now = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

        class _FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen_now if tz is not None else frozen_now.replace(tzinfo=None)

        monkeypatch.setattr(cache_manager_module.datetime, "datetime", _FrozenDateTime)

        retention_days = 30
        cutoff = frozen_now - timedelta(days=retention_days)
        one_second_before = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

        cache_row = _insert_cache_row(cm, one_second_before)
        task_row = _insert_task_row(cm, TaskStatus.SUCCESS, one_second_before, one_second_before)

        assert cm.cleanup_old_cache(days=retention_days) == 1
        assert cm.cleanup_task_status(retention_days=retention_days) == 1
        assert not _cache_row_exists(cm, cache_row)
        assert not _task_row_exists(cm, task_row)
