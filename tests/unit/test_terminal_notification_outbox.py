"""Red tests for the terminal-status notification single exit.

Milestone 1 locks CAS gating at the two historically ungated sites
(llm_ops finally-send and transcription worker top-level except).
Milestone 2 adds outbox schema, replay, and at-most-once tests.

All console output must be in English only (no emoji, no Chinese).
"""

import asyncio
import datetime
import sqlite3
import threading
from unittest.mock import MagicMock, patch

import pytest

from src.video_transcript_api.api.services import llm_ops, transcription
from src.video_transcript_api.api.services.terminal_status import (
    TERMINAL_FAILED_STATUS,
    _notification_accepted,
    deliver_pending_terminal_notifications,
    deliver_terminal_notification_dispatcher_round,
    finalize_terminal_status_and_notify,
)
from src.video_transcript_api.cache.cache_manager import (
    CacheManager,
)
from src.video_transcript_api.utils.task_status import TaskStatus


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _new_task(cm, url="https://example.com/v1"):
    return cm.create_task(url=url)["task_id"]


def _accepted_router():
    router = MagicMock()
    router.notify_task_status.return_value = {"wechat": True}
    return router


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
        "processing_options": {"calibrate": True, "summarize": True},
    }


class TestTerminalNotifyHelperCasGate:
    def test_cas_winner_sends_once(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        router = _accepted_router()

        written = finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.FAILED,
            error_message="boom",
            notify_error="【LLM API调用异常】boom",
            cache_manager=cm,
            router=router,
        )

        assert written is True
        assert cm.get_task_by_id(task_id)["status"] == "failed"
        router.notify_task_status.assert_called_once()
        assert router.notify_task_status.call_args.kwargs["status"] == TERMINAL_FAILED_STATUS
        assert "【LLM API调用异常】" in router.notify_task_status.call_args.kwargs["error"]

    def test_cas_loser_does_not_send(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.FAILED, error_message="first")
        router = _accepted_router()

        written = finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.FAILED,
            error_message="second",
            cache_manager=cm,
            router=router,
        )

        assert written is False
        router.notify_task_status.assert_not_called()
        assert cm.get_task_by_id(task_id)["error_message"] == "first"

    def test_success_write_persists_title_and_author(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        written = finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.SUCCESS,
            title="Cached Title",
            author="Cached Author",
            cache_manager=cm,
            router=_accepted_router(),
        )
        assert written is True
        row = cm.get_task_by_id(task_id)
        assert row["title"] == "Cached Title"
        assert row["author"] == "Cached Author"

    def test_deferred_delivery_stays_pending_until_dispatcher(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        router = _accepted_router()

        assert finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.SUCCESS,
            cache_manager=cm,
            router=router,
            defer_delivery=True,
        ) is True
        router.notify_task_status.assert_not_called()
        assert cm.is_terminal_notification_pending(task_id) is True

        assert deliver_pending_terminal_notifications(cm, router=router) == 1
        assert cm.is_terminal_notification_pending(task_id) is False
        router.notify_task_status.assert_called_once()

    def test_deferred_cas_loser_does_not_create_or_deliver(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.SUCCESS)
        router = _accepted_router()

        assert finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.SUCCESS,
            cache_manager=cm,
            router=router,
            defer_delivery=True,
        ) is False
        assert router.notify_task_status.call_count == 0
        assert len(cm.list_unattempted_terminal_notifications()) == 1


class TestSteadyStateDispatcherAgeGate:
    def test_fresh_deferred_row_is_not_sent_by_steady_state_round(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        router = _accepted_router()

        assert finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.SUCCESS,
            cache_manager=cm,
            router=router,
            defer_delivery=True,
        ) is True

        assert deliver_terminal_notification_dispatcher_round(
            cm, router=router,
        ) == 0
        assert cm.is_terminal_notification_pending(task_id) is True
        router.notify_task_status.assert_not_called()

    def test_aged_deferred_row_is_sent_by_steady_state_round(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        router = _accepted_router()
        assert finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.SUCCESS,
            cache_manager=cm,
            router=router,
            defer_delivery=True,
        ) is True
        with cm._get_cursor() as cursor:
            cursor.execute(
                "UPDATE task_terminal_notifications "
                "SET created_at = datetime('now', '-10 seconds') "
                "WHERE task_id = ?",
                (task_id,),
            )

        assert deliver_terminal_notification_dispatcher_round(
            cm, router=router,
        ) == 1
        assert cm.is_terminal_notification_pending(task_id) is False
        router.notify_task_status.assert_called_once()


