"""Real SQLite contracts for the local-upload safety baseline."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import uuid

import pytest

from src.video_transcript_api.api.routes.audit import check_view_token_ownership
from src.video_transcript_api.api.services import view_token_resolver as resolver_module
from src.video_transcript_api.api.services.view_token_resolver import ViewTokenResolver
from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.tempfile_manager import TempFileManager


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _intent_key(now=None):
    now = now or datetime.now(timezone.utc)
    epoch_ms = int(now.timestamp() * 1000)
    return f"{epoch_ms}-{uuid.uuid4()}"


def _accepted_upload(cm, *, retention="30d", owner="alice"):
    upload = cm.register_local_upload(
        owner_user_id=owner,
        idempotency_key=_intent_key(),
        retention=retention,
    )
    return cm.accept_local_upload(
        upload["upload_id"],
        media_id=f"media-{uuid.uuid4().hex}",
        title="本地上传",
    )


def _finish_with_transcript(cm, upload, status="success"):
    media_id = upload["media_id"]
    cm.save_cache(
        platform="local_upload",
        url="",
        media_id=media_id,
        use_speaker_recognition=False,
        transcript_data="A real local transcript.",
        transcript_type="capswriter",
        title="本地上传",
        author="",
        description="",
    )
    cm.update_task_status(
        upload["root_task_id"],
        status,
        platform="local_upload",
        media_id=media_id,
        title="本地上传",
        error_message="transcription failed" if status == "failed" else None,
    )


def _old_time(days=400):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def test_real_sqlite_upload_identity_owner_key_and_intent_window(cm):
    now = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
    key = _intent_key(now)
    first = cm.register_local_upload(
        owner_user_id="alice",
        idempotency_key=key,
        retention="30d",
        intent_metadata={"filename": "clip.mp4"},
        now=now,
    )
    replay = cm.register_local_upload(
        owner_user_id="alice",
        idempotency_key=key,
        retention="30d",
        intent_metadata={"filename": "clip.mp4"},
        now=now,
    )
    another_owner = cm.register_local_upload(
        owner_user_id="bob", idempotency_key=key, retention="30d", now=now
    )

    assert replay["upload_id"] == first["upload_id"]
    assert another_owner["upload_id"] != first["upload_id"]
    with pytest.raises(ValueError, match="metadata"):
        cm.register_local_upload(
            owner_user_id="alice", idempotency_key=key, retention="never", now=now
        )
    with pytest.raises(ValueError, match="metadata"):
        cm.register_local_upload(
            owner_user_id="alice",
            idempotency_key=key,
            retention="30d",
            intent_metadata={"filename": "different.mp4"},
            now=now,
        )

    too_old = _intent_key(now - timedelta(hours=24, milliseconds=1))
    too_far_future = _intent_key(now + timedelta(minutes=5, milliseconds=1))
    with pytest.raises(ValueError, match="24-hour"):
        cm.register_local_upload(
            owner_user_id="alice", idempotency_key=too_old, retention="30d", now=now
        )
    with pytest.raises(ValueError, match="clock skew"):
        cm.register_local_upload(
            owner_user_id="alice",
            idempotency_key=too_far_future,
            retention="30d",
            now=now,
        )

    # Existing receipts remain queryable after the acceptance window; once a
    # retired row is removed, its timestamp cannot create a second upload.
    old_receipt_key = _intent_key(now - timedelta(hours=23))
    old_receipt = cm.register_local_upload(
        owner_user_id="alice", idempotency_key=old_receipt_key, retention="30d", now=now
    )
    later = now + timedelta(hours=25)
    assert cm.get_local_upload_by_owner_key("alice", old_receipt_key)["upload_id"] == old_receipt["upload_id"]
    cm.register_local_upload(
        owner_user_id="alice", idempotency_key=old_receipt_key, retention="30d", now=later
    )
    with cm._get_cursor() as cursor:
        cursor.execute("DELETE FROM local_uploads WHERE upload_id = ?", (old_receipt["upload_id"],))
    with pytest.raises(ValueError, match="24-hour"):
        cm.register_local_upload(
            owner_user_id="alice", idempotency_key=old_receipt_key, retention="30d", now=later
        )


def test_concurrent_same_owner_key_creates_one_sqlite_receipt(cm):
    now = datetime.now(timezone.utc)
    key = _intent_key(now)
    start = threading.Barrier(2)

    def register():
        start.wait(timeout=5)
        return cm.register_local_upload(
            owner_user_id="alice",
            idempotency_key=key,
            retention="30d",
            intent_metadata={"filename": "same.mp4"},
            now=now,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(register)
        second = pool.submit(register)
        records = [first.result(timeout=10), second.result(timeout=10)]

    assert records[0]["upload_id"] == records[1]["upload_id"]
    with cm._get_cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM local_uploads WHERE owner_user_id = ? AND idempotency_key = ?",
            ("alice", key),
        )
        assert cursor.fetchone()[0] == 1


def test_upload_task_uses_blank_legacy_column_and_separate_upload_token(cm):
    upload = _accepted_upload(cm)

    with cm._get_cursor() as cursor:
        cursor.execute(
            "SELECT view_token, platform, submitted_by FROM task_status WHERE task_id = ?",
            (upload["root_task_id"],),
        )
        task_row = dict(cursor.fetchone())

    assert task_row == {"view_token": "", "platform": "local_upload", "submitted_by": "alice"}
    assert upload["view_token"].startswith("upload_")
    assert upload["view_token"] != task_row["view_token"]
    replay = cm.accept_local_upload(
        upload["upload_id"], media_id="ignored-replay-media", title="replay"
    )
    assert replay["root_task_id"] == upload["root_task_id"]
    assert replay["view_token"] == upload["view_token"]
    assert cm.get_task_by_view_token("") is None
    assert check_view_token_ownership(
        "", upload["root_task_id"], "alice", cm, None
    ) is False
    assert cm.get_task_by_view_token(upload["view_token"]) is None
    assert cm.get_local_upload_by_view_token(upload["view_token"])["upload_id"] == upload["upload_id"]

    # A legacy token collision cannot turn the upload capability into a task
    # lookup fallback, even if a legacy row was inserted by old code.
    with cm._get_cursor() as cursor:
        cursor.execute(
            """INSERT INTO task_status
               (task_id, view_token, url, platform, media_id, status, llm_config)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "legacy-shadow",
                upload["view_token"],
                "https://legacy.example",
                "youtube",
                "legacy",
                "success",
                '{"legacy-secret": true}',
            ),
        )
    assert cm.get_task_by_view_token(upload["view_token"]) is None
    assert ViewTokenResolver(cm)._get_llm_config_by_view_token(upload["view_token"]) is None


