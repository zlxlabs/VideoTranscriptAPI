"""Unit tests for CacheManager task-status guards and crash recovery.

Covers:
- update_task_status terminal-state stickiness (success/failed not clobbered)
- force=True explicit reset (recalibrate path)
- recover_orphaned_tasks() sweep on startup
- calibrating status round-trips

All console output must be in English only (no emoji, no Chinese).
"""

import datetime
import time

import pytest

from src.video_transcript_api.cache.cache_manager import (
    CacheManager,
    RUNTIME_RECONCILE_GRACE_SECONDS,
)
from src.video_transcript_api.utils.task_status import TaskStatus


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _new_task(cm, url="https://example.com/v1"):
    return cm.create_task(url=url)["task_id"]


class TestCalibratingStatus:
    def test_calibrating_round_trips(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)
        assert cm.get_task_by_id(task_id)["status"] == "calibrating"


class TestErrorMessage:
    def test_error_message_persists_on_failed(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.FAILED, error_message="ASR timeout")
        assert cm.get_task_by_id(task_id)["error_message"] == "ASR timeout"


class TestTerminalStickiness:
    """success / failed are terminal and must not be overwritten by late writes."""

    def test_success_not_overwritten_by_processing(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.SUCCESS)
        # A slow/stale worker tries to regress the state.
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        assert cm.get_task_by_id(task_id)["status"] == "success"

    def test_success_not_overwritten_by_failed(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.SUCCESS)
        cm.update_task_status(task_id, TaskStatus.FAILED)
        assert cm.get_task_by_id(task_id)["status"] == "success"

    def test_failed_not_overwritten_by_success(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.FAILED)
        cm.update_task_status(task_id, TaskStatus.SUCCESS)
        assert cm.get_task_by_id(task_id)["status"] == "failed"

    def test_non_terminal_transitions_allowed(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)
        cm.update_task_status(task_id, TaskStatus.SUCCESS)
        assert cm.get_task_by_id(task_id)["status"] == "success"

    def test_force_cannot_overwrite_terminal(self, cm):
        """force is only legal for non-terminal recovery transitions."""
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.SUCCESS)
        cm.update_task_status(task_id, TaskStatus.PROCESSING, force=True)
        assert cm.get_task_by_id(task_id)["status"] == "success"


