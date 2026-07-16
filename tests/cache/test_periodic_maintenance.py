"""Unit tests for `_periodic_maintenance` in
src/video_transcript_api/api/app.py (codex-review R10, two P2 findings).

The invariant is that task_status rows must not be reclaimed before the
/view/{view_token} resolver is done needing them. Audit history now reads
audit-owned snapshots and deliberately does not extend content lifetime.

#1 (shared now): cleanup_old_cache() walks and deletes files, which can
take seconds; if cleanup_task_status() then independently calls now()
again, the two cutoffs drift apart and open a race window where a record
is judged "not yet expired" by one cleanup and "expired" by the other.
_periodic_maintenance must compute one UTC now per maintenance pass and
hand the identical value to both cleanup_old_cache and cleanup_task_status.

#2 (retention floor follows cache): audit snapshots preserve history after
task cleanup, while clearing view_token prevents audit retention from
extending content access. Therefore only cache_retention_days constrains the
task_status floor.

These tests mock CacheManager/AuditLogger entirely (no real DB I/O) and
only assert on the call arguments _periodic_maintenance passes them, since
the cleanup functions' own retention/clamp math is already covered by
tests/cache/test_task_status_cleanup.py and
tests/cache/test_cleanup_clock_consistency.py.

Console output: English only, no emoji.
"""
import asyncio
import importlib
from unittest.mock import MagicMock

import pytest

# NOTE: `import src.video_transcript_api.api.app as app_module` (or the
# equivalent `from ... import app as app_module`) would silently bind
# app_module to a FastAPI *instance* instead of this module: `import a.b.c
# as x` resolves via attribute-walk (a -> a.b -> a.b.c) after ensuring
# sys.modules contains the submodule, and
# src/video_transcript_api/api/__init__.py does `from .server import app`,
# which rebinds the `app` *attribute* on the `api` package to
# server.py's `app = create_app()` FastAPI instance -- shadowing the
# `api.app` submodule reference that Python's import machinery placed
# there. importlib.import_module() looks the submodule up directly in
# sys.modules by its dotted name, so it is immune to that attribute
# shadowing and returns the real app.py module.
app_module = importlib.import_module("src.video_transcript_api.api.app")


class _StopLoop(Exception):
    """Raised by the patched asyncio.sleep to break out of
    _periodic_maintenance's `while True` loop after exactly one pass, so
    tests can assert on a single maintenance cycle without waiting 24h or
    running forever."""


async def _raise_stop_loop(*_args, **_kwargs):
    raise _StopLoop()


def _build_fake_managers():
    """CacheManager/AuditLogger doubles: no real DB, just call recording."""
    cache_manager = MagicMock(name="cache_manager")
    cache_manager.cleanup_old_cache.return_value = 0
    cache_manager.cleanup_task_status.return_value = 0
    audit_logger = MagicMock(name="audit_logger")
    audit_logger.cleanup_old_logs.return_value = 0
    return cache_manager, audit_logger


class _FakeInflightRegistry:
    """Minimal stand-in for RuntimeContext.inflight_registry. These
    maintenance tests don't exercise backpressure at all -- they just need
    the attribute _periodic_maintenance's runtime-reconcile step (P1, local
    codex review round 12) reads via `runtime.inflight_registry.
    all_task_ids()` to exist, so the step doesn't AttributeError before the
    rest of the maintenance pass (repair/cleanup) runs.

    task_ids is injectable (default empty) so
    TestPeriodicMaintenanceRuntimeReconcile can assert
    reconcile_runtime_orphaned_tasks receives exactly this snapshot as its
    exclude_task_ids argument."""

    def __init__(self, task_ids: set | None = None):
        self._task_ids = task_ids or set()

    def all_task_ids(self) -> set:
        return self._task_ids


