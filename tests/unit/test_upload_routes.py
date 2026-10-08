import base64
import datetime
import json
import sqlite3
import uuid

import pytest

from video_transcript_api.api.routes import uploads
from video_transcript_api.api.routes.uploads import decode_upload_metadata
from video_transcript_api.cache.cache_manager import CacheManager


def encode_metadata(payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_rejection_log_has_grepable_upload_intake_event(monkeypatch):
    observed = []
    monkeypatch.setattr(uploads.logger, "warning", lambda *args: observed.append(args))

    exception = uploads._reject(413, "declared_size", "too large")

    assert exception.status_code == 413
    assert observed == [
        ("UPLOAD_INTAKE_REJECTED reason={} status={}", "declared_size", 413)
    ]


def test_decode_upload_metadata_preserves_unicode_and_normalizes_options():
    payload = {
        "filename": "../会议 录音.mp4",
        "byte_size": 17,
        "title": "讨论：咖啡与模型",
        "source_url": "https://example.com/watch?id=1",
        "retention": "30d",
        "processing_options": {"calibrate": False, "summarize": True},
    }

    metadata = decode_upload_metadata(encode_metadata(payload))

    assert metadata == {
        **payload,
        "filename": "会议 录音.mp4",
        "processing_options": {
            "calibrate": False,
            "summarize": True,
            "infer_speaker_names": True,
            "chapters": True,
        },
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"title": "x" * 201},
        {"title": "bad\nname"},
        {"filename": "clip\u0000.mp4"},
        {"filename": "é" * 128},
        {"source_url": "https://user:secret@example.com/a"},
        {"source_url": "javascript:alert(1)"},
        {"byte_size": 0},
    ],
)
def test_decode_upload_metadata_rejects_invalid_bounded_fields(mutation):
    payload = {
        "filename": "clip.mp4",
        "byte_size": 17,
        "title": None,
        "source_url": None,
        "retention": "30d",
        "processing_options": {},
        **mutation,
    }

    with pytest.raises(ValueError):
        decode_upload_metadata(encode_metadata(payload))


def test_receiving_cleanup_retires_only_expired_keys_and_removes_staging_file(tmp_path):
    now = datetime.datetime(2026, 10, 8, 12, 0, tzinfo=datetime.timezone.utc)
    old_creation = now - datetime.timedelta(hours=23)
    old_key = f"{int(old_creation.timestamp() * 1000)}-{uuid.uuid4()}"
    current_key = f"{int(now.timestamp() * 1000)}-{uuid.uuid4()}"
    manager = CacheManager(str(tmp_path / "cache"))
    temp_dir = tmp_path / "temp"
    old_path = temp_dir / "task_old" / "partial.bin"
    current_path = temp_dir / "task_current" / "partial.bin"
    old_path.parent.mkdir(parents=True)
    current_path.parent.mkdir(parents=True)
    old_path.write_bytes(b"partial old body")
    current_path.write_bytes(b"still receiving")

    old_record = manager.register_local_upload(
        owner_user_id="alice", idempotency_key=old_key, retention="30d", now=now
    )
    current_record = manager.register_local_upload(
        owner_user_id="alice", idempotency_key=current_key, retention="30d", now=now
    )
    manager.set_local_upload_receiving_path(old_record["upload_id"], str(old_path))
    manager.set_local_upload_receiving_path(current_record["upload_id"], str(current_path))

    retired = manager.cleanup_expired_local_upload_receiving(
        str(temp_dir), now=now + datetime.timedelta(hours=2)
    )

    assert retired == 1
    assert not old_path.exists()
    assert current_path.read_bytes() == b"still receiving"
    assert manager.get_local_upload_by_owner_key("alice", old_key) is None
    assert manager.get_local_upload_by_owner_key("alice", current_key)["upload_id"] == current_record["upload_id"]
    with pytest.raises(ValueError, match="24-hour"):
        manager.register_local_upload(
            owner_user_id="alice",
            idempotency_key=old_key,
            retention="30d",
            now=now + datetime.timedelta(hours=25),
        )


def test_cache_manager_adds_upload_mapping_columns_to_existing_a_schema(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    database = cache_dir / "cache.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE local_uploads (
            upload_id TEXT PRIMARY KEY,
            owner_user_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            idempotency_created_at TEXT NOT NULL,
            metadata_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('receiving', 'accepted')),
            retention TEXT NOT NULL CHECK(retention IN ('30d', 'never')),
            root_task_id TEXT UNIQUE,
            media_id TEXT UNIQUE,
            view_token TEXT UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT,
            error_code TEXT,
            UNIQUE(owner_user_id, idempotency_key)
        )"""
    )
    connection.commit()
    connection.close()

    CacheManager(str(cache_dir))

    connection = sqlite3.connect(database)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(local_uploads)")}
    connection.close()
    assert {
        "filename", "source_url", "request_metadata", "media_path", "byte_size", "sha256"
    } <= columns


def test_acceptance_deadline_is_exactly_key_creation_plus_24_hours(tmp_path):
    now = datetime.datetime(2026, 10, 8, 12, 0, tzinfo=datetime.timezone.utc)
    created = now - datetime.timedelta(hours=23)
    key = f"{int(created.timestamp() * 1000)}-{uuid.uuid4()}"
    manager = CacheManager(str(tmp_path / "cache"))
    intent = manager.register_local_upload(
        owner_user_id="alice", idempotency_key=key, retention="30d", now=now
    )

    with pytest.raises(ValueError, match="24-hour"):
        manager.accept_local_upload(
            intent["upload_id"],
            media_id="upload-test-media",
            now=created + datetime.timedelta(hours=24),
        )
    assert manager.get_local_upload_by_id(intent["upload_id"])["state"] == "receiving"
    with manager._get_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM task_status")
        assert cursor.fetchone()[0] == 0
