import asyncio
import base64
import datetime
import hashlib
import json
import queue
import shutil
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager, contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from video_transcript_api.api.context import (
    ConfigError,
    RuntimeContext,
    bind_runtime,
    unbind_runtime,
    validate_config,
)
from video_transcript_api.api.routes import uploads
from video_transcript_api.api.services import transcription
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.accounts.user_manager import UserManager
from video_transcript_api.utils.tempfile_manager import TempFileManager


# Representative raw File/Blob producer payload, sent as application/octet-stream.
RAW_PRODUCER_BYTES = b"\x00VTA upload producer fixture\nmeeting audio bytes\x00"
PRODUCER_METADATA = {
    "filename": "C:\\fakepath\\会议 录音.mp4",
    "byte_size": len(RAW_PRODUCER_BYTES),
    "title": "讨论：咖啡与模型",
    "source_url": "https://display.example/recording?id=42",
    "retention": "30d",
    "processing_options": {"calibrate": False, "summarize": True},
}
OWNER_TOKEN = "integration-owner-token"
OTHER_TOKEN = "integration-other-token"


def _key():
    epoch_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    return f"{epoch_ms}-{uuid.uuid4()}"


def _metadata_header(payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class BodyReadCounter:
    def __init__(self, app, counters):
        self.app = app
        self.counters = counters

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/api/uploads":
            await self.app(scope, receive, send)
            return
        body_id = dict(scope["headers"]).get(b"x-body-id", b"").decode("ascii")

        async def counted_receive():
            message = await receive()
            if message["type"] == "http.request" and message.get("body"):
                self.counters[body_id] = self.counters.get(body_id, 0) + 1
            return message

        await self.app(scope, counted_receive, send)


def _config(tmp_path):
    return {
        "api": {"host": "127.0.0.1", "port": 8000, "auth_token": "integration-config-token"},
        "concurrent": {"max_workers": 1, "queue_size": 2, "llm_max_workers": 1},
        "storage": {
            "cache_dir": str(tmp_path / "cache"),
            "workspace_dir": str(tmp_path / "workspace"),
            "temp_dir": str(tmp_path / "temp"),
            "audit_db": str(tmp_path / "audit.db"),
            "upload_limits": {
                "max_file_mib": 2,
                "max_media_hours": 1,
                "receive_concurrency": 1,
                "upload_temp_budget_mib": 8,
            },
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


@pytest.fixture()
def upload_client(tmp_path, monkeypatch):
    config = _config(tmp_path)
    runtime = RuntimeContext(config)
    runtime.cache_manager = CacheManager(config["storage"]["cache_dir"])
    runtime.temp_manager = TempFileManager(config["storage"]["temp_dir"])
    runtime.task_queue = asyncio.Queue(maxsize=config["concurrent"]["queue_size"])
    runtime.executor = ThreadPoolExecutor(max_workers=1)
    runtime.started = True
    users_file = tmp_path / "users.json"
    users_file.write_text(json.dumps({"users": {
        OWNER_TOKEN: {"user_id": "alice", "name": "Alice"},
        OTHER_TOKEN: {"user_id": "bob", "name": "Bob"},
    }}), encoding="utf-8")
    monkeypatch.setattr(
        transcription, "user_manager",
        UserManager(users_config_path=str(users_file), fallback_config=config),
    )
    worker_calls = []
    worker_results = queue.Queue()
    worker_lock = threading.Lock()

    def fake_process_transcription(*args, **kwargs):
        path = kwargs["local_media_path"]
        result = {
            "task_id": args[0], "url": args[1],
            "local_media_id": kwargs["local_media_id"],
            "bytes": Path(path).read_bytes(), "path": path,
        }
        with worker_lock:
            worker_calls.append(result)
        worker_results.put(result)

    monkeypatch.setattr(transcription, "process_transcription", fake_process_transcription)
    counters = {}
    app = FastAPI()
    app.include_router(uploads.router)
    app.add_middleware(BodyReadCounter, counters=counters)

    @app.middleware("http")
    async def bind_upload_runtime(request, call_next):
        token = bind_runtime(runtime)
        try:
            return await call_next(request)
        finally:
            unbind_runtime(token)

    @asynccontextmanager
    async def lifespan(app):
        token = bind_runtime(runtime)
        app.state.runtime = runtime
        app.state.queue_processor = asyncio.create_task(transcription.process_task_queue())
        try:
            yield
        finally:
            app.state.queue_processor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await app.state.queue_processor
            runtime.executor.shutdown(wait=True)
            unbind_runtime(token)

    app.router.lifespan_context = lifespan
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, runtime, counters, worker_calls, worker_results


def _post(client, key, payload, raw_bytes, body_id, token=OWNER_TOKEN, **headers):
    request_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "Idempotency-Key": key,
        "X-Upload-Metadata": _metadata_header(payload),
        "X-Body-Id": body_id,
    }
    request_headers.update(headers)
    return client.post("/api/uploads", content=raw_bytes, headers=request_headers)


def _wait_for_worker_release(runtime):
    with runtime._worker_futures_condition:
        assert runtime._worker_futures_condition.wait_for(
            lambda: not runtime.worker_futures, timeout=2
        )
    assert runtime._upload_reserved_bytes == {}
    assert runtime.inflight_registry.size("transcription") == 0


def test_real_http_stream_sqlite_dispatch_idempotency_and_owner_stop(upload_client, monkeypatch):
    client, runtime, body_reads, worker_calls, worker_results = upload_client
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    post_schema = client.get("/openapi.json").json()["paths"]["/api/uploads"]["post"]
    assert "application/octet-stream" in post_schema["requestBody"]["content"]
    header_parameters = {item["name"] for item in post_schema["parameters"]}
    assert {"Idempotency-Key", "X-Upload-Metadata"} <= header_parameters
    capabilities = client.get(
        "/api/uploads/capabilities", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
    )
    assert capabilities.status_code == 200
    assert capabilities.json() == {
        "enabled": True, "default_retention": "30d",
        "retention_options": ["30d", "never"],
        "limits": {
            "max_file_mib": 2, "max_media_hours": 1,
            "receive_concurrency": 1, "upload_temp_budget_mib": 8,
        },
    }

    assert _post(client, _key(), PRODUCER_METADATA, RAW_PRODUCER_BYTES, "bad-auth", token="wrong").status_code == 401
    assert body_reads.get("bad-auth", 0) == 0
    wrong_type = _post(
        client, _key(), PRODUCER_METADATA, RAW_PRODUCER_BYTES, "wrong-type",
        **{"Content-Type": "multipart/form-data"},
    )
    assert wrong_type.status_code == 415
    assert body_reads.get("wrong-type", 0) == 0
    invalid_metadata = client.post(
        "/api/uploads",
        content=RAW_PRODUCER_BYTES,
        headers={
            "Authorization": f"Bearer {OWNER_TOKEN}",
            "Content-Type": "application/octet-stream",
            "Idempotency-Key": _key(),
            "X-Upload-Metadata": "not_base64url",
            "X-Body-Id": "bad-metadata",
        },
    )
    assert invalid_metadata.status_code == 400
    assert body_reads.get("bad-metadata", 0) == 0

    key = _key()
    response = _post(client, key, PRODUCER_METADATA, RAW_PRODUCER_BYTES, "producer")
    assert response.status_code == 202
    receipt = response.json()
    assert receipt["state"] == "accepted"
    assert receipt["task_id"]
    assert receipt["view_token"].startswith("upload_")
    assert body_reads.get("producer", 0) > 0
    stored = runtime.cache_manager.get_local_upload_by_owner_key("alice", key)
    assert Path(stored["media_path"]).read_bytes() == RAW_PRODUCER_BYTES
    assert Path(stored["media_path"]).stat().st_mode & 0o777 == 0o600
    assert stored["byte_size"] == len(RAW_PRODUCER_BYTES)
    assert stored["sha256"] == hashlib.sha256(RAW_PRODUCER_BYTES).hexdigest()
    assert stored["filename"] == "会议 录音.mp4"
    root = runtime.cache_manager.get_task_by_id(receipt["task_id"])
    assert root["view_token"] == ""
    assert root["platform"] == "local_upload"
    assert root["media_id"] == stored["media_id"]
    assert Path(stored["media_path"]).parent == Path(runtime.temp_manager.base_dir) / f"task_{receipt['task_id']}"
    dispatched = worker_results.get(timeout=2)
    assert dispatched["task_id"] == receipt["task_id"]
    assert dispatched["local_media_id"] == stored["media_id"]
    assert dispatched["bytes"] == RAW_PRODUCER_BYTES
    assert "upload-source.bin" in dispatched["path"]
    _wait_for_worker_release(runtime)

    replay = _post(client, key, PRODUCER_METADATA, b"R" * len(RAW_PRODUCER_BYTES), "replay")
    assert replay.status_code == 200
    assert replay.json()["upload_id"] == receipt["upload_id"]
    assert body_reads.get("replay", 0) == 0
    conflict = _post(
        client, key, {**PRODUCER_METADATA, "title": "changed"},
        RAW_PRODUCER_BYTES, "conflict",
    )
    assert conflict.status_code == 409
    assert body_reads.get("conflict", 0) == 0
    assert len(worker_calls) == 1

    independent = _post(client, _key(), PRODUCER_METADATA, RAW_PRODUCER_BYTES, "independent")
    assert independent.status_code == 202
    independent_receipt = independent.json()
    assert independent_receipt["upload_id"] != receipt["upload_id"]
    assert independent_receipt["task_id"] != receipt["task_id"]
    assert worker_results.get(timeout=2)["task_id"] == independent_receipt["task_id"]
    _wait_for_worker_release(runtime)
    assert len(worker_calls) == 2

    owner_receipt = client.get(
        f"/api/uploads/by-idempotency-key/{key}",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    )
    assert owner_receipt.status_code == 200
    assert owner_receipt.json()["upload_id"] == receipt["upload_id"]
    assert client.get(
        f"/api/uploads/by-idempotency-key/{key}",
        headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
    ).status_code == 404
    stopped = client.delete(
        f"/api/uploads/{receipt['upload_id']}/share",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["share_active"] is False
    repeated = client.delete(
        f"/api/uploads/{receipt['upload_id']}/share",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    )
    assert repeated.json()["revoked_at"] == stopped.json()["revoked_at"]
    assert client.delete(
        f"/api/uploads/{independent_receipt['upload_id']}/share",
        headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
    ).status_code == 404


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_file_mib", 0),
        ("max_media_hours", float("inf")),
        ("upload_temp_budget_mib", float("nan")),
        ("receive_concurrency", True),
        ("receive_concurrency", 1.5),
    ],
)
def test_upload_limit_config_rejects_nonpositive_nonfinite_or_noninteger(tmp_path, field, value):
    config = _config(tmp_path)
    config["storage"]["upload_limits"][field] = value
    with pytest.raises(ConfigError, match="storage.upload_limits"):
        validate_config(config)


def test_disabled_and_missing_limits_reject_before_body(upload_client, monkeypatch):
    client, runtime, body_reads, _, _ = upload_client
    monkeypatch.delenv("VTA_UPLOADS_ENABLED", raising=False)
    assert client.get(
        "/api/uploads/capabilities", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
    ).json()["enabled"] is False
    denied = _post(client, _key(), PRODUCER_METADATA, RAW_PRODUCER_BYTES, "disabled")
    assert denied.status_code == 503
    assert body_reads.get("disabled", 0) == 0
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    runtime.config["storage"]["upload_limits"]["upload_temp_budget_mib"] = None
    assert client.get(
        "/api/uploads/capabilities", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
    ).json()["enabled"] is False
    denied = _post(client, _key(), PRODUCER_METADATA, RAW_PRODUCER_BYTES, "missing-limit")
    assert denied.status_code == 503
    assert body_reads.get("missing-limit", 0) == 0


def test_receiver_and_inflight_limits_reject_without_body_consumption(upload_client, monkeypatch):
    client, runtime, body_reads, _, _ = upload_client
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    assert runtime.upload_receiver_slots.acquire(blocking=False)
    receiver_full = _post(client, _key(), PRODUCER_METADATA, RAW_PRODUCER_BYTES, "receiver-full")
    assert receiver_full.status_code == 503
    assert body_reads.get("receiver-full", 0) == 0
    runtime.upload_receiver_slots.release()
    registry = runtime.inflight_registry
    assert registry.try_register("transcription", "occupied-one")
    assert registry.try_register("transcription", "occupied-two")
    inflight_full = _post(client, _key(), PRODUCER_METADATA, RAW_PRODUCER_BYTES, "inflight-full")
    assert inflight_full.status_code == 503
    assert body_reads.get("inflight-full", 0) == 0
    registry.release("transcription", "occupied-one")
    registry.release("transcription", "occupied-two")
    assert registry.size("transcription") == 0
    assert not list(Path(runtime.temp_manager.base_dir).glob("task_*/upload-source.bin"))


def test_actual_free_space_shortage_rejects_before_body_or_file(upload_client, monkeypatch):
    client, runtime, body_reads, worker_calls, _ = upload_client
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(free=1))
    key = _key()
    response = _post(client, key, PRODUCER_METADATA, RAW_PRODUCER_BYTES, "disk-shortage")
    assert response.status_code == 507
    assert body_reads.get("disk-shortage", 0) == 0
    record = runtime.cache_manager.get_local_upload_by_owner_key("alice", key)
    assert record["error_code"] == "upload_temp_budget"
    assert record["root_task_id"] is None
    assert runtime.inflight_registry.size("transcription") == 0
    assert runtime._upload_reserved_bytes == {}
    assert worker_calls == []
    assert not list(Path(runtime.temp_manager.base_dir).glob("task_*/upload-source.bin"))


