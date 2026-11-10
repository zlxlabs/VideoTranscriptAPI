import queue
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_transcript_api.api.context import install_api_error_handlers
from video_transcript_api.api.routes import tasks
from video_transcript_api.api.services.transcription import verify_token
from video_transcript_api.cache.cache_manager import CacheManager


@pytest.mark.parametrize(
    ("endpoint", "body_fields"),
    [
        ("recalibrate", {"feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/recal-fixture"}),
        ("resummarize", {"feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/resum-fixture"}),
    ],
)
def test_reprocess_child_uses_new_request_targets_not_parent_targets(
    tmp_path, monkeypatch, endpoint, body_fields,
):
    from video_transcript_api.api import context

    cache = CacheManager(str(tmp_path / "cache"))
    parent = cache.create_task(
        url="https://www.youtube.com/watch?v=notify-rerun",
        platform="youtube",
        media_id="notify-rerun",
        submitted_by="notify-owner",
        notify_targets={"wechat": "parent-wechat", "feishu": "parent-feishu"},
    )
    cache.save_cache(
        platform="youtube",
        url="https://www.youtube.com/watch?v=notify-rerun",
        media_id="notify-rerun",
        use_speaker_recognition=False,
        transcript_data="fixture transcript",
        transcript_type="capswriter",
        title="Fixture title",
        author="Fixture author",
        description="Fixture description",
    )
    user_manager = MagicMock()
    user_manager.check_permission.return_value = True
    task_queue = queue.Queue()
    inflight_registry = MagicMock()
    inflight_registry.try_register.return_value = True
    monkeypatch.setattr(tasks, "cache_manager", cache)
    monkeypatch.setattr(tasks, "user_manager", user_manager)
    monkeypatch.setattr(tasks, "audit_logger", MagicMock())
    monkeypatch.setattr(tasks, "check_view_token_ownership", lambda *args: True)
    monkeypatch.setattr(context, "get_llm_queue", lambda: task_queue)
    monkeypatch.setattr(context, "get_inflight_registry", lambda: inflight_registry)

    app = FastAPI()
    install_api_error_handlers(app)
    app.include_router(tasks.router)
    app.dependency_overrides[verify_token] = lambda: {
        "user_id": "notify-owner",
        "api_key": "fixture-api-key",
    }

    try:
        with TestClient(app) as client:
            response = client.post(
                f"/api/{endpoint}",
                json={"view_token": parent["view_token"], **body_fields},
            )
        assert response.status_code == 200
        child = cache.get_task_by_id(response.json()["data"]["task_id"])
        feishu = body_fields["feishu_webhook"]
        assert child["notify_targets"] == {"feishu": feishu}
        assert "parent-wechat" not in child["notify_targets"].values()
        assert "parent-feishu" not in child["notify_targets"].values()
        assert task_queue.get_nowait()["notification_webhooks"] == {"feishu": feishu}
    finally:
        cache.close()
