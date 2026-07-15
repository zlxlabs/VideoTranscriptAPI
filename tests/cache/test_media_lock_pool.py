"""Unit tests for CacheManager's per-(platform, media_id) lock pool lifecycle.

Covers codex-review R3 finding #2: the pool's "pop on idle" logic used a
`not lock.locked()` check performed *after* release, which has a lifecycle
race. A waiting thread may have already fetched the lock object from the
pool dict (during the holder's critical section) but not yet completed its
blocking `acquire()` call when the holder releases and pops the entry. A
third thread then creates a brand-new lock object for the same key, so the
still-waiting thread (holding a reference to the OLD object) and the third
thread (holding the NEW object) can both enter their critical sections for
the same (platform, media_id) concurrently -- mutual exclusion is broken.

All console output must be in English only (no emoji, no Chinese).
"""
import threading
import time

import pytest

from src.video_transcript_api.cache.cache_manager import CacheManager


@pytest.fixture
def cm(tmp_path):
    """Create a CacheManager backed by a temporary directory/db."""
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


class TestMediaLockPoolLifecycle:
    """Regression test for the pool-eviction race in the media lock pool."""

    def test_no_overlapping_critical_sections_across_pool_eviction_race(self, cm):
        """Three-thread orchestration (thread A holds, thread B is parked
        waiting to acquire, thread C arrives right after A releases) must
        never let B and C hold the "same" (platform, media_id) lock at the
        same time, regardless of whether the pool evicted the entry between
        A's release and B's completed acquire.
        """
        key_platform, key_media_id = "youtube", "vidRace"

        log_guard = threading.Lock()
        event_log = []

        def log(event):
            with log_guard:
                event_log.append((event, time.monotonic()))

        a_holds = threading.Event()
        a_may_release = threading.Event()
        a_released = threading.Event()
        b_ready = threading.Event()
        c_may_start = threading.Event()
        b_may_exit = threading.Event()
        c_may_exit = threading.Event()

        def thread_a():
            with cm.media_lock(key_platform, key_media_id):
                a_holds.set()
                assert a_may_release.wait(timeout=2), "test setup timed out waiting to release A"
            a_released.set()

        def thread_b():
            # Signal readiness right before entering the context manager so
            # the main thread knows B is about to hit the (blocking, since A
            # still holds the lock) acquire() call inside media_lock().
            b_ready.set()
            with cm.media_lock(key_platform, key_media_id):
                log("B_enter")
                assert b_may_exit.wait(timeout=2), "test setup timed out waiting to release B"
                log("B_exit")

        def thread_c():
            assert c_may_start.wait(timeout=2), "C never got the start signal"
            with cm.media_lock(key_platform, key_media_id):
                log("C_enter")
                assert c_may_exit.wait(timeout=2), "test setup timed out waiting to release C"
                log("C_exit")

        t_a = threading.Thread(target=thread_a)
        t_b = threading.Thread(target=thread_b)
        t_c = threading.Thread(target=thread_c)

        t_a.start()
        assert a_holds.wait(timeout=2), "A never acquired the lock"

        t_b.start()
        assert b_ready.wait(timeout=2), "B never started"
        # Generous margin for B to reach the blocking acquire() call inside
        # media_lock() while A still holds the lock (A has not released yet).
        time.sleep(0.05)

        # Let A release and run its pop-check. This is the exact window the
        # pool-eviction race lives in: on the buggy implementation, A will
        # very likely finish "lock.release() -> not lock.locked() -> pop"
        # before B's separately-scheduled OS thread manages to complete its
        # pending acquire() call and return into Python bytecode.
        a_may_release.set()
        assert a_released.wait(timeout=2), "A never released"

        # C arrives right after A's release + pop-check decision.
        c_may_start.set()
        t_c.start()

        # Give both B (finishing its possibly-still-pending acquire) and C
        # (a fresh attempt) time to reach their "entered, now waiting to
        # exit" state before we let either one leave its critical section.
        time.sleep(0.3)
        b_may_exit.set()
        time.sleep(0.1)
        c_may_exit.set()

        t_a.join(timeout=2)
        t_b.join(timeout=2)
        t_c.join(timeout=2)
        assert not t_a.is_alive(), "thread A did not finish"
        assert not t_b.is_alive(), "thread B did not finish"
        assert not t_c.is_alive(), "thread C did not finish"

        events = dict(event_log)
        assert {"B_enter", "B_exit", "C_enter", "C_exit"} <= events.keys(), (
            f"missing expected lifecycle events, got: {sorted(events.keys())}"
        )

        b_enter, b_exit = events["B_enter"], events["B_exit"]
        c_enter, c_exit = events["C_enter"], events["C_exit"]

        overlap = b_enter < c_exit and c_enter < b_exit
        assert not overlap, (
            f"B and C both held the '{key_platform}:{key_media_id}' media lock "
            f"concurrently (B: {b_enter}-{b_exit}, C: {c_enter}-{c_exit}) -- the "
            f"lock pool handed out two distinct lock objects for the same key"
        )

    def test_lock_pool_entry_fully_evicted_once_all_users_are_done(self, cm):
        """Sanity check for the other half of the contract: once every
        acquirer of a given key has released, the pool entry must actually
        be removed (no unbounded growth as media keys accumulate)."""
        key_platform, key_media_id = "youtube", "vidCleanup"

        with cm.media_lock(key_platform, key_media_id):
            pass

        key = f"{key_platform}:{key_media_id}"
        assert key not in cm._media_locks, "lock pool entry was not evicted after use"
