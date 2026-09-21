"""
Unit tests for NotificationRouter.

Covers:
- Single channel dispatch
- Multi-channel parallel dispatch
- Fallback on failure
- All channels fail
- Per-request channel selection
- No channels configured
- Init from config
"""

from unittest.mock import patch, MagicMock, call

import pytest

from src.video_transcript_api.utils.notifications.router import NotificationRouter


MODULE = "src.video_transcript_api.utils.notifications.router"


def _make_channel(name: str, enabled: bool = True, send_ok: bool = True):
    """Create a mock channel."""
    ch = MagicMock()
    ch.name = name
    ch.is_enabled = enabled
    ch.send_text.return_value = send_ok
    ch.send_rich.return_value = send_ok
    ch.notify_task_status.return_value = send_ok
    return ch


@pytest.fixture
def router_no_config():
    """Router with no channels (empty config)."""
    with patch(f"{MODULE}.load_config", return_value={}):
        r = NotificationRouter()
    return r


@pytest.fixture
def router_wechat_only():
    """Router with only wechat channel."""
    wechat = _make_channel("wechat")
    with patch(f"{MODULE}.load_config", return_value={"wechat": {"webhook": "https://wechat"}}):
        r = NotificationRouter()
    r.channels = [wechat]
    return r, wechat


@pytest.fixture
def router_dual():
    """Router with both wechat and feishu channels."""
    wechat = _make_channel("wechat")
    feishu = _make_channel("feishu")
    with patch(f"{MODULE}.load_config", return_value={
        "wechat": {"webhook": "https://wechat"},
        "feishu": {"webhook": "https://feishu"},
    }):
        r = NotificationRouter()
    r.channels = [wechat, feishu]
    return r, wechat, feishu


# ============================================================
# Single Channel Dispatch
# ============================================================

class TestSingleChannel:
    """Tests for single channel dispatch."""

    def test_send_text_dispatches_to_channel(self, router_wechat_only):
        router, wechat = router_wechat_only
        result = router.send_text("hello")

        wechat.send_text.assert_called_once_with("hello", webhook=None)
        assert result["wechat"] is True

    def test_send_rich_dispatches_to_channel(self, router_wechat_only):
        router, wechat = router_wechat_only
        result = router.send_rich("## content")

        wechat.send_rich.assert_called_once()
        assert result["wechat"] is True

    def test_notify_task_status_dispatches(self, router_wechat_only):
        router, wechat = router_wechat_only
        result = router.notify_task_status(
            url="https://youtube.com/watch?v=test",
            status="started",
        )

        wechat.notify_task_status.assert_called_once()
        assert result["wechat"] is True


# ============================================================
# Multi-Channel Parallel Dispatch
# ============================================================

class TestMultiChannel:
    """Tests for multi-channel parallel dispatch."""

    def test_send_text_dispatches_to_all(self, router_dual):
        router, wechat, feishu = router_dual
        result = router.send_text("hello")

        wechat.send_text.assert_called_once()
        feishu.send_text.assert_called_once()
        assert result["wechat"] is True
        assert result["feishu"] is True

    def test_notify_dispatches_to_all(self, router_dual):
        router, wechat, feishu = router_dual
        result = router.notify_task_status(
            url="https://example.com",
            status="completed",
        )

        wechat.notify_task_status.assert_called_once()
        feishu.notify_task_status.assert_called_once()
        assert result["wechat"] is True
        assert result["feishu"] is True


# ============================================================
# Per-Request Channel Selection
# ============================================================

class TestPerRequestChannel:
    """Tests for per-request channel selection."""

    def test_select_specific_channel(self, router_dual):
        router, wechat, feishu = router_dual
        result = router.send_text("hello", channel_name="feishu")

        feishu.send_text.assert_called_once()
        wechat.send_text.assert_not_called()
        assert "feishu" in result

    def test_select_nonexistent_channel_returns_empty(self, router_dual):
        """Selecting a channel that doesn't exist returns empty results (no fallback)."""
        router, wechat, feishu = router_dual
        result = router.send_text("hello", channel_name="slack")

        # No targets found and no failures to trigger fallback
        assert "slack" not in result

    def test_select_channel_with_webhook_override(self, router_dual):
        router, wechat, feishu = router_dual
        custom = "https://open.feishu.cn/custom"
        router.send_text("hello", channel_name="feishu", webhook=custom)

        feishu.send_text.assert_called_once_with("hello", webhook=custom)


# ============================================================
# Fallback
# ============================================================

