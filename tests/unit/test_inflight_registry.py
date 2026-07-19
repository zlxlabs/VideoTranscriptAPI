"""Unit + end-to-end tests for the in-flight task registry (P1, local codex
review round 12): RuntimeContext.inflight_registry / _InflightTaskRegistry /
get_inflight_registry() / RuntimeContext.track_future's task_id release
wiring.

Background: transcription/llm pipelines previously bounded capacity only at
"queued position" (asyncio.Queue(maxsize=queue_size) / queue.Queue(maxsize=
LLM_QUEUE_MAXSIZE)) -- the consumer dequeues an item and immediately submits
it to an unbounded ThreadPoolExecutor, freeing the queue slot before the
work has even started. Under sustained request volume, "queued + running"
backlog grows unbounded and 503 is nearly unreachable
(transcription.py:305/361/377, llm_ops.py:72/83). The fix moves the
capacity cap to "admission" (register before enqueue, release when the
worker future completes), covered here.

All console output must be in English only (no emoji, no Chinese).
"""

import asyncio
import concurrent.futures
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _minimal_llm_config() -> dict:
    return {
        "api_key": "test-llm-key",
        "base_url": "http://127.0.0.1:1/v1",
        "calibrate_model": "test-calibrate-model",
        "summary_model": "test-summary-model",
    }


def _minimal_config(tmp_path: Path, *, queue_size: int = 2, max_workers: int = 1) -> dict:
    return {
        "api": {"host": "127.0.0.1", "port": 8000, "auth_token": "test-token"},
        "concurrent": {
            "max_workers": max_workers,
            "queue_size": queue_size,
            "llm_max_workers": 1,
        },
        "storage": {
            "cache_dir": str(tmp_path / "cache"),
            "workspace_dir": str(tmp_path / "workspace"),
            "temp_dir": str(tmp_path / "temp"),
            "audit_db": str(tmp_path / "audit.db"),
        },
        "web": {"base_url": "http://localhost:8000"},
        "llm": _minimal_llm_config(),
        "log": {"file": str(tmp_path / "app.log")},
    }


# ---------------------------------------------------------------------------
# _InflightTaskRegistry: pure logic, no I/O
# ---------------------------------------------------------------------------


class TestInflightTaskRegistryBasics:
    def _registry(self, **capacities):
        from video_transcript_api.api.context import _InflightTaskRegistry

        return _InflightTaskRegistry(capacities or {"transcription": 2, "llm": 2})

    def test_try_register_succeeds_under_capacity(self):
        registry = self._registry(transcription=2)
        assert registry.try_register("transcription", "t1") is True
        assert registry.try_register("transcription", "t2") is True
        assert registry.size("transcription") == 2

    def test_try_register_fails_at_capacity(self):
        registry = self._registry(transcription=1)
        assert registry.try_register("transcription", "t1") is True
        assert registry.try_register("transcription", "t2") is False
        assert registry.size("transcription") == 1

    def test_try_register_is_idempotent_for_same_task_id(self):
        """Registering the same task_id twice must not double-occupy a slot
        -- capacity=1 registering "t1" twice must both report success."""
        registry = self._registry(transcription=1)
        assert registry.try_register("transcription", "t1") is True
        assert registry.try_register("transcription", "t1") is True
        assert registry.size("transcription") == 1

    def test_release_frees_a_slot(self):
        registry = self._registry(transcription=1)
        registry.try_register("transcription", "t1")
        assert registry.try_register("transcription", "t2") is False
        registry.release("transcription", "t1")
        assert registry.try_register("transcription", "t2") is True

    def test_release_is_idempotent_for_unknown_task_id(self):
        """release() must silently no-op for a task_id that was never
        registered -- callers on cleanup paths cannot always know in
        advance whether registration ever happened."""
        registry = self._registry(transcription=1)
        registry.release("transcription", "never-registered")  # must not raise
        assert registry.size("transcription") == 0

    def test_release_is_idempotent_for_unknown_kind(self):
        """release() must silently no-op for a kind the registry was never
        constructed with -- defends against callers wiring track_future's
        optional task_id onto kinds like "maintenance" that aren't part of
        the backpressure mechanism."""
        registry = self._registry(transcription=1)
        registry.release("no-such-kind", "t1")  # must not raise

    def test_release_is_idempotent_when_called_twice(self):
        registry = self._registry(transcription=1)
        registry.try_register("transcription", "t1")
        registry.release("transcription", "t1")
        registry.release("transcription", "t1")  # must not raise
        assert registry.try_register("transcription", "t2") is True

    def test_kinds_are_independent(self):
        """The transcription and llm buckets must not share capacity --
        filling one must not affect admission into the other."""
        registry = self._registry(transcription=1, llm=1)
        assert registry.try_register("transcription", "t1") is True
        assert registry.try_register("llm", "t1") is True
        assert registry.try_register("transcription", "t2") is False
        assert registry.try_register("llm", "t2") is False

    def test_all_task_ids_returns_union_across_kinds(self):
        registry = self._registry(transcription=2, llm=2)
        registry.try_register("transcription", "t1")
        registry.try_register("llm", "l1")
        assert registry.all_task_ids() == {"t1", "l1"}

    def test_all_task_ids_reflects_releases(self):
        registry = self._registry(transcription=2)
        registry.try_register("transcription", "t1")
        registry.release("transcription", "t1")
        assert registry.all_task_ids() == set()

    def test_size_reports_unknown_kind_as_zero(self):
        registry = self._registry(transcription=1)
        assert registry.size("unknown") == 0

    def test_concurrent_try_register_never_exceeds_capacity(self):
        """Thread-safety smoke test: capacity=5, 50 threads racing
        try_register with distinct task_ids -- exactly 5 must succeed,
        regardless of scheduling order."""
        registry = self._registry(transcription=5)
        results = []
        lock = threading.Lock()

        def worker(i):
            ok = registry.try_register("transcription", f"t{i}")
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 5
        assert registry.size("transcription") == 5