def test_declared_and_observed_limits_check_bytes_not_content_length(upload_client, monkeypatch):
    client, runtime, body_reads, worker_calls, _ = upload_client
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    declared = {**PRODUCER_METADATA, "byte_size": 2 * 1024 * 1024 + 1}
    response = _post(client, _key(), declared, RAW_PRODUCER_BYTES, "declared-too-large")
    assert response.status_code == 413
    assert body_reads.get("declared-too-large", 0) == 0
    actual_bytes = b"B" * (2 * 1024 * 1024 + 1)
    observed = _post(
        client, _key(), {**PRODUCER_METADATA, "byte_size": 2 * 1024 * 1024},
        actual_bytes, "observed-too-large", **{"Content-Length": "1"},
    )
    assert observed.status_code == 413
    assert body_reads.get("observed-too-large", 0) > 0
    assert runtime.task_queue.qsize() == 0
    assert worker_calls == []
    assert not list(Path(runtime.temp_manager.base_dir).glob("task_*/upload-source.bin"))
    assert runtime._upload_reserved_bytes == {}


def test_client_disconnect_cleans_only_untransferred_partial_media(upload_client, monkeypatch):
    client, runtime, body_reads, worker_calls, _ = upload_client
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    key = _key()
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/api/uploads", "raw_path": b"/api/uploads", "query_string": b"",
        "root_path": "", "client": ("testclient", 50000), "server": ("testserver", 80),
        "headers": [
            (b"authorization", f"Bearer {OWNER_TOKEN}".encode()),
            (b"content-type", b"application/octet-stream"),
            (b"idempotency-key", key.encode()),
            (b"x-upload-metadata", _metadata_header(PRODUCER_METADATA).encode()),
            (b"x-body-id", b"client-disconnect"),
        ],
    }
    receive_index = 0

    async def receive():
        nonlocal receive_index
        receive_index += 1
        if receive_index == 1:
            return {"type": "http.request", "body": RAW_PRODUCER_BYTES[:8], "more_body": True}
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    with pytest.raises(ClientDisconnect):
        client.portal.call(client.app, scope, receive, send)
    record = runtime.cache_manager.get_local_upload_by_owner_key("alice", key)
    assert record["state"] == "receiving"
    assert record["error_code"] == "receive_incomplete"
    assert record["root_task_id"] is None
    assert body_reads.get("client-disconnect", 0) == 1
    assert runtime.task_queue.qsize() == 0
    assert worker_calls == []
    assert not list(Path(runtime.temp_manager.base_dir).glob("task_*/upload-source.bin"))
    assert runtime._upload_reserved_bytes == {}


