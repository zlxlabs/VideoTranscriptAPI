"""Contracts for local uploads entering the shared transcription dispatcher."""

import asyncio
import base64
import concurrent.futures
import datetime
import json
import logging
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_transcript_api.api.context import (
    bind_runtime,
    install_api_error_handlers,
    unbind_runtime,
)
from video_transcript_api.api.routes import uploads
from video_transcript_api.api.services import transcription
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.tempfile_manager import TempFileManager


def _key():
    now = datetime.datetime.now(datetime.timezone.utc)
    return f"{int(now.timestamp() * 1000)}-{uuid.uuid4()}"


def _metadata_header(payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _upload_app(tmp_path, monkeypatch, user_info, queue, notification_router):
    """Minimal upload route app: real cache/temp, mocked runtime collaborators."""
    config = {"storage": {"upload_limits": {
        "max_file_mib": 2,
        "max_media_hours": 1,
        "receive_concurrency": 1,
        "upload_temp_budget_mib": 8,
    }}}
    cache_manager = CacheManager(str(tmp_path / "cache"))
    temp_manager = TempFileManager(str(tmp_path / "temp"))
    monkeypatch.setattr(uploads, "get_config", lambda: config)
    monkeypatch.setattr(uploads, "get_cache_manager", lambda: cache_manager)
    monkeypatch.setattr(uploads, "get_temp_manager", lambda: temp_manager)
    monkeypatch.setattr(uploads, "get_task_queue", lambda: queue)
    monkeypatch.setattr(uploads, "get_inflight_registry", lambda: MagicMock())
    monkeypatch.setattr(uploads, "get_notification_router", lambda: notification_router)
    app = FastAPI()
    install_api_error_handlers(app)
    app.include_router(uploads.router)
    app.dependency_overrides[uploads.verify_token] = lambda: user_info
    runtime = SimpleNamespace(
        started=True,
        upload_receiver_slots=MagicMock(),
        reserve_upload_temp=lambda *args: True,
    )

    @asynccontextmanager
    async def lifespan(app):
        app.state.runtime = runtime
        token = bind_runtime(SimpleNamespace(
            task_queue=asyncio.Queue(),
            logger=logging.getLogger("upload-intake-standby"),
        ))
        processor = asyncio.create_task(transcription.process_task_queue())
        app.state.queue_processor = processor
        try:
            yield
        finally:
            processor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await processor
            unbind_runtime(token)

    app.router.lifespan_context = lifespan
    return app, cache_manager, temp_manager


def _capture_put(queue):
    captured = []
    original_put = queue.put_nowait
    queue.put_nowait = lambda item: (captured.append(item), original_put(item))
    return captured


def _post_upload(client, body):
    key = (
        f"{int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)}"
        f"-{uuid.uuid4()}"
    )
    metadata = {
        "filename": "clip.mp4",
        "byte_size": len(body),
        "title": "clip",
        "source_url": None,
        "retention": "30d",
        "processing_options": {},
    }
    return client.post(
        "/api/uploads",
        headers={
            "Content-Type": "application/octet-stream",
            "Idempotency-Key": key,
            "X-Upload-Metadata": _metadata_header(metadata),
        },
        content=body,
    )


def test_upload_intake_enqueues_submitter_webhooks_and_reuses_them_for_receipt_notice(
    tmp_path, monkeypatch
):
    hook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=alice"
    queue = asyncio.Queue()
    router = MagicMock()
    router.send_view_link.return_value = {"wechat": True}
    app, _cache_manager, _temp_manager = _upload_app(
        tmp_path, monkeypatch, {"user_id": "alice", "wechat_webhook": hook}, queue, router,
    )
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    with TestClient(app, raise_server_exceptions=False) as client:
        captured = _capture_put(queue)
        response = _post_upload(client, b"upload body bytes")

    assert response.status_code == 202
    assert len(captured) == 1
    assert captured[0]["notification_webhooks"] == {"wechat": hook}
    assert router.send_view_link.call_args.kwargs["webhooks"] == {"wechat": hook}


def test_uploaded_intake_payload_delivers_submitter_webhooks_to_terminal_notify(
    tmp_path, monkeypatch
):
    hook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=alice"
    queue = asyncio.Queue()
    router = MagicMock()
    router.send_view_link.return_value = {"wechat": True}
    app, cache_manager, temp_manager = _upload_app(
        tmp_path, monkeypatch, {"user_id": "alice", "wechat_webhook": hook}, queue, router,
    )
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    with TestClient(app, raise_server_exceptions=False) as client:
        captured = _capture_put(queue)
        response = _post_upload(client, b"uploaded intake media bytes")

    assert response.status_code == 202
    assert len(captured) == 1
    payload = captured[0]
    assert payload["notification_webhooks"] == {"wechat": hook}

    finalizer = MagicMock()
    executor = MagicMock()
    submitted = asyncio.Event()
    executor.submit.side_effect = lambda function, *args: (
        submitted.set() or concurrent.futures.Future()
    )

    def failed_process(*_args, **_kwargs):
        raise RuntimeError("ASR fixture failure")

    monkeypatch.setattr(transcription, "cache_manager", cache_manager)
    monkeypatch.setattr(transcription, "task_queue", queue)
    monkeypatch.setattr(transcription, "get_temp_manager", lambda: temp_manager)
    monkeypatch.setattr(transcription, "get_runtime", lambda: MagicMock())
    monkeypatch.setattr(transcription, "get_notification_router", lambda: MagicMock())
    monkeypatch.setattr(transcription, "executor", executor)
    monkeypatch.setattr(transcription, "process_transcription", failed_process)
    monkeypatch.setattr(
        transcription, "finalize_terminal_status_and_notify", finalizer
    )

    async def dispatch_one():
        dispatcher = asyncio.create_task(transcription.process_task_queue())
        await asyncio.wait_for(submitted.wait(), timeout=2)
        function, *args = executor.submit.call_args.args
        function(*args)
        dispatcher.cancel()
        try:
            await dispatcher
        except asyncio.CancelledError:
            pass

    asyncio.run(dispatch_one())

    finalizer.assert_called_once()
    assert finalizer.call_args.kwargs["webhooks"] == {"wechat": hook}
    assert queue._unfinished_tasks == 0


def test_dispatcher_consumes_persisted_media_mapping_and_processing_options(
    tmp_path, monkeypatch
):
    cache = CacheManager(str(tmp_path / "cache"))
    intent = cache.register_local_upload(
        owner_user_id="alice", idempotency_key=_key(), retention="30d"
    )
    task_id = cache.generate_task_id()
    temp_manager = TempFileManager(str(tmp_path / "temp"))
    task_dir = temp_manager.create_task_dir(task_id)
    media_path = task_dir / "upload-source.bin"
    media_bytes = b"actual upload media bytes"
    media_path.write_bytes(media_bytes)
    options = {
        "calibrate": True,
        "summarize": False,
        "infer_speaker_names": False,
        "chapters": False,
    }
    upload = cache.accept_local_upload(
        intent["upload_id"],
        media_id=f"upload_{uuid.uuid4().hex}",
        task_id=task_id,
        filename="clip.mp4",
        title="Owner title",
        source_url="",
        request_metadata={"filename": "clip.mp4", "title": "Owner title"},
        processing_options=options,
        media_path=str(media_path),
        byte_size=len(media_bytes),
        sha256="a" * 64,
    )
    queue = asyncio.Queue()
    queue.put_nowait(
        {
            "id": task_id,
            "url": "https://display-only.invalid/not-a-download-source",
            "platform": "local_upload",
            "processing_options": options,
        }
    )
    runtime = MagicMock()
    submitted = asyncio.Event()
    executor = MagicMock()
    executor.submit.side_effect = lambda function, *args: (
        submitted.set() or concurrent.futures.Future()
    )
    process_calls = []

    monkeypatch.setattr(transcription, "cache_manager", cache)
    monkeypatch.setattr(transcription, "task_queue", queue)
    monkeypatch.setattr(transcription, "executor", executor)
    monkeypatch.setattr(transcription, "get_runtime", lambda: runtime)
    monkeypatch.setattr(transcription, "get_temp_manager", lambda: temp_manager)
    monkeypatch.setattr(
        transcription,
        "process_transcription",
        lambda *args, **kwargs: process_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        transcription,
        "get_notification_router",
        lambda: MagicMock(),
    )

    async def dispatch_one():
        dispatcher = asyncio.create_task(transcription.process_task_queue())
        await asyncio.wait_for(submitted.wait(), timeout=2)
        submitted_args = executor.submit.call_args.args
        function, args = submitted_args[0], submitted_args[1:]
        function(*args)
        dispatcher.cancel()
        try:
            await dispatcher
        except asyncio.CancelledError:
            pass

    asyncio.run(dispatch_one())

    assert upload["root_task_id"] == task_id
    assert len(process_calls) == 1
    args, kwargs = process_calls[0]
    assert args[0] == task_id
    assert args[1] == ""
    assert kwargs["local_media_path"] == str(media_path.resolve())
    assert kwargs["local_media_id"] == upload["media_id"]
    assert kwargs["processing_options"] == options
    assert media_path.read_bytes() == media_bytes
    assert queue._unfinished_tasks == 0


def test_dispatcher_persists_named_local_upload_processing_failure(tmp_path, monkeypatch):
    cache = CacheManager(str(tmp_path / "cache"))
    intent = cache.register_local_upload(
        owner_user_id="alice", idempotency_key=_key(), retention="30d"
    )
    task_id = cache.generate_task_id()
    temp_manager = TempFileManager(str(tmp_path / "temp"))
    media_path = temp_manager.create_task_dir(task_id) / "upload-source.bin"
    media_path.write_bytes(b"failed media fixture")
    upload = cache.accept_local_upload(
        intent["upload_id"],
        media_id=f"upload_{uuid.uuid4().hex}",
        task_id=task_id,
        filename="failed.mp4",
        request_metadata={"filename": "failed.mp4"},
        media_path=str(media_path),
        byte_size=media_path.stat().st_size,
        sha256="b" * 64,
    )
    queue = asyncio.Queue()
    queue.put_nowait({"id": task_id, "url": "", "platform": "local_upload"})
    runtime = MagicMock()
    submitted = asyncio.Event()
    executor = MagicMock()
    executor.submit.side_effect = lambda *_args, **_kwargs: (
        submitted.set() or concurrent.futures.Future()
    )
    router = MagicMock()
    router.notify_task_status.return_value = {"fixture": True}

    def failed_process(*_args, **_kwargs):
        raise RuntimeError("ASR fixture failure")

    monkeypatch.setattr(transcription, "cache_manager", cache)
    monkeypatch.setattr(transcription, "task_queue", queue)
    monkeypatch.setattr(transcription, "executor", executor)
    monkeypatch.setattr(transcription, "get_runtime", lambda: runtime)
    monkeypatch.setattr(transcription, "get_temp_manager", lambda: temp_manager)
    monkeypatch.setattr(transcription, "process_transcription", failed_process)
    monkeypatch.setattr(transcription, "get_notification_router", lambda: router)

    async def dispatch_one():
        dispatcher = asyncio.create_task(transcription.process_task_queue())
        await asyncio.wait_for(submitted.wait(), timeout=2)
        function, *args = executor.submit.call_args.args
        function(*args)
        dispatcher.cancel()
        try:
            await dispatcher
        except asyncio.CancelledError:
            pass

    asyncio.run(dispatch_one())

    assert cache.get_task_by_id(task_id)["status"] == "failed"
    assert "UPLOAD_PROCESSING_FAILED" in cache.get_task_by_id(task_id)["error_message"]
    assert upload["root_task_id"] == task_id
    assert queue._unfinished_tasks == 0