class TestRecoverOrphanedTasks:
    """On boot, in-flight tasks (lost with the in-memory queues) are failed."""

    def test_sweeps_non_terminal_to_failed(self, cm):
        queued = _new_task(cm, "https://example.com/q")
        processing = _new_task(cm, "https://example.com/p")
        calibrating = _new_task(cm, "https://example.com/c")
        cm.update_task_status(processing, TaskStatus.PROCESSING)
        cm.update_task_status(calibrating, TaskStatus.CALIBRATING)

        recovered = cm.recover_orphaned_tasks()

        assert recovered == 3
        assert cm.get_task_by_id(queued)["status"] == "failed"
        assert cm.get_task_by_id(processing)["status"] == "failed"
        assert cm.get_task_by_id(calibrating)["status"] == "failed"

    def test_terminal_tasks_untouched(self, cm):
        done = _new_task(cm, "https://example.com/done")
        failed = _new_task(cm, "https://example.com/failed")
        cm.update_task_status(done, TaskStatus.SUCCESS)
        cm.update_task_status(failed, TaskStatus.FAILED)

        recovered = cm.recover_orphaned_tasks()

        assert recovered == 0
        assert cm.get_task_by_id(done)["status"] == "success"
        assert cm.get_task_by_id(failed)["status"] == "failed"

    def test_sets_completed_at_on_recovered(self, cm):
        processing = _new_task(cm, "https://example.com/p2")
        cm.update_task_status(processing, TaskStatus.PROCESSING)
        cm.recover_orphaned_tasks()
        assert cm.get_task_by_id(processing)["completed_at"] is not None

    def test_recovered_tasks_get_terminal_snapshot(self, cm):
        """A recovered task is terminal (status=failed) and must therefore
        carry an immutable terminal_snapshot like every other path that
        writes a terminal status (update_task_status's own snapshot
        assembly) -- otherwise it violates this PR's "terminal state always
        has a snapshot" invariant (a terminal row with a NULL snapshot)."""
        queued = _new_task(cm, "https://example.com/q2")
        processing = _new_task(cm, "https://example.com/p3")
        calibrating = _new_task(cm, "https://example.com/c2")
        cm.update_task_status(processing, TaskStatus.PROCESSING)
        cm.update_task_status(calibrating, TaskStatus.CALIBRATING)

        cm.recover_orphaned_tasks()

        for task_id in (queued, processing, calibrating):
            task = cm.get_task_by_id(task_id)
            assert task["status"] == "failed"
            assert task["completed_at"] is not None
            snapshot = task["terminal_snapshot"]
            assert snapshot is not None, f"{task_id} recovered with no terminal_snapshot"
            assert snapshot["status"] == "failed"
            assert snapshot.get("recovered") is True
            assert snapshot.get("reason") == "orphaned_on_startup"
            assert snapshot.get("recovered_at")

    def test_terminal_tasks_snapshot_untouched(self, cm):
        """CAS semantics extend to the snapshot too: a task already in a
        terminal state keeps its existing terminal_snapshot untouched by the
        recovery sweep, not overwritten with a "recovered" one."""
        done = _new_task(cm, "https://example.com/done2")
        cm.update_task_status(done, TaskStatus.SUCCESS, platform="youtube", title="Original")
        original_snapshot = cm.get_task_by_id(done)["terminal_snapshot"]

        cm.recover_orphaned_tasks()

        task = cm.get_task_by_id(done)
        assert task["status"] == "success"
        assert task["terminal_snapshot"] == original_snapshot

    def test_restrict_to_task_ids_recovers_pre_boot_task_and_spares_post_boot_task(self, cm):
        """L2 (CI review round 5, P1): recover_orphaned_tasks' scoping
        parameter used to be a rowid watermark (H4, local codex review round
        7) -- see test_task_id_snapshot_survives_rowid_reuse_after_highest_
        row_deleted below for why that was unsound on a TEXT-primary-key
        table. Replaced with an explicit task_id snapshot captured before
        the post-boot task exists. This test covers the ordinary case with
        no rowid reuse involved: the snapshot only contains the pre-boot
        task, so the retry must recover exactly that one and leave the
        post-boot task (created after the snapshot, unrelated to it) alone,
        regardless of insertion order."""
        pre_boot = _new_task(cm, "https://example.com/pre-boot")
        cm.update_task_status(pre_boot, TaskStatus.PROCESSING)

        snapshot = cm.get_non_terminal_task_ids()
        assert snapshot == {pre_boot}

        post_boot = _new_task(cm, "https://example.com/post-boot")
        cm.update_task_status(post_boot, TaskStatus.PROCESSING)

        recovered = cm.recover_orphaned_tasks(restrict_to_task_ids=snapshot)

        assert recovered == 1
        assert cm.get_task_by_id(pre_boot)["status"] == "failed"
        assert cm.get_task_by_id(post_boot)["status"] == "processing"

    def test_task_id_snapshot_survives_rowid_reuse_after_highest_row_deleted(self, cm):
        """L2 (CI review round 5, P1): the rowid-watermark scheme this
        replaces assumed SQLite's implicit rowid is strictly monotonic
        forever -- true only for INTEGER PRIMARY KEY AUTOINCREMENT tables.
        task_status uses a TEXT primary key (task_id), so rowid is plain
        SQLite housekeeping: deleting the row that currently holds the
        table's highest rowid, then inserting a new row, reuses that same
        rowid value (SQLite's own allocation rule is "one greater than the
        largest ROWID currently in use" -- once the row holding that largest
        value is gone, the next insert lands right back on it).

        Reproduces exactly that: a pre-boot zombie task is snapshotted by
        task_id, the table's current highest-rowid row (an unrelated,
        already-terminal task) is then deleted and immediately replaced by a
        brand new post-boot task, which lands on the freed rowid. Under the
        old rowid-watermark scheme this reused rowid could fall at or below
        whatever watermark had been captured earlier, misclassifying a live
        post-boot task as a pre-boot zombie and CAS'ing it to failed. The
        task_id-snapshot scheme is immune by construction: the post-boot
        task's task_id was simply never in the snapshot, independent of
        which rowid it ends up reusing."""
        pre_boot = _new_task(cm, "https://example.com/pre-boot-reuse")
        cm.update_task_status(pre_boot, TaskStatus.PROCESSING)

        # Startup snapshot: the fixed set of task_ids the recovery retry is
        # ever allowed to touch.
        snapshot = cm.get_non_terminal_task_ids()
        assert snapshot == {pre_boot}

        # An already-terminal row currently holds the table's highest
        # rowid -- ordinary traffic between boot and the eventual retry:
        # tasks arrive, finish, and (in production) later get swept by
        # retention cleanup.
        terminal_high = _new_task(cm, "https://example.com/terminal-high")
        cm.update_task_status(
            terminal_high, TaskStatus.SUCCESS, platform="youtube", title="t",
        )
        with cm._get_cursor() as cursor:
            max_rowid_before = cursor.execute(
                "SELECT MAX(rowid) FROM task_status"
            ).fetchone()[0]
            cursor.execute(
                "SELECT rowid FROM task_status WHERE task_id = ?", (terminal_high,)
            )
            assert cursor.fetchone()[0] == max_rowid_before, (
                "test setup: terminal_high must hold the table's current max rowid"
            )
            cursor.execute("DELETE FROM task_status WHERE task_id = ?", (terminal_high,))

        # A brand new post-boot task, created well after the snapshot above
        # and actively being processed by this same running process.
        post_boot = _new_task(cm, "https://example.com/post-boot-reuse")
        cm.update_task_status(post_boot, TaskStatus.PROCESSING)
        with cm._get_cursor() as cursor:
            cursor.execute(
                "SELECT rowid FROM task_status WHERE task_id = ?", (post_boot,)
            )
            reused_rowid = cursor.fetchone()[0]
        assert reused_rowid == max_rowid_before, (
            "test setup: SQLite must have reused the freed rowid for the "
            "new row -- otherwise this test isn't exercising rowid reuse"
        )

        recovered = cm.recover_orphaned_tasks(restrict_to_task_ids=snapshot)

        assert recovered == 1
        assert cm.get_task_by_id(pre_boot)["status"] == "failed"
        assert cm.get_task_by_id(post_boot)["status"] == "processing", (
            "the post-boot task must never be touched by the recovery "
            "retry, even though it reused a rowid that a watermark-based "
            "comparison would have misclassified as pre-boot"
        )

    def test_restrict_to_task_ids_none_behaves_like_before(self, cm):
        """No restriction (the startup call site's usage) must keep sweeping
        every non-terminal task regardless of insertion order -- the
        parameter must not change the existing default behavior."""
        queued = _new_task(cm, "https://example.com/no-watermark")

        recovered = cm.recover_orphaned_tasks(restrict_to_task_ids=None)

        assert recovered == 1
        assert cm.get_task_by_id(queued)["status"] == "failed"

    def test_get_non_terminal_task_ids_reflects_current_non_terminal_set(self, cm):
        """Direct unit coverage of the snapshot helper itself: an empty
        table reports an empty set, only queued/processing/calibrating
        task_ids are included, and a task_id drops out once it reaches a
        terminal status -- the property the whole L2 fix depends on."""
        assert cm.get_non_terminal_task_ids() == frozenset()

        t1 = _new_task(cm, "https://example.com/snap1")
        assert cm.get_non_terminal_task_ids() == frozenset({t1})

        t2 = _new_task(cm, "https://example.com/snap2")
        cm.update_task_status(t2, TaskStatus.CALIBRATING)
        assert cm.get_non_terminal_task_ids() == frozenset({t1, t2})

        cm.update_task_status(t1, TaskStatus.SUCCESS, platform="youtube", title="t")
        assert cm.get_non_terminal_task_ids() == frozenset({t2})


