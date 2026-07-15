"""Integration tests: the LLM stage owns the terminal task status.

Regression coverage for the silent-failure bug: a NORMAL (non-recalibrate)
task's LLM completion/failure must write success/failed to the DB. Before the
fix, only calibrate_only tasks updated terminal status, so normal LLM failures
were silent and the task stayed stuck.

All console output must be in English only (no emoji, no Chinese).
"""

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
