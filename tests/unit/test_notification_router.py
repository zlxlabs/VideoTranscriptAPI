"""
Unit tests for NotificationRouter (C1 stop-leak semantics).

Covers:
- Single channel dispatch to the declared webhook
- Multi-channel dispatch via the declared webhooks dict
- Per-request channel selection
- Zero-target behavior (NOTIFY_TARGET_MISSING, no channel called)
- No cross-channel fallback (a rejected channel is NOT rescued by others)
- Init: channels exist regardless of global webhook config
- Ops alerts: explicit global-target path
"""

from unittest.mock import patch, MagicMock

import pytest
from loguru import logger as loguru_logger

from src.video_transcript_api.utils.notifications.router import NotificationRouter


MODULE = "src.video_transcript_api.utils.notifications.router"

WECHAT_HOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=w1"
FEISHU_HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/f1"


def _make_channel(name: str, enabled: bool = True, send_ok: bool = True):
    """Create a mock channel."""
    ch = MagicMock()
    ch.name = name
    ch.is_enabled = enabled
    ch.webhook = f"https://global.example/{name}"
    ch.send_text.return_value = send_ok
    ch.send_rich.return_value = send_ok
    ch.notify_task_status.return_value = send_ok
    return ch


@pytest.fixture
def router_no_config():
    """Router whose channel classes are mocked out (empty global config)."""
    with patch(f"{MODULE}.load_config", return_value={}), \
         patch(f"{MODULE}.WeComChannel", return_value=_make_channel("wechat")), \
         patch(f"{MODULE}.FeishuChannel", return_value=_make_channel("feishu")):
        r = NotificationRouter()
    return r


@pytest.fixture
def router_wechat_only():
    """Router with only the wechat channel object registered."""
    wechat = _make_channel("wechat")
    with patch(f"{MODULE}.load_config", return_value={"wechat": {"webhook": WECHAT_HOOK}}):
        r = NotificationRouter()
    r.channels = [wechat]
    return r, wechat


@pytest.fixture
def router_dual():
    """Router with both wechat and feishu channels."""
    wechat = _make_channel("wechat")
    feishu = _make_channel("feishu")
    with patch(f"{MODULE}.load_config", return_value={
        "wechat": {"webhook": WECHAT_HOOK},
        "feishu": {"webhook": FEISHU_HOOK},
    }):
        r = NotificationRouter()
    r.channels = [wechat, feishu]
    return r, wechat, feishu


# ============================================================
# Single Channel Dispatch
# ============================================================

class TestSingleChannel:
    """Tests for single channel dispatch to the declared webhook."""

    def test_send_text_dispatches_to_channel(self, router_wechat_only):
        router, wechat = router_wechat_only
        result = router.send_text("hello", webhooks={"wechat": WECHAT_HOOK})

        wechat.send_text.assert_called_once_with("hello", webhook=WECHAT_HOOK)
        assert result["wechat"] is True

    def test_send_rich_dispatches_to_channel(self, router_wechat_only):
        router, wechat = router_wechat_only
        result = router.send_rich("## content", webhooks={"wechat": WECHAT_HOOK})

        wechat.send_rich.assert_called_once()
        assert result["wechat"] is True

    def test_notify_task_status_dispatches(self, router_wechat_only):
        router, wechat = router_wechat_only
        result = router.notify_task_status(
            url="https://youtube.com/watch?v=test",
            status="started",
            webhooks={"wechat": WECHAT_HOOK},
        )

        wechat.notify_task_status.assert_called_once()
        assert result["wechat"] is True

    def test_send_view_link_uses_received_task_heading(self, router_wechat_only):
        router, _wechat = router_wechat_only
        with patch.object(router, "send_rich", return_value={"wechat": True}) as send:
            router.send_view_link(
                title="Video title",
                view_token="token",
                original_url="https://youtube.com/watch?v=abc",
                task_id="task_abcdef123456",
                webhooks={"wechat": WECHAT_HOOK},
            )

        content = send.call_args.args[0]
        assert content.splitlines()[0] == "📥 [#abcdef] Video title"
        assert send.call_args.kwargs["title"] == content.splitlines()[0]


# ============================================================
# Multi-Channel Dispatch
# ============================================================

