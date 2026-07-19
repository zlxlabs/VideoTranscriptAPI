"""Regression test: save_cache() must participate in the same per-media
RLock (CacheManager.media_lock) that every other write path already uses
(_save_llm_results, save_llm_status, save_speaker_mapping, ...) and that
cleanup_old_cache() acquires for its whole per-record delete critical
section.

Background (Y6, PR3 review hardening 加固轮): a review pass flagged that
cleanup_old_cache()'s in-lock recheck ("does this media have any
non-terminal task_status row?") assumes save_cache() only ever runs while
the task's own row is non-terminal -- true for save_cache's sole call
chain (transcription.py::process_transcription, always invoked after
process_task_queue has already written that task's row to PROCESSING).
That recheck therefore does correctly cover "the current task's own
save_cache write".

Verifying it surfaced an adjacent, real gap: cleanup_old_cache's per-record
decision to delete is made once, at lock-acquisition time. If that
decision was made while genuinely no task existed for the media yet, and
cleanup then spends non-trivial time still holding the lock while it
finishes deleting (the row first, then rmtree -- see the Y5 fix), a brand
new request for the very same media landing in that window would reach
save_cache with no media_lock protection at all before this fix -- letting
its mkdir/file-write race the in-flight rmtree of the very directory it is
about to recreate.

The fix wraps save_cache's whole write critical section (mkdir + transcript
file writes + the video_cache INSERT OR REPLACE) in
`with self.media_lock(platform, media_id):`, the same lock object cleanup_
old_cache already serializes against -- no new mechanism, reusing what U1
already built. This test proves the serialization directly: a concurrent
holder of the media lock must block save_cache from completing until it
releases.

Console output: English only, no emoji (project convention).
"""
import threading

import pytest

from src.video_transcript_api.cache.cache_manager import CacheManager


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


class TestSaveCacheRespectsMediaLock:
    def test_save_cache_blocks_until_a_concurrent_lock_holder_releases(self, cm):
        platform, media_id = "youtube", "media-lock-test"
        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold_lock():
            with cm.media_lock(platform, media_id):
                lock_held.set()
                assert release_lock.wait(timeout=5), "test setup timed out"

        holder = threading.Thread(target=hold_lock)
        holder.start()
        assert lock_held.wait(timeout=2), "background thread never acquired the media lock"

        save_done = threading.Event()
        save_result = {}

        def do_save():
            save_result["value"] = cm.save_cache(
                platform=platform,
                url="https://example.com/lock-test",
                media_id=media_id,
                use_speaker_recognition=False,
                transcript_data="hello world",
                transcript_type="capswriter",
            )
            save_done.set()

        saver = threading.Thread(target=do_save)
        try:
            saver.start()

            # The crux of the fix: save_cache must NOT be able to complete
            # its write while a concurrent holder still has the media lock.
            # Red on the pre-fix code (save_cache took no lock at all): this
            # wait would return True almost immediately.
            assert not save_done.wait(timeout=0.3), (
                "save_cache completed while a concurrent holder still had the media lock -- "
                "it is not participating in media_lock"
            )

            release_lock.set()
            holder.join(timeout=2)

            assert save_done.wait(timeout=2), (
                "save_cache never completed after the concurrent lock holder released"
            )
        finally:
            release_lock.set()
            holder.join(timeout=2)
            saver.join(timeout=2)

        assert save_result["value"] is not None, "save_cache must still succeed once unblocked"
        with cm._get_cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM video_cache WHERE platform = ? AND media_id = ?",
                (platform, media_id),
            )
            assert cursor.fetchone() is not None, "the row must be persisted after the lock is released"

    def test_save_cache_is_reentrant_on_the_same_thread(self, cm):
        """media_lock is an RLock specifically so a single thread that
        already holds it (e.g. a future caller composing save_cache with
        another locked operation) does not self-deadlock. Calling
        save_cache from inside a `with cm.media_lock(...)` block on the
        same thread must succeed immediately, not hang."""
        platform, media_id = "youtube", "reentrant-test"

        with cm.media_lock(platform, media_id):
            result = cm.save_cache(
                platform=platform,
                url="https://example.com/reentrant",
                media_id=media_id,
                use_speaker_recognition=False,
                transcript_data="hello",
                transcript_type="capswriter",
            )

        assert result is not None
