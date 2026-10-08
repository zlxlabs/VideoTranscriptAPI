"""End-to-end delivery tests for terminal notifications (N3/N4 locks).

Real NotificationRouter over real WeComChannel/FeishuChannel; only the
bottom-most transports are fakes (``WechatNotifier.send_text`` and
``FeishuNotifier.send_card``). Both terminal entry points are exercised
(inline ``deliver_terminal_notification`` and outbox replay
``deliver_pending_terminal_notifications``) and both channels must receive
the unified heading as the first line plus the persisted summary body --
patching at the Router or Channel layer would not lock parameter threading,
so it is forbidden here.

All console output must be in English only (no emoji, no Chinese).
"""

import re
from unittest.mock import MagicMock

import pytest

from src.video_transcript_api.api.services.terminal_status import (
    deliver_pending_terminal_notifications,
    deliver_terminal_notification,
    finalize_terminal_status_and_notify,
)
from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.notifications.router import NotificationRouter
from src.video_transcript_api.utils.task_status import TaskStatus


CHANNEL_MODULE = "src.video_transcript_api.utils.notifications.channel"
ROUTER_MODULE = "src.video_transcript_api.utils.notifications.router"
WECHAT_MODULE = "src.video_transcript_api.utils.notifications.wechat"

WECHAT_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=e2e-wechat"
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/e2e-feishu"
PERSISTED_SUMMARY = "e2e persisted summary body"

SNAPSHOT = {
    "result": {
        "内容总结": PERSISTED_SUMMARY,
        "校对文本": "calibrated text",
        "stats": {
            "original_length": 14,
            "calibrated_length": 15,
            "summary_length": len(PERSISTED_SUMMARY),
        },
        "models_used": {},
    },
    "use_speaker_recognition": False,
}


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


@pytest.fixture
def delivery(monkeypatch):
    """Real router and channels with fake transports only.

    ``wechat`` records what ``WechatNotifier.send_text`` received;
    ``feishu`` is the FeishuNotifier stand-in whose ``send_card`` records
    the card actually submitted.
    """
    config = {
        "wechat": {"webhook": WECHAT_WEBHOOK},
        "feishu": {"webhook": FEISHU_WEBHOOK, "secret": None},
    }
    wechat_transport = MagicMock(name="WechatNotifier.send_text", return_value=True)
    feishu_notifier = MagicMock(name="FeishuNotifier")

    monkeypatch.setattr(f"{ROUTER_MODULE}.load_config", lambda: config)
    monkeypatch.setattr(f"{CHANNEL_MODULE}.load_config", lambda: config)
    monkeypatch.setattr(
        f"{CHANNEL_MODULE}._get_global_feishu_notifier", lambda: feishu_notifier,
    )
    monkeypatch.setattr(f"{WECHAT_MODULE}.load_config", lambda: config)
    monkeypatch.setattr(f"{WECHAT_MODULE}._get_global_notifier", lambda: MagicMock())

    router = NotificationRouter()
    assert [ch.name for ch in router.channels] == ["wechat", "feishu"]

    wechat_channel = next(ch for ch in router.channels if ch.name == "wechat")
    monkeypatch.setattr(wechat_channel._notifier, "send_text", wechat_transport)

    return {"router": router, "wechat": wechat_transport, "feishu": feishu_notifier}


def _persist_pending_success(cm, url):
    """Write a success terminal + outbox row without delivering it."""
    task_id = cm.create_task(url=url)["task_id"]
    cm.update_task_status(task_id, TaskStatus.PROCESSING)
    assert finalize_terminal_status_and_notify(
        task_id,
        TaskStatus.SUCCESS,
        title="E2E 贯通标题",
        terminal_snapshot=SNAPSHOT,
        cache_manager=cm,
        defer_delivery=True,
    ) is True
    return task_id


def _assert_both_channels_received_heading_and_summary(delivery):
    wechat_call = delivery["wechat"].call_args
    wechat_content = wechat_call.args[0]
    feishu_card = delivery["feishu"].send_card.call_args.kwargs

    for source, content in (
        ("wechat", wechat_content),
        ("feishu", feishu_card["content"]),
    ):
        first_line = content.splitlines()[0]
        assert re.match(r"^✅ \[#[0-9a-f]{6}\] ", first_line), (source, first_line)
        assert PERSISTED_SUMMARY in content, (source, content)

    assert feishu_card["title"] == wechat_content.splitlines()[0]


class TestInlineDeliveryReachesBothChannels:
    def test_deliver_terminal_notification_threads_task_id_and_body(
        self, cm, delivery,
    ):
        task_id = _persist_pending_success(
            cm, "https://youtube.com/watch?v=e2e-inline",
        )

        deliver_terminal_notification(
            task_id,
            TaskStatus.SUCCESS,
            cache_manager=cm,
            router=delivery["router"],
        )

        _assert_both_channels_received_heading_and_summary(delivery)
        assert cm.is_terminal_notification_pending(task_id) is False


class TestOutboxReplayReachesBothChannels:
    def test_deliver_pending_terminal_notifications_threads_task_id_and_body(
        self, cm, delivery,
    ):
        task_id = _persist_pending_success(
            cm, "https://youtube.com/watch?v=e2e-replay",
        )

        assert deliver_pending_terminal_notifications(
            cm, router=delivery["router"],
        ) == 1

        _assert_both_channels_received_heading_and_summary(delivery)
        assert cm.is_terminal_notification_pending(task_id) is False