class _FakeRuntimeForMaintenance:
    """Stand-in for RuntimeContext.run_maintenance in tests that don't need
    the real dedicated-executor/worker_futures tracking (see
    RuntimeContext.run_maintenance in api/context.py) -- just call the
    blocking function directly and return its result, same shape as the
    bare `asyncio.to_thread(func, *args, **kwargs)` these tests replaced.

    Deliberately has no `recovery_pending`/`startup_recovery_task_ids`
    attributes: the orphan-recovery-retry code in _periodic_maintenance
    reads them via `getattr(runtime, "recovery_pending", False)`, so this
    bare double exercises the "attribute absent -> treated as not pending,
    retry step skipped entirely" fallback that a hand-rolled Fake predates
    the real RuntimeContext fields would otherwise hit by accident.

    inflight_registry IS always set (unlike the two attributes above): it's
    an unconditional RuntimeContext.__init__ field on every real instance,
    not an optionally-absent one, so there's no equivalent "absent ->
    fallback" behavior to preserve here."""

    def __init__(self):
        self.inflight_registry = _FakeInflightRegistry()

    async def run_maintenance(self, func, *args, **kwargs):
        return func(*args, **kwargs)


class _FakeRuntimeWithRecovery(_FakeRuntimeForMaintenance):
    """Same run_maintenance stand-in as above, plus the two RuntimeContext
    fields the orphan-recovery-retry step reads: `recovery_pending`
    (whether the one-shot startup sweep failed and needs a retry) and
    `startup_recovery_task_ids` (the restrict_to_task_ids snapshot passed to
    recover_orphaned_tasks so the retry never sweeps up tasks this same
    process accepted after booting -- G3 introduced this as a created_at
    cutoff string; H4 changed it to a rowid watermark; L2, CI review round
    5 P1, replaced the rowid watermark with this fixed task_id snapshot
    after finding rowid reuse could defeat it)."""

    def __init__(self, recovery_pending: bool, startup_recovery_task_ids=None):
        super().__init__()
        self.recovery_pending = recovery_pending
        self.startup_recovery_task_ids = startup_recovery_task_ids


def _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger, runtime=None):
    """Run _periodic_maintenance(config) for exactly one loop iteration.

    get_cache_manager/get_audit_logger/get_logger/get_runtime are
    module-level names imported into app_module via `from .context import
    (...)`, so they are patched directly on app_module. asyncio.sleep is
    patched on the shared `asyncio` module object (app_module.asyncio is
    that same module) to raise _StopLoop, which stands in for "the 24h wait
    between passes" and lets the test observe exactly one iteration's call
    arguments.

    `runtime` defaults to a bare _FakeRuntimeForMaintenance() (no
    recovery_pending/startup_recovery_task_ids) so every pre-existing
    caller keeps exercising the "recovery retry skipped" path unchanged;
    G3 tests pass a _FakeRuntimeWithRecovery instead.
    """
    monkeypatch.setattr(app_module, "get_cache_manager", lambda: cache_manager)
    monkeypatch.setattr(app_module, "get_audit_logger", lambda: audit_logger)
    monkeypatch.setattr(app_module, "get_logger", lambda: MagicMock())
    monkeypatch.setattr(
        app_module, "get_runtime", lambda: runtime or _FakeRuntimeForMaintenance()
    )
    monkeypatch.setattr(app_module.asyncio, "sleep", _raise_stop_loop)

    with pytest.raises(_StopLoop):
        asyncio.run(app_module._periodic_maintenance(config))


# ---------------------------------------------------------------------------
# Problem 1: cleanup_old_cache and cleanup_task_status must share one now
# ---------------------------------------------------------------------------

class TestPeriodicMaintenanceSharedNow:
    def test_cleanup_old_cache_and_cleanup_task_status_receive_the_same_now(self, monkeypatch):
        cache_manager, audit_logger = _build_fake_managers()
        config = {
            "storage": {
                "cache_retention_days": 30,
                "task_status_retention_days": 180,
                "audit_log_retention_days": 90,
            }
        }

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        cache_now = cache_manager.cleanup_old_cache.call_args.kwargs.get("now")
        task_now = cache_manager.cleanup_task_status.call_args.kwargs.get("now")

        assert cache_now is not None, "cleanup_old_cache must receive an explicit now="
        assert task_now is not None, "cleanup_task_status must receive an explicit now="
        assert cache_now.tzinfo is not None, "shared now must be UTC tz-aware"
        assert cache_now == task_now, (
            "cleanup_old_cache and cleanup_task_status must be handed the exact "
            "same now within one maintenance pass -- independent now() calls "
            "would open a race window between the two cleanups (codex-review R10 #1)"
        )


