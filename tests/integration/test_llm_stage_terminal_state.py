"""Integration tests: the LLM stage owns the terminal task status.

Regression coverage for the silent-failure bug: a NORMAL (non-recalibrate)
task's LLM completion/failure must write success/failed to the DB. Before the
fix, only calibrate_only tasks updated terminal status, so normal LLM failures
were silent and the task stayed stuck.

All console output must be in English only (no emoji, no Chinese).
"""

import queue
import threading
import time

import pytest
from unittest.mock import patch, MagicMock

from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.task_status import TaskStatus
from src.video_transcript_api.api.services import llm_ops


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _calibrating_task(cm):
    task_id = cm.create_task(url="https://example.com/v1")["task_id"]
    cm.update_task_status(task_id, TaskStatus.CALIBRATING)
    return task_id


def _llm_task(task_id):
    return {
        "task_id": task_id,
        "url": "https://example.com/v1",
        "display_url": "https://example.com/v1",
        "platform": "youtube",
        "media_id": "vid1",
        "video_title": "Demo",
        "author": "Alice",
        "description": "",
        "transcript": "hello world",
        "use_speaker_recognition": False,
        "is_generic": False,
        "wechat_webhook": None,
        "notification_channel": None,
        "notification_webhooks": {},
    }


def _patches(cm, coordinator):
    """Patch llm_ops module globals to isolate the state-transition logic."""
    # _save_llm_results now returns the "effective" status dict it actually
    # persisted (see layered-cache suppression logic); _handle_llm_task uses
    # that return value to refresh result_dict["stats"] before the terminal
    # update_task_status() call. return_value=None here means "no layer
    # write happened" -- the state-transition assertions in this file only
    # care about success/failed, not the honest-status fields, so this keeps
    # the isolation intent while matching the real function's contract.
    mock_save_llm_results = MagicMock(return_value=None)
    return [
        patch.object(llm_ops, "cache_manager", cm),
        patch.object(llm_ops, "llm_coordinator", coordinator),
        # _handle_llm_task calls llm_task_queue.task_done() in finally; isolate it.
        patch.object(llm_ops, "llm_task_queue", MagicMock()),
        patch.object(llm_ops, "_build_result_dict", lambda r: {}),
        patch.object(llm_ops, "_save_llm_results", mock_save_llm_results),
        patch.object(llm_ops, "_send_notification", MagicMock()),
        patch.object(llm_ops, "get_notification_router", lambda: MagicMock()),
        patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
        patch.object(llm_ops, "_prepare_llm_content", lambda t, tr, spk: "content"),
    ]


class TestLlmTerminalWriteback:
    def test_normal_task_success_sets_db_success(self, cm):
        task_id = _calibrating_task(cm)
        coordinator = MagicMock()
        coordinator.process.return_value = MagicMock()

        ctxs = _patches(cm, coordinator)
        for c in ctxs:
            c.start()
        try:
            llm_ops._handle_llm_task(_llm_task(task_id))
        finally:
            for c in ctxs:
                c.stop()

        assert cm.get_task_by_id(task_id)["status"] == "success"

    def test_normal_task_llm_failure_sets_db_failed(self, cm):
        # R2: the bug — a normal task's LLM failure must surface as failed.
        task_id = _calibrating_task(cm)
        coordinator = MagicMock()
        coordinator.process.side_effect = RuntimeError("boom")

        ctxs = _patches(cm, coordinator)
        for c in ctxs:
            c.start()
        try:
            llm_ops._handle_llm_task(_llm_task(task_id))
        finally:
            for c in ctxs:
                c.stop()

        row = cm.get_task_by_id(task_id)
        assert row["status"] == "failed"
        assert "boom" in (row["error_message"] or "")


