import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_transcript_api.api.context import install_api_error_handlers
from video_transcript_api.api.routes import tasks
from video_transcript_api.api.services.transcription import verify_token
from video_transcript_api.utils.url_parser import ParsedURL


@pytest.fixture
def request_client(monkeypatch):
    cache_manager = MagicMock()
    cache_manager.generate_task_id.return_value = "task-notify-fixture"
    cache_manager.create_task.return_value = {
        "task_id": "task-notify-fixture",
        "view_token": "view-notify-fixture",
    }
    queue = asyncio.Queue()
    router = MagicMock()
    registry = MagicMock()
    registry.try_register.return_value = True
    monkeypatch.setattr(tasks, "cache_manager", cache_manager)
    monkeypatch.setattr(tasks, "audit_logger", MagicMock())
    monkeypatch.setattr(tasks, "get_task_queue", lambda: queue)
    monkeypatch.setattr(tasks, "get_notification_router", lambda: router)
    monkeypatch.setattr(tasks, "get_inflight_registry", lambda: registry)
    monkeypatch.setattr(tasks, "acquire_for_task", lambda _task_id: True)
    monkeypatch.setattr(tasks, "release_for_task", lambda _task_id: None)
    monkeypatch.setattr(
        "video_transcript_api.utils.url_parser.URLParser.parse",
        lambda _self, url: ParsedURL(
            platform="youtube",
            video_id="notify-fixture",
            normalized_url=url,
            is_short_url=False,
            original_url=url,
        ),
    )

    app = FastAPI()
    install_api_error_handlers(app)
    app.include_router(tasks.router)
    app.dependency_overrides[verify_token] = lambda: {
        "user_id": "notify-user",
        "api_key": "fixture-key",
        "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=user",
        "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/user",
    }
    return TestClient(app), cache_manager, queue


def test_transcribe_notify_none_is_persisted_and_sent_to_worker(request_client):
    client, cache_manager, queue = request_client
    response = client.post(
        "/api/transcribe",
        json={
            "url": "https://www.youtube.com/watch?v=abc123",
            "notify": "none",
            "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=request",
        },
    )

    assert response.status_code == 200
    assert cache_manager.create_task.call_args.kwargs["notify_targets"] == {}
    queued = queue.get_nowait()
    assert queued["notification_webhooks"] == {}
    assert queued["wechat_webhook"] is None


@pytest.mark.parametrize(
    "notification_config",
    [
        {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/fixture"},
        {"channel": "other"},
    ],
)
def test_invalid_notification_config_is_rejected_with_422(
    request_client, notification_config,
):
    client, cache_manager, _queue = request_client
    response = client.post(
        "/api/transcribe",
        json={
            "url": "https://www.youtube.com/watch?v=abc123",
            "notification_config": notification_config,
        },
    )

    assert response.status_code == 422
    cache_manager.create_task.assert_not_called()