class TestRedCLlmOpsFinallyCasGate:
    """llm_ops._handle_llm_task except used to send in a finally block
    even when the FAILED CAS lost. After the helper, a second pass over an
    already-terminal task must not emit a second status notification.
    """

    def _run_failing_llm(self, cm, task_id, router):
        coordinator = MagicMock()
        coordinator.process.side_effect = RuntimeError("boom")
        ctxs = [
            patch.object(llm_ops, "cache_manager", cm),
            patch.object(llm_ops, "llm_coordinator", coordinator),
            patch.object(llm_ops, "llm_task_queue", MagicMock()),
            patch.object(llm_ops, "_build_result_dict", lambda r: {}),
            patch.object(llm_ops, "_save_llm_results", MagicMock(return_value=None)),
            patch.object(llm_ops, "_send_notification", MagicMock()),
            patch.object(llm_ops, "get_notification_router", lambda: router),
            patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
            patch.object(llm_ops, "_prepare_llm_content", lambda t, tr, spk: "content"),
        ]
        for ctx in ctxs:
            ctx.start()
        try:
            llm_ops._handle_llm_task(_llm_task(task_id))
        finally:
            for ctx in ctxs:
                ctx.stop()

    def test_already_terminal_worker_does_not_send_second_notice(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        router = _accepted_router()

        first = finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.FAILED,
            error_message="first winner",
            notify_error="【LLM API调用异常】first",
            cache_manager=cm,
            router=router,
        )
        assert first is True
        assert router.notify_task_status.call_count == 1

        self._run_failing_llm(cm, task_id, router)

        assert cm.get_task_by_id(task_id)["status"] == "failed"
        status_notices = (
            router.notify_task_status.call_count + router.send_text.call_count
        )
        assert status_notices == 1
        assert "【LLM API调用异常】" in (
            router.notify_task_status.call_args.kwargs.get("error") or ""
        )


class TestSuccessNotificationOrder:
    def _run_success_task(self, cm, monkeypatch, router, *, calibrate_only=False):
        tracker = MagicMock()
        tracker.track.return_value.__enter__.return_value = None
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        llm_task = _llm_task(task_id)
        llm_task.update({
            "processing_options": {
                "calibrate": True,
                "summarize": True,
                "chapters": False,
            },
            "calibrate_only": calibrate_only,
            "notification_channel": "feishu",
            "notification_webhooks": {"feishu": "hook"},
            "perf_tracker": tracker,
        })
        result_dict = {"skip_summary": False, "stats": {}, "models_used": {}}
        monkeypatch.setattr(llm_ops, "cache_manager", cm)
        monkeypatch.setattr(llm_ops, "llm_task_queue", MagicMock())
        monkeypatch.setattr(llm_ops, "get_notification_router", lambda: router)
        monkeypatch.setattr(llm_ops, "_requires_llm_title", lambda *args, **kwargs: False)
        monkeypatch.setattr(llm_ops, "_prepare_llm_content", lambda *args: "content")
        monkeypatch.setattr(llm_ops, "_build_result_dict", lambda result: result_dict)
        monkeypatch.setattr(
            llm_ops,
            "_save_llm_results",
            lambda **kwargs: {
                "calibration_status": "full",
                "summary_status": "generated",
                "chapters_status": "disabled",
            },
        )
        monkeypatch.setattr(llm_ops.llm_coordinator, "process", lambda **kwargs: {})
        llm_ops._handle_llm_task(llm_task)
        return task_id

    def test_content_notification_is_queued_before_terminal_status(self, cm, monkeypatch):
        router = MagicMock()
        events = []
        router.send_long_text.side_effect = lambda *args, **kwargs: events.append("content")
        router.notify_task_status.side_effect = lambda *args, **kwargs: (
            events.append("terminal") or {"wechat": True}
        )

        self._run_success_task(cm, monkeypatch, router)

        assert events == ["content", "terminal"]
        assert router.send_long_text.call_args.kwargs["channel_name"] == "feishu"
        assert router.notify_task_status.call_args.kwargs["channel_name"] == "feishu"
        assert router.send_long_text.call_args.kwargs["webhooks"] == {"feishu": "hook"}
        assert router.notify_task_status.call_args.kwargs["webhooks"] == {"feishu": "hook"}

    def test_calibrate_only_sends_terminal_status_without_content(self, cm, monkeypatch):
        router = _accepted_router()
        send_content = MagicMock()
        monkeypatch.setattr(llm_ops, "_send_notification", send_content)

        self._run_success_task(cm, monkeypatch, router, calibrate_only=True)

        send_content.assert_not_called()
        router.notify_task_status.assert_called_once()