class TestLlmTaskFailedWriteReraises:
    """G1 (CI review round 2, major): _handle_llm_task's failure closure
    (the `except Exception as exc:` guarding the LLM coordinator call) used
    to swallow the FAILED terminal write's own exception with a bare
    `except Exception: pass` -- no log, no re-raise. The task stayed stuck
    in calibrating (non-terminal) with zero observable signal short of a
    runtime reconciliation sweep (up to ~27h later) -- the most silent of
    every terminal-write-failure site in this codebase. Fix: log then
    re-raise. _handle_llm_task is the worker entry point
    process_llm_queue submits to llm_executor and tracks via
    RuntimeContext.track_future(kind="llm", task_id=...), so the re-raised
    exception now surfaces on that future -- and the future's completion
    callback still releases the "llm" inflight-registry slot and
    llm_submit_semaphore regardless (see track_future's docstring),
    independent of whether the terminal write ever succeeded."""

    class _RaisesOnFailedWrite:
        """Delegates everything to a real CacheManager except
        update_task_status(..., FAILED, ...), which raises instead."""

        def __init__(self, inner):
            self._inner = inner

        def update_task_status(self, task_id, status, **kwargs):
            if status == TaskStatus.FAILED:
                raise RuntimeError("db unavailable")
            return self._inner.update_task_status(task_id, status, **kwargs)

        def get_task_by_id(self, task_id):
            return self._inner.get_task_by_id(task_id)

    def test_failed_write_exception_propagates_and_task_done_still_called(self, cm):
        task_id = _calibrating_task(cm)
        coordinator = MagicMock()
        coordinator.process.side_effect = RuntimeError("llm boom")

        wrapped_cm = self._RaisesOnFailedWrite(cm)
        router = MagicMock()
        task_queue = MagicMock()

        ctxs = [
            patch.object(llm_ops, "cache_manager", wrapped_cm),
            patch.object(llm_ops, "llm_coordinator", coordinator),
            patch.object(llm_ops, "llm_task_queue", task_queue),
            patch.object(llm_ops, "_build_result_dict", lambda r: {}),
            patch.object(llm_ops, "_save_llm_results", MagicMock(return_value=None)),
            patch.object(llm_ops, "_send_notification", MagicMock()),
            patch.object(llm_ops, "get_notification_router", lambda: router),
            patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
            patch.object(llm_ops, "_prepare_llm_content", lambda t, tr, spk: "content"),
        ]
        for c in ctxs:
            c.start()
        try:
            with pytest.raises(RuntimeError, match="db unavailable"):
                llm_ops._handle_llm_task(_llm_task(task_id))
        finally:
            for c in ctxs:
                c.stop()

        # task_done() must still fire from the outer `finally:` even though
        # the terminal write raised and the exception propagated past it.
        task_queue.task_done.assert_called_once()
        # The best-effort failure notification must still be attempted
        # (moved into a `finally` alongside the re-raise, see the fix).
        router.send_text.assert_called_once()
        # The task row itself must still show calibrating (not failed) --
        # proof the terminal write genuinely never landed, which is exactly
        # why observability (the re-raise) matters here.
        assert cm.get_task_by_id(task_id)["status"] == "calibrating"