def test_queue_full_has_no_success_receipt_or_dispatch(upload_client, monkeypatch):
    client, runtime, body_reads, worker_calls, _ = upload_client
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    full_queue = asyncio.Queue(maxsize=1)
    full_queue.put_nowait({"occupies": "the only queue slot"})
    monkeypatch.setattr(uploads, "get_task_queue", lambda: full_queue)
    key = _key()
    response = _post(client, key, PRODUCER_METADATA, RAW_PRODUCER_BYTES, "queue-full")
    assert response.status_code == 503
    assert full_queue.qsize() == 1
    assert body_reads.get("queue-full", 0) == 0
    record = runtime.cache_manager.get_local_upload_by_owner_key("alice", key)
    assert record["error_code"] == "queue_full"
    assert record["state"] == "receiving"
    assert record["root_task_id"] is None
    assert worker_calls == []
    assert not list(Path(runtime.temp_manager.base_dir).glob("task_*/upload-source.bin"))
    assert runtime._upload_reserved_bytes == {}


def test_sql_commit_failure_rolls_back_root_and_never_queues(upload_client, monkeypatch):
    client, runtime, body_reads, worker_calls, _ = upload_client
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    cache = runtime.cache_manager

    @contextmanager
    def fail_root_commit():
        connection = cache._get_connection()
        cursor = connection.cursor()
        inserted_root = False

        class CursorProxy:
            def execute(self, sql, parameters=()):
                nonlocal inserted_root
                if "INSERT INTO task_status" in sql:
                    inserted_root = True
                return cursor.execute(sql, parameters)

            def __getattr__(self, name):
                return getattr(cursor, name)

        try:
            yield CursorProxy()
            if inserted_root:
                raise sqlite3.OperationalError("injected commit failure")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()

    monkeypatch.setattr(cache, "_get_cursor", fail_root_commit)
    key = _key()
    response = _post(client, key, PRODUCER_METADATA, RAW_PRODUCER_BYTES, "commit-failure")
    assert response.status_code == 500
    assert body_reads.get("commit-failure", 0) > 0
    assert runtime.task_queue.qsize() == 0
    record = cache.get_local_upload_by_owner_key("alice", key)
    assert record["state"] == "receiving"
    assert record["root_task_id"] is None
    with cache._get_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM task_status")
        assert cursor.fetchone()[0] == 0
    assert worker_calls == []
    assert runtime._upload_reserved_bytes == {}
    assert not list(Path(runtime.temp_manager.base_dir).glob("task_*/upload-source.bin"))