class TestRedCTranscriptionWorkerExceptCasGate:
    """transcription.process_task_queue run_and_finalize except used to
    ignore the CAS return and always notify. A second pass over an
    already-terminal task must not emit a second status notification.
    """

    def test_already_terminal_worker_except_does_not_send_second_notice(
        self, tmp_path, monkeypatch,
    ):
        from src.video_transcript_api.api.context import (
            RuntimeContext,
            bind_runtime,
            unbind_runtime,
        )

        config = {
            "api": {"host": "127.0.0.1", "port": 8000, "auth_token": "test-token"},
            "concurrent": {"max_workers": 1, "queue_size": 2, "llm_max_workers": 1},
            "storage": {
                "cache_dir": str(tmp_path / "cache"),
                "workspace_dir": str(tmp_path / "workspace"),
                "temp_dir": str(tmp_path / "temp"),
                "audit_db": str(tmp_path / "audit.db"),
            },
            "web": {"base_url": "http://localhost:8000"},
            "llm": {
                "api_key": "test-llm-key",
                "base_url": "http://127.0.0.1:1/v1",
                "calibrate_model": "test-calibrate-model",
                "summary_model": "test-summary-model",
            },
            "log": {"file": str(tmp_path / "app.log")},
        }
        runtime = RuntimeContext(config)
        runtime.start()
        token = bind_runtime(runtime)
        router = _accepted_router()
        worker_started = threading.Event()

        def _boom(*args, **kwargs):
            worker_started.set()
            raise RuntimeError("worker boom")

        monkeypatch.setattr(transcription, "get_notification_router", lambda: router)
        monkeypatch.setattr(transcription, "process_transcription", _boom)

        task_id = runtime.cache_manager.create_task(
            url="https://example.com/worker-except",
        )["task_id"]
        first = finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.FAILED,
            error_message="first winner",
            cache_manager=runtime.cache_manager,
            router=router,
        )
        assert first is True
        assert router.notify_task_status.call_count == 1

        async def scenario():
            processor = asyncio.create_task(transcription.process_task_queue())
            try:
                runtime.inflight_registry.try_register("transcription", task_id)
                await runtime.task_queue.put(
                    {"id": task_id, "url": "https://example.com/worker-except"}
                )
                deadline = asyncio.get_event_loop().time() + 5
                while asyncio.get_event_loop().time() < deadline:
                    if worker_started.is_set() and (
                        runtime.inflight_registry.size("transcription") == 0
                    ):
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise AssertionError(
                        "worker future did not finish; cannot judge CAS gating"
                    )
            finally:
                processor.cancel()
                try:
                    await processor
                except asyncio.CancelledError:
                    pass

        try:
            asyncio.run(scenario())
            assert runtime.cache_manager.get_task_by_id(task_id)["status"] == "failed"
            assert router.notify_task_status.call_count == 1
        finally:
            unbind_runtime(token)
            runtime.close()


