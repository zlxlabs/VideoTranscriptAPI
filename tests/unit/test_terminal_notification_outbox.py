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
    deliver_pending_terminal_notifications,
    finalize_terminal_status_and_notify,
)
from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.task_status import TaskStatus


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _new_task(cm, url="https://example.com/v1"):
    return cm.create_task(url=url)["task_id"]


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
        router = MagicMock()

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
        router = MagicMock()

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
            router=MagicMock(),
        )
        assert written is True
        row = cm.get_task_by_id(task_id)
        assert row["title"] == "Cached Title"
        assert row["author"] == "Cached Author"


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
        router = MagicMock()

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
        router = MagicMock()
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
        router = MagicMock()
        assert cm.recover_orphaned_tasks() == 1
        _assert_one_pending_and_one_notify(
            cm, task_id, router, reason="orphaned_on_startup",
        )

    def test_drain_on_shutdown_enqueues_and_dispatcher_sends(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)
        router = MagicMock()
        assert cm.drain_non_terminal_tasks_on_shutdown() == 1
        _assert_one_pending_and_one_notify(
            cm, task_id, router, reason="shutdown_drain",
        )

    def test_reconcile_runtime_enqueues_and_dispatcher_sends(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        router = MagicMock()
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

        router = MagicMock()
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
        router = MagicMock()
        assert deliver_pending_terminal_notifications(cm, router=router) == 1
        assert router.notify_task_status.call_count == 1
        assert deliver_pending_terminal_notifications(cm, router=router) == 0
        assert router.notify_task_status.call_count == 1