class TestDrainNonTerminalTasksOnShutdown:
    """Graceful-shutdown counterpart of TestRecoverOrphanedTasks above: same
    per-task CAS terminal-write loop (CacheManager._fail_non_terminal_tasks),
    triggered at process shutdown instead of the next startup, with
    reason="shutdown_drain" instead of "orphaned_on_startup" so the two
    trigger points remain distinguishable in a terminal_snapshot."""

    def test_sweeps_non_terminal_to_failed(self, cm):
        queued = _new_task(cm, "https://example.com/dq")
        processing = _new_task(cm, "https://example.com/dp")
        calibrating = _new_task(cm, "https://example.com/dc")
        cm.update_task_status(processing, TaskStatus.PROCESSING)
        cm.update_task_status(calibrating, TaskStatus.CALIBRATING)

        drained = cm.drain_non_terminal_tasks_on_shutdown()

        assert drained == 3
        assert cm.get_task_by_id(queued)["status"] == "failed"
        assert cm.get_task_by_id(processing)["status"] == "failed"
        assert cm.get_task_by_id(calibrating)["status"] == "failed"

    def test_terminal_tasks_untouched(self, cm):
        done = _new_task(cm, "https://example.com/ddone")
        failed = _new_task(cm, "https://example.com/dfailed")
        cm.update_task_status(done, TaskStatus.SUCCESS)
        cm.update_task_status(failed, TaskStatus.FAILED)

        drained = cm.drain_non_terminal_tasks_on_shutdown()

        assert drained == 0
        assert cm.get_task_by_id(done)["status"] == "success"
        assert cm.get_task_by_id(failed)["status"] == "failed"

    def test_drained_tasks_get_terminal_snapshot_tagged_shutdown_drain(self, cm):
        """Same "terminal state always has a snapshot" invariant as orphan
        recovery, tagged with a distinct reason so the two code paths that
        can produce a `recovered: True` snapshot stay distinguishable."""
        processing = _new_task(cm, "https://example.com/dp2")
        cm.update_task_status(processing, TaskStatus.PROCESSING)

        cm.drain_non_terminal_tasks_on_shutdown()

        task = cm.get_task_by_id(processing)
        assert task["status"] == "failed"
        assert task["completed_at"] is not None
        snapshot = task["terminal_snapshot"]
        assert snapshot is not None, f"{processing} drained with no terminal_snapshot"
        assert snapshot["status"] == "failed"
        assert snapshot.get("recovered") is True
        assert snapshot.get("reason") == "shutdown_drain"
        assert snapshot.get("recovered_at")

    def test_terminal_tasks_snapshot_untouched(self, cm):
        """CAS semantics: a task already terminal keeps its existing
        terminal_snapshot untouched by the shutdown drain."""
        done = _new_task(cm, "https://example.com/ddone2")
        cm.update_task_status(done, TaskStatus.SUCCESS, platform="youtube", title="Original")
        original_snapshot = cm.get_task_by_id(done)["terminal_snapshot"]

        cm.drain_non_terminal_tasks_on_shutdown()

        task = cm.get_task_by_id(done)
        assert task["status"] == "success"
        assert task["terminal_snapshot"] == original_snapshot

    def test_deadline_budget_stops_early_and_leaves_remainder_non_terminal(self, cm):
        """H3 (local codex review round 7): the shutdown drain previously ran
        with no time budget at all -- a large backlog of non-terminal tasks,
        or a single terminal write slowed down (e.g. disk IO jitter), could
        block aclose() indefinitely, violating the "aclose returns within a
        bound" invariant that governs the rest of the shutdown path (see
        _stop_workers' three bounded wait_for calls). deadline_seconds now
        caps the total wall-clock time the drain loop may spend; once the
        budget is exhausted it stops immediately instead of continuing to
        drain the rest of the backlog.

        Reproduced directly: 6 non-terminal tasks, update_task_status
        monkeypatched to sleep 0.05s per call (models a slow terminal
        write), deadline_seconds=0.08 -- only enough budget for ~2 of the 6
        writes. The drain must return well before 6 * 0.05s = 0.3s, and the
        tasks it didn't get to must remain non-terminal (not silently
        dropped or half-written)."""
        task_ids = [_new_task(cm, f"https://example.com/budget{i}") for i in range(6)]

        real_update_task_status = cm.update_task_status
        processed = []

        def slow_update_task_status(task_id, status, **kwargs):
            processed.append(task_id)
            time.sleep(0.05)
            return real_update_task_status(task_id, status, **kwargs)

        cm.update_task_status = slow_update_task_status

        start = time.monotonic()
        drained = cm.drain_non_terminal_tasks_on_shutdown(deadline_seconds=0.08)
        elapsed = time.monotonic() - start
        cm.update_task_status = real_update_task_status

        assert 0 < drained < len(task_ids), (
            f"expected a partial drain (budget exhausted mid-backlog), got {drained}"
        )
        assert len(processed) < len(task_ids)
        assert elapsed < len(task_ids) * 0.05, (
            "drain must stop within its budget, not serially process the entire backlog"
        )

        statuses = {tid: cm.get_task_by_id(tid)["status"] for tid in task_ids}
        remaining_non_terminal = [
            tid for tid, status in statuses.items() if status not in ("success", "failed")
        ]
        assert remaining_non_terminal, (
            "expected some tasks to remain non-terminal once the budget ran out"
        )

        # Closed-loop semantics (already existing, not new to this fix): the
        # next startup's orphan recovery sweep picks up whatever the
        # shutdown drain didn't get to.
        recovered = cm.recover_orphaned_tasks()
        assert recovered == len(remaining_non_terminal)
        for tid in task_ids:
            assert cm.get_task_by_id(tid)["status"] == "failed"