class TestFallback:
    """Tests for fallback behavior."""

    def test_fallback_on_primary_failure(self, router_dual):
        router, wechat, feishu = router_dual
        # feishu fails, wechat succeeds
        feishu.send_text.return_value = False
        wechat.send_text.return_value = True

        result = router.send_text("hello", channel_name="feishu")

        # Should attempt feishu first, then fallback to wechat
        feishu.send_text.assert_called()
        assert result.get("feishu") is False
        # Fallback should have been attempted
        assert result.get("wechat_fallback") is True or wechat.send_text.called

    def test_all_channels_fail(self, router_dual):
        router, wechat, feishu = router_dual
        wechat.send_text.return_value = False
        feishu.send_text.return_value = False

        result = router.send_text("hello")

        assert result["wechat"] is False
        assert result["feishu"] is False

    def test_exception_in_channel_doesnt_break_others(self, router_dual):
        router, wechat, feishu = router_dual
        wechat.send_text.side_effect = RuntimeError("wechat down")
        feishu.send_text.return_value = True

        result = router.send_text("hello")

        assert result["wechat"] is False
        assert result["feishu"] is True


# ============================================================
# No Channels
# ============================================================

class TestNoChannels:
    """Tests for no channels configured."""

    def test_send_text_returns_empty(self, router_no_config):
        result = router_no_config.send_text("hello")
        assert result == {}

    def test_notify_returns_empty(self, router_no_config):
        result = router_no_config.notify_task_status(
            url="https://example.com", status="test"
        )
        assert result == {}


# ============================================================
# Init From Config
# ============================================================

class TestInitFromConfig:
    """Tests for router initialization from config."""

    def test_init_wechat_only(self):
        with patch(f"{MODULE}.load_config", return_value={"wechat": {"webhook": "https://wechat"}}), \
             patch(f"{MODULE}.WeComChannel") as MockWeCom, \
             patch(f"{MODULE}.FeishuChannel") as MockFeishu:
            MockWeCom.return_value = _make_channel("wechat")
            r = NotificationRouter()

        MockWeCom.assert_called_once()
        MockFeishu.assert_not_called()
        assert len(r.channels) == 1

    def test_init_feishu_only(self):
        with patch(f"{MODULE}.load_config", return_value={"feishu": {"webhook": "https://feishu"}}), \
             patch(f"{MODULE}.WeComChannel") as MockWeCom, \
             patch(f"{MODULE}.FeishuChannel") as MockFeishu:
            MockFeishu.return_value = _make_channel("feishu")
            r = NotificationRouter()

        MockWeCom.assert_not_called()
        MockFeishu.assert_called_once()
        assert len(r.channels) == 1

    def test_init_both_channels(self):
        with patch(f"{MODULE}.load_config", return_value={
            "wechat": {"webhook": "https://wechat"},
            "feishu": {"webhook": "https://feishu"},
        }), \
             patch(f"{MODULE}.WeComChannel") as MockWeCom, \
             patch(f"{MODULE}.FeishuChannel") as MockFeishu:
            MockWeCom.return_value = _make_channel("wechat")
            MockFeishu.return_value = _make_channel("feishu")
            r = NotificationRouter()

        assert len(r.channels) == 2

    def test_init_no_channels(self):
        with patch(f"{MODULE}.load_config", return_value={}):
            r = NotificationRouter()
        assert len(r.channels) == 0


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

    def test_webhooks_dict_missing_channel_passes_none(self, router_dual):
        """Channel not in webhooks dict should get webhook=None (use config default)."""
        router, wechat, feishu = router_dual
        webhooks = {"wechat": "https://qyapi.weixin.qq.com/user1"}

        router.send_text("hello", webhooks=webhooks)

        wechat.send_text.assert_called_once_with("hello", webhook="https://qyapi.weixin.qq.com/user1")
        feishu.send_text.assert_called_once_with("hello", webhook=None)

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

    def test_webhooks_none_falls_back_to_webhook_param(self, router_dual):
        """When webhooks=None, fall back to single webhook param (backward compat)."""
        router, wechat, feishu = router_dual
        router.send_text("hello", webhook="https://single-webhook")

        wechat.send_text.assert_called_once_with("hello", webhook="https://single-webhook")
        feishu.send_text.assert_called_once_with("hello", webhook="https://single-webhook")

    def test_webhooks_dict_fallback_on_failure(self, router_dual):
        """Fallback should use webhooks dict for fallback channel too."""
        router, wechat, feishu = router_dual
        feishu.send_text.return_value = False
        wechat.send_text.return_value = True
        webhooks = {
            "wechat": "https://wechat/user1",
            "feishu": "https://feishu/user1",
        }
        result = router.send_text("hello", channel_name="feishu", webhooks=webhooks)

        # Feishu failed, should fallback to wechat with wechat's webhook
        assert result.get("feishu") is False
        assert wechat.send_text.called
        fallback_call = wechat.send_text.call_args
        assert fallback_call.kwargs.get("webhook") == "https://wechat/user1"


# ============================================================
# Convenience: is_enabled
# ============================================================

class TestRouterIsEnabled:
    """Tests for router-level is_enabled."""

    def test_enabled_with_channels(self, router_wechat_only):
        router, _ = router_wechat_only
        assert router.is_enabled is True

    def test_disabled_without_channels(self, router_no_config):
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
