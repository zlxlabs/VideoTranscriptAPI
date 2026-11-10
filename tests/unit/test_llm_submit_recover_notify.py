"""Tests for per-user notification routing during LLM submit recovery."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.video_transcript_api.api import context
from src.video_transcript_api.api.context import _retry_terminal_write_pending
from src.video_transcript_api.api.services import terminal_status
from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.task_status import TaskStatus


@pytest.fixture
def cache_manager(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def test_compensation_drain_routes_to_submitting_users_wechat_webhook(
    cache_manager, monkeypatch
):
    user_id = "submitter-17"
    webhook = "https://notify.example/user-hook"
    task_id = cache_manager.create_task(
        url="https://example.com/video",
        submitted_by=user_id,
        notify_targets={"wechat": webhook},
    )["task_id"]
    cache_manager.update_task_status(task_id, TaskStatus.PROCESSING)

    user_manager = MagicMock()
    user_manager.get_user_by_id.return_value = {"wechat_webhook": webhook}
    monkeypatch.setattr(
        context,
        "get_runtime",
        lambda: SimpleNamespace(user_manager=user_manager),
    )
    router = MagicMock()
    router.notify_task_status.return_value = {"wechat": True}
    monkeypatch.setattr(
        terminal_status, "get_notification_router", lambda: router
    )

    still_pending = _retry_terminal_write_pending(
        cache_manager, {task_id}, logger=MagicMock()
    )

    assert still_pending == set()
    user_manager.get_user_by_id.assert_not_called()
    router.notify_task_status.assert_called_once()
    assert router.notify_task_status.call_args.kwargs["webhooks"] == {
        "wechat": webhook
    }