def test_acceptance_window_expiry_after_receive_is_explicit_410(upload_client, monkeypatch):
    client, runtime, body_reads, worker_calls, _ = upload_client
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    validate = CacheManager._validate_upload_key_window.__func__
    calls = 0

    def expire_at_accept(cls, key, now):
        nonlocal calls
        calls += 1
        if calls == 2:
            now += datetime.timedelta(hours=24)
        return validate(cls, key, now)

    monkeypatch.setattr(CacheManager, "_validate_upload_key_window", classmethod(expire_at_accept))
    key = _key()
    response = _post(client, key, PRODUCER_METADATA, RAW_PRODUCER_BYTES, "window-expired")
    assert calls == 2
    assert response.status_code == 410
    assert body_reads.get("window-expired", 0) > 0
    record = runtime.cache_manager.get_local_upload_by_owner_key("alice", key)
    assert record["state"] == "receiving"
    assert record["error_code"] == "acceptance_window_expired"
    assert record["root_task_id"] is None
    assert runtime.task_queue.qsize() == 0
    assert worker_calls == []
    assert not list(Path(runtime.temp_manager.base_dir).glob("task_*/upload-source.bin"))


def test_queue_put_failure_rolls_back_formal_receipt(upload_client, monkeypatch):
    client, runtime, body_reads, worker_calls, _ = upload_client
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")

    class FailingQueue(asyncio.Queue):
        def full(self):
            return False

        def put_nowait(self, item):
            raise asyncio.QueueFull

    failing_queue = FailingQueue(maxsize=1)
    monkeypatch.setattr(uploads, "get_task_queue", lambda: failing_queue)
    key = _key()
    response = _post(client, key, PRODUCER_METADATA, RAW_PRODUCER_BYTES, "put-failure")
    assert response.status_code == 503
    assert body_reads.get("put-failure", 0) > 0
    record = runtime.cache_manager.get_local_upload_by_owner_key("alice", key)
    assert record["state"] == "receiving"
    assert record["error_code"] == "queue_full"
    assert record["root_task_id"] is None
    assert record["view_token"] is None
    with runtime.cache_manager._get_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM task_status")
        assert cursor.fetchone()[0] == 0
    assert failing_queue.qsize() == 0
    assert worker_calls == []
    assert runtime._upload_reserved_bytes == {}
    assert not list(Path(runtime.temp_manager.base_dir).glob("task_*/upload-source.bin"))