class TestShutdownDrainBusyTimeoutBudget:
    """P2 (local codex review round 12, finding e): the shutdown-drain
    SQLite operations inside _fail_non_terminal_tasks previously ignored
    deadline_seconds entirely from the database's point of view -- the
    initial SELECT ran before `deadline` was even computed, and every
    per-task UPDATE reused the connection's default busy_timeout (~5s, from
    sqlite3.connect()'s default `timeout=5.0`), regardless of how little
    budget was actually left. A single lock-contention event could
    therefore block up to ~5s even when the caller asked for a much
    smaller deadline_seconds. Fixed by tightening the connection's
    busy_timeout to the remaining budget (with a floor) for the duration
    of a bounded drain call, restored afterward."""

    def _current_busy_timeout_ms(self, cm) -> int:
        with cm._get_cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout")
            return cursor.fetchone()[0]

    def test_apply_connection_busy_timeout_ms_sets_pragma(self, cm):
        cm._apply_connection_busy_timeout_ms(1234)
        assert self._current_busy_timeout_ms(cm) == 1234

    def test_apply_connection_busy_timeout_ms_floors_negative_to_zero(self, cm):
        cm._apply_connection_busy_timeout_ms(-100)
        assert self._current_busy_timeout_ms(cm) == 0

    def test_shutdown_drain_busy_timeout_ms_floors_near_zero_remaining(self, cm):
        from src.video_transcript_api.cache.cache_manager import (
            _SHUTDOWN_DRAIN_MIN_BUSY_TIMEOUT_MS,
        )

        almost_expired_deadline = time.monotonic() + 0.0001
        time.sleep(0.001)  # guarantee it has actually passed by the time we compute
        result = cm._shutdown_drain_busy_timeout_ms(almost_expired_deadline)
        assert result == _SHUTDOWN_DRAIN_MIN_BUSY_TIMEOUT_MS

    def test_shutdown_drain_busy_timeout_ms_reflects_remaining_budget(self, cm):
        deadline = time.monotonic() + 10.0
        result = cm._shutdown_drain_busy_timeout_ms(deadline)
        # Allow generous slack for scheduling jitter between computing
        # `deadline` above and the method's own time.monotonic() call.
        assert 9000 <= result <= 10000

    def test_drain_restores_default_busy_timeout_after_completion(self, cm):
        from src.video_transcript_api.cache.cache_manager import (
            _DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
        )

        task_id = _new_task(cm, "https://example.com/busy-timeout-restore")
        cm.update_task_status(task_id, TaskStatus.PROCESSING)

        cm.drain_non_terminal_tasks_on_shutdown(deadline_seconds=5.0)

        assert self._current_busy_timeout_ms(cm) == _DEFAULT_SQLITE_BUSY_TIMEOUT_MS

    def test_drain_restores_default_busy_timeout_even_when_a_task_write_raises(self, cm):
        """The busy_timeout restoration lives in a `finally` -- it must run
        even when a per-task update_task_status() call raises mid-loop, not
        only on the clean-completion path."""
        from src.video_transcript_api.cache.cache_manager import (
            _DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
        )

        task_id = _new_task(cm, "https://example.com/busy-timeout-restore-on-raise")
        cm.update_task_status(task_id, TaskStatus.PROCESSING)

        def boom(*args, **kwargs):
            raise RuntimeError("simulated write failure")

        cm.update_task_status = boom
        try:
            with pytest.raises(RuntimeError):
                cm.drain_non_terminal_tasks_on_shutdown(deadline_seconds=5.0)
        finally:
            del cm.update_task_status

        assert self._current_busy_timeout_ms(cm) == _DEFAULT_SQLITE_BUSY_TIMEOUT_MS

    def test_recover_orphaned_tasks_does_not_touch_busy_timeout(self, cm):
        """recover_orphaned_tasks() never passes deadline_seconds -- its
        existing unbounded usage must not be affected by the P2 busy_timeout
        tightening, which is scoped to callers that actually supply a
        deadline (currently only drain_non_terminal_tasks_on_shutdown)."""
        from src.video_transcript_api.cache.cache_manager import (
            _DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
        )

        task_id = _new_task(cm, "https://example.com/recover-busy-timeout-untouched")
        cm.update_task_status(task_id, TaskStatus.PROCESSING)

        before = self._current_busy_timeout_ms(cm)
        assert before == _DEFAULT_SQLITE_BUSY_TIMEOUT_MS

        cm.recover_orphaned_tasks()

        assert self._current_busy_timeout_ms(cm) == _DEFAULT_SQLITE_BUSY_TIMEOUT_MS

    def test_initial_select_skipped_when_deadline_already_expired(self, cm):
        """H3/P2: an already-expired budget must not even issue the
        initial SELECT (本地 codex review 第 12 轮 P2 发现 e) -- this is
        stricter than the pre-existing per-task loop check, which only
        guards the UPDATEs, not the SELECT before them."""
        task_id = _new_task(cm, "https://example.com/expired-before-select")
        cm.update_task_status(task_id, TaskStatus.PROCESSING)

        def boom_get_cursor(*args, **kwargs):
            raise AssertionError("SELECT must not be issued when the budget is already gone")

        real_get_cursor = cm._get_cursor
        cm._get_cursor = boom_get_cursor
        try:
            count = cm.drain_non_terminal_tasks_on_shutdown(deadline_seconds=-1.0)
        finally:
            cm._get_cursor = real_get_cursor

        assert count == 0
        assert cm.get_task_by_id(task_id)["status"] == "processing"

    def test_drain_bounded_despite_held_write_lock(self, cm):
        """End-to-end reproduction of finding e: a second raw connection
        holds a real SQLite write lock (BEGIN IMMEDIATE, never committed)
        on the exact row the drain needs to update. Before the fix, the
        drain's UPDATE would retry against this lock for up to the
        connection's default ~5s busy_timeout. After the fix, busy_timeout
        is capped to (roughly) deadline_seconds, so the call returns much
        sooner -- regardless of whether it ultimately raises (lock never
        released within budget) or returns a partial count."""
        import sqlite3

        task_id = _new_task(cm, "https://example.com/lock-contended")
        cm.update_task_status(task_id, TaskStatus.PROCESSING)

        blocker = sqlite3.connect(str(cm.db_path))
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "UPDATE task_status SET title = 'lock-holder' WHERE task_id = ?",
            (task_id,),
        )
        try:
            start = time.monotonic()
            try:
                cm.drain_non_terminal_tasks_on_shutdown(deadline_seconds=0.3)
            except sqlite3.OperationalError:
                pass  # acceptable outcome: SQLITE_BUSY once busy_timeout is exhausted
            elapsed = time.monotonic() - start
        finally:
            blocker.rollback()
            blocker.close()

        assert elapsed < 2.0, (
            f"elapsed={elapsed:.2f}s, expected well under the ~5s default "
            f"busy_timeout, close to the 0.3s deadline_seconds budget instead"
        )