class TestMultiChannel:
    """Tests for multi-channel dispatch via the declared webhooks dict."""

    def test_send_text_dispatches_to_all_declared(self, router_dual):
        router, wechat, feishu = router_dual
        result = router.send_text(
            "hello", webhooks={"wechat": WECHAT_HOOK, "feishu": FEISHU_HOOK},
        )

        wechat.send_text.assert_called_once()
        feishu.send_text.assert_called_once()
        assert result["wechat"] is True
        assert result["feishu"] is True

    def test_notify_dispatches_to_all_declared(self, router_dual):
        router, wechat, feishu = router_dual
        result = router.notify_task_status(
            url="https://example.com",
            status="completed",
            webhooks={"wechat": WECHAT_HOOK, "feishu": FEISHU_HOOK},
        )

        wechat.notify_task_status.assert_called_once()
        feishu.notify_task_status.assert_called_once()
        assert result["wechat"] is True
        assert result["feishu"] is True

    def test_undeclared_channel_is_skipped_silently(self, router_dual):
        """A channel without a declared address gets no call at all — the
        global group is never contacted for a tenant send."""
        router, wechat, feishu = router_dual
        result = router.send_text("hello", webhooks={"feishu": FEISHU_HOOK})

        wechat.send_text.assert_not_called()
        feishu.send_text.assert_called_once_with("hello", webhook=FEISHU_HOOK)
        assert result == {"feishu": True}


# ============================================================
# Zero-target behavior
# ============================================================

class TestZeroTarget:
    """Empty/absent webhooks: no channel is called, one MISSING line logged."""

    def test_empty_weblogs_request_no_channel(self, router_dual):
        router, wechat, feishu = router_dual
        result = router.send_text("hello", webhooks={})
        assert result == {}
        wechat.send_text.assert_not_called()
        feishu.send_text.assert_not_called()

    def test_absent_webhooks_request_no_channel(self, router_dual):
        router, _wechat, _feishu = router_dual
        assert router.send_text("hello") == {}
        assert router.notify_task_status(url="https://e.test", status="s") == {}

    def test_zero_target_logs_missing_line_without_leaking_url(self, router_dual):
        router, _wechat, _feishu = router_dual
        records = []
        sink_id = loguru_logger.add(
            lambda message: records.append(message.record), level="WARNING"
        )
        try:
            router.notify_task_status(
                url="https://example.test/video",
                status="started",
                webhooks={},
                task_id="task_missing0123456",
            )
            warnings = [
                record["message"] for record in records
                if "NOTIFY_TARGET_MISSING" in record["message"]
            ]
            assert len(warnings) == 1
            assert "method=notify_task_status" in warnings[0]
            assert "task_id=task_missing0123456" in warnings[0]
            assert "https://" not in warnings[0]
        finally:
            loguru_logger.remove(sink_id)


# ============================================================
# Per-Request Channel Selection
# ============================================================

class TestPerRequestChannel:
    """Tests for per-request channel selection."""

    def test_select_specific_channel(self, router_dual):
        router, wechat, feishu = router_dual
        result = router.send_text(
            "hello", channel_name="feishu", webhooks={"feishu": FEISHU_HOOK},
        )

        feishu.send_text.assert_called_once()
        wechat.send_text.assert_not_called()
        assert "feishu" in result

    def test_select_nonexistent_channel_returns_empty(self, router_dual):
        """Selecting a channel that doesn't exist returns empty results."""
        router, wechat, feishu = router_dual
        result = router.send_text(
            "hello", channel_name="slack", webhooks={"slack": "https://slack"},
        )
        assert result == {}

    def test_select_channel_with_declared_webhook(self, router_dual):
        router, wechat, feishu = router_dual
        custom = "https://open.feishu.cn/custom"
        router.send_text(
            "hello", channel_name="feishu", webhooks={"feishu": custom},
        )

        feishu.send_text.assert_called_once_with("hello", webhook=custom)


# ============================================================
# No cross-channel fallback
# ============================================================

class TestNoCrossChannelFallback:
    """A rejected declared channel is never rescued by another channel."""

    def test_failed_channel_is_not_rescued(self, router_dual):
        router, wechat, feishu = router_dual
        feishu.send_text.return_value = False
        wechat.send_text.return_value = True

        result = router.send_text(
            "hello", channel_name="feishu", webhooks={"feishu": FEISHU_HOOK},
        )

        assert result == {"feishu": False}
        wechat.send_text.assert_not_called()
        assert not any(key.endswith("_fallback") for key in result)

    def test_all_declared_channels_fail(self, router_dual):
        router, wechat, feishu = router_dual
        webhooks = {"wechat": WECHAT_HOOK, "feishu": FEISHU_HOOK}
        wechat.send_text.return_value = False
        feishu.send_text.return_value = False

        result = router.send_text("hello", webhooks=webhooks)

        assert result == {"wechat": False, "feishu": False}

    def test_exception_in_channel_doesnt_break_others(self, router_dual):
        router, wechat, feishu = router_dual
        webhooks = {"wechat": WECHAT_HOOK, "feishu": FEISHU_HOOK}
        wechat.send_text.side_effect = RuntimeError("wechat down")
        feishu.send_text.return_value = True

        result = router.send_text("hello", webhooks=webhooks)

        assert result["wechat"] is False
        assert result["feishu"] is True