def test_root_terminal_sets_fixed_30_day_expiry_for_success_and_failure(cm):
    for status in ("success", "failed"):
        upload = _accepted_upload(cm, retention="30d")
        _finish_with_transcript(cm, upload, status=status)
        stored = cm.get_local_upload_by_id(upload["upload_id"])
        with cm._get_cursor() as cursor:
            cursor.execute(
                "SELECT datetime(completed_at, '+30 days') FROM task_status WHERE task_id = ?",
                (upload["root_task_id"],),
            )
            expected = cursor.fetchone()[0]
        assert stored["expires_at"] == expected

        # Subsequent task writes, including late updates, cannot extend the
        # root's original expiry because terminal task state is write-once.
        cm.update_task_status(upload["root_task_id"], "failed", error_message="late")
        assert cm.get_local_upload_by_id(upload["upload_id"])["expires_at"] == expected


def test_never_expiry_and_revocation_are_write_once(cm):
    upload = _accepted_upload(cm, retention="never")
    _finish_with_transcript(cm, upload)
    assert cm.get_local_upload_by_id(upload["upload_id"])["expires_at"] is None

    first_time = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
    later_time = first_time + timedelta(days=3)
    first = cm.revoke_local_upload(upload["upload_id"], now=first_time)
    replay = cm.revoke_local_upload(upload["upload_id"], now=later_time)
    assert first["revoked_at"] == "2026-10-08 09:00:00"
    assert replay["revoked_at"] == first["revoked_at"]
    with pytest.raises(sqlite3.IntegrityError, match="write-once"):
        with cm._get_cursor() as cursor:
            cursor.execute(
                "UPDATE local_uploads SET revoked_at = NULL WHERE upload_id = ?",
                (upload["upload_id"],),
            )


