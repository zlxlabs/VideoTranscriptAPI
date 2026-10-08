"""Cross-route local-upload lifecycle contracts backed by real SQLite/cache files."""

import asyncio
import base64
import concurrent.futures
import datetime
import hashlib
import json
import queue
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_transcript_api.api.routes import audit as audit_routes
from video_transcript_api.api.routes import tasks as tasks_routes
from video_transcript_api.api.routes import uploads as uploads_routes
from video_transcript_api.api.routes import views as views_routes
from video_transcript_api.api.services import llm_ops, transcription
from video_transcript_api.api.services.transcription import verify_token
from video_transcript_api.api.context import bind_runtime, unbind_runtime
from video_transcript_api.api.services.view_token_resolver import ViewTokenResolver
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.llm_status import ChaptersStatus, NotesStatus, SummaryStatus
from video_transcript_api.utils.tempfile_manager import TempFileManager


OWNER = {"user_id": "alice", "api_key": "sk-alice", "is_legacy": True}


@pytest.fixture
def lifecycle_client(tmp_path, monkeypatch):
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    cache = CacheManager(str(tmp_path / "cache"))
    user = dict(OWNER)
    llm_queue = queue.Queue()
    registry = SimpleNamespace(
        try_register=lambda *_args: True,
        release=lambda *_args: None,
    )
    audit_stub = SimpleNamespace(
        _mask_api_key=lambda _key: "sk-***",
        log_api_call=lambda **_kwargs: True,
    )
    monkeypatch.setattr(tasks_routes, "cache_manager", cache)
    monkeypatch.setattr(tasks_routes, "user_manager", SimpleNamespace(check_permission=lambda *_args: True))
    monkeypatch.setattr(tasks_routes, "audit_logger", audit_stub)
    monkeypatch.setattr(tasks_routes, "config", {})
    monkeypatch.setattr(audit_routes, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(audit_routes, "audit_logger", audit_stub)
    monkeypatch.setattr(views_routes, "cache_manager", cache)
    import video_transcript_api.api.context as context
    monkeypatch.setattr(context, "get_llm_queue", lambda: llm_queue)
    monkeypatch.setattr(context, "get_inflight_registry", lambda: registry)

    async def current_user():
        return dict(user)

    app = FastAPI()
    app.include_router(tasks_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(views_routes.router)
    app.dependency_overrides[verify_token] = current_user
    with TestClient(app) as client:
        yield client, cache, llm_queue, user
    cache.close()


def _make_upload(
    cache, *, owner="alice", media_name=None, title="上传标题",
    include_summary=True, retention="never",
):
    now = datetime.datetime.now(datetime.timezone.utc)
    key = f"{int(now.timestamp() * 1000)}-{uuid.uuid4()}"
    intent = cache.register_local_upload(
        owner_user_id=owner,
        idempotency_key=key,
        retention=retention,
        intent_metadata={"filename": "clip.mp4", "title": title},
    )
    media_id = media_name or f"upload_{uuid.uuid4().hex}"
    upload = cache.accept_local_upload(
        intent["upload_id"],
        media_id=media_id,
        task_id=cache.generate_task_id(),
        filename="clip.mp4",
        title=title,
        source_url="https://source.example.test/media?id=7",
        request_metadata={"filename": "clip.mp4", "title": title},
        media_path="/tmp/never-return-this-path/source.mp4",
        byte_size=128,
        sha256="c" * 64,
    )
    cache.save_cache(
        platform="local_upload",
        url="https://source.example.test/media?id=7",
        media_id=media_id,
        use_speaker_recognition=False,
        transcript_data="actual transcript body",
        transcript_type="capswriter",
        title=title,
        author="",
        description="",
    )
    if include_summary:
        cache.save_llm_result(
            platform="local_upload",
            media_id=media_id,
            use_speaker_recognition=False,
            llm_type="summary",
            content="A real public summary.",
        )
    cache.save_llm_status(
        platform="local_upload",
        media_id=media_id,
        use_speaker_recognition=False,
        summary_status=(
            SummaryStatus.GENERATED if include_summary else SummaryStatus.FAILED
        ),
        chapters_status=ChaptersStatus.GENERATED,
        notes_status=NotesStatus.FAILED,
    )
    cache.update_task_status(
        upload["root_task_id"],
        "success",
        platform="local_upload",
        media_id=media_id,
        title=title,
    )
    return cache.get_local_upload_by_id(upload["upload_id"])


def _make_expired_upload(cache, *, title):
    now = datetime.datetime.now(datetime.timezone.utc)
    intent = cache.register_local_upload(
        owner_user_id="alice",
        idempotency_key=f"{int(now.timestamp() * 1000)}-{uuid.uuid4()}",
        retention="30d",
        intent_metadata={"filename": "old.mp4", "title": title},
    )
    upload = cache.accept_local_upload(
        intent["upload_id"],
        media_id=f"upload_{uuid.uuid4().hex}",
        task_id=cache.generate_task_id(),
        filename="old.mp4",
        title=title,
        request_metadata={"filename": "old.mp4", "title": title},
    )
    expired_at = "2000-01-01 00:00:00"
    with cache._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET status = 'failed', completed_at = ? WHERE task_id = ?",
            (expired_at, upload["root_task_id"]),
        )
        cursor.execute(
            "UPDATE local_uploads SET terminal_at = ?, expires_at = ? WHERE upload_id = ?",
            (expired_at, expired_at, upload["upload_id"]),
        )
    return cache.get_local_upload_by_id(upload["upload_id"])


def test_http_upload_acceptance_notifies_independent_public_capability(lifecycle_client, monkeypatch):
    _client, cache, _llm_queue, _user = lifecycle_client
    """The actual raw-body producer must put only upload identity on accepted notices."""
    temp_manager = TempFileManager(str(cache.cache_dir.parent / "accepted-temp"))
    task_queue = asyncio.Queue(maxsize=2)
    registry = SimpleNamespace(
        try_register=lambda *_args: True,
        release=lambda *_args: None,
    )
    reservations = []
    notifications = []

    class ReadyProcessor:
        done = lambda _self: False
        get_coro = staticmethod(lambda: transcription.process_task_queue)

    runtime = SimpleNamespace(
        started=True,
        upload_receiver_slots=threading.BoundedSemaphore(1),
        reserve_upload_temp=lambda _task, size, budget, _dir: (
            reservations.append(size) or size <= budget
        ),
        release_upload_temp=lambda _task: None,
    )
    config = {
        "storage": {
            "temp_dir": str(temp_manager.base_dir),
            "upload_limits": {
                "max_file_mib": 1,
                "max_media_hours": 1,
                "receive_concurrency": 1,
                "upload_temp_budget_mib": 4,
            },
        }
    }
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    monkeypatch.setattr(uploads_routes, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(uploads_routes, "get_config", lambda: config)
    monkeypatch.setattr(uploads_routes, "get_task_queue", lambda: task_queue)
    monkeypatch.setattr(uploads_routes, "get_temp_manager", lambda: temp_manager)
    monkeypatch.setattr(uploads_routes, "get_inflight_registry", lambda: registry)
    monkeypatch.setattr(
        uploads_routes,
        "get_notification_router",
        lambda: SimpleNamespace(
            send_view_link=lambda **kwargs: notifications.append(kwargs) or {"test": True}
        ),
    )
    app = FastAPI()
    app.state.runtime = runtime
    app.state.queue_processor = ReadyProcessor()
    app.include_router(uploads_routes.router)
    app.dependency_overrides[verify_token] = lambda: OWNER
    with TestClient(app) as client:
        raw = b"real accepted producer bytes\x00"
        metadata = {
            "filename": "user-file.mp4",
            "byte_size": len(raw),
            "title": "Owner selected title",
            "source_url": "https://source.example.test/watch?id=9",
            "retention": "30d",
            "processing_options": {"calibrate": False, "summarize": False},
        }
        key = f"{int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)}-{uuid.uuid4()}"
        response = client.post(
            "/api/uploads",
            content=raw,
            headers={
                "Authorization": "Bearer test-owner",
                "Content-Type": "application/octet-stream",
                "Idempotency-Key": key,
                "X-Upload-Metadata": base64.urlsafe_b64encode(
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
                ).decode().rstrip("="),
            },
        )
    assert response.status_code == 202, response.text
    receipt = response.json()
    assert reservations == [len(raw), len(raw)]
    assert len(notifications) == 1
    assert notifications[0] == {
        "title": "Owner selected title",
        "view_token": receipt["view_token"],
        "original_url": "https://source.example.test/watch?id=9",
        "source_label": "本地上传",
        "task_id": receipt["task_id"],
        "webhooks": {},
    }
    assert notifications[0]["view_token"].startswith("upload_")
    assert "user-file.mp4" not in notifications[0]["view_token"]
    assert "/accepted-temp/" not in json.dumps(notifications[0])
    assert task_queue.get_nowait()["id"] == receipt["task_id"]


def test_upload_notification_exception_keeps_acceptance_and_receipt(
    lifecycle_client, monkeypatch
):
    _client, cache, _llm_queue, _user = lifecycle_client
    temp_manager = TempFileManager(str(cache.cache_dir.parent / "notification-failure-temp"))
    task_queue = asyncio.Queue(maxsize=2)
    registry = SimpleNamespace(
        try_register=lambda *_args: True,
        release=lambda *_args: None,
    )
    runtime = SimpleNamespace(
        started=True,
        upload_receiver_slots=threading.BoundedSemaphore(1),
        reserve_upload_temp=lambda _task, size, budget, _dir: size <= budget,
        release_upload_temp=lambda _task: None,
    )
    config = {
        "storage": {
            "temp_dir": str(temp_manager.base_dir),
            "upload_limits": {
                "max_file_mib": 1,
                "max_media_hours": 1,
                "receive_concurrency": 1,
                "upload_temp_budget_mib": 4,
            },
        }
    }

    class ReadyProcessor:
        done = lambda _self: False
        get_coro = staticmethod(lambda: transcription.process_task_queue)

    def fail_notification(**_kwargs):
        raise RuntimeError("notification formatter failure")

    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    monkeypatch.setattr(uploads_routes, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(uploads_routes, "get_config", lambda: config)
    monkeypatch.setattr(uploads_routes, "get_task_queue", lambda: task_queue)
    monkeypatch.setattr(uploads_routes, "get_temp_manager", lambda: temp_manager)
    monkeypatch.setattr(uploads_routes, "get_inflight_registry", lambda: registry)
    monkeypatch.setattr(
        uploads_routes,
        "get_notification_router",
        lambda: SimpleNamespace(send_view_link=fail_notification),
    )

    raw = b"accepted despite notification failure"
    metadata = {
        "filename": "notification.mp4",
        "byte_size": len(raw),
        "title": "Notification failure",
        "source_url": None,
        "retention": "never",
        "processing_options": {},
    }
    key = f"{int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)}-{uuid.uuid4()}"
    headers = {
        "Authorization": "Bearer test-owner",
        "Content-Type": "application/octet-stream",
        "Idempotency-Key": key,
        "X-Upload-Metadata": base64.urlsafe_b64encode(
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
        ).decode().rstrip("="),
    }

    app = FastAPI()
    app.state.runtime = runtime
    app.state.queue_processor = ReadyProcessor()
    app.include_router(uploads_routes.router)
    app.dependency_overrides[verify_token] = lambda: OWNER
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/uploads", content=raw, headers=headers)
        duplicate = client.post("/api/uploads", content=b"wrong body", headers=headers)

    assert response.status_code == 500
    accepted = cache.get_local_upload_by_owner_key("alice", key)
    assert accepted["state"] == "accepted"
    assert accepted["root_task_id"]
    assert response.text == "Internal Server Error"
    assert duplicate.status_code == 200
    assert duplicate.json()["task_id"] == accepted["root_task_id"]
    assert task_queue.qsize() == 1


def test_upload_reprocess_routes_use_owner_gate_and_never_persist_legacy_token(
    lifecycle_client,
):
    client, cache, llm_queue, user = lifecycle_client
    endpoints = ("/api/recalibrate", "/api/resummarize", "/api/generate_notes")
    uploads = [
        _make_upload(
            cache,
            title=f"Owner title {index}",
            include_summary=index != 1,
        )
        for index in range(4)
    ]
    child_ids = []

    for endpoint, upload in zip(endpoints, uploads):
        response = client.post(endpoint, json={"view_token": upload["view_token"]})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["code"] == 202
        assert payload["data"]["view_token"] == upload["view_token"]
        child_id = payload["data"]["task_id"]
        child_ids.append(child_id)
        child = cache.get_task_by_id(child_id)
        assert child["platform"] == "local_upload"
        assert child["media_id"] == upload["media_id"]
        assert child["view_token"] == ""
        assert child["submitted_by"] == "alice"
        assert llm_queue.get_nowait()["task_id"] == child_id

    repeated_notes = client.post(
        "/api/generate_notes", json={"view_token": uploads[2]["view_token"]}
    )
    assert repeated_notes.status_code == 409
    distinct_notes = client.post(
        "/api/generate_notes", json={"view_token": uploads[3]["view_token"]}
    )
    assert distinct_notes.status_code == 200, distinct_notes.text
    distinct_child = cache.get_task_by_id(distinct_notes.json()["data"]["task_id"])
    assert distinct_child["media_id"] == uploads[3]["media_id"]
    assert llm_queue.get_nowait()["task_id"] == distinct_child["task_id"]

    user.update(user_id="bob", api_key="sk-bob")
    for endpoint, upload in zip(endpoints, uploads):
        response = client.post(endpoint, json={"view_token": upload["view_token"]})
        assert response.status_code == 404
    assert llm_queue.empty()

    root_before = cache.get_local_upload_by_id(uploads[0]["upload_id"])
    revoked = cache.revoke_local_upload(uploads[0]["upload_id"])
    cache.update_task_status(
        child_ids[0], "success", platform="local_upload", media_id=uploads[0]["media_id"]
    )
    cache.update_task_status(
        uploads[0]["root_task_id"], "failed", error_message="late root completion"
    )
    root_after = cache.get_local_upload_by_id(uploads[0]["upload_id"])
    assert root_after["revoked_at"] == revoked["revoked_at"]
    assert root_after["terminal_at"] == root_before["terminal_at"]
    assert root_after["expires_at"] == root_before["expires_at"]
    assert ViewTokenResolver(cache).get_view_data_by_token(uploads[0]["view_token"]) is None


def test_public_read_summary_exports_and_owner_history_close_without_content_bypass(
    lifecycle_client,
):
    client, cache, _llm_queue, user = lifecycle_client
    upload = _make_upload(cache, title="Private owner title")
    token = upload["view_token"]

    raw = client.get(f"/view/{token}?raw=transcript")
    page = client.get(f"/view/{token}?page=transcript")
    export = client.get(f"/export/{token}/transcript")
    summary = client.get(f"/api/audit/summary?view_token={token}")
    assert raw.status_code == 200 and "actual transcript body" in raw.text
    assert page.status_code == 200 and "actual transcript body" in page.text
    assert export.status_code == 200 and "actual transcript body" in export.text
    assert summary.status_code == 200
    assert summary.json()["data"]["summary"] == "A real public summary."

    history = client.get("/api/audit/history?source=upload&status=all&limit=1&offset=0")
    item = history.json()["data"]["items"][0]
    assert history.status_code == 200
    assert history.json()["data"]["total"] == 1
    assert item["source"] == "upload"
    assert item["upload_id"] == upload["upload_id"]
    assert item["share_active"] is True
    assert "summary" not in item and "transcript" not in item
    assert item["view_token"] is None

    user.update(user_id="bob", api_key="sk-bob")
    assert client.get(f"/api/audit/summary?view_token={token}").status_code == 200
    user.update(user_id="alice", api_key="sk-alice")
    revoked = cache.revoke_local_upload(upload["upload_id"])
    assert revoked["revoked_at"]

    expired = _make_expired_upload(
        cache, title="Expired owner title"
    )
    closed_history = client.get("/api/audit/history?source=upload&status=all")
    closed_items = closed_history.json()["data"]["items"]
    closed_item = next(item for item in closed_items if item["upload_id"] == upload["upload_id"])
    expired_item = next(item for item in closed_items if item["upload_id"] == expired["upload_id"])
    assert closed_item["share_active"] is False
    assert closed_item["share_inactive_reason"] == "revoked"
    assert closed_item["title"] == "Private owner title"
    assert expired_item["share_active"] is False
    assert expired_item["share_inactive_reason"] == "expired"
    assert "summary" not in closed_item and "transcript" not in closed_item
    assert client.get(f"/view/{token}?raw=transcript").status_code == 404
    assert client.get(f"/view/{token}?page=transcript").status_code == 404
    assert client.get(f"/export/{token}/transcript").status_code == 404
    assert client.get(f"/api/audit/summary?view_token={token}").status_code == 404
    for endpoint in ("/api/recalibrate", "/api/resummarize", "/api/generate_notes"):
        assert client.post(endpoint, json={"view_token": token}).status_code == 404


def test_reprocess_admission_holds_sqlite_order_against_concurrent_close(
    lifecycle_client, monkeypatch,
):
    client, cache, llm_queue, _user = lifecycle_client
    upload = _make_upload(cache, title="Concurrent close source")
    original_admit = tasks_routes._admit_upload_reprocess
    admission_paused = threading.Event()
    release_admission = threading.Event()
    revoke_started = threading.Event()
    revoke_done = threading.Event()
    call_count = 0

    def pause_transaction_admission(cursor, view_token, user_id, cache_data):
        nonlocal call_count
        record = original_admit(cursor, view_token, user_id, cache_data)
        call_count += 1
        if call_count == 2:
            admission_paused.set()
            if not release_admission.wait(timeout=5):
                raise TimeoutError("controlled reprocess admission pause expired")
        return record

    monkeypatch.setattr(
        tasks_routes, "_admit_upload_reprocess", pause_transaction_admission
    )
    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        admission = executor.submit(
            client.post,
            "/api/recalibrate",
            json={"view_token": upload["view_token"]},
        )
        assert admission_paused.wait(timeout=3)

        def close_share():
            revoke_started.set()
            try:
                cache.revoke_local_upload(upload["upload_id"])
            finally:
                revoke_done.set()

        revoke = executor.submit(close_share)
        assert revoke_started.wait(timeout=3)
        assert revoke_done.wait(timeout=0.1) is False
        release_admission.set()
        response = admission.result(timeout=3)
        revoke.result(timeout=3)
    finally:
        release_admission.set()
        executor.shutdown(wait=True)

    assert response.status_code == 200, response.text
    child = cache.get_task_by_id(response.json()["data"]["task_id"])
    assert child["view_token"] == ""
    assert child["media_id"] == upload["media_id"]
    assert cache.get_local_upload_by_id(upload["upload_id"])["revoked_at"] is not None
    assert llm_queue.qsize() == 1


def test_upload_history_filters_owner_before_page_limit(lifecycle_client):
    client, cache, _llm_queue, _user = lifecycle_client
    older = _make_upload(cache, title="Alice older")
    newer = _make_upload(cache, title="Alice newer")
    outsider = _make_upload(cache, owner="bob", title="Bob newest")
    with cache._get_cursor() as cursor:
        for upload, timestamp in (
            (older, "2026-10-01 00:00:00"),
            (newer, "2026-10-02 00:00:00"),
            (outsider, "2026-10-03 00:00:00"),
        ):
            cursor.execute(
                "UPDATE task_status SET completed_at = ? WHERE task_id = ?",
                (timestamp, upload["root_task_id"]),
            )

    response = client.get(
        "/api/audit/history?source=upload&status=all&limit=1&offset=0"
    )
    data = response.json()["data"]
    assert response.status_code == 200
    assert data["total"] == 2
    assert [item["upload_id"] for item in data["items"]] == [newer["upload_id"]]
    assert data["items"][0]["title"] == "Alice newer"
    mismatched_platform = client.get(
        "/api/audit/history?source=upload&platform=youtube&status=all"
    )
    assert mismatched_platform.json()["data"]["total"] == 0


def test_dispatcher_uses_real_transcriber_then_cleans_owned_media_before_llm(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    cache = CacheManager(str(tmp_path / "cache"))
    temp_manager = TempFileManager(str(tmp_path / "temp"))
    task_queue = asyncio.Queue()
    llm_queue = queue.Queue()
    source_bytes = b"owned upload producer fixture bytes\x00\x01"
    options = {
        "calibrate": False,
        "summarize": False,
        "infer_speaker_names": False,
        "chapters": False,
    }
    now = datetime.datetime.now(datetime.timezone.utc)
    intent = cache.register_local_upload(
        owner_user_id="alice",
        idempotency_key=f"{int(now.timestamp() * 1000)}-{uuid.uuid4()}",
        retention="30d",
        intent_metadata={"filename": "fixture.mp4", "title": "Owned title"},
    )
    task_id = cache.generate_task_id()
    task_dir = temp_manager.create_task_dir(task_id)
    source_path = task_dir / "upload-source.bin"
    source_path.write_bytes(source_bytes)
    upload = cache.accept_local_upload(
        intent["upload_id"],
        media_id=f"upload_{uuid.uuid4().hex}",
        task_id=task_id,
        filename="fixture.mp4",
        title="Owned title",
        request_metadata={"filename": "fixture.mp4", "title": "Owned title"},
        media_path=str(source_path),
        byte_size=len(source_bytes),
        sha256=hashlib.sha256(source_bytes).hexdigest(),
        processing_options=options,
    )
    task_queue.put_nowait(
        {"id": task_id, "url": "", "platform": "local_upload", "processing_options": options}
    )

    from video_transcript_api.transcriber import capswriter_client as capswriter_module
    from video_transcript_api.transcriber import transcriber as transcriber_module
    from video_transcript_api.transcriber.transcriber import Transcriber

    workspace_dir = tmp_path / "workspace"
    monkeypatch.setattr(transcriber_module, "get_workspace_dir", lambda: str(workspace_dir))
    monkeypatch.setattr(capswriter_module.Config, "load_from_project_config", lambda: None)
    monkeypatch.setattr(
        capswriter_module,
        "load_config",
        lambda: {"capswriter": {"server_url": "ws://127.0.0.1:6006"}},
    )
    real_transcriber = Transcriber(
        config={"capswriter": {"server_url": "ws://127.0.0.1:6006", "max_retries": 0}}
    )
    transcript_bytes = "真实 CapsWriter 输出文本".encode("utf-8")
    asr_calls = []
    asr_output_paths = []
    reservations = []
    llm_observation = {}
    llm_started = threading.Event()
    llm_done = threading.Event()

    def capswriter_fixture(audio_path, *, media_duration=None):
        asr_calls.append((str(audio_path), Path(audio_path).read_bytes(), media_duration))
        transcript_path = Path(real_transcriber.capswriter_client.output_dir) / "capswriter-output.txt"
        from video_transcript_api.transcriber.capswriter_client import _atomic_write_text

        _atomic_write_text(
            transcript_path,
            transcript_bytes.decode("utf-8"),
            write_admission=real_transcriber.capswriter_client.write_admission,
        )
        asr_output_paths.append(transcript_path)
        return True, [transcript_path]

    real_transcriber.capswriter_client.transcribe_file = capswriter_fixture
    monkeypatch.setattr(transcription, "Transcriber", lambda: real_transcriber)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(transcription, "cache_manager", cache)
    monkeypatch.setattr(transcription, "task_queue", task_queue)
    monkeypatch.setattr(transcription, "executor", executor)
    monkeypatch.setattr(transcription, "llm_task_queue", llm_queue)
    monkeypatch.setattr(transcription, "get_temp_manager", lambda: temp_manager)
    monkeypatch.setattr(
        transcription,
        "get_config",
        lambda: {"storage": {"upload_limits": {"max_media_hours": 1, "upload_temp_budget_mib": 8}}},
    )
    monkeypatch.setattr(transcription, "_ensure_audio_track", lambda _path: 60.0)
    monkeypatch.setattr(
        transcription,
        "create_downloader",
        lambda *_args, **_kwargs: pytest.fail("local upload reached URL downloader"),
    )
    from video_transcript_api.utils.url_parser import URLParser
    monkeypatch.setattr(
        URLParser, "parse", lambda *_args, **_kwargs: pytest.fail("local upload parsed a URL")
    )
    monkeypatch.setattr(transcription.slow_alert, "track_transcription", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(transcription.slow_alert, "duration_known", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(transcription.slow_alert, "cancel_all", lambda *_args, **_kwargs: None)

    delivered = []
    router = SimpleNamespace(
        notify_task_status=lambda **kwargs: delivered.append(kwargs) or {"fixture": True}
    )
    monkeypatch.setattr(transcription, "get_notification_router", lambda: router)
    monkeypatch.setattr(llm_ops, "cache_manager", cache)
    monkeypatch.setattr(llm_ops, "get_notification_router", lambda: router)
    monkeypatch.setattr(llm_ops.slow_alert, "track_llm", lambda *_args, **_kwargs: None)

    class NoModelWork:
        def process(self, **kwargs):
            llm_observation["source_exists"] = source_path.exists()
            llm_observation["task_dir_exists"] = task_dir.exists()
            llm_observation["asr_outputs_exist"] = [path.exists() for path in asr_output_paths]
            llm_started.set()
            assert kwargs["skip_calibration"] is True
            assert kwargs["skip_summary"] is True
            assert kwargs["skip_chapters"] is True
            return {
                "calibrated_text": "",
                "summary_text": None,
                "stats": {
                    "calibration_status": "disabled",
                    "summary_status": "disabled",
                    "chapters_status": "disabled",
                },
            }

    monkeypatch.setattr(llm_ops, "llm_coordinator", NoModelWork())
    loop_state = {}
    from loguru import logger as test_logger

    runtime = SimpleNamespace(
        inflight_registry=SimpleNamespace(
            release=lambda *_args: None,
            register_internal=lambda *_args: None,
        ),
        reserve_upload_temp=lambda _task, size, budget, _dir: (
            reservations.append(size) or size <= budget
        ),
        release_upload_temp=lambda _task: None,
        track_future=None,
        temp_manager=temp_manager,
        executor=executor,
        llm_queue=llm_queue,
        config={"llm": {"structured_calibration_for_plain": False}},
        logger=test_logger,
    )

    from video_transcript_api.api.context import run_with_runtime

    original_llm_put = llm_queue.put

    def put_before_cleanup(item, *args, **kwargs):
        original_llm_put(item, *args, **kwargs)
        if not llm_started.wait(timeout=5):
            raise TimeoutError("LLM consumer did not reach process before cleanup")

    llm_queue.put = put_before_cleanup

    def consume_llm_task():
        task = llm_queue.get()
        llm_observation["task"] = task
        try:
            run_with_runtime(runtime, llm_ops._handle_llm_task, task)
        finally:
            llm_done.set()

    llm_consumer = threading.Thread(target=consume_llm_task, name="upload-llm-consumer")
    llm_consumer.start()

    async def dispatch_and_wait():
        loop_state["loop"] = asyncio.get_running_loop()
        future_done = asyncio.Event()

        def track_future(future, *, kind="transcription", task_id=None):
            runtime.future = future
            future.add_done_callback(
                lambda _finished: loop_state["loop"].call_soon_threadsafe(future_done.set)
            )

        runtime.track_future = track_future
        bound = bind_runtime(runtime)
        dispatcher = asyncio.create_task(transcription.process_task_queue())
        try:
            await asyncio.wait_for(future_done.wait(), timeout=10)
            await asyncio.wait_for(asyncio.wrap_future(runtime.future), timeout=10)
        finally:
            dispatcher.cancel()
            try:
                await dispatcher
            except asyncio.CancelledError:
                pass
            unbind_runtime(bound)

    try:
        asyncio.run(dispatch_and_wait())
        assert llm_done.wait(timeout=5)
        llm_consumer.join(timeout=5)
    finally:
        executor.shutdown(wait=True)
    assert not llm_consumer.is_alive()
    assert len(asr_calls) == 1
    assert asr_calls[0] == (str(source_path), source_bytes, 60.0)
    expected_temp_bytes = len(source_bytes) + len(transcript_bytes)
    assert reservations == [expected_temp_bytes, expected_temp_bytes]
    assert llm_queue.empty()
    llm_task = llm_observation["task"]
    assert llm_task["task_id"] == task_id
    assert llm_task["media_id"] == upload["media_id"]
    assert llm_task["processing_options"] == options
    assert llm_observation["source_exists"] is False
    assert llm_observation["task_dir_exists"] is False
    assert llm_observation["asr_outputs_exist"] == [False]
    assert not source_path.exists()
    assert not task_dir.exists()
    root = cache.get_task_by_id(task_id)
    assert root["status"] == "success"
    assert root["view_token"] == ""
    stored = cache.get_local_upload_by_id(upload["upload_id"])
    assert stored["terminal_at"] == root["completed_at"]
    assert stored["expires_at"] is not None
    public_view = ViewTokenResolver(cache).get_view_data_by_token(upload["view_token"])
    assert public_view["status"] == "success"
    assert public_view["transcript"] == "真实 CapsWriter 输出文本"
    assert any(
        item.get("view_url", "").endswith(f"/view/{upload['view_token']}")
        and item.get("title") == "Owned title"
        for item in delivered
    )
    cache.close()
