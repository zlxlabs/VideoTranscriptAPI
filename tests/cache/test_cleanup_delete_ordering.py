"""Regression tests: cleanup_old_cache()'s per-record deletion must order
the DB row DELETE before the directory shutil.rmtree, and must not let an
rmtree failure make the function silently under-report what it actually
deleted.

Background (Y5, PR3 review hardening 加固轮): the pre-fix code did
`shutil.rmtree(file_path)` first, in a plain (uncaught) call, and only
*then* ran `DELETE FROM video_cache WHERE id = ?` in a second, independent
`_get_cursor()` transaction. If that DELETE failed for any reason (disk
I/O error, the DB being momentarily locked, ...), the directory was already
gone but the row survived, pointing at a directory that no longer exists --
a deterministic, silent data loss (the row is "findable but unreadable"
from then on). Worse, the module-level `except Exception: return 0` at the
bottom of cleanup_old_cache() would swallow that failure and report "0
records cleaned", hiding the fact that a real file deletion had just
happened.

The fix reorders the two operations (DB DELETE first, inside its own
committed/rolled-back transaction; rmtree second) and wraps rmtree in its
own try/except OSError so a filesystem failure, once the DB row is already
gone, can only leave a harmless orphaned directory (nothing points to it
any more) instead of corrupting a live record. This test module locks down
both halves of that contract:

  1. A DB DELETE failure must leave BOTH the row and the directory intact,
     so the record is naturally retried on the next cleanup cycle.
  2. An rmtree failure, occurring strictly after the DB row is already
     gone, must not raise, must not block cleanup_old_cache() from
     continuing to process other candidates, and must not cause the
     function to under-report deleted_count.

Console output: English only, no emoji (project convention).
"""
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from src.video_transcript_api.cache.cache_manager import CacheManager


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _insert_expired_cache_row_with_files(cm, updated_at: str, platform: str = "youtube"):
    """Insert a video_cache row via raw SQL (so `updated_at` can be
    backdated directly) AND create a real directory with a dummy file
    underneath it, so shutil.rmtree has something concrete to remove and
    file_path.exists() checks are meaningful. Mirrors the identical helper
    in test_cleanup_media_lock_concurrency.py -- duplicated locally (rather
    than cross-importing a sibling test module, which this repo's test
    suite has no existing precedent for) to keep this file self-contained.

    Returns (platform, media_id, files_loc, file_path).
    """
    media_id = f"vid-{uuid.uuid4().hex[:8]}"
    files_loc = f"{platform}/2026/202601/{media_id}"
    file_path = cm.cache_dir / files_loc
    file_path.mkdir(parents=True, exist_ok=True)
    (file_path / "transcript_funasr.json").write_text("{}", encoding="utf-8")

    with cm._get_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO video_cache
                (platform, url, media_id, use_speaker_recognition, files_loc, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                platform,
                f"https://example.com/{media_id}",
                media_id,
                0,
                files_loc,
                updated_at,
            ),
        )
    return platform, media_id, files_loc, file_path


def _cache_row_exists(cm, platform: str, media_id: str) -> bool:
    with cm._get_cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM video_cache WHERE platform = ? AND media_id = ?",
            (platform, media_id),
        )
        return cursor.fetchone() is not None


class _GuardedCursor:
    """Delegates every call to the real sqlite3.Cursor except execute(),
    which raises for any SQL statement starting with `sql_prefix`. Needed
    because sqlite3.Cursor is a C-extension type that does not allow
    monkeypatching its `execute` attribute directly (raises AttributeError:
    read-only), so the interception has to happen at a wrapper level
    instead."""

    def __init__(self, real_cursor, sql_prefix, exc):
        self._real = real_cursor
        self._sql_prefix = sql_prefix
        self._exc = exc

    def execute(self, sql, params=()):
        if sql.strip().startswith(self._sql_prefix):
            raise self._exc
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _make_cursor_that_fails_on(cm, sql_prefix, exc, monkeypatch):
    """Monkeypatch cm._get_cursor so any cursor.execute() call whose SQL
    text starts with `sql_prefix` raises `exc`; every other query on the
    same connection (the lock-protected recheck queries, the
    files_loc-sharing check, ...) goes through unmodified."""
    real_get_cursor = cm._get_cursor

    @contextmanager
    def wrapped():
        with real_get_cursor() as real_cursor:
            yield _GuardedCursor(real_cursor, sql_prefix, exc)

    monkeypatch.setattr(cm, "_get_cursor", wrapped)