def test_resolver_uses_real_subprocess_environment_and_sqlite(tmp_path):
    cache_root = tmp_path / "cache"
    manager = CacheManager(cache_dir=str(cache_root))
    upload = _accepted_upload(manager, retention="never")
    _finish_with_transcript(manager, upload)
    manager.close()

    script = (
        "import sys; "
        "from src.video_transcript_api.cache.cache_manager import CacheManager; "
        "from src.video_transcript_api.api.services.view_token_resolver import ViewTokenResolver; "
        "cm=CacheManager(cache_dir=sys.argv[1]); "
        "data=ViewTokenResolver(cm).get_view_data_by_token(sys.argv[2]); "
        "print('RESULT=' + str((data or {}).get('status', 'NONE'))); "
        "print('BODY=' + str((data or {}).get('transcript', 'NONE'))); cm.close()"
    )
    disabled_env = os.environ.copy()
    disabled_env.pop("VTA_UPLOADS_ENABLED", None)
    disabled = subprocess.run(
        [sys.executable, "-c", script, str(cache_root), upload["view_token"]],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=disabled_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert disabled.returncode == 0, disabled.stderr
    assert "UPLOAD_DISABLED" in disabled.stdout
    assert "RESULT=NONE" in disabled.stdout
    assert "BODY=NONE" in disabled.stdout

    enabled_env = disabled_env | {"VTA_UPLOADS_ENABLED": "true"}
    enabled = subprocess.run(
        [sys.executable, "-c", script, str(cache_root), upload["view_token"]],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=enabled_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert enabled.returncode == 0, enabled.stderr
    assert "RESULT=success" in enabled.stdout
    assert "BODY=A real local transcript." in enabled.stdout


def test_upload_progress_token_resolves_before_30_day_terminal_clock(cm, monkeypatch):
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    upload = _accepted_upload(cm, retention="30d")

    assert ViewTokenResolver(cm).get_view_data_by_token(upload["view_token"])["status"] == "processing"
    assert cm.get_local_upload_by_id(upload["upload_id"])["expires_at"] is None


def test_resolver_reads_active_upload_and_denies_disabled_revoked_or_expired(cm, monkeypatch):
    warnings = []
    monkeypatch.setattr(resolver_module.logger, "warning", lambda *args, **kwargs: warnings.append(args))
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    short = _accepted_upload(cm, retention="30d")
    long = _accepted_upload(cm, retention="never")
    _finish_with_transcript(cm, short)
    _finish_with_transcript(cm, long)
    resolver = ViewTokenResolver(cm)

    assert resolver.get_view_data_by_token(short["view_token"])["transcript"] == "A real local transcript."
    assert resolver.get_cache_by_view_token(long["view_token"])["transcript_data"] == "A real local transcript."

    monkeypatch.delenv("VTA_UPLOADS_ENABLED", raising=False)
    assert resolver.get_view_data_by_token(short["view_token"]) is None
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "false")
    assert resolver.get_view_data_by_token(short["view_token"]) is None
    assert resolver.get_cache_by_view_token(long["view_token"]) is None
    assert any(resolver_module.UPLOAD_DISABLED in str(args) for args in warnings)
    assert check_view_token_ownership(
        short["view_token"], short["root_task_id"], "alice", cm, None
    ) is False

    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    cm.revoke_local_upload(long["upload_id"])
    assert resolver.get_view_data_by_token(long["view_token"]) is None

    with cm._get_cursor() as cursor:
        cursor.execute(
            "UPDATE local_uploads SET expires_at = ? WHERE upload_id = ?",
            (_old_time(31), short["upload_id"]),
        )
    assert resolver.get_view_data_by_token(short["view_token"]) is None
    expired_record = cm.get_local_upload_by_view_token(short["view_token"])
    exactly_expired = datetime.strptime(
        expired_record["expires_at"], "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone.utc)
    assert cm.local_upload_share_is_active(expired_record, exactly_expired) is False


def test_cleanup_protects_active_upload_cache_even_when_feature_is_disabled(cm, monkeypatch):
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "false")
    for retention in ("30d", "never"):
        upload = _accepted_upload(cm, retention=retention)
        _finish_with_transcript(cm, upload)
        with cm._get_cursor() as cursor:
            cursor.execute(
                "UPDATE video_cache SET updated_at = ? WHERE platform = 'local_upload' AND media_id = ?",
                (_old_time(), upload["media_id"]),
            )
        assert cm.cleanup_old_cache(days=1) == 0
        assert cm.get_cache("local_upload", upload["media_id"]) is not None


def test_cleanup_reclaims_revoked_or_expired_upload_cache(cm):
    revoked = _accepted_upload(cm, retention="never")
    _finish_with_transcript(cm, revoked)
    cm.revoke_local_upload(revoked["upload_id"])

    expired = _accepted_upload(cm, retention="30d")
    _finish_with_transcript(cm, expired)
    with cm._get_cursor() as cursor:
        cursor.execute(
            "UPDATE local_uploads SET expires_at = ? WHERE upload_id = ?",
            (_old_time(31), expired["upload_id"]),
        )
        cursor.execute(
            "UPDATE video_cache SET updated_at = ? WHERE platform = 'local_upload'",
            (_old_time(),),
        )

    assert cm.cleanup_old_cache(days=1) == 2
    assert cm.get_cache("local_upload", revoked["media_id"]) is None
    assert cm.get_cache("local_upload", expired["media_id"]) is None


def test_task_cleanup_keeps_effective_share_root_for_audit_and_url_rows_still_expire(cm):
    uploads = [
        _accepted_upload(cm, retention="never"),
        _accepted_upload(cm, retention="30d"),
    ]
    for upload in uploads:
        _finish_with_transcript(cm, upload)
    url_task = cm.create_task("https://example.com/old", platform="youtube", media_id="old-url")
    cm.update_task_status(url_task["task_id"], "success", platform="youtube", media_id="old-url")
    with cm._get_cursor() as cursor:
        for upload in uploads:
            cursor.execute(
                "UPDATE task_status SET completed_at = ?, created_at = ? WHERE task_id = ?",
                (_old_time(), _old_time(), upload["root_task_id"]),
            )
        cursor.execute(
            "UPDATE task_status SET completed_at = ?, created_at = ? WHERE task_id = ?",
            (_old_time(), _old_time(), url_task["task_id"]),
        )
    archived = []
    cm.audit_logger = type(
        "Audit",
        (),
        {
            "archive_task_snapshot": lambda self, task: archived.append(task["task_id"]),
            "expire_task_snapshot": lambda self, task_id: False,
            "restore_live_task_snapshot": lambda self, task: None,
        },
    )()

    assert cm.cleanup_task_status(retention_days=1) == 1
    assert all(cm.get_task_by_id(upload["root_task_id"]) is not None for upload in uploads)
    assert cm.get_task_by_id(url_task["task_id"]) is None
    assert all(upload["root_task_id"] not in archived for upload in uploads)


def test_revoke_and_task_cleanup_are_ordered_at_the_sqlite_guard(cm, monkeypatch):
    upload = _accepted_upload(cm, retention="never")
    _finish_with_transcript(cm, upload)
    with cm._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET completed_at = ?, created_at = ? WHERE task_id = ?",
            (_old_time(), _old_time(), upload["root_task_id"]),
        )
    cm.audit_logger = type(
        "Audit",
        (),
        {
            "archive_task_snapshot": lambda self, task: None,
            "expire_task_snapshot": lambda self, task_id: False,
            "restore_live_task_snapshot": lambda self, task: None,
        },
    )()

    predicate_checked = threading.Event()
    release_cleanup = threading.Event()
    original_predicate = cm._has_active_local_upload_for_task

    def pause_after_active_check(cursor, task_id, now_text):
        active = original_predicate(cursor, task_id, now_text)
        predicate_checked.set()
        assert release_cleanup.wait(timeout=10)
        return active

    monkeypatch.setattr(cm, "_has_active_local_upload_for_task", pause_after_active_check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        cleanup = pool.submit(cm.cleanup_task_status, 1)
        assert predicate_checked.wait(timeout=10)
        revoke_started = threading.Event()

        def revoke_after_cleanup_locked():
            revoke_started.set()
            return cm.revoke_local_upload(upload["upload_id"])

        revoke = pool.submit(revoke_after_cleanup_locked)
        assert revoke_started.wait(timeout=10)
        release_cleanup.set()
        assert cleanup.result(timeout=10) == 0
        assert revoke.result(timeout=10)["revoked_at"] is not None
    assert cm.get_task_by_id(upload["root_task_id"]) is not None

    # Opposite order: complete revocation before the cleaner takes its guarded
    # snapshot. The now-inactive root follows the pre-existing retention rule.
    second = _accepted_upload(cm, retention="never")
    _finish_with_transcript(cm, second)
    with cm._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET completed_at = ?, created_at = ? WHERE task_id = ?",
            (_old_time(), _old_time(), second["root_task_id"]),
        )
    cm.revoke_local_upload(second["upload_id"])
    monkeypatch.setattr(cm, "_has_active_local_upload_for_task", original_predicate)
    assert cm.cleanup_task_status(retention_days=1) == 2
    assert cm.get_task_by_id(upload["root_task_id"]) is None
    assert cm.get_task_by_id(second["root_task_id"]) is None


