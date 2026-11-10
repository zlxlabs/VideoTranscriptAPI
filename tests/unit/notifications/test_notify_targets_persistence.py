import datetime
import json
import sqlite3
import uuid

from loguru import logger
import pytest

from video_transcript_api.cache.cache_manager import CacheManager


def test_notify_targets_survive_sqlite_close_and_reopen(tmp_path):
    db_path = tmp_path / "cache.db"
    cache = CacheManager(cache_dir=str(tmp_path / "cache"), db_path=str(db_path))
    task = cache.create_task(
        url="https://example.invalid/task",
        submitted_by="user-a",
        notify_targets={"feishu": "fixture-feishu"},
    )
    task_id = task["task_id"]
    cache.close()

    reopened = CacheManager(cache_dir=str(tmp_path / "cache"), db_path=str(db_path))
    try:
        stored = reopened.get_task_by_id(task_id)
        assert stored["notify_targets"] == {"feishu": "fixture-feishu"}
        with sqlite3.connect(db_path) as connection:
            payload = connection.execute(
                "SELECT notify_targets FROM task_status WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
        assert json.loads(payload) == {"feishu": "fixture-feishu"}
    finally:
        reopened.close()


def test_new_upload_task_without_explicit_targets_persists_empty_object(tmp_path):
    db_path = tmp_path / "uploads.db"
    cache = CacheManager(cache_dir=str(tmp_path / "cache"), db_path=str(db_path))
    now = datetime.datetime.now(datetime.timezone.utc)
    intent = cache.register_local_upload(
        owner_user_id="upload-owner",
        idempotency_key=f"{int(now.timestamp() * 1000)}-{uuid.uuid4()}",
        retention="30d",
        intent_metadata={"filename": "fixture.mp4"},
    )
    task_id = "task-upload-target-fixture"
    cache.accept_local_upload(
        intent["upload_id"],
        media_id="upload_notify_fixture",
        task_id=task_id,
        filename="fixture.mp4",
        title="fixture",
        request_metadata={"filename": "fixture.mp4"},
        media_path="/tmp/fixture.mp4",
        byte_size=1,
        sha256="a" * 64,
    )

    task = cache.get_task_by_id(task_id)
    assert task["notify_targets"] == {}
    with sqlite3.connect(db_path) as connection:
        stored = connection.execute(
            "SELECT notify_targets FROM task_status WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    assert stored == "{}"
    cache.close()


def test_old_task_rows_keep_null_targets_after_idempotent_migration(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """CREATE TABLE task_status (
                task_id TEXT PRIMARY KEY, view_token TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
                download_url TEXT, platform TEXT, media_id TEXT,
                use_speaker_recognition BOOLEAN DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued', progress TEXT, title TEXT,
                author TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP, cache_id INTEGER, llm_config TEXT,
                error_message TEXT, calibration_status TEXT, summary_status TEXT,
                chapters_status TEXT, processing_options TEXT, submitted_by TEXT,
                terminal_snapshot TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO task_status(task_id, view_token, url, submitted_by) "
            "VALUES ('legacy-task', 'view-legacy', 'https://example.invalid', 'user-a')"
        )

    cache = CacheManager(cache_dir=str(tmp_path / "cache"), db_path=str(db_path))
    cache.close()
    migrated = CacheManager(cache_dir=str(tmp_path / "cache"), db_path=str(db_path))
    try:
        task = migrated.get_task_by_id("legacy-task")
        assert task["notify_targets"] is None
        assert task["url"] == "https://example.invalid"
        with sqlite3.connect(db_path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(task_status)")}
            assert "notify_targets" in columns
            assert connection.execute(
                "SELECT notify_targets FROM task_status WHERE task_id='legacy-task'"
            ).fetchone()[0] is None
    finally:
        migrated.close()


def test_corrupt_non_null_targets_fail_fast_instead_of_becoming_legacy(tmp_path):
    cache = CacheManager(cache_dir=str(tmp_path / "cache"))
    task_id = cache.create_task(
        url="https://example.invalid/corrupt", notify_targets={}
    )["task_id"]
    with cache._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET notify_targets = '[]' WHERE task_id = ?", (task_id,)
        )

    with pytest.raises(ValueError, match="notify_targets must be a JSON object"):
        cache.get_task_by_id(task_id)
    cache.close()


def test_missing_task_row_has_no_in_memory_target_fallback():
    from video_transcript_api.api.services.terminal_status import load_task_targets

    class MissingTaskCache:
        def get_task_by_id(self, task_id):
            return None

    assert load_task_targets(
        MissingTaskCache(), "missing-task"
    ) == {}


def test_legacy_target_fallback_emits_grepable_task_id(tmp_path, monkeypatch):
    from video_transcript_api.api import context
    from video_transcript_api.api.services.terminal_status import _task_notify_targets

    cache = CacheManager(cache_dir=str(tmp_path / "cache"))
    task_id = cache.create_task(url="https://example.invalid/legacy", submitted_by="user-a")[
        "task_id"
    ]
    with cache._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET notify_targets = NULL WHERE task_id = ?",
            (task_id,),
        )
    runtime = type("Runtime", (), {})()
    runtime.user_manager = type("UserManager", (), {})()
    runtime.user_manager.get_user_by_id = lambda user_id: {
        "user_id": user_id, "wechat_webhook": "fixture-legacy-wechat"
    }
    monkeypatch.setattr(context, "get_runtime", lambda: runtime)
    records = []
    sink_id = logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        assert _task_notify_targets(cache.get_task_by_id(task_id)) == {
            "wechat": "fixture-legacy-wechat"
        }
    finally:
        logger.remove(sink_id)
        cache.close()

    assert any(f"NOTIFY_TARGETS_LEGACY_ROW task_id={task_id}" in line for line in records)
