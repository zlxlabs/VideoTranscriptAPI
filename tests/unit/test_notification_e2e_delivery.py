"""End-to-end terminal payloads through real notifier queues.

The router, channels, third-party webhook managers, FIFO consumers and JSON
payload producers are real. Only the final external HTTP POST is replaced.
"""

import datetime
import json
import re
import sqlite3
import threading
import uuid

import pytest
from wecom_notifier import FeishuNotifier, WeComNotifier
from wecom_notifier.platforms.feishu import sender as feishu_sender
from wecom_notifier.platforms.wecom import sender as wecom_sender

from src.video_transcript_api.api.services import transcription
from src.video_transcript_api.api.services.terminal_status import (
    deliver_pending_terminal_notifications,
    deliver_terminal_notification,
    finalize_terminal_status_and_notify,
)
from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.llm_status import CalibrationStatus, ChaptersStatus
from src.video_transcript_api.utils.notifications.router import NotificationRouter
from src.video_transcript_api.utils.task_status import TaskStatus


CHANNEL_MODULE = "src.video_transcript_api.utils.notifications.channel"
ROUTER_MODULE = "src.video_transcript_api.utils.notifications.router"
WECHAT_MODULE = "src.video_transcript_api.utils.notifications.wechat"
RENDERING_MODULE = "src.video_transcript_api.utils.rendering"
WECHAT_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=e2e-wechat"
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/e2e-feishu"
BASE_URL = "https://share.example.test"
TITLE = "E2E 贯通标题"
PERSISTED_SUMMARY = (
    "# E2E 摘要标题\n\n"
    "第一段含 **重点**、[链接文本](https://example.test/summary) 和中文。\n\n"
    "第二段必须完整发送但不能进入短回执。"
)
FIRST_PARAGRAPH = "第一段含 重点、链接文本 和中文。"

SNAPSHOT = {
    "result": {
        "内容总结": PERSISTED_SUMMARY,
        "校对文本": "calibrated text",
        "skip_summary": False,
        "stats": {
            "original_length": 14,
            "calibrated_length": 15,
            "summary_length": len(PERSISTED_SUMMARY),
            "summary_status": "generated",
        },
        "models_used": {},
    },
    "use_speaker_recognition": False,
    "calibrate_only": False,
}


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