class TestOutboxSchema:
    def test_task_id_unique_constraint(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.FAILED, error_message="boom")
        with cm._get_cursor() as cursor:
            cursor.execute("PRAGMA table_info(task_terminal_notifications)")
            pk_cols = [row[1] for row in cursor.fetchall() if row[5]]
            cursor.execute("PRAGMA index_list(task_terminal_notifications)")
            indexes = list(cursor.fetchall())
        assert pk_cols == ["task_id"]
        assert indexes, "expected a unique index on task_id"
        with pytest.raises(sqlite3.IntegrityError):
            with cm._get_cursor() as cursor:
                cursor.execute(
                    "INSERT INTO task_terminal_notifications (task_id, status) "
                    "VALUES (?, ?)",
                    (task_id, "failed"),
                )

    def test_outbox_schema_has_no_owner_columns(self, tmp_path):
        # I2/history lock: the owner+lease fields were deleted with the claim
        # mutex. A fresh database must not carry them, and a legacy database
        # keeping the old columns is harmless (code no longer reads them).
        manager = CacheManager(cache_dir=str(tmp_path / "cache"))
        try:
            with manager._get_cursor() as cursor:
                cursor.execute("PRAGMA table_info(task_terminal_notifications)")
                columns = [row[1] for row in cursor.fetchall()]
        finally:
            manager.close()
        assert columns == [
            "task_id", "status", "error_message", "created_at",
            "completed_at", "notified_at", "attempts",
        ]


def _assert_one_pending_and_one_notify(cm, task_id, router, *, reason):
    pending = cm.list_unattempted_terminal_notifications()
    assert [row["task_id"] for row in pending] == [task_id]
    sent = deliver_pending_terminal_notifications(cm, router=router)
    assert sent == 1
    router.notify_task_status.assert_called_once()
    kwargs = router.notify_task_status.call_args.kwargs
    assert kwargs["status"] == TERMINAL_FAILED_STATUS
    error = kwargs.get("error") or ""
    assert f"completed_at=" in error
    assert "由服务重启恢复判定" in error
    snapshot = cm.get_task_by_id(task_id)["terminal_snapshot"]
    assert snapshot.get("reason") == reason