class TestLlmStageCasLossSuppressesNotification:
    """H2 (local codex review round 7): before this fix, _handle_llm_task
    sent the "task complete" notification BEFORE the CAS-guarded
    update_task_status(SUCCESS) write, and silently ignored its boolean
    return value. update_task_status()'s terminal stickiness means that
    return value is False whenever the task was already closed to a
    terminal state by another path (e.g. shutdown liquidation marking it
    failed after a timeout) before the LLM stage's own write lands -- so a
    user could receive a "success" notification for a task the database
    (and audit trail) actually recorded as failed, with the log line
    "任务状态已更新为 success" lying about what happened.

    Fix: reorder to CAS-write-then-notify, and only notify on a genuine
    win; log a warning with the actual terminal status on loss instead.
    """

    def _run(self, cm, task_id, mock_send_notification):
        coordinator = MagicMock()
        coordinator.process.return_value = MagicMock()

        ctxs = [
            patch.object(llm_ops, "cache_manager", cm),
            patch.object(llm_ops, "llm_coordinator", coordinator),
            # _handle_llm_task calls llm_task_queue.task_done() in finally; isolate it.
            patch.object(llm_ops, "llm_task_queue", MagicMock()),
            patch.object(llm_ops, "_build_result_dict", lambda r: {}),
            patch.object(llm_ops, "_save_llm_results", MagicMock(return_value=None)),
            patch.object(llm_ops, "_send_notification", mock_send_notification),
            patch.object(llm_ops, "get_notification_router", lambda: MagicMock()),
            patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
            patch.object(llm_ops, "_prepare_llm_content", lambda t, tr, spk: "content"),
        ]
        for c in ctxs:
            c.start()
        try:
            llm_ops._handle_llm_task(_llm_task(task_id))
        finally:
            for c in ctxs:
                c.stop()

    def test_cas_loss_suppresses_success_notification(self, cm):
        task_id = _calibrating_task(cm)
        # Simulate the task being closed elsewhere (e.g. shutdown
        # liquidation, or orphan-recovery) as failed *before* the LLM
        # stage's own terminal write lands -- update_task_status()'s
        # terminal stickiness means the LLM stage's later SUCCESS write
        # below will lose the CAS race.
        cm.update_task_status(
            task_id, TaskStatus.FAILED,
            error_message="closed by shutdown liquidation",
        )

        mock_send_notification = MagicMock()
        self._run(cm, task_id, mock_send_notification)

        # Terminal stickiness (covered elsewhere) must hold: the LLM
        # stage's SUCCESS write must not have overwritten the real outcome.
        assert cm.get_task_by_id(task_id)["status"] == "failed"
        # The actual bug this test pins down: once the CAS write loses, no
        # success notification may be sent.
        mock_send_notification.assert_not_called()

    def test_cas_win_still_sends_success_notification(self, cm):
        """Sanity/regression companion: the normal path (CAS wins) must
        still notify -- the fix must not accidentally suppress real
        completions."""
        task_id = _calibrating_task(cm)

        mock_send_notification = MagicMock()
        self._run(cm, task_id, mock_send_notification)

        assert cm.get_task_by_id(task_id)["status"] == "success"
        mock_send_notification.assert_called_once()


class TestLlmStageNotificationExceptionDoesNotFailTask:
    """K3（本地 codex review 第 8 轮）：H2 把 CAS 提到通知之前是对的，但
    _send_notification 调用此前仍在最外层通用失败处理的 try/except（本
    函数的 `except Exception as exc:`）覆盖范围内——success 已经落库后，
    _send_notification 抛出的任何异常都会被那个 except 当成"LLM 任务处理
    异常"：发一条误导性的"【LLM API调用异常】"通知，且无条件尝试把
    task_status 覆盖成 failed（即便这次覆盖会被终态黏性拒绝）。修复后
    _send_notification 有自己独立的 try/except，异常只记日志，不影响
    已经写定的 success 结果。
    """

    def _run(self, cm, task_id, router_mock):
        coordinator = MagicMock()
        coordinator.process.return_value = MagicMock()

        ctxs = [
            patch.object(llm_ops, "cache_manager", cm),
            patch.object(llm_ops, "llm_coordinator", coordinator),
            # _handle_llm_task calls llm_task_queue.task_done() in finally; isolate it.
            patch.object(llm_ops, "llm_task_queue", MagicMock()),
            patch.object(llm_ops, "_build_result_dict", lambda r: {}),
            patch.object(llm_ops, "_save_llm_results", MagicMock(return_value=None)),
            patch.object(
                llm_ops, "_send_notification",
                MagicMock(side_effect=RuntimeError("webhook timeout")),
            ),
            patch.object(llm_ops, "get_notification_router", lambda: router_mock),
            patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
            patch.object(llm_ops, "_prepare_llm_content", lambda t, tr, spk: "content"),
        ]
        for c in ctxs:
            c.start()
        try:
            llm_ops._handle_llm_task(_llm_task(task_id))
        finally:
            for c in ctxs:
                c.stop()

    def test_notification_exception_after_success_cas_does_not_fail_task(self, cm):
        task_id = _calibrating_task(cm)
        # get_notification_router() is called once at the top of
        # _handle_llm_task to build task_notifier (task_notifier.send_text
        # proxies to router.send_text) -- fix the same instance across the
        # call so the assertions below can inspect it afterwards.
        router_mock = MagicMock()

        # 只监听调用次数/参数，真正的写入仍然落到真实 CacheManager——用来
        # 证明 outer except 从未被触发：SUCCESS CAS 只被真正尝试过一次，
        # 没有第二次试图把状态覆盖成 FAILED 的写入。
        with patch.object(
            cm, "update_task_status", wraps=cm.update_task_status,
        ) as update_status_spy:
            self._run(cm, task_id, router_mock)

        # 任务结果如实反映 success 已经落库——不能因为通知失败就整体判成
        # failed。
        row = cm.get_task_by_id(task_id)
        assert row["status"] == "success"

        # 没有误导性的"【LLM API调用异常】"通知（task_notifier.send_text
        # 代理到 router_mock.send_text）。
        router_mock.send_text.assert_not_called()

        assert update_status_spy.call_count == 1
        assert update_status_spy.call_args.args[1] == TaskStatus.SUCCESS


