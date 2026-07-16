"""Regression tests: cleanup_old_cache() must not delete a media directory
or its video_cache row while that media is concurrently being written to,
queued for processing, or was just refreshed.

Background (U1, PR3 review hardening): cleanup_old_cache() used to select
candidate rows by stale updated_at and immediately shutil.rmtree + DELETE
them, without acquiring the media's media_lock, without re-checking
freshness, and without excluding media that has an in-flight task. Racing
this against a same-media write (e.g. a user hits /api/recalibrate on an
old media right as the periodic cleanup cycle runs) could:
  - rmtree a directory that llm_ops._save_llm_results is actively writing
    into (the active task then fails), or
  - delete the video_cache DB row for a media whose task just finished
    writing fresh products (orphaned files + a "vanished" cache entry).

The fix makes cleanup_old_cache() process candidates one at a time, each
guarded by:
  1. acquiring that media's `media_lock` (bounded by
     _CLEANUP_MEDIA_LOCK_TIMEOUT_SECONDS -- skip and retry next cycle on
     timeout instead of blocking the whole sweep indefinitely),
  2. inside the lock, checking task_status for any non-terminal row
     (queued/processing/calibrating) for that (platform, media_id) --
     this is the scenario the "recheck updated_at" trick alone cannot
     catch, because create_task()/the /api/recalibrate route INSERT a
     non-terminal task_status row *before* any file write starts, while
     the LLM write path (llm_ops._save_llm_results /
     CacheManager.save_llm_status) never refreshes
     video_cache.updated_at,
  3. inside the lock, re-reading updated_at for that exact row and
     bailing out if it is no longer older than cutoff (catches a
     concurrent save_cache() refresh that happened while cleanup was
     waiting for the lock).

Console output: English only, no emoji (project convention).
"""
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.video_transcript_api.cache.cache_manager import CacheManager
import src.video_transcript_api.cache.cache_manager as cache_manager_module
from src.video_transcript_api.utils.task_status import TaskStatus


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def cm(tmp_path):
    """CacheManager backed by a temporary directory/db."""
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _insert_expired_cache_row_with_files(cm, updated_at: str, platform: str = "youtube"):
    """Insert a video_cache row via raw SQL (so `updated_at` can be
    backdated directly, mirroring tests/cache/test_cleanup_clock_consistency.py)
    AND create a real directory with a dummy file underneath it, so
    shutil.rmtree has something concrete to remove and file_path.exists()
    checks are meaningful (unlike the pure-DB-row boundary tests in
    test_cleanup_clock_consistency.py, which never materialize files).

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


def _row_updated_at(cm, platform: str, media_id: str) -> str:
    with cm._get_cursor() as cursor:
        cursor.execute(
            "SELECT updated_at FROM video_cache WHERE platform = ? AND media_id = ?",
            (platform, media_id),
        )
        row = cursor.fetchone()
        return row["updated_at"]


def _insert_sibling_variant_row(
    cm, platform: str, media_id: str, files_loc: str,
    use_speaker_recognition: int, updated_at: str,
):
    """Insert a SECOND video_cache row for the same (platform, media_id) but
    the other use_speaker_recognition variant, pointed at the SAME files_loc
    directory -- mirrors reality: CacheManager._get_file_path() builds
    `cache_dir/platform/YYYY/YYYYMM/media_id`, which does not fold in
    use_speaker_recognition, so both variants of one media physically share
    one directory on disk (see the W3 fix's docstring inside
    cleanup_old_cache)."""
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
                use_speaker_recognition,
                files_loc,
                updated_at,
            ),
        )


def _cache_row_exists_for_variant(
    cm, platform: str, media_id: str, use_speaker_recognition: int
) -> bool:
    with cm._get_cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM video_cache WHERE platform = ? AND media_id = ? "
            "AND use_speaker_recognition = ?",
            (platform, media_id, use_speaker_recognition),
        )
        return cursor.fetchone() is not None


# ---------------------------------------------------------------------------
# (a) Writer holds media_lock concurrently with cleanup -> cleanup skips,
#     directory/DB row survive.
# ---------------------------------------------------------------------------

class TestCleanupSkipsWhileWriterHoldsMediaLock:
    def test_directory_and_row_survive_when_writer_holds_the_lock(self, cm, monkeypatch):
        # Small timeout so the test does not need to wait out the real
        # (5s) default before observing the skip-on-timeout behavior.
        monkeypatch.setattr(cache_manager_module, "_CLEANUP_MEDIA_LOCK_TIMEOUT_SECONDS", 0.2)

        retention_days = 30
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        expired = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

        platform, media_id, _, file_path = _insert_expired_cache_row_with_files(cm, expired)

        writer_holds = threading.Event()
        writer_may_release = threading.Event()

        def writer():
            with cm.media_lock(platform, media_id):
                writer_holds.set()
                assert writer_may_release.wait(timeout=5), "test setup timed out"

        t = threading.Thread(target=writer)
        t.start()
        assert writer_holds.wait(timeout=2), "writer never acquired the media lock"

        try:
            deleted = cm.cleanup_old_cache(days=retention_days, now=now)
        finally:
            writer_may_release.set()
            t.join(timeout=2)

        assert deleted == 0, "cleanup must not count a lock-timeout skip as deleted"
        assert file_path.exists(), "directory was removed while the writer still held the media lock"
        assert _cache_row_exists(cm, platform, media_id), (
            "video_cache row was deleted while the writer still held the media lock"
        )

    def test_media_deleted_on_the_next_cycle_once_the_writer_is_done(self, cm, monkeypatch):
        """Sanity companion: once the writer (a *different* thread -- RLock
        is reentrant per-thread, so holding it on the same thread that
        calls cleanup_old_cache would not exercise contention at all)
        releases, a later cleanup call (simulating "next cycle") reclaims
        the still-expired record -- the skip is a deferral, not a
        permanent exemption."""
        monkeypatch.setattr(cache_manager_module, "_CLEANUP_MEDIA_LOCK_TIMEOUT_SECONDS", 0.2)

        retention_days = 30
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        expired = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

        platform, media_id, _, file_path = _insert_expired_cache_row_with_files(cm, expired)

        writer_holds = threading.Event()
        writer_may_release = threading.Event()

        def writer():
            with cm.media_lock(platform, media_id):
                writer_holds.set()
                assert writer_may_release.wait(timeout=5), "test setup timed out"

        t = threading.Thread(target=writer)
        t.start()
        assert writer_holds.wait(timeout=2), "writer never acquired the media lock"
        try:
            deleted_during_hold = cm.cleanup_old_cache(days=retention_days, now=now)
        finally:
            writer_may_release.set()
            t.join(timeout=2)
        assert deleted_during_hold == 0
        assert file_path.exists()

        deleted_after_release = cm.cleanup_old_cache(days=retention_days, now=now)
        assert deleted_after_release == 1
        assert not file_path.exists()
        assert not _cache_row_exists(cm, platform, media_id)


# ---------------------------------------------------------------------------
# (b) In-lock recheck: updated_at gets refreshed while cleanup waits for
#     the lock -> record must be kept.
# ---------------------------------------------------------------------------

class TestCleanupRechecksUpdatedAtInsideTheLock:
    def test_row_refreshed_while_cleanup_waits_for_the_lock_is_kept(self, cm):
        retention_days = 30
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        expired = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = now.strftime("%Y-%m-%d %H:%M:%S")

        platform, media_id, _, file_path = _insert_expired_cache_row_with_files(cm, expired)

        blocker_holds = threading.Event()
        blocker_may_refresh_and_release = threading.Event()
        cleanup_result = {}

        def blocker():
            with cm.media_lock(platform, media_id):
                blocker_holds.set()
                assert blocker_may_refresh_and_release.wait(timeout=5), "test setup timed out"
                # Simulate a concurrent writer bumping updated_at (what
                # save_cache()'s INSERT OR REPLACE ... CURRENT_TIMESTAMP
                # does) right before it releases the lock cleanup is
                # waiting on.
                with cm._get_cursor() as cursor:
                    cursor.execute(
                        "UPDATE video_cache SET updated_at = ? WHERE platform = ? AND media_id = ?",
                        (fresh, platform, media_id),
                    )

        def run_cleanup():
            cleanup_result["deleted"] = cm.cleanup_old_cache(days=retention_days, now=now)

        t_blocker = threading.Thread(target=blocker)
        t_blocker.start()
        assert blocker_holds.wait(timeout=2), "blocker never acquired the media lock"

        t_cleanup = threading.Thread(target=run_cleanup)
        t_cleanup.start()
        # Give the cleanup thread a generous margin to reach the blocking
        # media_lock() acquire call inside cleanup_old_cache (mirrors the
        # scheduling margin used in test_media_lock_pool.py).
        time.sleep(0.2)

        blocker_may_refresh_and_release.set()
        t_blocker.join(timeout=2)
        t_cleanup.join(timeout=2)

        assert cleanup_result.get("deleted") == 0, "refreshed row must not be counted as deleted"
        assert file_path.exists(), "directory was removed even though updated_at was refreshed under the lock"
        assert _cache_row_exists(cm, platform, media_id), (
            "video_cache row was deleted even though updated_at was refreshed under the lock"
        )
        assert _row_updated_at(cm, platform, media_id) == fresh


# ---------------------------------------------------------------------------
# (c) No concurrency, genuinely expired -> deleted normally (no regression).
# ---------------------------------------------------------------------------

class TestCleanupStillDeletesUncontendedExpiredMedia:
    def test_expired_media_with_no_inflight_task_and_no_lock_contention_is_deleted(self, cm):
        retention_days = 30
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        expired = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

        platform, media_id, _, file_path = _insert_expired_cache_row_with_files(cm, expired)

        deleted = cm.cleanup_old_cache(days=retention_days, now=now)

        assert deleted == 1
        assert not file_path.exists()
        assert not _cache_row_exists(cm, platform, media_id)

    def test_media_with_inflight_task_status_row_is_skipped_even_without_lock_contention(self, cm):
        """The scenario the "recheck updated_at" trick alone cannot catch:
        create_task()/the /api/recalibrate route INSERT a non-terminal
        task_status row *before* any file write begins and *before*
        media_lock is ever taken, and the LLM write path never refreshes
        video_cache.updated_at. Cleanup must still skip this media purely
        because task_status shows it as queued/processing/calibrating --
        no writer is holding media_lock at all here."""
        retention_days = 30
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        expired = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

        platform, media_id, _, file_path = _insert_expired_cache_row_with_files(cm, expired)

        # Mirrors create_task()'s real INSERT: a fresh task row for this
        # exact (platform, media_id), left in its default 'queued' status,
        # with a *current* created_at -- video_cache.updated_at is the only
        # thing that is stale here, exactly like a real recalibrate request
        # racing a periodic cleanup cycle.
        cm.create_task(
            url=f"https://example.com/{media_id}-recal",
            platform=platform,
            media_id=media_id,
        )

        deleted = cm.cleanup_old_cache(days=retention_days, now=now)

        assert deleted == 0, "media with a queued task_status row must not be reclaimed"
        assert file_path.exists()
        assert _cache_row_exists(cm, platform, media_id)

    def test_media_returns_to_being_reclaimable_once_its_task_reaches_a_terminal_state(self, cm):
        retention_days = 30
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        expired = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

        platform, media_id, _, file_path = _insert_expired_cache_row_with_files(cm, expired)
        task_info = cm.create_task(
            url=f"https://example.com/{media_id}-recal",
            platform=platform,
            media_id=media_id,
        )

        assert cm.cleanup_old_cache(days=retention_days, now=now) == 0
        assert file_path.exists()

        with cm._get_cursor() as cursor:
            cursor.execute(
                "UPDATE task_status SET status = ? WHERE task_id = ?",
                (TaskStatus.SUCCESS, task_info["task_id"]),
            )

        deleted = cm.cleanup_old_cache(days=retention_days, now=now)
        assert deleted == 1
        assert not file_path.exists()
        assert not _cache_row_exists(cm, platform, media_id)


# ---------------------------------------------------------------------------
# (d) Two variant rows (speaker-recognition on/off) for the same media share
#     one files_loc directory -- reclaiming one expired variant must not
#     collaterally delete a fresh sibling variant's files.
# ---------------------------------------------------------------------------

class TestCleanupPreservesSharedFilesLocForFreshSiblingVariant:
    """W3 (PR3 review hardening 二轮): CacheManager._get_file_path() does not
    fold use_speaker_recognition into the directory path, so the two variant
    rows of one (platform, media_id) -- speaker recognition on vs. off --
    physically share the SAME files_loc directory. cleanup_old_cache() used
    to shutil.rmtree that whole directory as soon as ONE variant's row aged
    past cutoff, even when the other variant was still fresh: the fresh
    variant's files vanished from disk while its video_cache row stayed
    behind, pointing at nothing (data loss + a dangling row). The fix is a
    reference-counting-style subtraction: before rmtree, check whether any
    OTHER video_cache row still references the same files_loc; if so, only
    delete this row's DB record and keep the directory."""

    def test_directory_and_fresh_sibling_survive_when_only_one_variant_expires(self, cm):
        retention_days = 30
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        expired = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = now.strftime("%Y-%m-%d %H:%M:%S")

        # use_speaker_recognition=0 variant: expired, the only row cleanup's
        # initial candidate query will pick up.
        platform, media_id, files_loc, file_path = _insert_expired_cache_row_with_files(cm, expired)
        # A second file in the SAME shared directory standing in for the
        # fresh sibling (use_speaker_recognition=1) variant's own artifact.
        (file_path / "transcript_speaker.json").write_text('{"fresh": true}', encoding="utf-8")
        _insert_sibling_variant_row(
            cm, platform, media_id, files_loc,
            use_speaker_recognition=1, updated_at=fresh,
        )

        deleted = cm.cleanup_old_cache(days=retention_days, now=now)

        assert deleted == 1, "the expired variant's DB row must still be reclaimed"
        assert file_path.exists(), (
            "the shared directory must survive -- a fresh sibling variant's "
            "files still live in it (red on the pre-W3 code: rmtree fires "
            "unconditionally and removes it)"
        )
        assert (file_path / "transcript_speaker.json").exists(), (
            "the fresh sibling variant's own file must not be collaterally deleted"
        )
        assert not _cache_row_exists_for_variant(cm, platform, media_id, 0), (
            "the expired variant's own DB row must still be deleted"
        )
        assert _cache_row_exists_for_variant(cm, platform, media_id, 1), (
            "the fresh sibling variant's DB row must be untouched"
        )

    def test_directory_is_removed_once_every_variant_sharing_it_has_expired(self, cm):
        """Non-regression: with no surviving sibling, the existing behavior
        (rmtree the now fully-orphaned shared directory) is unchanged."""
        retention_days = 30
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        expired = (cutoff - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        also_expired = (cutoff - timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S")

        platform, media_id, files_loc, file_path = _insert_expired_cache_row_with_files(cm, expired)
        _insert_sibling_variant_row(
            cm, platform, media_id, files_loc,
            use_speaker_recognition=1, updated_at=also_expired,
        )

        deleted = cm.cleanup_old_cache(days=retention_days, now=now)

        assert deleted == 2, "both expired sibling variants must be reclaimed"
        assert not file_path.exists(), (
            "once no variant referencing files_loc survives, the directory "
            "must still be removed (existing behavior, not weakened by W3)"
        )
        assert not _cache_row_exists_for_variant(cm, platform, media_id, 0)
        assert not _cache_row_exists_for_variant(cm, platform, media_id, 1)