def test_expired_or_revoked_upload_task_rows_follow_existing_cleanup(cm):
    upload = _accepted_upload(cm, retention="never")
    _finish_with_transcript(cm, upload)
    cm.revoke_local_upload(upload["upload_id"])
    with cm._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET completed_at = ?, created_at = ? WHERE task_id = ?",
            (_old_time(), _old_time(), upload["root_task_id"]),
        )
    cm.audit_logger = type(
        "Audit",
        (),
        {
            "archive_task_snapshot": lambda self, task: None,
            "expire_task_snapshot": lambda self, task_id: False,
            "restore_live_task_snapshot": lambda self, task: None,
        },
    )()

    assert cm.cleanup_task_status(retention_days=1) == 1
    assert cm.get_task_by_id(upload["root_task_id"]) is None


def test_temp_source_media_remains_ephemeral_for_active_share(tmp_path, cm):
    upload = _accepted_upload(cm, retention="never")
    _finish_with_transcript(cm, upload)
    manager = TempFileManager(str(tmp_path / "temp"), retention_hours=0)
    task_dir = manager.create_task_dir("local-root")
    source_media = task_dir / "source.mp4"
    source_media.write_bytes(b"original media")
    old = datetime.now(timezone.utc).timestamp() - 7200
    os.utime(source_media, (old, old))

    assert manager.clean_up_old_files(hours=0) == 1
    assert not task_dir.exists()