class TestLlmStageFailureNotificationExceptionDoesNotStarveTerminalState:
    """R2 (PR3 review hardening): the failure-side counterpart to K3 above.

    Before this fix, the outer `except Exception as exc:` block sent the
    failure notification (task_notifier.send_text -> "【LLM API调用异常】")
    BEFORE writing the FAILED CAS. If the notification call itself raised
    (webhook timeout/rate-limit, same failure mode K3 already handles on the
    success side), the exception escaped the entire except block -- skipping
    the FAILED update_task_status() write below it -- and the task was left
    stuck in `calibrating` (a non-terminal status) forever, with the client
    polling indefinitely for a result that will never arrive.

    Fix: reorder to FAILED-CAS-then-notify (mirroring K3's CAS-then-notify
    ordering on the success side), with the notification wrapped in its own
    try/except that only logs.
    """

    def _run(self, cm, task_id, router_mock):
        coordinator = MagicMock()
        coordinator.process.side_effect = RuntimeError("boom")

        ctxs = [
            patch.object(llm_ops, "cache_manager", cm),
            patch.object(llm_ops, "llm_coordinator", coordinator),
            # _handle_llm_task calls llm_task_queue.task_done() in finally; isolate it.
            patch.object(llm_ops, "llm_task_queue", MagicMock()),
            patch.object(llm_ops, "_build_result_dict", lambda r: {}),
            patch.object(llm_ops, "_save_llm_results", MagicMock(return_value=None)),
            patch.object(llm_ops, "get_notification_router", lambda: router_mock),
            patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
            patch.object(llm_ops, "_prepare_llm_content", lambda t, tr, spk: "content"),
        ]
        for c in ctxs:
            c.start()
        try:
            llm_ops._handle_llm_task(_llm_task(task_id))
        finally:
            for c in ctxs:
                c.stop()

    def test_failure_notification_exception_does_not_prevent_failed_cas(self, cm):
        task_id = _calibrating_task(cm)
        # task_notifier.send_text proxies to router_mock.send_text -- raising
        # here reproduces the webhook-timeout/rate-limit failure mode this
        # fix must survive.
        router_mock = MagicMock()
        router_mock.send_text.side_effect = RuntimeError("webhook timeout")

        self._run(cm, task_id, router_mock)

        # The actual bug: the task must not be left stuck in calibrating just
        # because the failure notification itself blew up.
        row = cm.get_task_by_id(task_id)
        assert row["status"] == "failed"
        assert "boom" in (row["error_message"] or "")
        # The notification was attempted (and its exception swallowed) --
        # not skipped entirely.
        router_mock.send_text.assert_called_once()

    def test_failure_notification_still_sent_on_normal_failure(self, cm):
        """Sanity/regression companion: the reorder must not accidentally
        suppress the failure notification on the normal (non-raising)
        path."""
        task_id = _calibrating_task(cm)
        router_mock = MagicMock()

        self._run(cm, task_id, router_mock)

        row = cm.get_task_by_id(task_id)
        assert row["status"] == "failed"
        router_mock.send_text.assert_called_once()
        assert "【LLM API调用异常】" in router_mock.send_text.call_args.args[0]


