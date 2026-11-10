import datetime
import json
import sqlite3
import threading
import uuid

from loguru import logger
import pytest
from wecom_notifier import FeishuNotifier, WeComNotifier
from wecom_notifier.platforms.feishu import sender as feishu_sender
from wecom_notifier.platforms.wecom import sender as wecom_sender

from video_transcript_api.cache.cache_manager import CacheManager


# Global group addresses are deliberately distinct from the persisted task
# targets below, so any leak back to the global config is observable at the
# HTTP boundary.
_GLOBAL_WECHAT = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=persist-global-w"
_GLOBAL_FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/persist-global-f"
_TENANT_WECHAT = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=persist-tenant-w"
_TENANT_FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/persist-tenant-f"
_BASE_URL = "https://share.example.test"


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


@pytest.fixture
def http_boundary(monkeypatch):
    """Real router, channels and notifier queues; only the final external
    HTTP POST is replaced so assertions land on actual request URLs.
    """
    from video_transcript_api.utils.notifications import channel, router, wechat
    from video_transcript_api.utils import rendering

    config = {
        "wechat": {"webhook": _GLOBAL_WECHAT},
        "feishu": {"webhook": _GLOBAL_FEISHU, "secret": None},
    }
    records = []
    condition = threading.Condition()

    class Response:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def post(url, *, json, **_kwargs):
        with condition:
            records.append((url, json))
            condition.notify_all()
        if "qyapi.weixin.qq.com" in url:
            return Response({"errcode": 0, "errmsg": "ok"})
        return Response({"code": 0, "msg": "ok"})

    monkeypatch.setattr(wecom_sender.requests, "post", post)
    monkeypatch.setattr(feishu_sender.requests, "post", post)
    monkeypatch.setattr(router, "load_config", lambda: config)
    monkeypatch.setattr(channel, "load_config", lambda: config)
    monkeypatch.setattr(wechat, "load_config", lambda: config)
    monkeypatch.setattr(rendering, "get_base_url", lambda: _BASE_URL)

    wecom_notifier = WeComNotifier(max_retries=0)
    feishu_notifier = FeishuNotifier(max_retries=0)
    monkeypatch.setattr(wechat, "_get_global_notifier", lambda: wecom_notifier)
    monkeypatch.setattr(
        channel, "_get_global_feishu_notifier", lambda: feishu_notifier,
    )

    yield {"router": router.NotificationRouter(), "records": records,
           "condition": condition}

    for manager in wecom_notifier.webhook_managers.values():
        manager._stop_flag.set()
        manager.worker_thread.join(timeout=5)
    feishu_notifier.stop_all()


def _wait_for_records(boundary, count):
    with boundary["condition"]:
        assert boundary["condition"].wait_for(
            lambda: len(boundary["records"]) >= count, timeout=5
        ), boundary["records"]
    return list(boundary["records"])


def test_dispatcher_round_isolates_poison_row_and_delivers_neighbors(
    tmp_path, http_boundary,
):
    """Outbox holds 3 pending rows; the middle task's persisted notify_targets
    JSON is corrupt. One dispatcher round must deliver both neighbor rows to
    their persisted targets (HTTP boundary), keep the poison row pending with
    a bumped attempt counter, and log NOTIFY_TARGETS_RESOLVE_FAILED. Moving
    the per-row resolve back outside the try boundary kills the round after
    the first row, so this test goes red on that regression.
    """
    from video_transcript_api.api.services.terminal_status import (
        deliver_pending_terminal_notifications,
        finalize_terminal_status_and_notify,
    )
    from video_transcript_api.utils.task_status import TaskStatus

    cache = CacheManager(cache_dir=str(tmp_path / "cache"))
    targets = {"wechat": _TENANT_WECHAT, "feishu": _TENANT_FEISHU}
    task_ids = []
    try:
        for index in range(3):
            task_id = cache.create_task(
                url=f"https://example.invalid/poison-{index}",
                notify_targets=dict(targets),
            )["task_id"]
            cache.update_task_status(task_id, TaskStatus.PROCESSING)
            assert finalize_terminal_status_and_notify(
                task_id,
                TaskStatus.SUCCESS,
                title=f"poison fixture {index}",
                terminal_snapshot={
                    "result": {"内容总结": f"persisted {index}", "stats": {}},
                    "use_speaker_recognition": False,
                },
                cache_manager=cache,
                router=http_boundary["router"],
                defer_delivery=True,
            ) is True
            task_ids.append(task_id)
        poison_id = task_ids[1]

        # Corrupt the middle row after the outbox row exists, and pin the
        # replay order (attempts equal -> created_at) to before/poison/after
        # so the red-sensitivity direction is explicit.
        with sqlite3.connect(cache.db_path) as connection:
            connection.execute(
                "UPDATE task_status SET notify_targets = 'not-json' "
                "WHERE task_id = ?",
                (poison_id,),
            )
            for offset, task_id in enumerate(task_ids):
                connection.execute(
                    "UPDATE task_terminal_notifications "
                    "SET created_at = datetime('now', ?) WHERE task_id = ?",
                    (f"-{30 - 10 * offset} seconds", task_id),
                )

        records = []
        sink_id = logger.add(
            lambda message: records.append(str(message)), level="WARNING"
        )
        try:
            assert deliver_pending_terminal_notifications(
                cache, router=http_boundary["router"],
            ) == 2
            payloads = _wait_for_records(http_boundary, 8)
        finally:
            logger.remove(sink_id)

        assert {url for url, _payload in payloads} == {
            _TENANT_WECHAT, _TENANT_FEISHU,
        }
        for neighbor_id in (task_ids[0], task_ids[2]):
            assert cache.is_terminal_notification_pending(neighbor_id) is False

        assert cache.is_terminal_notification_pending(poison_id) is True
        with cache._get_cursor() as cursor:
            cursor.execute(
                "SELECT attempts, notified_at FROM task_terminal_notifications "
                "WHERE task_id = ?",
                (poison_id,),
            )
            row = cursor.fetchone()
        assert row["attempts"] == 1
        assert row["notified_at"] is None
        assert any(
            f"NOTIFY_TARGETS_RESOLVE_FAILED task_id={poison_id}" in line
            for line in records
        )
    finally:
        cache.close()