# ---------------------------------------------------------------------------
# Problem 2: task_status retention floor follows content cache retention
# ---------------------------------------------------------------------------

class TestPeriodicMaintenanceRetentionFloor:
    def test_audit_retention_does_not_extend_content_lifetime(self, monkeypatch):
        cache_manager, audit_logger = _build_fake_managers()
        config = {
            "storage": {
                "cache_retention_days": 30,
                "task_status_retention_days": 5,
                "audit_log_retention_days": 365,
            }
        }

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        call = cache_manager.cleanup_task_status.call_args
        assert call.args[0] == 5, "retention_days positional arg must be untouched"
        assert call.args[1] == 30

    def test_floor_clamped_to_cache_when_longer_than_audit(self, monkeypatch):
        """The public view remains valid for the complete cache lifetime."""
        cache_manager, audit_logger = _build_fake_managers()
        config = {
            "storage": {
                "cache_retention_days": 200,
                "task_status_retention_days": 5,
                "audit_log_retention_days": 10,
            }
        }

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        call = cache_manager.cleanup_task_status.call_args
        assert call.args[1] == 200

    def test_floor_forced_to_zero_when_cache_retained_forever(self, monkeypatch):
        """cache_retention_days=0 means the cache is kept forever; the floor
        must be forced to the 'retained forever' sentinel (0) rather than
        max(0, audit_days), otherwise a finite audit_log_retention_days
        would let task_status (and its view links) expire even though the
        underlying cache never does."""
        cache_manager, audit_logger = _build_fake_managers()
        config = {
            "storage": {
                "cache_retention_days": 0,
                "task_status_retention_days": 50,
                "audit_log_retention_days": 100,
            }
        }

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        call = cache_manager.cleanup_task_status.call_args
        assert call.args[1] == 0

    def test_permanent_audit_does_not_make_content_permanent(self, monkeypatch):
        cache_manager, audit_logger = _build_fake_managers()
        config = {
            "storage": {
                "cache_retention_days": 100,
                "task_status_retention_days": 50,
                "audit_log_retention_days": 0,
            }
        }

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        call = cache_manager.cleanup_task_status.call_args
        assert call.args[1] == 100


# ---------------------------------------------------------------------------
# Problem 3 (local Codex review): repair_task_snapshots must not be gated by
# the retention-based cleanup switches.
# ---------------------------------------------------------------------------

class TestPeriodicMaintenanceRepairIndependence:
    def test_repair_runs_even_when_all_retention_is_permanent(self, monkeypatch):
        """repair_task_snapshots is a terminal-snapshot backfill, unrelated
        to the cache/task_status/audit_log retention cleanup gate. Before
        this fix, _periodic_maintenance returned immediately (never even
        entering its `while True` loop) whenever every storage.*
        _retention_days was 0 -- silently disabling periodic repair too, not
        just the retention-based deletes. This test drives a real
        maintenance pass with all-permanent retention and asserts repair
        still runs."""
        cache_manager, audit_logger = _build_fake_managers()
        audit_logger.repair_task_snapshots.return_value = 0
        config = {
            "storage": {
                "cache_retention_days": 0,
                "task_status_retention_days": 0,
                "audit_log_retention_days": 0,
            }
        }

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        audit_logger.repair_task_snapshots.assert_called_once_with(cache_manager, 500)
        # None of the retention-based deletes should run when every
        # retention_days is 0 (permanent) -- only repair is unconditional.
        cache_manager.cleanup_old_cache.assert_not_called()
        cache_manager.cleanup_task_status.assert_not_called()
        audit_logger.cleanup_old_logs.assert_not_called()