class _ScriptedQueue:
    """Feeds queued LLM task dicts to process_llm_queue one at a time.

    After the last scripted task is drained, signals the stop event and
    raises queue.Empty -- this lets process_llm_queue's real while-loop exit
    on its own (via the `except queue.Empty: continue` branch followed by
    the outer `while not runtime.llm_stop_event.is_set()` check) with no
    background thread, no sleeps, and no timing races.
    """

    def __init__(self, tasks, stop_event):
        self._tasks = list(tasks)
        self._stop_event = stop_event
        self.task_done_calls = 0

    def get(self, timeout=None):
        if self._tasks:
            return self._tasks.pop(0)
        self._stop_event.set()
        raise queue.Empty

    def task_done(self):
        self.task_done_calls += 1


class _FakeInflightRegistry:
    """Minimal stand-in for RuntimeContext.inflight_registry -- these tests
    exercise process_llm_queue's submit-failure branch, which releases the
    "llm" registry slot explicitly (P1, local codex review round 12, since
    track_future's own completion-callback release never fires when
    submit() itself raises). Records calls so tests can assert on them;
    release itself is a no-op like the real registry's (idempotent, never
    raises)."""

    def __init__(self):
        self.released = []

    def release(self, kind, task_id):
        self.released.append((kind, task_id))


class _FakeRuntime:
    def __init__(self, stop_event, inflight_registry=None, llm_submit_semaphore=None):
        self.llm_stop_event = stop_event
        self.tracked_futures = []
        self.inflight_registry = (
            inflight_registry if inflight_registry is not None else _FakeInflightRegistry()
        )
        # local codex review round 14: process_llm_queue now acquires this
        # before every submit -- a large default capacity means it never
        # blocks for tests that aren't specifically exercising the gate
        # (see TestLlmQueuePumpCapacityGate below for gate-specific
        # coverage, which passes in a small-capacity semaphore explicitly).
        self.llm_submit_semaphore = (
            llm_submit_semaphore
            if llm_submit_semaphore is not None
            else threading.BoundedSemaphore(10**9)
        )
        # K1 bucket b (CI review round 3, major): mirrors RuntimeContext.
        # terminal_write_pending -- process_llm_queue's submit-failure
        # branch registers a task_id here (after task_done()) when the
        # FAILED terminal write itself also raises. Real thread-safety
        # (a lock) isn't needed in this single-threaded fake; the shape
        # (register/drain) is what process_llm_queue actually calls.
        self.terminal_write_pending = set()

    def track_future(self, future, kind=None, task_id=None):
        self.tracked_futures.append((future, kind, task_id))

    def register_terminal_write_pending(self, task_id):
        self.terminal_write_pending.add(task_id)

    def drain_terminal_write_pending(self):
        drained = set(self.terminal_write_pending)
        self.terminal_write_pending.clear()
        return drained


class _SubmitAlwaysRaisesExecutor:
    """Stands in for llm_executor when the thread pool submission itself
    fails (e.g. pool exhausted/shutting down) -- _handle_llm_task never
    runs, so it never gets a chance to write any terminal status."""

    def submit(self, *args, **kwargs):
        raise RuntimeError("thread pool exhausted")