def test_lost_202_receipt_retry_queries_original_without_requeue(upload_client, monkeypatch):
    client, runtime, body_reads, worker_calls, worker_results = upload_client
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    key = _key()
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/api/uploads", "raw_path": b"/api/uploads", "query_string": b"",
        "root_path": "", "client": ("testclient", 50000), "server": ("testserver", 80),
        "headers": [
            (b"authorization", f"Bearer {OWNER_TOKEN}".encode()),
            (b"content-type", b"application/octet-stream"),
            (b"idempotency-key", key.encode()),
            (b"x-upload-metadata", _metadata_header(PRODUCER_METADATA).encode()),
            (b"x-body-id", b"lost-receipt"),
        ],
    }
    sent_body = False
    server_status = []

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": RAW_PRODUCER_BYTES, "more_body": False}
        return {"type": "http.disconnect"}

    async def discard_response(message):
        if message["type"] == "http.response.start":
            server_status.append(message["status"])
        # Simulate a lost network response: the server sends it, producer receives none.

    client.portal.call(client.app, scope, receive, discard_response)
    assert server_status == [202]
    assert body_reads.get("lost-receipt", 0) > 0
    original = runtime.cache_manager.get_local_upload_by_owner_key("alice", key)
    assert original["state"] == "accepted"
    task_id = original["root_task_id"]
    assert worker_results.get(timeout=2)["task_id"] == task_id
    _wait_for_worker_release(runtime)

    retry = _post(client, key, PRODUCER_METADATA, b"Z" * len(RAW_PRODUCER_BYTES), "receipt-retry")
    assert retry.status_code == 200
    assert retry.json()["task_id"] == task_id
    assert retry.json()["upload_id"] == original["upload_id"]
    assert body_reads.get("receipt-retry", 0) == 0
    assert len(worker_calls) == 1
    assert runtime.task_queue.qsize() == 0