class TestRegisterInternal:
    """register_internal (local codex review round 13, sole finding):
    transcription.py's five internal llm_task_queue.put() call sites use
    this instead of try_register -- the work is already committed (a
    transcription worker finished downloading+transcribing and is handing
    off to the LLM stage), so there is no "reject" outcome available the
    way there is for try_register's HTTP-admission callers. Unlike
    try_register, this method never checks capacity and can never fail."""

    def _registry(self, **capacities):
        from video_transcript_api.api.context import _InflightTaskRegistry

        return _InflightTaskRegistry(capacities or {"transcription": 2, "llm": 2})

    def test_registers_under_capacity(self):
        registry = self._registry(llm=2)
        registry.register_internal("llm", "t1")
        assert registry.size("llm") == 1
        assert "t1" in registry.all_task_ids()

    def test_ignores_capacity_ceiling(self):
        """The one behavioral difference from try_register: capacity=1 must
        not block a second, third, ... registration -- register_internal is
        unconditional by design (see its docstring's "数学上界" derivation:
        the real clamp is upstream, at the two HTTP admission points)."""
        registry = self._registry(llm=1)
        for i in range(10):
            registry.register_internal("llm", f"t{i}")
        assert registry.size("llm") == 10

    def test_is_idempotent_for_the_same_task_id(self):
        registry = self._registry(llm=5)
        registry.register_internal("llm", "t1")
        registry.register_internal("llm", "t1")
        registry.register_internal("llm", "t1")
        assert registry.size("llm") == 1

    def test_does_not_disturb_an_existing_try_register_admission(self):
        """A task_id already admitted via try_register (e.g. /api/
        recalibrate) must not be double-counted or have its registration
        timestamp reset by a later register_internal call for the same id
        -- this mirrors try_register's own idempotency."""
        registry = self._registry(llm=5)
        assert registry.try_register("llm", "t1") is True
        registry.register_internal("llm", "t1")
        assert registry.size("llm") == 1

    def test_kinds_are_independent(self):
        registry = self._registry(transcription=1, llm=1)
        registry.register_internal("llm", "t1")
        assert registry.try_register("transcription", "t1") is True
        assert registry.size("transcription") == 1
        assert registry.size("llm") == 1

    def test_release_removes_a_register_internal_entry(self):
        """release() does not distinguish how a task_id was admitted -- an
        internally-registered entry is released exactly like a
        try_register'd one (see _InflightTaskRegistry's docstring: the
        completion callback release hook treats both admission sources
        identically)."""
        registry = self._registry(llm=1)
        registry.register_internal("llm", "t1")
        registry.release("llm", "t1")
        assert registry.size("llm") == 0
        assert registry.all_task_ids() == set()

    def test_concurrent_register_internal_never_loses_a_registration(self):
        """Thread-safety smoke test mirroring try_register's: unlike
        try_register, there is no capacity ceiling to race against, so the
        invariant under test is simply that concurrent calls with distinct
        task_ids never clobber each other under the shared lock."""
        registry = self._registry(llm=1000)
        threads = [
            threading.Thread(target=registry.register_internal, args=("llm", f"t{i}"))
            for i in range(50)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert registry.size("llm") == 50


# ---------------------------------------------------------------------------
# RuntimeContext wiring: __init__ capacities, start()'s LLM_QUEUE_MAXSIZE,
# get_inflight_registry() bound-vs-fallback behavior.
# ---------------------------------------------------------------------------


class TestRuntimeContextInflightRegistryWiring:
    def test_capacities_come_from_config(self, tmp_path):
        from video_transcript_api.api.context import RuntimeContext, LLM_QUEUE_MAXSIZE

        runtime = RuntimeContext(_minimal_config(tmp_path, queue_size=3))

        for i in range(3):
            assert runtime.inflight_registry.try_register("transcription", f"t{i}") is True
        assert runtime.inflight_registry.try_register("transcription", "overflow") is False

        for i in range(LLM_QUEUE_MAXSIZE):
            assert runtime.inflight_registry.try_register("llm", f"l{i}") is True
        assert runtime.inflight_registry.try_register("llm", "overflow") is False

    def test_get_inflight_registry_returns_bound_runtime_instance(self, tmp_path):
        from video_transcript_api.api.context import (
            RuntimeContext,
            bind_runtime,
            get_inflight_registry,
            unbind_runtime,
        )

        runtime = RuntimeContext(_minimal_config(tmp_path))
        token = bind_runtime(runtime)
        try:
            assert get_inflight_registry() is runtime.inflight_registry
        finally:
            unbind_runtime(token)

    def test_get_inflight_registry_fallback_is_fresh_each_call_when_unbound(self):
        """No bound runtime (e.g. a test calling a route function directly,
        or a script outside create_app()'s lifespan) -- get_inflight_registry
        must not return a cached module-level singleton (that would leak
        registration state across unrelated test cases that never release),
        so two calls in a row must yield independent instances."""
        from video_transcript_api.api.context import get_inflight_registry

        first = get_inflight_registry()
        first.try_register("transcription", "leaked-task")
        second = get_inflight_registry()
        assert second is not first
        assert second.size("transcription") == 0


# ---------------------------------------------------------------------------
# track_future's task_id release wiring: a real concurrent.futures.Future
# completing must release the registered slot, regardless of outcome.
# ---------------------------------------------------------------------------


class TestTrackFutureReleasesInflightRegistry:
    def test_future_completion_releases_registered_slot(self, tmp_path):
        from video_transcript_api.api.context import RuntimeContext

        runtime = RuntimeContext(_minimal_config(tmp_path, queue_size=1))
        assert runtime.inflight_registry.try_register("transcription", "t1") is True
        assert runtime.inflight_registry.try_register("transcription", "t2") is False

        future = concurrent.futures.Future()
        runtime.track_future(future, task_id="t1")
        future.set_result(None)

        assert runtime.inflight_registry.size("transcription") == 0
        assert runtime.inflight_registry.try_register("transcription", "t2") is True

    def test_future_exception_still_releases_registered_slot(self, tmp_path):
        """A worker future that completes with an exception (rather than a
        result) must still count as "done" for release purposes --
        add_done_callback fires for both outcomes, and release must not be
        conditioned on success."""
        from video_transcript_api.api.context import RuntimeContext

        runtime = RuntimeContext(_minimal_config(tmp_path, queue_size=1))
        runtime.inflight_registry.try_register("transcription", "t1")

        future = concurrent.futures.Future()
        runtime.track_future(future, task_id="t1")
        future.set_exception(RuntimeError("boom"))

        assert runtime.inflight_registry.size("transcription") == 0

    def test_no_task_id_does_not_touch_registry(self, tmp_path):
        """kind="maintenance" callers (RuntimeContext.run_maintenance) never
        pass task_id -- track_future must not attempt any registry
        interaction for them (release() would be a harmless no-op anyway,
        but this locks the "no task_id -> skip entirely" branch)."""
        from video_transcript_api.api.context import RuntimeContext

        runtime = RuntimeContext(_minimal_config(tmp_path))
        future = concurrent.futures.Future()
        runtime.track_future(future, kind="maintenance")
        future.set_result(None)  # must not raise

    def test_release_is_scoped_to_the_given_kind(self, tmp_path):
        """A "transcription" future completing must not release a same-
        named task_id registered under "llm" -- kinds are independent
        capacity pools. (Local codex review round 13 superseded the
        original comment here, which assumed transcription.py's internal
        llm_task_queue.put() calls were deliberately never registered
        under "llm" at all -- round 13 found that assumption caused
        runtime reconciliation to misclassify in-flight LLM handoffs as
        orphans, and transcription.py now registers them via
        register_internal("llm", task_id) before every put(). This test's
        own scenario is unaffected either way: it registers "llm" directly
        via try_register to isolate the kind-scoping behavior itself, see
        TestRegisterInternal below for the register_internal-specific
        coverage.)"""
        from video_transcript_api.api.context import RuntimeContext

        runtime = RuntimeContext(_minimal_config(tmp_path, queue_size=1))
        runtime.inflight_registry.try_register("transcription", "shared-id")
        runtime.inflight_registry.try_register("llm", "shared-id")

        future = concurrent.futures.Future()
        runtime.track_future(future, kind="transcription", task_id="shared-id")
        future.set_result(None)

        assert runtime.inflight_registry.size("transcription") == 0
        assert runtime.inflight_registry.size("llm") == 1


# ---------------------------------------------------------------------------
# H1 (增量复核): track_future's completion callback must actually consume
# done_future.exception() -- before this fix it only discarded the future
# and released quota/semaphore, leaving any exception that reached the
# tracked future (e.g. a terminal-state DB write re-raised out of
# llm_ops._handle_llm_task -- see TestLlmTaskFailedWriteReraises in
# test_llm_stage_terminal_state.py, which proves the exception reaches the
# future but never asserts anything observes it past that point) silently
# stranded: nothing in production code awaits or .result()s a
# track_future-tracked future, so the exception had no other consumer
# anywhere in the codebase.
# ---------------------------------------------------------------------------


class TestTrackFutureLogsUnconsumedException:
    def test_real_submit_exception_is_logged_at_error_with_task_id(
        self, tmp_path, monkeypatch,
    ):
        """Real ThreadPoolExecutor.submit() -> track_future chain: a worker
        callable that raises (standing in for a terminal-state repository
        write failure re-raised out of the worker) must be consumed and
        logged at ERROR with kind/task_id/exception repr, and release
        semantics (inflight registry slot) must not regress."""
        from video_transcript_api.api.context import RuntimeContext

        runtime = RuntimeContext(_minimal_config(tmp_path, queue_size=1))
        runtime.inflight_registry.try_register("llm", "t-err")
        mock_logger = MagicMock()
        monkeypatch.setattr(runtime, "logger", mock_logger)
        # Mirror process_llm_queue's real protocol: acquire llm_submit_
        # semaphore before submit() so track_future's kind="llm" release in
        # the completion callback has a matching acquire (BoundedSemaphore
        # raises ValueError on an unmatched release).
        runtime.llm_submit_semaphore.acquire()

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            def _boom():
                raise RuntimeError("db unavailable")

            future = executor.submit(_boom)
            runtime.track_future(future, kind="llm", task_id="t-err")
            with pytest.raises(RuntimeError, match="db unavailable"):
                future.result(timeout=5)
            # future.result() only proves the *caller's* wait observed the
            # exception -- the done callback runs asynchronously via
            # add_done_callback and isn't guaranteed to have finished the
            # instant result() returns, so wait for it explicitly instead
            # of asserting on a race.
            deadline = time.monotonic() + 5
            while not mock_logger.error.called and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            executor.shutdown(wait=True)

        assert mock_logger.error.call_count == 1, (
            "red on the pre-fix code: the completion callback never called "
            "done_future.exception(), so no ERROR log was ever produced"
        )
        message = str(mock_logger.error.call_args)
        assert "t-err" in message, "log must include the task_id"
        assert "db unavailable" in message, "log must include the exception"
        # Release semantics must not regress: quota is still freed even
        # though the callback now also logs.
        assert runtime.inflight_registry.size("llm") == 0

    def test_cancelled_future_does_not_raise_in_done_callback(
        self, tmp_path, monkeypatch,
    ):
        """Future.exception() raises CancelledError (not returns None) when
        the future was cancelled -- the callback must check cancelled()
        first and skip calling exception() entirely, or add_done_callback's
        internal exception handling would swallow a broken callback here
        with only concurrent.futures' own noisy warning, not our ERROR log,
        and (more importantly) a naive `except Exception` around exception()
        would misreport a routine cancellation as an unconsumed error."""
        from video_transcript_api.api.context import RuntimeContext

        runtime = RuntimeContext(_minimal_config(tmp_path, queue_size=1))
        mock_logger = MagicMock()
        monkeypatch.setattr(runtime, "logger", mock_logger)

        future = concurrent.futures.Future()
        runtime.track_future(future, kind="transcription", task_id="t-cancel")
        assert future.cancel() is True  # must not raise inside the callback

        mock_logger.error.assert_not_called()

    def test_successful_future_does_not_log_error(self, tmp_path, monkeypatch):
        """Regression companion: a future that completes normally must
        never produce an ERROR log."""
        from video_transcript_api.api.context import RuntimeContext

        runtime = RuntimeContext(_minimal_config(tmp_path, queue_size=1))
        mock_logger = MagicMock()
        monkeypatch.setattr(runtime, "logger", mock_logger)

        future = concurrent.futures.Future()
        runtime.track_future(future, kind="transcription", task_id="t-ok")
        future.set_result(None)

        mock_logger.error.assert_not_called()


# ---------------------------------------------------------------------------
# J3 (本地增量复核第 3 轮): H1 above made the completion callback consume
# and log an unconsumed exception, but the log call landed *after* the
# callback had already discarded the future from worker_futures and
# notify_all()'d -- and aclose()'s shutdown drain (_stop_workers) waits on
# exactly that notify_all() via _worker_futures_condition.wait_for(...)
# before continuing on to _finish_close() -> shutdown_logger() (which tears
# down the logging sink). A future completing with an exception during the
# shutdown window could therefore wake the drain and let it reach
# shutdown_logger() before the ERROR log for that exception was ever
# written -- a fresh observability gap stacked on top of the one H1 just
# closed. The fix reorders the callback into try (consume + log) / finally
# (release quota + discard + notify_all), proven below with event-driven
# synchronization rather than sleeps.
# ---------------------------------------------------------------------------


class TestTrackFutureCallbackOrdering:
    def test_exception_is_logged_before_future_leaves_worker_futures(
        self, tmp_path, monkeypatch,
    ):
        """Block the mocked logger.error() call mid-callback and prove the
        future is STILL present in worker_futures (and its inflight quota
        still held) while the log call is in flight -- red on the pre-fix
        code, where discard() removed the future, notified waiters, and
        released quota *before* ever calling logger.error(), so by the time
        the mocked logger.error() ran, all of that would have already
        happened."""
        from video_transcript_api.api.context import RuntimeContext

        runtime = RuntimeContext(_minimal_config(tmp_path, queue_size=1))
        runtime.inflight_registry.try_register("transcription", "t-order")

        log_started = threading.Event()
        allow_log_to_finish = threading.Event()

        def _blocking_error(*args, **kwargs):
            log_started.set()
            # Hold the callback inside the logging step long enough for the
            # main thread to observe worker_futures' state mid-callback.
            assert allow_log_to_finish.wait(timeout=5), "test deadlocked"

        mock_logger = MagicMock()
        mock_logger.error.side_effect = _blocking_error
        monkeypatch.setattr(runtime, "logger", mock_logger)

        future = concurrent.futures.Future()
        runtime.track_future(future, kind="transcription", task_id="t-order")
        entry = ("transcription", future)
        assert entry in runtime.worker_futures

        # add_done_callback runs synchronously in whichever thread calls
        # set_exception() -- run it off-thread so the main thread stays free
        # to inspect state while the callback is blocked mid-log.
        setter = threading.Thread(
            target=future.set_exception, args=(RuntimeError("boom"),),
        )
        setter.start()
        try:
            assert log_started.wait(timeout=5), "logger.error was never called"
            # The log call is currently blocked inside the callback's try
            # block -- the finally block (release quota + discard + notify)
            # must not have run yet.
            assert entry in runtime.worker_futures, (
                "red on the pre-fix code: the future was removed from "
                "worker_futures (and waiters notified) before the "
                "exception was logged"
            )
            assert runtime.inflight_registry.size("transcription") == 1, (
                "quota must not be released before the exception is logged "
                "either -- both live in the same finally block"
            )
        finally:
            allow_log_to_finish.set()
            setter.join(timeout=5)

        assert entry not in runtime.worker_futures
        assert runtime.inflight_registry.size("transcription") == 0

    def test_shutdown_waiter_does_not_wake_until_logging_completes(
        self, tmp_path, monkeypatch,
    ):
        """End-to-end proof using the exact primitive _stop_workers relies
        on: a thread blocked on _worker_futures_condition.wait_for(...)
        (mirroring the shutdown drain) must not wake up until the
        completion callback's finally block runs -- i.e. only after the
        ERROR log for the exception has already been written. If this
        ordering regressed, aclose() could reach shutdown_logger() while an
        ERROR log for this exact exception is still in flight (or never
        happened at all)."""
        from video_transcript_api.api.context import RuntimeContext

        runtime = RuntimeContext(_minimal_config(tmp_path, queue_size=1))

        log_started = threading.Event()
        allow_log_to_finish = threading.Event()
        order: list[str] = []

        def _blocking_error(*args, **kwargs):
            order.append("logged")
            log_started.set()
            assert allow_log_to_finish.wait(timeout=5), "test deadlocked"

        mock_logger = MagicMock()
        mock_logger.error.side_effect = _blocking_error
        monkeypatch.setattr(runtime, "logger", mock_logger)

        future = concurrent.futures.Future()
        runtime.track_future(future, kind="transcription", task_id="t-shutdown")

        waiter_woke = threading.Event()

        def _waiter():
            # Mirrors _stop_workers' own wait_for call exactly (see
            # context.py:_stop_workers).
            with runtime._worker_futures_condition:
                runtime._worker_futures_condition.wait_for(
                    lambda: not runtime.worker_futures, timeout=5
                )
            order.append("woke")
            waiter_woke.set()

        waiter = threading.Thread(target=_waiter)
        waiter.start()

        setter = threading.Thread(
            target=future.set_exception, args=(RuntimeError("boom"),),
        )
        setter.start()
        try:
            assert log_started.wait(timeout=5), "logger.error was never called"
            # Give the waiter thread every opportunity to have (incorrectly)
            # woken up already if the ordering regressed.
            assert waiter_woke.wait(timeout=0.3) is False, (
                "shutdown waiter woke up before the exception was logged"
            )
        finally:
            allow_log_to_finish.set()
            setter.join(timeout=5)
            waiter.join(timeout=5)

        assert waiter_woke.is_set()
        assert order == ["logged", "woke"]

    def test_logger_error_raising_still_releases_quota_and_discards_future(
        self, tmp_path, monkeypatch,
    ):
        """The try/finally split exists specifically so a broken logging
        call can't leak quota or wedge shutdown waiters -- if logger.error()
        itself raises, the finally block must still release the inflight
        slot, discard the future from worker_futures, and notify_all()."""
        from video_transcript_api.api.context import RuntimeContext

        runtime = RuntimeContext(_minimal_config(tmp_path, queue_size=1))
        runtime.inflight_registry.try_register("transcription", "t-log-fail")

        mock_logger = MagicMock()
        mock_logger.error.side_effect = RuntimeError("logging backend down")
        monkeypatch.setattr(runtime, "logger", mock_logger)

        future = concurrent.futures.Future()
        runtime.track_future(future, kind="transcription", task_id="t-log-fail")
        # concurrent.futures swallows exceptions raised inside a done
        # callback (logs its own internal warning); it must not propagate
        # out of set_exception().
        future.set_exception(RuntimeError("boom"))

        assert ("transcription", future) not in runtime.worker_futures
        assert runtime.inflight_registry.size("transcription") == 0


# ---------------------------------------------------------------------------
# End-to-end: real asyncio.Queue admission + real process_task_queue
# consumer + real ThreadPoolExecutor with a blocked worker, proving the
# executor backlog stays bounded at the registered capacity (not the
# queue's own maxsize, which the consumer drains immediately on dequeue --
# see the module docstring) and that completing the blocked worker frees
# exactly one admission slot.
# ---------------------------------------------------------------------------


def test_blocked_worker_bounds_admission_and_release_frees_a_slot(tmp_path, monkeypatch):
    """T2 (local codex review): this test previously waited for the worker
    to start via `await asyncio.to_thread(worker_started.wait, 5)` followed
    by a *separate* `assert worker_started.is_set()` -- two statements that
    are logically fine on their own, but mix two different synchronization
    idioms (an OS-thread-pool-mediated blocking Event.wait, routed through
    the event loop's shared default executor via asyncio.to_thread) with
    the pure async bounded-polling idiom already used just a few lines
    below for `_slot_freed()`. Under a loaded CI machine the two waits
    (this one blocking a borrowed thread-pool thread, the other polling the
    event loop directly) do not behave identically under contention, which
    is a plausible source of the observed intermittent failures (6 runs, 2
    failures) even though this test could not be reproduced failing locally
    across ~50 runs (isolated repeats, whole-file repeats, and repeats
    under synthetic CPU load). Hardened by replacing the to_thread(Event.
    wait) + is_set() pair with the same `_wait_until` bounded async-polling
    helper used for the slot-freed check below -- one deterministic
    synchronization primitive for every condition in this test, or asserted
    with a diagnostic timeout instead of a bare sleep. Timeouts also widened
    from 5s to 10s (both here and in the worker-thread-side wait) for extra
    margin against CI load, as defense-in-depth on top of the primitive
    change."""
    from video_transcript_api.api.context import RuntimeContext, bind_runtime, unbind_runtime
    from video_transcript_api.api.services import transcription

    config = _minimal_config(tmp_path, queue_size=1, max_workers=1)
    runtime = RuntimeContext(config)
    runtime.start()
    token = bind_runtime(runtime)

    worker_started = threading.Event()
    release_worker = threading.Event()
    submit_count = 0
    submit_lock = threading.Lock()

    def blocking_process_transcription(*args, **kwargs):
        worker_started.set()
        assert release_worker.wait(timeout=10), "test failed to release the blocked worker in time"

    real_submit = runtime.executor.submit

    def counting_submit(*args, **kwargs):
        nonlocal submit_count
        with submit_lock:
            submit_count += 1
        return real_submit(*args, **kwargs)

    monkeypatch.setattr(transcription, "process_transcription", blocking_process_transcription)
    monkeypatch.setattr(runtime.executor, "submit", counting_submit)

    async def _wait_until(predicate, *, timeout: float) -> bool:
        """Deterministic bounded async poll: checks `predicate()` from the
        event loop's own turn (no borrowed thread-pool thread involved)
        every 10ms until it's True or `timeout` seconds have elapsed.
        Shared by every wait condition in this test so there is exactly one
        synchronization idiom to reason about, not a mix of thread-pool
        Event.wait and event-loop polling."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return False

    async def scenario():
        cache_manager = runtime.cache_manager
        processor = asyncio.create_task(transcription.process_task_queue())
        try:
            # Admission #1: capacity is 1 (queue_size=1) -- must succeed and
            # actually reach the executor (worker_started proves the
            # consumer dequeued + submitted it, not just enqueued).
            task_id_1 = cache_manager.generate_task_id()
            assert runtime.inflight_registry.try_register("transcription", task_id_1) is True
            cache_manager.create_task(task_id=task_id_1, url="https://example.com/blocked")
            await runtime.task_queue.put({"id": task_id_1, "url": "https://example.com/blocked"})

            assert await _wait_until(worker_started.is_set, timeout=10), (
                "worker never started running"
            )

            # The old bug: the asyncio.Queue itself is already empty here
            # (the consumer dequeued the item), so a queue-occupancy-based
            # cap would let more admissions through. The registry must
            # still show the slot occupied, because release only happens
            # when the *worker* finishes, not when it's dequeued.
            assert runtime.task_queue.qsize() == 0
            assert runtime.inflight_registry.size("transcription") == 1

            # Admission #2 at the same capacity must be rejected while the
            # worker is still blocked -- this is the "阻塞全部 worker 后持续
            # 提交 -> 稳定 503" invariant at the registry level (the route
            # layer turns this False into an actual 503, covered by
            # test_api_routes.py::TestTranscribeQueueBackpressure).
            task_id_2 = cache_manager.generate_task_id()
            assert runtime.inflight_registry.try_register("transcription", task_id_2) is False

            # The executor must never have received more than the one
            # admitted job -- backlog stays bounded at the registered
            # capacity, closing the finding-a bug (transcription.py:305/
            # 361/377: consumer submits to an unbounded executor the
            # instant it dequeues, regardless of admission).
            assert submit_count == 1

            # Unblock the worker; its future completing must release the
            # slot via track_future's completion callback.
            release_worker.set()

            assert await _wait_until(
                lambda: runtime.inflight_registry.size("transcription") == 0, timeout=10,
            ), "registry slot was never released after the worker finished"
            assert runtime.inflight_registry.try_register("transcription", task_id_2) is True
        finally:
            processor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await processor

    try:
        asyncio.run(scenario())
    finally:
        unbind_runtime(token)
        runtime.executor.shutdown(wait=False, cancel_futures=True)


def test_submit_failure_releases_slot_even_when_update_task_status_raises(tmp_path, monkeypatch):
    """Y1 (PR3 review hardening 加固轮): process_task_queue's submit-exception
    branch (transcription.py's `except Exception as exc:` right after
    `executor.submit(...)`) must release the "transcription" inflight slot
    even when the subsequent FAILED-status CAS write itself raises.

    Before the fix, `cache_manager.update_task_status(task_id, FAILED, ...)`
    ran *before* `inflight_registry.release(...)`, both unguarded by a
    shared try/except or finally. If update_task_status raised, execution
    jumped straight out of the except block -- the release call further
    down was never reached. Because executor.submit() itself failed, no
    future was ever created, so track_future's completion callback (the
    only other release path) never fires either -- the slot leaks
    permanently. Repeated submit failures (e.g. executor exhaustion during
    an incident) would silently drain the "transcription" bucket's capacity
    to zero, making /api/transcribe return 503 forever until a restart.

    This test forces exactly that double failure (submit() raises, and the
    FAILED-status write inside the except handler also raises) and asserts
    the slot is freed anyway.
    """
    from video_transcript_api.api.context import RuntimeContext, bind_runtime, unbind_runtime
    from video_transcript_api.api.services import transcription
    from video_transcript_api.utils.task_status import TaskStatus

    config = _minimal_config(tmp_path, queue_size=2, max_workers=1)
    runtime = RuntimeContext(config)
    runtime.start()
    token = bind_runtime(runtime)

    def failing_submit(*args, **kwargs):
        raise RuntimeError("synthetic submit failure")

    monkeypatch.setattr(runtime.executor, "submit", failing_submit)

    real_update_task_status = runtime.cache_manager.update_task_status

    def flaky_update_task_status(task_id, status, *args, **kwargs):
        # Let the PROCESSING transition (written before submit is even
        # attempted) go through normally; only the FAILED write inside the
        # submit-exception handler is made to fail, so the test exercises
        # exactly the ordering bug described above without preventing the
        # scenario from reaching that handler in the first place.
        if status == TaskStatus.FAILED:
            raise RuntimeError("synthetic update_task_status failure")
        return real_update_task_status(task_id, status, *args, **kwargs)

    monkeypatch.setattr(runtime.cache_manager, "update_task_status", flaky_update_task_status)

    async def _wait_until(predicate, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return False

    async def scenario():
        cache_manager = runtime.cache_manager
        processor = asyncio.create_task(transcription.process_task_queue())
        try:
            task_id = cache_manager.generate_task_id()
            assert runtime.inflight_registry.try_register("transcription", task_id) is True
            cache_manager.create_task(task_id=task_id, url="https://example.com/submit-fails")
            await runtime.task_queue.put({"id": task_id, "url": "https://example.com/submit-fails"})

            assert await _wait_until(
                lambda: runtime.inflight_registry.size("transcription") == 0, timeout=10,
            ), (
                "submit() failure combined with a failing FAILED-status CAS "
                "write must not leak the transcription admission slot"
            )
            # The freed slot must actually be usable by a subsequent admission,
            # not just reporting zero while still internally wedged.
            task_id_2 = cache_manager.generate_task_id()
            assert runtime.inflight_registry.try_register("transcription", task_id_2) is True
        finally:
            processor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await processor

    try:
        asyncio.run(scenario())
    finally:
        unbind_runtime(token)
        runtime.executor.shutdown(wait=False, cancel_futures=True)


def test_submit_and_failed_write_double_failure_still_calls_task_done_and_keeps_pumping(
    tmp_path, monkeypatch
):
    """G1 (CI review round 2, major): the sibling Y1 test above
    (test_submit_failure_releases_slot_even_when_update_task_status_raises)
    already proves the inflight slot is released when both submit() and the
    subsequent FAILED-status CAS write raise. What it doesn't cover: this
    fix additionally makes the FAILED-write exception re-raise (previously
    only logged and swallowed) instead of being silently absorbed --
    "repository 清理/保存失败必须抛错". This is only safe here because
    `task_queue.task_done()` lives in a `finally:` enclosing the whole
    except block (unlike llm_ops.py's process_llm_queue pump, where the
    equivalent site deliberately stays log-only because its task_done() is
    NOT finally-protected -- re-raising there would skip it and leak queue
    bookkeeping). This test drives TWO tasks through the same double-failure
    and asserts:
    (1) task_done() still fires for both despite the re-raise -- if the fix
        had mis-scoped the raise outside the finally-protected region,
        asyncio.Queue.join() below would hang and the wait_for would time
        out;
    (2) the pump survives past the first double-failure and keeps consuming
        (both admission slots end up released, not just the first)."""
    from video_transcript_api.api.context import RuntimeContext, bind_runtime, unbind_runtime
    from video_transcript_api.api.services import transcription
    from video_transcript_api.utils.task_status import TaskStatus

    config = _minimal_config(tmp_path, queue_size=2, max_workers=1)
    runtime = RuntimeContext(config)
    runtime.start()
    token = bind_runtime(runtime)

    def failing_submit(*args, **kwargs):
        raise RuntimeError("synthetic submit failure")

    monkeypatch.setattr(runtime.executor, "submit", failing_submit)

    real_update_task_status = runtime.cache_manager.update_task_status

    def flaky_update_task_status(task_id, status, *args, **kwargs):
        if status == TaskStatus.FAILED:
            raise RuntimeError("synthetic update_task_status failure")
        return real_update_task_status(task_id, status, *args, **kwargs)

    monkeypatch.setattr(runtime.cache_manager, "update_task_status", flaky_update_task_status)

    async def scenario():
        cache_manager = runtime.cache_manager
        processor = asyncio.create_task(transcription.process_task_queue())
        try:
            task_ids = []
            for i in range(2):
                task_id = cache_manager.generate_task_id()
                task_ids.append(task_id)
                assert runtime.inflight_registry.try_register("transcription", task_id) is True
                cache_manager.create_task(
                    task_id=task_id, url=f"https://example.com/submit-fails-{i}"
                )
                await runtime.task_queue.put(
                    {"id": task_id, "url": f"https://example.com/submit-fails-{i}"}
                )

            # If the re-raise had skipped task_done() (wrong finally scoping),
            # this would hang and the wait_for would time out.
            await asyncio.wait_for(runtime.task_queue.join(), timeout=10)

            for task_id in task_ids:
                assert task_id not in runtime.inflight_registry.all_task_ids(), (
                    f"{task_id}'s admission slot must be released -- a "
                    "live-lock from the re-raise breaking the pump loop "
                    "would leave later items' slots still held"
                )
        finally:
            processor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await processor

    try:
        asyncio.run(scenario())
    finally:
        unbind_runtime(token)
        runtime.executor.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# Runtime reconciliation integration (local codex review round 13, sole
# finding 1): a task registered via register_internal("llm", ...) -- the
# transcription-to-LLM internal handoff -- must be excluded from
# CacheManager.reconcile_runtime_orphaned_tasks exactly like one admitted
# via try_register, even though it never went through an HTTP admission
# point. Before this fix, none of transcription.py's five internal
# llm_task_queue.put() call sites registered anything at all, so a handed-
# off task that outlived the grace period (RUNTIME_RECONCILE_GRACE_SECONDS,
# currently 10800s) while legitimately queued/executing in the LLM stage
# would be misclassified as an orphan and CAS'd to failed -- and the
# eventual real LLM success would then lose that CAS race against an
# already-terminal failed row (a normal task's terminal-status write is
# compare-and-set once-only; see CacheManager.update_task_status's
# terminal-stickiness).
# ---------------------------------------------------------------------------


class TestRegisterInternalProtectsAgainstRuntimeReconciliation:
    def _teardown(self, runtime, token):
        from video_transcript_api.api.context import unbind_runtime

        unbind_runtime(token)
        # wait=True (unlike the pre-existing blocked-worker test above,
        # which genuinely has an in-flight job to abandon): these tests
        # never submit any work to runtime's executors, so there is nothing
        # to wait for -- waiting keeps teardown deterministic and avoids a
        # worker thread from a `wait=False` shutdown still exiting after
        # this test function returns and polluting an unrelated later
        # test's threading.active_count() snapshot (e.g.
        # test_runtime_lifecycle.py's side-effect-free checks).
        runtime.executor.shutdown(wait=True, cancel_futures=True)
        runtime.llm_executor.shutdown(wait=True, cancel_futures=True)
        runtime.maintenance_executor.shutdown(wait=True, cancel_futures=True)

    def test_handed_off_task_past_grace_period_survives_reconciliation(self, tmp_path):
        """The exact scenario finding 1 describes: a task has been handed
        off to the LLM stage (register_internal("llm", ...) called, as
        transcription.py now does immediately before every
        llm_task_queue.put() -- see _register_llm_handoff) and is
        legitimately still calibrating well past the runtime-reconciliation
        grace period (e.g. a slow LLM backend). Before this fix nothing
        registered the handoff at all, so the reconcile call below would
        have returned 1 (misclassified as an orphan, CAS'd to failed) --
        reproduced by temporarily reverting context.py/transcription.py and
        re-running this test, which then fails with `assert 1 == 0`."""
        from video_transcript_api.api.context import RuntimeContext, bind_runtime
        from video_transcript_api.cache.cache_manager import RUNTIME_RECONCILE_GRACE_SECONDS
        from video_transcript_api.utils.task_status import TaskStatus
        import datetime

        runtime = RuntimeContext(_minimal_config(tmp_path))
        runtime.start()
        token = bind_runtime(runtime)
        try:
            cm = runtime.cache_manager
            task_id = cm.create_task(url="https://example.com/handoff")["task_id"]
            cm.update_task_status(task_id, TaskStatus.CALIBRATING)

            # Simulate the internal handoff: transcription.py's worker
            # thread registers into the "llm" bucket immediately before
            # llm_task_queue.put(), while its "transcription" slot is still
            # held (not exercised here -- that ordering is covered at the
            # real call sites by tests/features/test_transcription_flow_
            # regression.py::TestLlmHandoffRegistersBeforePut).
            runtime.inflight_registry.register_internal("llm", task_id)

            # Backdate created_at well past the grace period.
            now = datetime.datetime.now(datetime.timezone.utc)
            old_created_at = (
                now - datetime.timedelta(seconds=RUNTIME_RECONCILE_GRACE_SECONDS + 60)
            ).strftime("%Y-%m-%d %H:%M:%S")
            with cm._get_cursor() as cursor:
                cursor.execute(
                    "UPDATE task_status SET created_at = ? WHERE task_id = ?",
                    (old_created_at, task_id),
                )

            # app.py::_periodic_maintenance's exact call shape.
            count = cm.reconcile_runtime_orphaned_tasks(
                exclude_task_ids=runtime.inflight_registry.all_task_ids(), now=now,
            )

            assert count == 0
            assert cm.get_task_by_id(task_id)["status"] == "calibrating"

            # The LLM future eventually completes (success) -- track_future's
            # completion callback releases the "llm" bucket slot exactly as
            # production does (llm_ops.process_llm_queue's runtime.
            # track_future(future, kind="llm", task_id=...) call).
            #
            # acquire llm_submit_semaphore first (local codex review round
            # 14): production only ever calls track_future(kind="llm", ...)
            # right after a real llm_executor.submit(), itself gated by a
            # prior runtime.llm_submit_semaphore.acquire() in process_llm_
            # queue -- track_future's completion callback release()s that
            # same semaphore unconditionally for kind="llm". Skipping the
            # acquire here would make the callback's release() exceed the
            # BoundedSemaphore's initial value (nothing was ever checked
            # out), which raises ValueError -- silently swallowed by
            # concurrent.futures.Future._invoke_callbacks() (logged, not
            # re-raised to set_result()'s caller) rather than failing this
            # assertion, so the test would still pass while inaccurately
            # simulating production and leaving a noisy error in the log.
            assert runtime.llm_submit_semaphore.acquire(blocking=False)
            future = concurrent.futures.Future()
            runtime.track_future(future, kind="llm", task_id=task_id)
            cm.update_task_status(task_id, TaskStatus.SUCCESS)
            future.set_result(None)

            assert runtime.inflight_registry.size("llm") == 0
            # Terminal rows are never reconciled regardless of registry
            # state -- reconcile only ever touches non-terminal rows.
            assert cm.reconcile_runtime_orphaned_tasks(
                exclude_task_ids=runtime.inflight_registry.all_task_ids(), now=now,
            ) == 0
            assert cm.get_task_by_id(task_id)["status"] == "success"
        finally:
            self._teardown(runtime, token)

    def test_truly_orphaned_task_still_converges_after_release(self, tmp_path):
        """Complements the test above: once the "llm" bucket slot is
        released (LLM future completed) *without* the task ever reaching a
        terminal DB status (e.g. the process crashed between release and
        the terminal write), reconciliation must still catch it once it
        ages past the grace period -- release alone does not grant
        permanent immunity, only presence in the registry does."""
        from video_transcript_api.api.context import RuntimeContext, bind_runtime
        from video_transcript_api.cache.cache_manager import RUNTIME_RECONCILE_GRACE_SECONDS
        from video_transcript_api.utils.task_status import TaskStatus
        import datetime

        runtime = RuntimeContext(_minimal_config(tmp_path))
        runtime.start()
        token = bind_runtime(runtime)
        try:
            cm = runtime.cache_manager
            task_id = cm.create_task(url="https://example.com/orphan")["task_id"]
            cm.update_task_status(task_id, TaskStatus.CALIBRATING)

            runtime.inflight_registry.register_internal("llm", task_id)
            # Released without ever reaching a terminal write -- e.g. the
            # process crashed mid-LLM-stage.
            runtime.inflight_registry.release("llm", task_id)

            now = datetime.datetime.now(datetime.timezone.utc)
            old_created_at = (
                now - datetime.timedelta(seconds=RUNTIME_RECONCILE_GRACE_SECONDS + 60)
            ).strftime("%Y-%m-%d %H:%M:%S")
            with cm._get_cursor() as cursor:
                cursor.execute(
                    "UPDATE task_status SET created_at = ? WHERE task_id = ?",
                    (old_created_at, task_id),
                )

            count = cm.reconcile_runtime_orphaned_tasks(
                exclude_task_ids=runtime.inflight_registry.all_task_ids(), now=now,
            )

            assert count == 1
            assert cm.get_task_by_id(task_id)["status"] == "failed"
        finally:
            self._teardown(runtime, token)


# ---------------------------------------------------------------------------
# Internal-handoff backpressure (local codex review round 13, finding 2):
# transcription.py's internal llm_task_queue.put() calls are blocking
# (queue.Queue.put(), the default block=True/timeout=None) -- when the LLM
# stage cannot keep up and the queue fills to its maxsize (LLM_QUEUE_
# MAXSIZE), further put() calls block the calling *transcription* worker
# thread. Because that thread does not return until put() succeeds, its
# "transcription"-bucket admission slot stays occupied the whole time it is
# blocked -- backpressure chains upstream to the transcription bucket (and
# from there to /api/transcribe's 503) without register_internal itself
# needing to reject anything.
# ---------------------------------------------------------------------------


class TestInternalHandoffBackpressure:
    """Exercises the chain above directly with a real queue.Queue and real
    blocked threads (no consumer draining it, standing in for "the whole
    LLM stage is stalled") rather than through the full process_
    transcription()/process_llm_queue() machinery, to keep the scenario
    deterministic and fast.

    Scope note (see register_internal's docstring for the full derivation):
    this proves the *transient* bound -- entries counted while their
    originating transcription worker is still attempting the handoff
    (registered, put() in flight or already queued) cannot exceed
    LLM_QUEUE_MAXSIZE + the transcription bucket's own capacity, because
    only that many workers can simultaneously be attempting a handoff at
    all (gated upstream by try_register("transcription", ...)). It does
    NOT, and cannot, bound the *sustained* backlog once a handed-off task's
    transcription worker has already returned (transcription slot released)
    while the item is still sitting in llm_executor's own unbounded
    internal work queue awaiting a free worker thread -- that residual gap
    (finding 2's "LLM 消费者出队即 submit 无界 executor" observation) was
    left open by round 13; register_internal deliberately did not attempt
    to close it (capacity clamping stayed at the two HTTP admission points;
    internal handoffs "cannot fail" by design).

    Round 14 update: the residual *sustained*-backlog gap this class
    documents is now closed, but not here and not by register_internal --
    it is closed on the consumption side, by process_llm_queue acquiring a
    new dedicated RuntimeContext.llm_submit_semaphore (capacity =
    LLM_QUEUE_MAXSIZE) right after dequeue, before every submit; the
    semaphore is released when the future completes (track_future's
    completion callback, kind="llm" branch). See llm_ops.process_llm_queue's
    docstring and register_internal's "数学上界" section for the fix, and
    RuntimeContext.__init__'s llm_submit_semaphore comment for why this is
    a *separate* counter rather than comparing inflight_registry's own
    size("llm") against its configured capacity: an early version of this
    fix tried exactly that and round 14 found it deadlocks -- size("llm")
    conflates "still queued, consumer hasn't touched it yet" with "already
    submitted, not yet complete", so a burst of register_internal handoffs
    can push size("llm") over capacity before the consumer has ever
    submitted a single future; with nothing in flight yet, nothing can ever
    complete to free a slot, and the gate never opens. A dedicated
    semaphore starts "empty" (no pressure from backlog size) so the first
    acquire always succeeds immediately, regardless of how large the
    registry's backlog already is.

    This class's own test below is unaffected and still valid: it exercises
    the registry in isolation (a bare queue.Queue standing in for
    llm_task_queue, no real process_llm_queue consumer) to pin down the
    pre-gate *transient* bound specifically. See TestLlmQueuePumpCapacityGate
    in tests/integration/test_llm_stage_terminal_state.py for gate-level
    unit coverage (including a reproduction-shaped test proving a saturated
    semaphore alone withholds submission, and the shutdown-while-waiting
    path), and
    test_llm_pump_gate_bounds_sustained_backlog_and_chains_backpressure_to_transcription
    below for the full, real-thread, real-process_llm_queue end-to-end proof
    that the sustained backlog is now bounded too."""

    def test_handoff_registrations_stay_bounded_while_queue_and_workers_are_saturated(self):
        import queue as queue_module
        from video_transcript_api.api.context import _InflightTaskRegistry

        transcription_capacity = 3
        llm_queue_maxsize = 2
        registry = _InflightTaskRegistry({"transcription": transcription_capacity, "llm": 100})
        llm_task_queue = queue_module.Queue(maxsize=llm_queue_maxsize)

        # Admit `transcription_capacity` workers -- the real upstream gate
        # (api/routes/tasks.py's try_register("transcription", ...)) that
        # bounds how many can simultaneously attempt an internal handoff.
        task_ids = [f"t{i}" for i in range(transcription_capacity)]
        for task_id in task_ids:
            assert registry.try_register("transcription", task_id) is True

        released_barrier = threading.Barrier(transcription_capacity + 1)

        def handoff(task_id):
            # Mirrors _register_llm_handoff followed immediately by
            # llm_task_queue.put() in transcription.py: register before the
            # (possibly blocking) put().
            registry.register_internal("llm", task_id)
            released_barrier.wait(timeout=5)
            # X2 修复（PR3 review hardening 三轮）：原来这里是无超时的
            # put()，且整个测试函数体连一个 try/finally 都没有——下面任意
            # 一条断言（第 789-796 行）先失败，这个线程（非 daemon）就会
            # 永远卡在这里（队列满了、没有任何 finally 逻辑去排空它），
            # 解释器再也无法退出，CI 只会静默超时、看不到任何断言输出。
            # 10s 远大于正常路径下排空所需的时间（下面紧接着就会主动
            # get() 排空），只是给"断言真的失败了"这种情况一个确定性
            # 上限——超时后放弃这一项，让线程正常退出。
            try:
                llm_task_queue.put(task_id, timeout=10)
            except queue_module.Full:
                return

        threads = [threading.Thread(target=handoff, args=(tid,)) for tid in task_ids]
        for t in threads:
            t.start()
        try:
            released_barrier.wait(timeout=5)

            # No consumer is draining llm_task_queue (the whole LLM stage is
            # stalled) -- wait for it to actually fill to capacity.
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and llm_task_queue.qsize() < llm_queue_maxsize:
                time.sleep(0.01)

            # All three workers have registered -- register_internal never
            # blocks or fails, unlike put(). At most llm_queue_maxsize of
            # them got past put(); the remainder are blocked, still holding
            # their "transcription" slot.
            assert registry.size("llm") == transcription_capacity
            assert registry.size("llm") <= llm_queue_maxsize + transcription_capacity
            assert llm_task_queue.qsize() == llm_queue_maxsize
            assert registry.size("transcription") == transcription_capacity
            # The backpressure chain in action: a 4th admission attempt sees
            # the transcription bucket still full because the blocked
            # worker(s) have not returned.
            assert registry.try_register("transcription", "overflow") is False
        finally:
            # X2 修复：无论上面的断言是否通过，都无条件排空队列——这是
            # 解除所有仍阻塞在 put() 里的 handoff() 线程的唯一确定性方式
            # （put() 的 10s 超时只是最后兜底，不应该依赖它才能收尾）。
            # 按声明容量精确排空、每次 get() 都带超时（而不是"get_nowait()
            # 直到 Empty 就退出"）：Queue.put()/get() 共享同一把内部锁，
            # "移除一项 -> 被阻塞的 put() 醒来并真正入队"之间存在极短的
            # 重新竞争锁的窗口，用一次 get_nowait() 探测容易在这个窗口内
            # 误判为空、提前退出，把本该被排空、解除阻塞的线程漏掉。
            for _ in range(transcription_capacity):
                try:
                    llm_task_queue.get(timeout=10)
                except queue_module.Empty:
                    break
            for t in threads:
                t.join(timeout=10)
                assert not t.is_alive(), "handoff thread failed to exit during teardown"

        # Simulate each task's LLM future eventually completing (the real
        # release hook, track_future(kind="llm", task_id=...)) and the
        # transcription worker itself returning (releasing its
        # "transcription" slot) -- registrations fall back to zero.
        for task_id in task_ids:
            registry.release("llm", task_id)
            registry.release("transcription", task_id)

        assert registry.size("llm") == 0
        assert registry.size("transcription") == 0


# ---------------------------------------------------------------------------
# End-to-end proof (local codex review round 14) that the *sustained*-backlog
# gap TestInternalHandoffBackpressure's docstring documents is now closed --
# not by register_internal (it still, deliberately, never checks capacity),
# but by process_llm_queue's new consumption-pump capacity gate. Unlike the
# test above (a bare queue.Queue, no real consumer), this drives a real
# RuntimeContext, a real process_llm_queue background thread, and a real
# (deliberately stalled) llm_executor worker, mirroring test_inflight_
# registry.py's own test_blocked_worker_bounds_admission_and_release_frees_
# a_slot for the transcription side.
# ---------------------------------------------------------------------------


def test_llm_pump_gate_bounds_sustained_backlog_and_chains_backpressure_to_transcription(
    tmp_path, monkeypatch,
):
    """持续到达的 transcription->llm 交接（多个并发"生产者"线程反复
    register_internal + 真实 llm_queue.put()，直接模拟持续过载下
    "transcription" 桶高频周转产生的一连串 distinct task_id，绕开完整
    transcription 流水线，与上面 TestInternalHandoffBackpressure 同一套
    简化手法）+ 唯一的 LLM worker 永久阻塞，此前会让 "已提交未完成" 总量
    无限增长（见 TestInternalHandoffBackpressure 类文档"残余 gap"一节）。

    这里证明新增的消费泵闸门（RuntimeContext.llm_submit_semaphore，见其
    注释与 llm_ops.process_llm_queue 的 docstring）堵住了这个口子：
    - inflight_registry.size("llm")（"排队中+已提交未完成"合计，登记表
      的既有语义）即使在持续的新增交接压力下也会在短时间内 plateau
      （不再继续增长），且远小于总共尝试的交接次数；
    - llm_queue 真的被填满到 maxsize（不再像此前那样几乎永远清空）；
    - 填满之后，新的 put() 真的阻塞——背压链闭合到"生产者"（对应真实
      transcription worker）；
    - 唯一的 LLM worker 恢复后，积压被彻底消化，所有阻塞的 put() 都能
      返回，每个交接最终都被处理且只处理一次。
    """
    import queue as queue_module
    from video_transcript_api.api import context as context_module
    from video_transcript_api.api.context import RuntimeContext
    from video_transcript_api.api.services import llm_ops

    # Small LLM_QUEUE_MAXSIZE so the scenario is fast and deterministic --
    # the real llm_queue's maxsize and llm_submit_semaphore's capacity are
    # both derived from this constant at RuntimeContext construction/
    # start() time (see LLM_QUEUE_MAXSIZE's own module docstring).
    llm_capacity = 2
    monkeypatch.setattr(context_module, "LLM_QUEUE_MAXSIZE", llm_capacity)

    config = _minimal_config(tmp_path, queue_size=1000, max_workers=1)
    runtime = RuntimeContext(config)
    runtime.start()

    # Stand-in for the single LLM worker: blocks forever on the first call
    # (simulating a stalled LLM backend) until release_event fires, after
    # which every call (including ones already queued up behind it) returns
    # immediately -- models "the worker recovers and rips through backlog".
    release_event = threading.Event()
    processed_order = []
    processed_lock = threading.Lock()

    def blocking_handle_llm_task(llm_task):
        with processed_lock:
            processed_order.append(llm_task.get("task_id"))
        release_event.wait()

    monkeypatch.setattr(llm_ops, "_handle_llm_task", blocking_handle_llm_task)

    # Matches app.py's real production wiring for the LLM consumer thread
    # (threading.Thread(target=run_with_runtime, args=(runtime,
    # process_llm_queue))) -- contextvars set via bind_runtime in the test's
    # own thread do NOT propagate to a plain threading.Thread, so the target
    # itself must bind the runtime from inside the new thread.
    pump_thread = threading.Thread(
        target=llm_ops.run_with_runtime, args=(runtime, llm_ops.process_llm_queue),
    )
    pump_thread.start()

    producer_count = 3
    iterations_per_producer = 5
    total_handoffs = producer_count * iterations_per_producer

    def producer(idx):
        for j in range(iterations_per_producer):
            task_id = f"handoff-{idx}-{j}"
            runtime.inflight_registry.register_internal("llm", task_id)
            # X2 修复（PR3 review hardening 三轮）：原来这里是无超时的
            # put()——一旦下面任何一条断言在 release_event.set()（把唯一的
            # LLM worker 从永久阻塞里解出来）之前失败，这个线程（非
            # daemon）就可能永远卡在这里，解释器再也无法退出，CI 只会
            # 静默超时、看不到任何断言输出。10s 远大于正常路径下排空所需
            # 的时间（release 之后 pump 在毫秒/秒级就能追上），只是给
            # "断言真的失败了、finally 兜底也没能及时排空"这种极端情况
            # 一个确定性上限——超时后放弃这一项，让线程正常退出，交给
            # finally 的 join(timeout=...) + is_alive 断言如实报告异常
            # 状况，而不是让线程永远卡住。
            try:
                runtime.llm_queue.put({"task_id": task_id}, timeout=10)
            except queue_module.Full:
                return

    producers = [threading.Thread(target=producer, args=(i,)) for i in range(producer_count)]

    try:
        for t in producers:
            t.start()

        # Wait for the scenario to reach steady state: the sole worker stuck
        # on the first item, the queue genuinely full, and (since nothing
        # ever drains it) every producer eventually wedged inside a put()
        # call it can never return from on its own.
        def _settled():
            return runtime.llm_queue.qsize() == llm_capacity and any(
                t.is_alive() for t in producers
            )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _settled():
            time.sleep(0.02)
        assert _settled(), "scenario never reached the expected steady state"
        # Give any producer still mid-registration a moment to also wedge on
        # put() before sampling -- avoids a flaky read mid-transition.
        time.sleep(0.3)

        # The core invariant: "submitted but not completed" work (which
        # size("llm") tracks end-to-end, from register_internal all the way
        # to the LLM future completing) must plateau at a small, fixed value
        # -- not keep climbing toward total_handoffs the way it would
        # pre-fix (see TestInternalHandoffBackpressure's docstring "残余
        # gap"). With no worker progress, nothing can free a slot, so once
        # settled the count is deterministic.
        samples = [runtime.inflight_registry.size("llm")]
        for _ in range(9):
            time.sleep(0.05)
            samples.append(runtime.inflight_registry.size("llm"))
        assert max(samples) < total_handoffs, (
            f"registry size must stay well below the {total_handoffs} total "
            f"attempted handoffs, not climb toward it: samples={samples}"
        )
        assert max(samples) == min(samples), (
            f"with the sole worker permanently stalled, the backlog must "
            f"plateau, not keep growing: samples={samples}"
        )

        # Real backpressure reached upstream: llm_queue is genuinely full,
        # and producer threads are genuinely blocked inside put() (not just
        # "haven't gotten around to it yet").
        assert runtime.llm_queue.qsize() == llm_capacity
        assert any(t.is_alive() for t in producers)

        # The stalled worker recovers -- the whole backlog drains as the
        # pump's gate keeps reopening and resubmitting.
        release_event.set()

        def _drained():
            return (
                runtime.inflight_registry.size("llm") == 0
                and runtime.llm_queue.qsize() == 0
                and all(not t.is_alive() for t in producers)
            )

        # Generous but not tight: llm_submit_semaphore.acquire(timeout=0.2)
        # is a real threading.Semaphore -- unlike a plain Event, its
        # internal Condition is notified by every release() (see
        # RuntimeContext.track_future's kind="llm" branch), so a blocked
        # acquire() wakes up almost immediately once a slot frees, not only
        # after the 0.2s timeout. Draining should therefore be fast even for
        # total_handoffs items; this deadline is just a safety margin.
        deadline = time.monotonic() + max(5.0, total_handoffs * 0.2)
        while time.monotonic() < deadline and not _drained():
            time.sleep(0.02)
        assert _drained(), "backlog never drained after the worker recovered"

        for t in producers:
            t.join(timeout=5)
            assert not t.is_alive()

        # Every distinct handoff was eventually processed, exactly once.
        assert sorted(processed_order) == sorted(
            f"handoff-{i}-{j}" for i in range(producer_count) for j in range(iterations_per_producer)
        )
    finally:
        # X2 修复（PR3 review hardening 三轮）：以下清理必须无条件执行，
        # 且顺序严格——上面 try 块里任意一条断言（_settled/样本/qsize/
        # 存活性/_drained…）失败都会直接跳到这里。修复前，这里只 set 了
        # llm_stop_event，从不 set release_event，也不主动排空 backlog：
        # blocking_handle_llm_task 里的 release_event.wait() 没有超时，
        # 卡在 llm_executor 里那个非 daemon 工作线程会永远运行下去
        # （executor.shutdown(wait=False) 既不等待也不能取消一个已经在
        # 跑的 future）；同时 llm_queue 仍然满着，堵在 producer() 里
        # put() 的生产者线程（同样非 daemon）也永远醒不过来（旧代码的
        # put() 还没有超时兜底）——两类线程只要有一个还活着，解释器就
        # 退不出，CI 只会静默超时、看不到任何断言输出。

        # 第一步：无条件解除唯一的 LLM worker 的阻塞（幂等，即便 try 块
        # 里已经成功 set 过一次也无妨）。
        release_event.set()

        # 第二步：有界轮询排空 backlog——不依赖 llm_stop_event 的时序
        # （stop_event 一旦被 set，pump 只会再处理当前正在进行的这一项
        # 就退出主循环，不会继续追着排空剩余 backlog）。即便上面的断言
        # 提前失败，也要让 pump 有机会把 llm_queue 和登记表清空，解除
        # 所有生产者线程；上限比 try 块里 _drained() 的正常路径宽松得多
        # （那里 total_handoffs * 0.2，这里额外乘 2.5 倍并设 10s 下限），
        # 只是兜底，不影响正常路径的执行时间。
        drain_deadline = time.monotonic() + max(10.0, total_handoffs * 0.5)
        while time.monotonic() < drain_deadline and not (
            runtime.inflight_registry.size("llm") == 0
            and runtime.llm_queue.qsize() == 0
            and all(not t.is_alive() for t in producers)
        ):
            time.sleep(0.02)

        # 第三步：backlog 排空后再关泵——此时 llm_queue 应已见底，sentinel
        # 能否真正入队不再关键（pump 主循环下一次 get(timeout=0.2) 空转
        # 就会看到 llm_stop_event 已置位并退出）。
        runtime.llm_stop_event.set()
        try:
            runtime.llm_queue.put_nowait(None)
        except queue_module.Full:
            pass

        # 第四步：有超时的 join + 显式存活性断言——不再是"join 一下就算数"
        # 的短暂等待（旧代码 pump 只等 5s、producer 只等 1s，且 producer
        # 那一路完全没有 assert not is_alive()，join 超时返回也会被当作
        # "收尾成功"悄悄放过）。这里失败会得到一条清楚的 AssertionError，
        # 而不是让线程带着"非 daemon 卡死"的状态默默留到解释器退出阶段。
        pump_thread.join(timeout=10)
        assert not pump_thread.is_alive(), "pump thread failed to exit during teardown"
        for t in producers:
            t.join(timeout=10)
            assert not t.is_alive(), "producer thread failed to exit during teardown"
        runtime.executor.shutdown(wait=False, cancel_futures=True)
        runtime.llm_executor.shutdown(wait=False, cancel_futures=True)
        runtime.maintenance_executor.shutdown(wait=False, cancel_futures=True)