class TestLlmQueueSubmitFailureWritesTerminalState:
    """local codex review 第 9 轮：process_llm_queue 的提交分支
    (llm_executor.submit(...) 抛异常) 此前只 logger.exception + task_done()，
    不写终态。此时 _handle_llm_task 永远不会运行，之前转录阶段写入的
    calibrating 中间态不会再被任何路径推进为终态 -- 任务永久停留在
    calibrating，客户端一直轮询。修复：提交失败分支补写 failed（带错误
    信息），且这次终态写入自身的异常被单独兜住，不让它跳过 task_done()
    或把整个队列处理器循环带崩。
    """

    def test_submit_failure_writes_failed_and_calls_task_done(self, cm):
        task_id = _calibrating_task(cm)
        stop_event = threading.Event()
        work_queue = _ScriptedQueue([_llm_task(task_id)], stop_event)
        runtime = _FakeRuntime(stop_event)
        sleep_calls = []

        ctxs = [
            patch.object(llm_ops, "cache_manager", cm),
            patch.object(llm_ops, "llm_task_queue", work_queue),
            patch.object(llm_ops, "llm_executor", _SubmitAlwaysRaisesExecutor()),
            patch.object(llm_ops, "get_runtime", lambda: runtime),
            # A crash that escapes the inner except would fall through to the
            # loop's outer `except Exception: ... time.sleep(1)` branch --
            # asserting this never fires is the proof the fix keeps the
            # failure entirely inside the inner except.
            patch.object(llm_ops, "time", MagicMock(sleep=lambda s: sleep_calls.append(s))),
        ]
        for c in ctxs:
            c.start()
        try:
            llm_ops.process_llm_queue()
        finally:
            for c in ctxs:
                c.stop()

        row = cm.get_task_by_id(task_id)
        assert row["status"] == "failed"
        assert "提交LLM任务失败" in (row["error_message"] or "")
        assert work_queue.task_done_calls == 1
        assert sleep_calls == []
        # K1 bucket b (CI review round 3, major): the terminal write
        # succeeded on the first try here -- nothing should be registered
        # for compensation.
        assert runtime.terminal_write_pending == set()

    def test_submit_failure_terminal_write_itself_raising_does_not_crash_loop(self, cm):
        """The terminal-state write triggered by a submit failure can itself
        raise (e.g. a transient DB error) -- that must not propagate past
        the inner except (which would skip task_done() and/or hit the outer
        crash-recovery branch instead of cleanly continuing to the next
        queued item).

        K1 bucket b (CI review round 3, major): this double-failure (submit
        fails AND the FAILED terminal write itself also fails) is exactly
        the scenario the bounded compensation path exists for. Before this
        fix it was pure log-only -- the task stayed stuck in a non-terminal
        state with no explicit follow-up. Now process_llm_queue must
        register the task_id into RuntimeContext.terminal_write_pending
        (after task_done(), see the production code's ordering rationale)
        so the next _periodic_maintenance pass can retry the CAS write."""
        task_id = _calibrating_task(cm)
        stop_event = threading.Event()
        work_queue = _ScriptedQueue([_llm_task(task_id)], stop_event)
        runtime = _FakeRuntime(stop_event)
        sleep_calls = []

        broken_cache = MagicMock()
        broken_cache.update_task_status.side_effect = RuntimeError("db unavailable")

        ctxs = [
            patch.object(llm_ops, "cache_manager", broken_cache),
            patch.object(llm_ops, "llm_task_queue", work_queue),
            patch.object(llm_ops, "llm_executor", _SubmitAlwaysRaisesExecutor()),
            patch.object(llm_ops, "get_runtime", lambda: runtime),
            patch.object(llm_ops, "time", MagicMock(sleep=lambda s: sleep_calls.append(s))),
        ]
        for c in ctxs:
            c.start()
        try:
            llm_ops.process_llm_queue()
        finally:
            for c in ctxs:
                c.stop()

        # task_done() must still fire even though the terminal write blew up,
        # and the loop must not have fallen into its outer crash-recovery
        # sleep -- both would mean the terminal-write exception escaped the
        # inner except instead of being contained there.
        assert work_queue.task_done_calls == 1
        assert sleep_calls == []
        broken_cache.update_task_status.assert_called_once()
        assert broken_cache.update_task_status.call_args.args[1] == TaskStatus.FAILED
        # K1 bucket b: the double failure must be registered for bounded
        # compensation, not silently dropped after the ERROR log.
        assert runtime.terminal_write_pending == {task_id}

    def test_submit_failure_continues_consuming_next_queued_item(self, cm):
        """Two submit failures in a row: the loop must not crash after the
        first, and must keep consuming (task_done()'d) the second."""
        task_id_1 = _calibrating_task(cm)
        task_id_2 = _calibrating_task(cm)
        stop_event = threading.Event()
        work_queue = _ScriptedQueue(
            [_llm_task(task_id_1), _llm_task(task_id_2)], stop_event
        )
        runtime = _FakeRuntime(stop_event)
        sleep_calls = []

        ctxs = [
            patch.object(llm_ops, "cache_manager", cm),
            patch.object(llm_ops, "llm_task_queue", work_queue),
            patch.object(llm_ops, "llm_executor", _SubmitAlwaysRaisesExecutor()),
            patch.object(llm_ops, "get_runtime", lambda: runtime),
            patch.object(llm_ops, "time", MagicMock(sleep=lambda s: sleep_calls.append(s))),
        ]
        for c in ctxs:
            c.start()
        try:
            llm_ops.process_llm_queue()
        finally:
            for c in ctxs:
                c.stop()

        assert cm.get_task_by_id(task_id_1)["status"] == "failed"
        assert cm.get_task_by_id(task_id_2)["status"] == "failed"
        assert work_queue.task_done_calls == 2
        assert sleep_calls == []