# ============================================================
# Ops alerts (explicit global path)
# ============================================================

class TestOpsAlert:
    """send_ops_alert targets the channels' global webhooks explicitly."""

    def test_ops_alert_uses_global_webhooks(self, router_dual):
        router, wechat, feishu = router_dual
        result = router.send_ops_alert("ASR service down")

        wechat.send_text.assert_called_once_with(
            "ASR service down", webhook="https://global.example/wechat",
        )
        feishu.send_text.assert_called_once_with(
            "ASR service down", webhook="https://global.example/feishu",
        )
        assert result == {"wechat": True, "feishu": True}

    def test_ops_alert_skips_disabled_channels(self, router_dual):
        router, _wechat, feishu = router_dual
        feishu.is_enabled = False

        result = router.send_ops_alert("ASR service down")

        feishu.send_text.assert_not_called()
        assert result == {"wechat": True}


# ============================================================
# No Channels
# ============================================================

class TestNoChannels:
    """Tests for a router whose channels are unavailable (init failures)."""

    def test_send_text_returns_empty(self, router_no_config):
        router_no_config.channels = []
        result = router_no_config.send_text("hello")
        assert result == {}

    def test_notify_returns_empty(self, router_no_config):
        router_no_config.channels = []
        result = router_no_config.notify_task_status(
            url="https://example.com", status="test"
        )
        assert result == {}


# ============================================================
# Init From Config
# ============================================================

class TestInitFromConfig:
    """Channel objects exist regardless of global webhook config; tenant
    targeting is decided per call by the declared webhooks dict (C1)."""

    def test_init_wechat_global_only(self):
        with patch(f"{MODULE}.load_config", return_value={"wechat": {"webhook": WECHAT_HOOK}}), \
             patch(f"{MODULE}.WeComChannel") as MockWeCom, \
             patch(f"{MODULE}.FeishuChannel") as MockFeishu:
            MockWeCom.return_value = _make_channel("wechat")
            MockFeishu.return_value = _make_channel("feishu")
            r = NotificationRouter()

        MockWeCom.assert_called_once()
        MockFeishu.assert_called_once()
        assert len(r.channels) == 2

    def test_init_no_global_webhooks_still_constructs_channels(self):
        with patch(f"{MODULE}.load_config", return_value={}), \
             patch(f"{MODULE}.WeComChannel") as MockWeCom, \
             patch(f"{MODULE}.FeishuChannel") as MockFeishu:
            MockWeCom.return_value = _make_channel("wechat")
            MockFeishu.return_value = _make_channel("feishu")
            r = NotificationRouter()

        MockWeCom.assert_called_once()
        MockFeishu.assert_called_once()
        assert len(r.channels) == 2

    def test_init_channel_failure_is_isolated(self):
        with patch(f"{MODULE}.load_config", return_value={}), \
             patch(f"{MODULE}.WeComChannel", side_effect=RuntimeError("boom")), \
             patch(f"{MODULE}.FeishuChannel") as MockFeishu:
            MockFeishu.return_value = _make_channel("feishu")
            r = NotificationRouter()

        assert [channel.name for channel in r.channels] == ["feishu"]


# ============================================================
# Per-Channel Webhooks Dict
# ============================================================

class TestWebhooksDict:
    """Tests for per-channel webhooks dict dispatch."""

    def test_webhooks_dict_routes_correct_webhook_per_channel(self, router_dual):
        """Each channel should receive its own webhook from the dict."""
        router, wechat, feishu = router_dual
        webhooks = {
            "wechat": "https://qyapi.weixin.qq.com/user1",
            "feishu": "https://open.feishu.cn/user1",
        }
        router.send_text("hello", webhooks=webhooks)

        wechat.send_text.assert_called_once_with("hello", webhook="https://qyapi.weixin.qq.com/user1")
        feishu.send_text.assert_called_once_with("hello", webhook="https://open.feishu.cn/user1")

    def test_webhooks_dict_with_channel_name(self, router_dual):
        """When channel_name is set, webhooks dict still provides the right webhook."""
        router, wechat, feishu = router_dual
        webhooks = {
            "wechat": "https://qyapi.weixin.qq.com/user1",
            "feishu": "https://open.feishu.cn/user1",
        }
        router.send_text("hello", channel_name="feishu", webhooks=webhooks)

        feishu.send_text.assert_called_once_with("hello", webhook="https://open.feishu.cn/user1")
        wechat.send_text.assert_not_called()

    def test_webhooks_dict_notify_task_status(self, router_dual):
        """notify_task_status should also respect webhooks dict."""
        router, wechat, feishu = router_dual
        webhooks = {
            "wechat": "https://wechat/user1",
            "feishu": "https://feishu/user1",
        }
        router.notify_task_status(
            url="https://example.com", status="started", webhooks=webhooks
        )

        wechat_call = wechat.notify_task_status.call_args
        feishu_call = feishu.notify_task_status.call_args
        assert wechat_call.kwargs.get("webhook") == "https://wechat/user1"
        assert feishu_call.kwargs.get("webhook") == "https://feishu/user1"

    def test_webhooks_dict_send_rich(self, router_dual):
        """send_rich should also respect webhooks dict."""
        router, wechat, feishu = router_dual
        webhooks = {
            "wechat": "https://wechat/user1",
            "feishu": "https://feishu/user1",
        }
        router.send_rich("## content", webhooks=webhooks)

        wechat_call = wechat.send_rich.call_args
        feishu_call = feishu.send_rich.call_args
        assert wechat_call.kwargs.get("webhook") == "https://wechat/user1"
        assert feishu_call.kwargs.get("webhook") == "https://feishu/user1"