# ---------------------------------------------------------------------------
# G3 (local codex review round 6): startup recovery-failure retry.
#
# app.py::startup_event()'s one-shot recover_orphaned_tasks() call only logs
# and continues on exception -- previously with no retry mechanism at all,
# so any non-terminal task left over from a crashed previous process would
# hang around for the entire lifetime of the new process. The fix threads a
# RuntimeContext.recovery_pending flag through: startup_event() sets it on
# failure, and _periodic_maintenance retries exactly when it is set,
# clearing it on success. The retry is deliberately NOT unconditional (that
# would kill this process's own in-flight tasks every single maintenance
# pass) -- it is scoped by restrict_to_task_ids=RuntimeContext.
# startup_recovery_task_ids so only tasks that predate this process's own
# boot are ever swept. This scoping parameter has been reworked twice:
# G3 (local codex review round 6) introduced cutoff=RuntimeContext.
# started_at, a created_at string compared at only second resolution; H4
# (round 7) replaced it with a rowid watermark to sidestep that same-second
# boundary bug; L2 (CI review round 5, P1) replaced the rowid watermark in
# turn after finding it unsound on a TEXT-primary-key table -- SQLite
# reuses a deleted row's rowid for the next insert, which could let a
# post-boot task fall at or below the watermark and get misclassified as a
# pre-boot zombie (see CacheManager.get_non_terminal_task_ids for the full
# reuse scenario). The fixed task_id snapshot this settled on is immune by
# construction: it is captured once at boot and never grows.
# ---------------------------------------------------------------------------

class TestPeriodicMaintenanceOrphanRecoveryRetry:
    def test_retries_recovery_when_pending_and_clears_flag_on_success(self, monkeypatch):
        cache_manager, audit_logger = _build_fake_managers()
        cache_manager.recover_orphaned_tasks.return_value = 2
        config = {"storage": {}}
        snapshot = frozenset({"pre-boot-task-1", "pre-boot-task-2"})
        runtime = _FakeRuntimeWithRecovery(
            recovery_pending=True, startup_recovery_task_ids=snapshot
        )

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger, runtime=runtime)

        cache_manager.recover_orphaned_tasks.assert_called_once_with(
            restrict_to_task_ids=snapshot
        )
        assert runtime.recovery_pending is False, (
            "flag must be cleared once the retry completes without raising"
        )

    def test_does_not_retry_when_not_pending(self, monkeypatch):
        """The common case (startup recovery succeeded, or there was nothing
        to recover): recovery_pending stays False and the retry step must
        not call recover_orphaned_tasks at all -- calling it unconditionally
        every maintenance pass would risk marking this process's own
        in-flight tasks as failed for no reason."""
        cache_manager, audit_logger = _build_fake_managers()
        config = {"storage": {}}
        runtime = _FakeRuntimeWithRecovery(recovery_pending=False)

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger, runtime=runtime)

        cache_manager.recover_orphaned_tasks.assert_not_called()
        assert runtime.recovery_pending is False

    def test_flag_absent_on_bare_runtime_double_is_treated_as_not_pending(self, monkeypatch):
        """Defensive: a runtime double with no recovery_pending attribute at
        all (the pre-G3 shape every other test in this file already uses)
        must not accidentally trigger the retry."""
        cache_manager, audit_logger = _build_fake_managers()
        config = {"storage": {}}

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        cache_manager.recover_orphaned_tasks.assert_not_called()