@pytest.fixture
def delivery(monkeypatch):
    """Real Router and channel queues with a fake external HTTP boundary."""
    config = {
        "wechat": {"webhook": WECHAT_WEBHOOK},
        "feishu": {"webhook": FEISHU_WEBHOOK, "secret": None},
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

    def wait_for_payloads(count, timeout=5):
        with condition:
            return condition.wait_for(lambda: len(records) >= count, timeout=timeout)

    monkeypatch.setattr(wecom_sender.requests, "post", post)
    monkeypatch.setattr(feishu_sender.requests, "post", post)
    monkeypatch.setattr(f"{ROUTER_MODULE}.load_config", lambda: config)
    monkeypatch.setattr(f"{CHANNEL_MODULE}.load_config", lambda: config)
    monkeypatch.setattr(f"{WECHAT_MODULE}.load_config", lambda: config)
    monkeypatch.setattr(f"{RENDERING_MODULE}.get_base_url", lambda: BASE_URL)

    wecom_notifier = WeComNotifier(max_retries=0)
    feishu_notifier = FeishuNotifier(max_retries=0)
    monkeypatch.setattr(f"{WECHAT_MODULE}._get_global_notifier", lambda: wecom_notifier)
    monkeypatch.setattr(
        f"{CHANNEL_MODULE}._get_global_feishu_notifier", lambda: feishu_notifier,
    )

    router = NotificationRouter()
    assert [channel.name for channel in router.channels] == ["wechat", "feishu"]

    yield {
        "router": router,
        "records": records,
        "condition": condition,
        "wait_for_payloads": wait_for_payloads,
        "wecom_notifier": wecom_notifier,
        "feishu_notifier": feishu_notifier,
    }

    for manager in wecom_notifier.webhook_managers.values():
        manager._stop_flag.set()
        manager.worker_thread.join(timeout=5)
    feishu_notifier.stop_all()


def _persist_pending_success(cm, url):
    """Persist one producer snapshot and outbox row through SQLite."""
    created = cm.create_task(url=url)
    task_id = created["task_id"]
    cm.update_task_status(task_id, TaskStatus.PROCESSING)
    assert finalize_terminal_status_and_notify(
        task_id,
        TaskStatus.SUCCESS,
        title=TITLE,
        terminal_snapshot=SNAPSHOT,
        cache_manager=cm,
        defer_delivery=True,
    ) is True
    with sqlite3.connect(cm.db_path) as connection:
        raw_snapshot = connection.execute(
            "SELECT terminal_snapshot FROM task_status WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    decoded = json.loads(raw_snapshot)
    expected = {**SNAPSHOT, "status": "success", "title": TITLE}
    assert raw_snapshot.encode("utf-8") == json.dumps(
        expected, ensure_ascii=False, sort_keys=True, default=str,
    ).encode("utf-8")
    assert decoded == expected
    return task_id, created["view_token"]


def _wait_payloads(delivery, count):
    assert delivery["wait_for_payloads"](count), delivery["records"]
    with delivery["condition"]:
        return list(delivery["records"])


def _payload_content(url, payload):
    if "qyapi.weixin.qq.com" in url:
        assert payload["msgtype"] == "markdown_v2"
        return payload["markdown_v2"]["content"]
    assert payload["msg_type"] == "interactive"
    return payload["card"]["body"]["elements"][0]["content"]


def _assert_ordered_summary_receipt(delivery, task_id, original_url, view_token):
    records = _wait_payloads(delivery, 4)
    expected_view_url = f"{BASE_URL}/view/{view_token}"
    groups = {
        "wechat": [r for r in records if "qyapi.weixin.qq.com" in r[0]],
        "feishu": [r for r in records if "open.feishu.cn" in r[0]],
    }
    assert all(groups.values()), records

    for source, channel_records in groups.items():
        contents = [_payload_content(url, payload) for url, payload in channel_records]
        receipt = contents[-1]
        complete_body = "\n".join(contents[:-1])
        assert PERSISTED_SUMMARY in complete_body, (source, contents)
        assert "第二段必须完整发送但不能进入短回执。" in complete_body
        assert re.match(r"^✅ \[#[0-9a-f]{6}\] E2E 贯通标题", contents[0])
        assert re.match(r"^✅ \[#[0-9a-f]{6}\] E2E 贯通标题", receipt)
        assert f"原始地址：{original_url}" in receipt
        assert f"总结和校对：{expected_view_url}" in receipt
        assert FIRST_PARAGRAPH in receipt
        assert "第二段必须完整发送但不能进入短回执。" not in receipt
        assert all("总结和校对：" not in content for content in contents[:-1])
        if source == "feishu":
            assert channel_records[-1][1]["card"]["header"]["title"]["content"] == receipt.splitlines()[0]


def test_accepted_upload_link_reaches_final_http_payload_with_public_capability(
    delivery,
):
    view_token = "upload_public-capability-fixture"
    delivery["router"].send_view_link(
        title=TITLE,
        view_token=view_token,
        original_url="https://source.example.test/watch?id=9",
        source_label="本地上传",
        task_id="task_abcdef0123456789",
        webhooks={"wechat": WECHAT_WEBHOOK, "feishu": FEISHU_WEBHOOK},
    )
    records = _wait_payloads(delivery, 2)
    expected_url = f"{BASE_URL}/view/{view_token}"
    assert len(records) == 2
    for url, payload in records:
        content = _payload_content(url, payload)
        assert TITLE in content
        assert expected_url in content
        assert "本地上传" in content
        assert "https://source.example.test/watch" in content
        assert "fakepath" not in content
        assert "server-owned" not in content


@pytest.mark.parametrize("entry_point", ["inline", "replay"])
def test_success_snapshot_reaches_final_http_payloads_in_order(
    cm, delivery, entry_point,
):
    original_url = f"https://youtube.com/watch?v=e2e-{entry_point}&track=original"
    task_id, view_token = _persist_pending_success(cm, original_url)
    if entry_point == "inline":
        deliver_terminal_notification(
            task_id, TaskStatus.SUCCESS, cache_manager=cm, router=delivery["router"],
        )
    else:
        assert deliver_pending_terminal_notifications(cm, router=delivery["router"]) == 1

    _assert_ordered_summary_receipt(delivery, task_id, original_url, view_token)
    assert cm.is_terminal_notification_pending(task_id) is False


def test_process_transcription_notify_via_reaches_both_real_channels(
    cm, delivery, monkeypatch,
):
    """Exercise the real local _TaskNotifier adapter; no notify_via mock."""
    from video_transcript_api.api.services import transcription as implementation
    from video_transcript_api.utils import rendering as runtime_rendering

    monkeypatch.setattr(runtime_rendering, "get_base_url", lambda: BASE_URL)
    original_url = "https://www.youtube.com/watch?v=budget"
    task_id = cm.create_task(url=original_url)["task_id"]
    cm.update_task_status(task_id, TaskStatus.PROCESSING)
    cm.save_cache(
        platform="youtube", url=original_url, media_id="budget",
        use_speaker_recognition=False, transcript_data="cached transcript",
        transcript_type="capswriter", title=TITLE, author="E2E author", description="",
    )
    for layer, text in (("calibrated", "calibrated fixture"), ("summary", PERSISTED_SUMMARY)):
        cm.save_llm_result(
            platform="youtube", media_id="budget", use_speaker_recognition=False,
            llm_type=layer, content=text,
        )
    cm.save_llm_status(
        platform="youtube", media_id="budget", use_speaker_recognition=False,
        calibration_status=CalibrationStatus.FULL, summary_status="generated",
        chapters_status=ChaptersStatus.SKIPPED_SHORT,
    )

    class CachedPlatformDownloader:
        use_api_server = False

        def get_metadata(self, _url):
            return type("Metadata", (), {
                "id": "budget", "platform": "youtube", "title": TITLE,
                "author": "E2E author", "description": "",
            })()

        def get_subtitle_result(self, _url):
            return None

    monkeypatch.setattr(implementation, "cache_manager", cm)
    monkeypatch.setattr(implementation, "get_notification_router", lambda: delivery["router"])
    monkeypatch.setattr(implementation, "create_downloader", lambda _url: CachedPlatformDownloader())

    result = implementation.process_transcription(
        task_id=task_id,
        url=original_url,
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
        notification_channel=None,
        notification_webhooks={"wechat": WECHAT_WEBHOOK, "feishu": FEISHU_WEBHOOK},
        processing_options={"calibrate": True, "summarize": True, "chapters": False},
    )

    assert result["status"] == "success"
    row = cm.get_task_by_id(task_id)
    assert row["status"] == TaskStatus.SUCCESS
    _assert_ordered_summary_receipt(delivery, task_id, original_url, row["view_token"])
    assert cm.is_terminal_notification_pending(task_id) is False


def test_local_upload_terminal_producer_sends_public_share_url_and_source_label(
    cm, delivery, monkeypatch,
):
    monkeypatch.setattr(f"{RENDERING_MODULE}.get_base_url", lambda: BASE_URL)
    now = datetime.datetime.now(datetime.timezone.utc)
    key = f"{int(now.timestamp() * 1000)}-{uuid.uuid4()}"
    intent = cm.register_local_upload(
        owner_user_id="alice", idempotency_key=key, retention="never",
        intent_metadata={"filename": "clip.mp4", "title": TITLE},
    )
    media_id = f"upload_{uuid.uuid4().hex}"
    root_task_id = cm.generate_task_id()
    upload = cm.accept_local_upload(
        intent["upload_id"],
        media_id=media_id,
        task_id=root_task_id,
        filename="clip.mp4",
        title=TITLE,
        source_url="https://source.example.test/watch?id=7",
        request_metadata={"filename": "clip.mp4", "title": TITLE},
        media_path="/not-in-notification/persistent/source.mp4",
        byte_size=42,
        sha256="a" * 64,
    )
    cm.save_cache(
        platform="local_upload",
        url="https://source.example.test/watch?id=7",
        media_id=media_id,
        use_speaker_recognition=False, transcript_data="upload transcript",
        transcript_type="capswriter", title=TITLE, author="", description="",
    )
    child_task_id = cm.generate_task_id()
    with cm._get_cursor() as cursor:
        cursor.execute(
            """INSERT INTO task_status
               (task_id, view_token, url, platform, media_id, status, submitted_by)
               VALUES (?, '', '', 'local_upload', ?, 'queued', 'alice')""",
            (child_task_id, media_id),
        )
    cm.update_task_status(root_task_id, TaskStatus.PROCESSING)
    cm.update_task_status(child_task_id, TaskStatus.PROCESSING)
    assert finalize_terminal_status_and_notify(
        child_task_id,
        TaskStatus.SUCCESS,
        title=TITLE,
        terminal_snapshot=SNAPSHOT,
        cache_manager=cm,
        defer_delivery=True,
    ) is True
    cm.revoke_local_upload(upload["upload_id"])

    deliver_pending_terminal_notifications(cm, router=delivery["router"])

    records = _wait_payloads(delivery, 4)
    expected_url = f"{BASE_URL}/view/{upload['view_token']}"
    for channel_records in (
        [record for record in records if "qyapi.weixin.qq.com" in record[0]],
        [record for record in records if "open.feishu.cn" in record[0]],
    ):
        contents = [_payload_content(url, payload) for url, payload in channel_records]
        assert len(contents) == 2
        full_message, receipt = contents
        assert TITLE in full_message and TITLE in receipt
        assert expected_url in full_message and expected_url in receipt
        assert "本地上传" in receipt
        assert "原始地址：https://source.example.test/watch" in receipt
        assert PERSISTED_SUMMARY in full_message
        assert "第一段含 重点、链接文本 和中文。" in receipt
        assert "persistent/source.mp4" not in "\n".join(contents)
        assert "upload transcript" not in "\n".join(contents)
    assert cm.get_task_by_id(root_task_id)["view_token"] == ""
    assert cm.get_task_by_id(child_task_id)["view_token"] == ""
    stored = cm.get_local_upload_by_id(upload["upload_id"])
    assert stored["view_token"] == upload["view_token"]
    assert stored["terminal_at"] is None
    assert stored["expires_at"] is None


def test_failed_terminal_sends_one_failure_payload_per_channel(cm, delivery):
    task_id = cm.create_task(url="https://example.test/fail")["task_id"]
    cm.update_task_status(task_id, TaskStatus.PROCESSING)
    assert finalize_terminal_status_and_notify(
        task_id,
        TaskStatus.FAILED,
        error_message="fixture failure",
        title="Failed title",
        cache_manager=cm,
        defer_delivery=True,
    ) is True
    deliver_terminal_notification(
        task_id,
        TaskStatus.FAILED,
        error_message="fixture failure",
        cache_manager=cm,
        router=delivery["router"],
    )

    records = _wait_payloads(delivery, 2)
    assert len(records) == 2
    for url, payload in records:
        content = _payload_content(url, payload)
        assert "❌" in content
        assert "fixture failure" in content
        assert "总结和校对：" not in content
        assert "第一段含" not in content
    assert cm.is_terminal_notification_pending(task_id) is False
