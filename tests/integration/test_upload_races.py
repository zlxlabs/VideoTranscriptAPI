"""Controlled lifecycle races through the production public-read router."""

import concurrent.futures
import datetime
import threading
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_transcript_api.api.routes import views as views_routes
from video_transcript_api.cache.cache_manager import CacheManager


@pytest.fixture
def public_upload_client(tmp_path, monkeypatch):
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    cache = CacheManager(str(tmp_path / "cache"))
    monkeypatch.setattr(views_routes, "cache_manager", cache)
    app = FastAPI()
    app.include_router(views_routes.router)
    with TestClient(app) as client:
        yield client, cache
    cache.close()


def _accepted_upload(cache):
    now = datetime.datetime.now(datetime.timezone.utc)
    key = f"{int(now.timestamp() * 1000)}-{uuid.uuid4()}"
    intent = cache.register_local_upload(
        owner_user_id="alice",
        idempotency_key=key,
        retention="30d",
        intent_metadata={"filename": "race.mp4"},
    )
    upload = cache.accept_local_upload(
        intent["upload_id"],
        media_id=f"upload_{uuid.uuid4().hex}",
        task_id=cache.generate_task_id(),
        filename="race.mp4",
        request_metadata={"filename": "race.mp4"},
    )
    cache.save_cache(
        platform="local_upload",
        url="https://upload.invalid/race",
        media_id=upload["media_id"],
        use_speaker_recognition=False,
        transcript_data="race body from SQLite-backed cache",
        transcript_type="capswriter",
        title="Race fixture",
        author="",
        description="",
    )
    cache.update_task_status(
        upload["root_task_id"],
        "success",
        platform="local_upload",
        media_id=upload["media_id"],
        title="Race fixture",
    )
    return cache.get_local_upload_by_id(upload["upload_id"])


def test_public_read_and_expiry_both_controlled_orders(public_upload_client, monkeypatch):
    client, cache = public_upload_client
    upload = _accepted_upload(cache)
    token = upload["view_token"]
    loaded_upload = threading.Event()
    continue_read = threading.Event()
    check_share = cache.local_upload_share_is_active
    expiration = datetime.datetime.fromisoformat(upload["expires_at"]).replace(
        tzinfo=datetime.timezone.utc
    )
    before_expiration = expiration - datetime.timedelta(seconds=1)
    at_expiration = expiration
    decisions = 0

    def pause_after_real_authorization(row):
        nonlocal decisions
        decisions += 1
        result = check_share(
            row, now=before_expiration if decisions == 1 else at_expiration
        )
        if decisions == 1:
            loaded_upload.set()
            if not continue_read.wait(timeout=5):
                raise TimeoutError("test did not release the authorized read")
        return result

    monkeypatch.setattr(cache, "local_upload_share_is_active", pause_after_real_authorization)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        in_flight_read = executor.submit(client.get, f"/view/{token}?raw=transcript")
        assert loaded_upload.wait(timeout=5), "public route did not authorize the read"
        continue_read.set()
        response = in_flight_read.result(timeout=5)

    assert response.status_code == 200
    assert "race body from SQLite-backed cache" in response.text
    fresh_consumer = client.get(f"/view/{token}?raw=transcript")
    assert fresh_consumer.status_code == 404
    assert "race body from SQLite-backed cache" not in fresh_consumer.text