# ============================================================
# Convenience: is_enabled
# ============================================================

class TestRouterIsEnabled:
    """Router-level is_enabled: channels available (ops alerts have targets)."""

    def test_enabled_with_channels(self, router_wechat_only):
        router, _ = router_wechat_only
        assert router.is_enabled is True

    def test_disabled_without_channels(self, router_no_config):
        router_no_config.channels = []
        assert router_no_config.is_enabled is False


class TestStatusEmojiMatchesLayeredCopy:
    """New intermediate copy must not fall through to the default emoji."""

    def test_in_progress_transcription_copy_is_not_default_emoji(self):
        from src.video_transcript_api.utils.notifications.channel import (
            _get_status_emoji,
        )
        from src.video_transcript_api.utils.notifications.wechat import WechatNotifier

        intermediate = "转录完成（进行中）- 普通转录(CapsWriter)，后面还有校对/摘要"
        default = _get_status_emoji("unknown state")
        assert _get_status_emoji(intermediate) == "✅"
        assert _get_status_emoji(intermediate) != default
        wechat_emoji = WechatNotifier._get_status_emoji(None, intermediate)
        assert wechat_emoji == "✅"
        assert wechat_emoji != default

    def test_terminal_copy_stays_distinct(self):
        from src.video_transcript_api.utils.notifications.channel import (
            _get_status_emoji,
        )

        assert _get_status_emoji("【任务完成】") == "✅"
        assert _get_status_emoji("【任务失败】") == "❌"
        assert "进行中" in "转录完成（进行中）- 普通转录(CapsWriter)，后面还有校对/摘要"
        assert "【任务完成】" != "转录完成（进行中）- 普通转录(CapsWriter)，后面还有校对/摘要"


class TestCompletionReceiptGroupResults:
    def test_feishu_second_submission_failure_keeps_channel_group_false(self):
        from src.video_transcript_api.api.services.terminal_status import _notification_accepted
        from src.video_transcript_api.utils.notifications.channel import FeishuChannel

        feishu = FeishuChannel.__new__(FeishuChannel)
        feishu.send_rich = MagicMock(side_effect=[True, False])
        router = NotificationRouter.__new__(NotificationRouter)
        router.channels = [feishu]

        results = router.notify_task_status(
            url="https://example.test/video",
            status="【任务完成】",
            title="Receipt failure",
            task_id="task_abcdef123456",
            completion_body="full summary body",
            completion_receipt="✅ [#abcdef] Receipt failure\nsummary excerpt",
            webhooks={"feishu": FEISHU_HOOK},
        )

        assert results == {"feishu": False}
        assert _notification_accepted(results) is False
        calls = feishu.send_rich.call_args_list
        assert len(calls) == 2
        assert "full summary body" in calls[0].args[0]
        assert calls[1].args[0].startswith("✅ [#abcdef]")

    def test_wecom_first_submission_failure_suppresses_receipt(self):
        from src.video_transcript_api.utils.notifications.channel import WeComChannel
        from src.video_transcript_api.utils.notifications.wechat import WechatNotifier

        notifier = WechatNotifier.__new__(WechatNotifier)
        notifier.webhook = "hook"
        notifier.send_text = MagicMock(return_value=False)
        wecom = WeComChannel.__new__(WeComChannel)
        wecom._notifier = notifier

        accepted = wecom.notify_task_status(
            url="https://example.test/video",
            status="【任务完成】",
            title="Full body failure",
            task_id="task_abcdef123456",
            completion_body="full summary body",
            completion_receipt="✅ [#abcdef] Full body failure\nsummary excerpt",
            webhook="hook",
        )

        assert accepted is False
        notifier.send_text.assert_called_once()
        assert "full summary body" in notifier.send_text.call_args.args[0]