class TestLlmQueuePumpCapacityGate:
    """local codex review 第 14 轮：补第 12 轮验收标准的遗漏项 -- "LLM 已
    提交但尚未完成的工作也计入同一个明确容量限制"。process_llm_queue 此前
    出队即 submit 给无界的 llm_executor，"已提交未完成"的这部分工作不受
    任何容量约束。修复后，出队到 submit 之间新增一道闸门：acquire
    RuntimeContext.llm_submit_semaphore（容量 = LLM_QUEUE_MAXSIZE，future
    完成时 release），acquire 不到时原地等待
    (llm_submit_semaphore.acquire(timeout=0.2) 短轮询)，不消费队列下一项。

    这里直接用一个真实的 threading.BoundedSemaphore 作为
    runtime.llm_submit_semaphore（而不是 mock）：闸门逻辑的正确性依赖
    acquire/release 的真实阻塞语义，测试通过预先 acquire 掉全部名额来
    模拟"已经有 N 个 LLM future 提交但尚未完成"这一真实场景。

    没有直接拿 inflight_registry.size("llm") 与配置容量比较（第一版
    实现，第 14 轮实测中发现的死锁）：那张登记表同时统计"还在排队，
    消费者还没碰过"和"已经 submit，还没完成"两类条目，用 register_
    internal 预先占满容量来模拟"持续交接"时，消费者会在从未成功 submit
    过任何一项的情况下卡在闸门上——没有任何 future 存在，谁都无法释放
    名额，永久打不开。改用独立的信号量后，测试改为直接 acquire 掉初始
    名额来模拟饱和状态，不再需要依赖登记表，也就不会重现这个死锁——见
    tests/unit/test_inflight_registry.py::
    test_llm_pump_gate_bounds_sustained_backlog_and_chains_backpressure_to_transcription
    对这个死锁的完整复现记录，与 RuntimeContext.__init__ 里
    llm_submit_semaphore 的注释。

    process_llm_queue 在被闸门挡住时会真的阻塞（Semaphore.acquire），
    所以这里必须让它跑在一个真实的后台线程里，用有界的 deadline 轮询
    断言，而不是同步调用。"""

    def _semaphore(self, capacity):
        return threading.BoundedSemaphore(capacity)

    def _saturate(self, semaphore, n):
        """预先 acquire 掉 n 个名额，模拟"已经有 n 个 LLM future 提交但
        尚未完成"。"""
        for _ in range(n):
            assert semaphore.acquire(blocking=False)

    def _wait_until(self, predicate, *, timeout=3.0, interval=0.02):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()

    def test_pump_withholds_submit_while_semaphore_is_saturated(self):
        """capacity=1，但名额已经被预先 acquire 掉（模拟"已经有一个 LLM
        future 提交但尚未完成"）-- 闸门必须在 submit 之前挡住出队的这一
        项，既不 submit，也不 task_done()（这一项仍然"在途"，没有真正
        离开处理流程）。"""
        task_id = "gate-blocked-task"
        stop_event = threading.Event()
        work_queue = _ScriptedQueue([_llm_task(task_id)], stop_event)
        semaphore = self._semaphore(1)
        self._saturate(semaphore, 1)

        runtime = _FakeRuntime(stop_event, llm_submit_semaphore=semaphore)
        fake_executor = MagicMock()

        ctxs = [
            patch.object(llm_ops, "llm_task_queue", work_queue),
            patch.object(llm_ops, "llm_executor", fake_executor),
            patch.object(llm_ops, "get_runtime", lambda: runtime),
        ]
        for c in ctxs:
            c.start()
        thread = threading.Thread(target=llm_ops.process_llm_queue)
        try:
            thread.start()
            # The pump must dequeue (task_done_calls stays 0 -- the item is
            # not yet resolved) and then sit at the gate without submitting,
            # for as long as the semaphore stays saturated.
            time.sleep(0.5)
            assert fake_executor.submit.call_count == 0
            assert work_queue.task_done_calls == 0
        finally:
            stop_event.set()
            thread.join(timeout=3)
            assert not thread.is_alive()
            for c in ctxs:
                c.stop()

    def test_pump_submits_once_capacity_frees_up(self):
        """Companion to the test above: once release() frees a semaphore
        slot, the pump must resume and submit the withheld item -- the gate
        is a pause, not a permanent rejection."""
        task_id = "gate-resume-task"
        stop_event = threading.Event()
        work_queue = _ScriptedQueue([_llm_task(task_id)], stop_event)
        semaphore = self._semaphore(1)
        self._saturate(semaphore, 1)

        runtime = _FakeRuntime(stop_event, llm_submit_semaphore=semaphore)
        fake_executor = MagicMock()

        ctxs = [
            patch.object(llm_ops, "llm_task_queue", work_queue),
            patch.object(llm_ops, "llm_executor", fake_executor),
            patch.object(llm_ops, "get_runtime", lambda: runtime),
        ]
        for c in ctxs:
            c.start()
        thread = threading.Thread(target=llm_ops.process_llm_queue)
        try:
            thread.start()
            time.sleep(0.5)
            assert fake_executor.submit.call_count == 0

            # Simulate the one in-flight LLM future completing (the real
            # release hook is track_future's completion callback).
            semaphore.release()

            assert self._wait_until(lambda: fake_executor.submit.call_count == 1), (
                "pump never resumed submitting after capacity freed up"
            )
            assert self._wait_until(lambda: work_queue.task_done_calls == 0), (
                "a successful submit must not call task_done() itself -- that "
                "belongs to the worker's own finally block (_handle_llm_task), "
                "not the pump"
            )
            assert runtime.tracked_futures[0][1:] == ("llm", task_id)
        finally:
            stop_event.set()
            thread.join(timeout=3)
            assert not thread.is_alive()
            for c in ctxs:
                c.stop()

    def test_pump_exits_promptly_on_stop_event_while_waiting_at_gate(self):
        """关闭响应性（TDD 规格第 4 点）：泵在闸门等待中途收到 stop_event
        必须及时退出（用 llm_submit_semaphore.acquire(timeout=0.2) 短
        轮询，不是无限阻塞），已出队但放弃提交的任务不 submit、不 release
        它可能持有的登记（关闭路径的既有取舍：留给下次启动的孤儿恢复
        兜底），但要调用一次 task_done() 让队列自身的记账归零，且不应该
        凭空释放它从未真正 acquire 到的信号量名额。"""
        task_id = "gate-stop-task"
        stop_event = threading.Event()
        work_queue = _ScriptedQueue([_llm_task(task_id)], stop_event)
        semaphore = self._semaphore(1)
        self._saturate(semaphore, 1)

        runtime = _FakeRuntime(stop_event, llm_submit_semaphore=semaphore)
        fake_executor = MagicMock()

        ctxs = [
            patch.object(llm_ops, "llm_task_queue", work_queue),
            patch.object(llm_ops, "llm_executor", fake_executor),
            patch.object(llm_ops, "get_runtime", lambda: runtime),
        ]
        for c in ctxs:
            c.start()
        thread = threading.Thread(target=llm_ops.process_llm_queue)
        try:
            thread.start()
            # Let the pump actually reach the gate before pulling the plug.
            time.sleep(0.5)
            assert fake_executor.submit.call_count == 0

            stop_event.set()
            thread.join(timeout=2)
            assert not thread.is_alive(), (
                "pump must exit promptly (bounded by the 0.2s poll) while "
                "waiting at the capacity gate, not hang until process exit"
            )

            assert fake_executor.submit.call_count == 0
            assert work_queue.task_done_calls == 1
            # The pump never actually acquired a slot -- it must not have
            # spuriously released one either (that would corrupt the
            # BoundedSemaphore's accounting for the next real acquire).
            assert not semaphore.acquire(blocking=False), (
                "pump must not have released a semaphore slot it never "
                "acquired while abandoning the gate on shutdown"
            )
        finally:
            if thread.is_alive():
                thread.join(timeout=2)
            for c in ctxs:
                c.stop()