class TestDbDeleteFailurePreservesRowAndDirectory:
    def test_db_delete_failure_leaves_both_row_and_directory_intact(
        self, cm, monkeypatch,
    ):
        retention_days = 30
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        expired = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

        platform, media_id, _, file_path = _insert_expired_cache_row_with_files(cm, expired)

        _make_cursor_that_fails_on(
            cm, "DELETE FROM video_cache WHERE id", RuntimeError("synthetic DB failure"),
            monkeypatch,
        )

        # cleanup_old_cache's own module-level `except Exception: return 0`
        # catches the propagated failure -- this is pre-existing, unrelated
        # behavior (not what Y5 changes); what matters here is the state
        # left behind, not the return value.
        deleted = cm.cleanup_old_cache(days=retention_days, now=now)

        assert deleted == 0
        assert file_path.exists(), (
            "rmtree must not have run before the DB row was confirmed deleted"
        )
        assert _cache_row_exists(cm, platform, media_id), (
            "a failed DELETE must leave the row in place for the next cleanup cycle"
        )

        # Prove it is a genuine retry story, not a permanently wedged state:
        # once the synthetic failure is lifted, the very next cleanup cycle
        # reclaims the still-expired record normally.
        monkeypatch.undo()
        deleted_next_cycle = cm.cleanup_old_cache(days=retention_days, now=now)
        assert deleted_next_cycle == 1
        assert not file_path.exists()
        assert not _cache_row_exists(cm, platform, media_id)


class TestRmtreeFailureAfterDbRowIsGone:
    def test_rmtree_failure_still_deletes_the_row_and_reports_it_honestly(
        self, cm, monkeypatch,
    ):
        retention_days = 30
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        expired = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

        platform, media_id, _, file_path = _insert_expired_cache_row_with_files(cm, expired)

        def failing_rmtree(path):
            raise OSError("synthetic permission denied")

        monkeypatch.setattr(
            "shutil.rmtree", failing_rmtree,
        )

        deleted = cm.cleanup_old_cache(days=retention_days, now=now)

        # The DB row must be gone -- no dangling reference to a directory
        # cleanup tried (and failed) to remove -- and the honest count of 1
        # must be reported, not silently swallowed into a "nothing
        # happened" 0 by some outer catch-all.
        assert deleted == 1
        assert not _cache_row_exists(cm, platform, media_id)
        # The directory itself is left behind (rmtree failed) -- a harmless
        # orphan, since nothing in the DB references it any more.
        assert file_path.exists()

    def test_rmtree_failure_on_one_candidate_does_not_block_others_in_the_same_sweep(
        self, cm, monkeypatch,
    ):
        """A single rmtree failure must not abort the whole cleanup sweep --
        other, unrelated expired candidates in the same cleanup_old_cache()
        call must still be reclaimed normally."""
        retention_days = 30
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        expired = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

        platform_a, media_id_a, _, file_path_a = _insert_expired_cache_row_with_files(
            cm, expired, platform="youtube",
        )
        platform_b, media_id_b, _, file_path_b = _insert_expired_cache_row_with_files(
            cm, expired, platform="bilibili",
        )

        real_rmtree = __import__("shutil").rmtree

        def selective_failing_rmtree(path):
            if str(file_path_a) in str(path):
                raise OSError("synthetic permission denied for media A only")
            return real_rmtree(path)

        monkeypatch.setattr("shutil.rmtree", selective_failing_rmtree)

        deleted = cm.cleanup_old_cache(days=retention_days, now=now)

        assert deleted == 2, "both DB rows must be reclaimed even though one rmtree failed"
        assert not _cache_row_exists(cm, platform_a, media_id_a)
        assert not _cache_row_exists(cm, platform_b, media_id_b)
        assert file_path_a.exists(), "media A's directory is the harmless orphan left by the failed rmtree"
        assert not file_path_b.exists(), "media B's rmtree succeeded normally"