class TestRedARecoveryPathsNotify:
    def test_recover_orphaned_tasks_enqueues_and_dispatcher_sends(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        router = _accepted_router()
        assert cm.recover_orphaned_tasks() == 1
        _assert_one_pending_and_one_notify(
            cm, task_id, router, reason="orphaned_on_startup",
        )

    def test_drain_on_shutdown_enqueues_and_dispatcher_sends(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)
        router = _accepted_router()
        assert cm.drain_non_terminal_tasks_on_shutdown() == 1
        _assert_one_pending_and_one_notify(
            cm, task_id, router, reason="shutdown_drain",
        )

    def test_reconcile_runtime_enqueues_and_dispatcher_sends(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        router = _accepted_router()
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        assert cm.reconcile_runtime_orphaned_tasks(
            grace_period_seconds=0, now=future,
        ) == 1
        _assert_one_pending_and_one_notify(
            cm, task_id, router, reason="runtime_reconcile",
        )


class TestRedBReplayIncludesCompletedAt:
    def test_replay_after_process_death_sends_original_completed_at(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        assert cm.update_task_status(
            task_id, TaskStatus.FAILED,
            error_message="Task interrupted by service restart",
        ) is True
        pending = cm.list_unattempted_terminal_notifications()
        assert len(pending) == 1
        original_completed_at = pending[0]["completed_at"]
        assert original_completed_at

        router = _accepted_router()
        # Reconstruct a dispatcher without having started one -- this is
        # the "pending written, process killed before send" window.
        sent = deliver_pending_terminal_notifications(cm, router=router)
        assert sent == 1
        error = router.notify_task_status.call_args.kwargs.get("error") or ""
        assert f"completed_at={original_completed_at}" in error


class TestRedDSentRowsAreNotResent:
    def test_replay_after_mark_sent_does_not_resend(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(
            task_id, TaskStatus.FAILED, error_message="boom",
        )
        router = _accepted_router()
        assert deliver_pending_terminal_notifications(cm, router=router) == 1
        assert router.notify_task_status.call_count == 1
        assert deliver_pending_terminal_notifications(cm, router=router) == 0
        assert router.notify_task_status.call_count == 1


def _outbox_state(cm, task_id):
    with cm._get_cursor() as cursor:
        cursor.execute(
            "SELECT notified_at, attempts "
            "FROM task_terminal_notifications "
            "WHERE task_id = ?",
            (task_id,),
        )
        row = cursor.fetchone()
    return {
        "notified_at": row["notified_at"],
        "attempts": row["attempts"],
    }


class TestSerialDeliveryIsExclusive:
    """History lock for the deleted claim mutex (rounds 3/4/5 findings).

    Mutual exclusion now comes from `_DELIVERY_LOCK`, not from a DB field:
    the inline helper path and the dispatcher both re-check `notified_at IS
    NULL` under it, so the same row cannot be sent twice within one process.
    """

    def test_row_is_delivered_once_per_pass_and_never_again(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.FAILED, error_message="boom")
        router = _accepted_router()

        assert deliver_pending_terminal_notifications(cm, router=router) == 1
        assert cm.list_unattempted_terminal_notifications() == []
        assert deliver_pending_terminal_notifications(cm, router=router) == 0
        router.notify_task_status.assert_called_once()

    def test_r12_helper_and_dispatcher_never_double_send(self, cm):
        """R12.1: the inline helper and the dispatcher are two in-process
        deliverers of the same row. Before `_DELIVERY_LOCK` the dispatcher
        could send a row the helper was already sending (no crash required).
        """
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)

        helper_in_router = threading.Event()
        release_helper = threading.Event()
        dispatcher_done = threading.Event()
        router = MagicMock()

        def _gated_notify(*args, **kwargs):
            helper_in_router.set()
            assert release_helper.wait(5), "helper gate never released"
            return {"wechat": True}

        router.notify_task_status.side_effect = _gated_notify
        helper_error = []

        def _run_helper():
            try:
                finalize_terminal_status_and_notify(
                    task_id,
                    TaskStatus.FAILED,
                    error_message="boom",
                    cache_manager=cm,
                    router=router,
                )
            except BaseException as exc:  # surfaced below
                helper_error.append(exc)

        helper_thread = threading.Thread(target=_run_helper)
        helper_thread.start()
        assert helper_in_router.wait(5), "helper never entered the notifier"

        dispatcher_sent = []

        def _run_dispatcher():
            dispatcher_sent.append(
                deliver_pending_terminal_notifications(cm, router=router)
            )
            dispatcher_done.set()

        dispatcher_thread = threading.Thread(target=_run_dispatcher)
        dispatcher_thread.start()
        dispatcher_thread.join(timeout=0.3)
        assert not dispatcher_done.is_set(), (
            "dispatcher finished while the helper held the delivery lock; "
            "the lock is not covering the send"
        )

        release_helper.set()
        helper_thread.join(timeout=5)
        dispatcher_thread.join(timeout=5)

        assert not helper_error, helper_error
        router.notify_task_status.assert_called_once()
        assert dispatcher_sent == [0], dispatcher_sent
        assert cm.is_terminal_notification_pending(task_id) is False


class TestNotificationAcceptance:
    @pytest.mark.parametrize(
        ("result", "accepted"),
        [
            (None, False),
            ({"wechat": False}, False),
            ({"wechat": True}, True),
            ({"wechat": False, "feishu": True}, True),
            ({"wechat": object()}, False),
            ({"wechat": "ok"}, False),
            ({"wechat": 1}, False),
            ({"wechat": []}, False),
            ({"wechat": {}}, False),
            ({}, False),
            ("True", False),
            ({"wechat": True}, True),
            ({"wechat": False, "feishu": True}, True),
            ({"wechat": False, "feishu": False}, False),
        ],
    )
    def test_only_dict_with_accepted_channel_is_accepted(self, result, accepted):
        assert _notification_accepted(result) is accepted

    def test_mixed_channel_acceptance_marks_row_sent(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        router = MagicMock()
        router.notify_task_status.return_value = {"wechat": False, "feishu": True}

        assert finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.FAILED,
            error_message="boom",
            cache_manager=cm,
            router=router,
        ) is True
        assert _outbox_state(cm, task_id)["notified_at"] is not None



class TestBoundedAtLeastOnceReplay:
    def test_i5_send_exception_leaves_row_replayable(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        router = MagicMock()
        router.notify_task_status.side_effect = RuntimeError("webhook down")
        finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.FAILED,
            error_message="boom",
            cache_manager=cm,
            router=router,
        )
        state = _outbox_state(cm, task_id)
        assert state["notified_at"] is None
        assert state["attempts"] == 1
        replay = _accepted_router()
        assert deliver_pending_terminal_notifications(cm, router=replay) == 1
        replay.notify_task_status.assert_called_once()

    def test_i5_all_channels_false_leaves_row_replayable(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        router = MagicMock()
        router.notify_task_status.return_value = {"wechat": False, "feishu": False}
        finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.FAILED,
            error_message="boom",
            cache_manager=cm,
            router=router,
        )
        state = _outbox_state(cm, task_id)
        assert state["notified_at"] is None
        assert state["attempts"] == 1
        replay = _accepted_router()
        assert deliver_pending_terminal_notifications(cm, router=replay) == 1
        replay.notify_task_status.assert_called_once()

    def test_i4_row_past_any_attempt_threshold_is_still_replayed(self, cm):
        # Consultant P1: `attempts < 3` used to filter permanently-False rows
        # out of the listing -> terminal written, zero notices, no trace.
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.FAILED, error_message="boom")
        failing = MagicMock()
        failing.notify_task_status.side_effect = RuntimeError("down")
        for _ in range(5):
            deliver_pending_terminal_notifications(cm, router=failing)
        state = _outbox_state(cm, task_id)
        assert state["notified_at"] is None
        assert [r["task_id"] for r in cm.list_unattempted_terminal_notifications()] == [
            task_id
        ]
        assert state["attempts"] == 5
        assert cm.count_attempted_terminal_notifications(3) == 1
        replay = _accepted_router()
        assert deliver_pending_terminal_notifications(cm, router=replay) == 1
        replay.notify_task_status.assert_called_once()
        assert _outbox_state(cm, task_id)["notified_at"] is not None

    def test_r12_limit_does_not_starve_fresh_rows_behind_stuck_head(self, cm):
        """R12.2: 21 stuck rows (attempts=5) sit at the head of created_at ASC.
        With `LIMIT 20` the newest row was never attempted at all -- listed but
        starved, which is the same silent loss I4 forbids.
        """
        stuck_ids = []
        for i in range(21):
            tid = _new_task(cm, url=f"https://example.com/stuck{i}")
            cm.update_task_status(tid, TaskStatus.FAILED, error_message="boom")
            with cm._get_cursor() as cursor:
                cursor.execute(
                    "UPDATE task_terminal_notifications SET attempts = 5, "
                    "created_at = '2020-01-01 00:00:00' WHERE task_id = ?",
                    (tid,),
                )
            stuck_ids.append(tid)

        fresh_id = _new_task(cm, url="https://example.com/fresh")
        cm.update_task_status(fresh_id, TaskStatus.FAILED, error_message="boom")
        with cm._get_cursor() as cursor:
            cursor.execute(
                "UPDATE task_terminal_notifications "
                "SET created_at = '2026-01-01 00:00:00' WHERE task_id = ?",
                (fresh_id,),
            )

        listed = cm.list_unattempted_terminal_notifications(limit=20)
        assert [r["task_id"] for r in listed][0] == fresh_id, (
            "a fresh row must outrank stuck rows within the limit"
        )
        assert len(listed) == 20
        assert fresh_id not in stuck_ids

        router = _accepted_router()
        sent = deliver_pending_terminal_notifications(cm, router=router, limit=20)
        assert sent >= 1
        assert _outbox_state(cm, fresh_id)["notified_at"] is not None, (
            "the fresh row was starved by the stuck head"
        )
        delivered_ids = {
            call.kwargs["url"] for call in router.notify_task_status.call_args_list
        }
        assert "https://example.com/fresh" in delivered_ids


class TestI6SuppressedNotificationNeverExists:
    def test_i6_suppressed_terminal_write_creates_no_outbox_row(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        router = _accepted_router()
        written = finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.FAILED,
            error_message="queue full",
            cache_manager=cm,
            router=router,
            suppress_terminal_notification=True,
        )
        assert written is True
        assert cm.get_task_by_id(task_id)["status"] == "failed"
        with cm._get_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM task_terminal_notifications "
                "WHERE task_id = ?",
                (task_id,),
            )
            assert cursor.fetchone()[0] == 0
        router.notify_task_status.assert_not_called()
        assert deliver_pending_terminal_notifications(cm, router=router) == 0
        router.notify_task_status.assert_not_called()