class TestPeriodicMaintenanceRuntimeReconcile:
    """P1 (local codex review round 12, finding c): _periodic_maintenance
    must call CacheManager.reconcile_runtime_orphaned_tasks once per pass,
    passing the in-flight registry's current snapshot
    (runtime.inflight_registry.all_task_ids()) as the exclusion set -- this
    is the wiring half of the fix; the reconcile method's own filtering
    logic (created_before/exclude_task_ids semantics) is covered directly
    in tests/unit/test_cache_task_recovery.py::TestReconcileRuntimeOrphanedTasks.
    """

    def test_reconcile_called_with_current_registry_snapshot(self, monkeypatch):
        cache_manager, audit_logger = _build_fake_managers()
        cache_manager.reconcile_runtime_orphaned_tasks.return_value = 0
        config = {"storage": {}}
        runtime = _FakeRuntimeForMaintenance()
        runtime.inflight_registry = _FakeInflightRegistry({"t1", "t2"})

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger, runtime=runtime)

        cache_manager.reconcile_runtime_orphaned_tasks.assert_called_once_with(
            exclude_task_ids={"t1", "t2"}
        )

    def test_reconcile_runs_with_empty_registry(self, monkeypatch):
        """No in-flight tasks at maintenance time -- reconcile is still
        called (with an empty exclusion set), not skipped."""
        cache_manager, audit_logger = _build_fake_managers()
        cache_manager.reconcile_runtime_orphaned_tasks.return_value = 0
        config = {"storage": {}}

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        cache_manager.reconcile_runtime_orphaned_tasks.assert_called_once_with(
            exclude_task_ids=set()
        )

    def test_reconcile_runs_before_repair_and_cleanup_steps(self, monkeypatch):
        """The reconcile step must not block the rest of the maintenance
        pass (repair_task_snapshots, cache/task_status/audit cleanup) --
        all of them must still run in the same pass alongside it."""
        cache_manager, audit_logger = _build_fake_managers()
        cache_manager.reconcile_runtime_orphaned_tasks.return_value = 3
        audit_logger.repair_task_snapshots.return_value = 0
        config = {
            "storage": {
                "cache_retention_days": 30,
                "task_status_retention_days": 180,
                "audit_log_retention_days": 90,
            }
        }

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        cache_manager.reconcile_runtime_orphaned_tasks.assert_called_once()
        audit_logger.repair_task_snapshots.assert_called_once_with(cache_manager, 500)
        cache_manager.cleanup_old_cache.assert_called_once()
        cache_manager.cleanup_task_status.assert_called_once()
        audit_logger.cleanup_old_logs.assert_called_once()

    def test_reconcile_exception_does_not_crash_the_maintenance_loop(self, monkeypatch):
        """Mirrors the outer try/except's existing contract for every other
        maintenance step: reconcile_runtime_orphaned_tasks raising must be
        logged and retried next pass, not propagate and take down
        _periodic_maintenance's background task."""
        cache_manager, audit_logger = _build_fake_managers()
        cache_manager.reconcile_runtime_orphaned_tasks.side_effect = RuntimeError("db locked")
        config = {"storage": {}}

        # Must still reach asyncio.sleep (which raises _StopLoop) instead of
        # letting the RuntimeError escape _periodic_maintenance entirely.
        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)


# ---------------------------------------------------------------------------
# K1 bucket b (CI review round 3, major): bounded compensation for the
# process_llm_queue submit-failure double-failure case (submit() fails AND
# the FAILED terminal write itself also fails). llm_ops.process_llm_queue
# registers the task_id into RuntimeContext.terminal_write_pending after
# task_done() in that scenario (see tests/integration/
# test_llm_stage_terminal_state.py::TestLlmQueueSubmitFailureWritesTerminalState
# for the pump-side registration coverage). _periodic_maintenance is the
# other half: every pass it must drain that set and retry the CAS write via
# context._retry_terminal_write_pending, re-registering only the ids that
# still fail.
# ---------------------------------------------------------------------------

class _FakeRuntimeWithTerminalWritePending(_FakeRuntimeForMaintenance):
    """Stand-in exposing RuntimeContext.terminal_write_pending's
    register/drain pair so _periodic_maintenance's retry step has
    something real to drain. Deliberately does not implement
    recovery_pending/startup_recovery_task_ids (inherited bare from
    _FakeRuntimeForMaintenance) -- this class is only about the K1 bucket b
    retry step."""

    def __init__(self, pending: set | None = None):
        super().__init__()
        self.terminal_write_pending = set(pending or ())

    def register_terminal_write_pending(self, task_id: str) -> None:
        self.terminal_write_pending.add(task_id)

    def drain_terminal_write_pending(self) -> set:
        drained = set(self.terminal_write_pending)
        self.terminal_write_pending.clear()
        return drained