class TestReconcileRuntimeOrphanedTasks:
    """P1 (local codex review round 12, finding c): runtime.py's in-flight
    registry is the primary guard against orphaned non-terminal tasks
    (admission is gated, release fires when a worker's future completes),
    but the queue-reject cleanup path (api/routes/tasks.py's two 503
    branches) tries to CAS an already-created/inserted task row to failed
    as a best-effort step -- and that cleanup write can itself fail (e.g. a
    transient DB lock), leaving the row stuck non-terminal with no other
    trigger point during normal operation (aclose()'s shutdown drain and
    startup's orphan recovery only fire once each, at process boundaries,
    not while the service keeps running). reconcile_runtime_orphaned_tasks
    closes this gap; app.py's _periodic_maintenance wires it up with the
    in-flight registry's current snapshot as the exclusion set (covered
    separately in tests/cache/test_periodic_maintenance.py)."""

    def _backdate(self, cm, task_id, created_at):
        with cm._get_cursor() as cursor:
            cursor.execute(
                "UPDATE task_status SET created_at = ? WHERE task_id = ?",
                (created_at, task_id),
            )

    def _old_created_at(self, now, *, extra_seconds=60):
        return (
            now - datetime.timedelta(seconds=RUNTIME_RECONCILE_GRACE_SECONDS + extra_seconds)
        ).strftime("%Y-%m-%d %H:%M:%S")

    def test_old_non_terminal_task_is_reconciled_to_failed(self, cm):
        task_id = _new_task(cm, "https://example.com/reconcile-old")
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        now = datetime.datetime.now(datetime.timezone.utc)
        self._backdate(cm, task_id, self._old_created_at(now))

        count = cm.reconcile_runtime_orphaned_tasks(exclude_task_ids=set(), now=now)

        assert count == 1
        task = cm.get_task_by_id(task_id)
        assert task["status"] == "failed"
        assert task["completed_at"] is not None
        snapshot = task["terminal_snapshot"]
        assert snapshot is not None, "reconciled task has no terminal_snapshot"
        assert snapshot["status"] == "failed"
        assert snapshot.get("recovered") is True
        assert snapshot.get("reason") == "runtime_reconcile"
        assert snapshot.get("recovered_at")

    def test_recent_non_terminal_task_is_not_reconciled(self, cm):
        """created_at defaults to "now" (CURRENT_TIMESTAMP) -- well within
        the grace period, so it must survive."""
        task_id = _new_task(cm, "https://example.com/reconcile-recent")
        cm.update_task_status(task_id, TaskStatus.PROCESSING)

        count = cm.reconcile_runtime_orphaned_tasks(exclude_task_ids=set())

        assert count == 0
        assert cm.get_task_by_id(task_id)["status"] == "processing"

    def test_excluded_task_is_not_reconciled_even_if_old(self, cm):
        """The in-flight registry snapshot is a stronger guard than the
        time threshold -- a task_id present in exclude_task_ids must
        survive regardless of how old created_at is."""
        task_id = _new_task(cm, "https://example.com/reconcile-excluded")
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        now = datetime.datetime.now(datetime.timezone.utc)
        self._backdate(cm, task_id, self._old_created_at(now))

        count = cm.reconcile_runtime_orphaned_tasks(
            exclude_task_ids={task_id}, now=now,
        )

        assert count == 0
        assert cm.get_task_by_id(task_id)["status"] == "processing"

    def test_terminal_tasks_are_never_reconciled(self, cm):
        task_id = _new_task(cm, "https://example.com/reconcile-terminal")
        cm.update_task_status(task_id, TaskStatus.SUCCESS)
        now = datetime.datetime.now(datetime.timezone.utc)
        self._backdate(cm, task_id, self._old_created_at(now))

        count = cm.reconcile_runtime_orphaned_tasks(exclude_task_ids=set(), now=now)

        assert count == 0
        assert cm.get_task_by_id(task_id)["status"] == "success"

    def test_default_exclude_task_ids_is_none_safe(self, cm):
        """exclude_task_ids=None (the defensive default) must behave like
        an empty set, not raise -- callers besides _periodic_maintenance
        (e.g. a script or a test) may not always have a registry snapshot
        on hand."""
        count = cm.reconcile_runtime_orphaned_tasks()
        assert count == 0

    def test_grace_period_boundary_is_strict_less_than(self, cm):
        """A task created exactly at the grace-period boundary must not be
        reconciled -- only created_at strictly earlier than the cutoff
        qualifies (mirrors _fail_non_terminal_tasks's created_before
        semantics: `created_at < cutoff`, not `<=`)."""
        task_id = _new_task(cm, "https://example.com/reconcile-boundary")
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        now = datetime.datetime.now(datetime.timezone.utc)
        boundary_created_at = (
            now - datetime.timedelta(seconds=RUNTIME_RECONCILE_GRACE_SECONDS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        self._backdate(cm, task_id, boundary_created_at)

        count = cm.reconcile_runtime_orphaned_tasks(exclude_task_ids=set(), now=now)

        assert count == 0
        assert cm.get_task_by_id(task_id)["status"] == "processing"

    def test_custom_grace_period_is_honored(self, cm):
        """grace_period_seconds is overridable -- a much shorter custom
        window must reconcile a task that the default (much larger) window
        would have spared."""
        task_id = _new_task(cm, "https://example.com/reconcile-custom-grace")
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        now = datetime.datetime.now(datetime.timezone.utc)
        created_at = (now - datetime.timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
        self._backdate(cm, task_id, created_at)

        # Well within the real default grace period -- must survive it.
        assert cm.reconcile_runtime_orphaned_tasks(exclude_task_ids=set(), now=now) == 0

        count = cm.reconcile_runtime_orphaned_tasks(
            exclude_task_ids=set(), grace_period_seconds=10, now=now,
        )

        assert count == 1
        assert cm.get_task_by_id(task_id)["status"] == "failed"

    def test_fail_non_terminal_tasks_combines_restrict_to_task_ids_and_created_before(self, cm):
        """Direct unit coverage for the WHERE-clause builder itself
        (_fail_non_terminal_tasks): restrict_to_task_ids and created_before
        must combine with AND, not override each other -- reconcile_
        runtime_orphaned_tasks only ever supplies created_before and
        recover_orphaned_tasks only ever supplies restrict_to_task_ids, so
        this combination is otherwise never exercised."""
        in_range = _new_task(cm, "https://example.com/combo-in-range")
        cm.update_task_status(in_range, TaskStatus.PROCESSING)
        snapshot = cm.get_non_terminal_task_ids()

        # Created *after* the snapshot was captured -- restrict_to_task_ids
        # alone must exclude it even though it also satisfies the
        # created_before filter below.
        out_of_range = _new_task(cm, "https://example.com/combo-out-of-range")
        cm.update_task_status(out_of_range, TaskStatus.PROCESSING)

        now = datetime.datetime.now(datetime.timezone.utc)
        old_created_at = (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        with cm._get_cursor() as cursor:
            cursor.execute(
                "UPDATE task_status SET created_at = ? WHERE task_id IN (?, ?)",
                (old_created_at, in_range, out_of_range),
            )

        count = cm._fail_non_terminal_tasks(
            reason="test-combo",
            error_message="combo test",
            restrict_to_task_ids=snapshot,
            created_before=(now + datetime.timedelta(seconds=1)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        )

        assert count == 1
        assert cm.get_task_by_id(in_range)["status"] == "failed"
        assert cm.get_task_by_id(out_of_range)["status"] == "processing"

    def test_queue_reject_cleanup_write_failure_scenario_is_reconciled_next_cycle(
        self, cm,
    ):
        """End-to-end reproduction of the exact scenario finding c
        describes: /api/transcribe's queue-full 503 branch (api/routes/
        tasks.py) leaves a task_status row at its create_task() default
        ('queued') and best-effort tries to CAS it to failed -- if that
        cleanup write itself fails (DB lock, see
        test_api_routes.py::test_queue_full_cleanup_write_failure_still_
        returns_503 for the route-level behavior), the row is left exactly
        in the shape reproduced here: status='queued', never touched by
        update_task_status at all. No aclose()/startup-recovery trigger
        point exists for it while the service keeps running -- only the
        next periodic maintenance pass's reconcile step does, once the row
        ages past the grace period and drops out of the (here: empty,
        simulating "worker already gave up on it") in-flight registry
        snapshot."""
        task_id = cm.create_task(url="https://example.com/queue-reject-cleanup-failed")[
            "task_id"
        ]
        assert cm.get_task_by_id(task_id)["status"] == "queued"

        now = datetime.datetime.now(datetime.timezone.utc)
        self._backdate(cm, task_id, self._old_created_at(now))

        count = cm.reconcile_runtime_orphaned_tasks(exclude_task_ids=set(), now=now)

        assert count == 1
        task = cm.get_task_by_id(task_id)
        assert task["status"] == "failed"
        assert task["terminal_snapshot"]["reason"] == "runtime_reconcile"