class TestPeriodicMaintenanceTerminalWritePendingRetry:
    def test_pending_task_written_to_failed_with_snapshot_and_set_cleared(
        self, monkeypatch, tmp_path,
    ):
        """End-to-end against a real CacheManager (not a MagicMock): a task
        stuck in 'calibrating' whose task_id was registered into
        terminal_write_pending must actually land as 'failed' with a
        populated terminal_snapshot after one maintenance pass, and the
        pending set must be cleared once the retry succeeds."""
        from src.video_transcript_api.cache.cache_manager import CacheManager
        from src.video_transcript_api.utils.task_status import TaskStatus

        cache_manager = CacheManager(cache_dir=str(tmp_path / "cache"))
        try:
            task_id = cache_manager.create_task(url="https://example.com/v1")["task_id"]
            cache_manager.update_task_status(task_id, TaskStatus.CALIBRATING)

            audit_logger = MagicMock(name="audit_logger")
            audit_logger.repair_task_snapshots.return_value = 0
            # All retention-based cleanup disabled so this pass only runs
            # the unconditional steps (reconcile/repair) plus the retry
            # step under test -- keeps the real-CacheManager side effects
            # scoped to exactly what this test cares about.
            config = {
                "storage": {
                    "cache_retention_days": 0,
                    "task_status_retention_days": 0,
                    "audit_log_retention_days": 0,
                }
            }
            runtime = _FakeRuntimeWithTerminalWritePending(pending={task_id})

            _run_one_maintenance_pass(
                monkeypatch, config, cache_manager, audit_logger, runtime=runtime,
            )

            row = cache_manager.get_task_by_id(task_id)
            assert row["status"] == TaskStatus.FAILED
            snapshot = row.get("terminal_snapshot")
            assert snapshot is not None, "compensated terminal write must carry a snapshot"
            assert snapshot["status"] == TaskStatus.FAILED
            assert runtime.terminal_write_pending == set(), (
                "a successful retry must clear the pending set, not leave "
                "the id registered forever"
            )
        finally:
            cache_manager.close()

    def test_still_failing_task_is_re_registered_for_next_pass(self, monkeypatch):
        """The retry write can itself fail again (e.g. still-transient DB
        error) -- the id must be re-registered, not dropped, so the next
        maintenance pass gets another chance."""
        from src.video_transcript_api.utils.task_status import TaskStatus

        cache_manager, audit_logger = _build_fake_managers()
        cache_manager.update_task_status.side_effect = RuntimeError("db unavailable")
        config = {"storage": {}}
        runtime = _FakeRuntimeWithTerminalWritePending(pending={"stuck-task-1"})

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger, runtime=runtime)

        cache_manager.update_task_status.assert_called_once()
        call = cache_manager.update_task_status.call_args
        assert call.args[0] == "stuck-task-1"
        assert call.args[1] == TaskStatus.FAILED
        assert runtime.terminal_write_pending == {"stuck-task-1"}, (
            "a retry that fails again must re-register the id so the next "
            "maintenance pass tries again"
        )

    def test_empty_pending_set_does_not_call_update_task_status(self, monkeypatch):
        """The common case (no double failures happened): nothing to
        retry, no wasted DB call."""
        cache_manager, audit_logger = _build_fake_managers()
        config = {"storage": {}}
        runtime = _FakeRuntimeWithTerminalWritePending()

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger, runtime=runtime)

        cache_manager.update_task_status.assert_not_called()

    def test_bare_runtime_without_pending_support_is_skipped_without_error(
        self, monkeypatch,
    ):
        """Defensive: a runtime double with no terminal_write_pending
        methods at all (the shape every pre-existing test in this file
        already uses) must not raise -- mirrors the recovery_pending
        flag's own defensive-getattr treatment (see
        test_flag_absent_on_bare_runtime_double_is_treated_as_not_pending
        above)."""
        cache_manager, audit_logger = _build_fake_managers()
        config = {"storage": {}}

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        cache_manager.update_task_status.assert_not_called()
