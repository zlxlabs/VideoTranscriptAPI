"""
Unit tests for FeishuChannel.

Covers:
- Initialization from config
- send_text delegation to FeishuNotifier
- send_rich delegation to FeishuNotifier.send_card
- notify_task_status message formatting
- Disabled when no webhook configured
- Secret parameter forwarding
"""

from unittest.mock import patch, MagicMock

import pytest

from src.video_transcript_api.utils.notifications.channel import (
    FeishuChannel,
    WeComChannel,
    NotificationChannel,
)


MODULE = "src.video_transcript_api.utils.notifications.channel"

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/test-key"


@pytest.fixture
def feishu_config():
    return {
        "feishu": {"webhook": FEISHU_WEBHOOK, "secret": "test-secret"},
        "wechat": {},
    }


@pytest.fixture
def feishu_channel(feishu_config):
    """Create a FeishuChannel with mocked dependencies."""
    with patch(f"{MODULE}.load_config", return_value=feishu_config), \
         patch(f"{MODULE}._get_global_feishu_notifier", return_value=MagicMock()):
        ch = FeishuChannel()
    return ch


@pytest.fixture
def feishu_channel_with_webhook():
    """Create a FeishuChannel with explicit webhook."""
    with patch(f"{MODULE}.load_config", return_value={"feishu": {}, "wechat": {}}), \
         patch(f"{MODULE}._get_global_feishu_notifier", return_value=MagicMock()):
        ch = FeishuChannel(webhook=FEISHU_WEBHOOK)
    return ch


# ============================================================
# Protocol Compliance
# ============================================================

class TestProtocolCompliance:
    """FeishuChannel and WeComChannel must satisfy NotificationChannel."""

    def test_feishu_channel_has_required_attributes(self, feishu_channel):
        assert hasattr(feishu_channel, "name")
        assert hasattr(feishu_channel, "is_enabled")
        assert callable(feishu_channel.send_text)
        assert callable(feishu_channel.send_rich)
        assert callable(feishu_channel.notify_task_status)

    def test_feishu_channel_name(self, feishu_channel):
        assert feishu_channel.name == "feishu"


# ============================================================
# Initialization
# ============================================================

class TestFeishuChannelInit:
    """Tests for FeishuChannel initialization."""

    def test_enabled_when_webhook_configured(self, feishu_channel):
        assert feishu_channel.is_enabled is True

    def test_disabled_when_no_webhook(self):
        with patch(f"{MODULE}.load_config", return_value={"feishu": {}, "wechat": {}}), \
             patch(f"{MODULE}._get_global_feishu_notifier", return_value=MagicMock()):
            ch = FeishuChannel()
        assert ch.is_enabled is False

    def test_explicit_webhook_overrides_config(self, feishu_channel_with_webhook):
        assert feishu_channel_with_webhook.is_enabled is True
        assert feishu_channel_with_webhook.webhook == FEISHU_WEBHOOK

    def test_webhook_from_config(self, feishu_channel):
        assert feishu_channel.webhook == FEISHU_WEBHOOK


# ============================================================
# send_text
# ============================================================

class TestFeishuSendText:
    """Tests for FeishuChannel.send_text."""

    def test_send_text_calls_feishu_notifier(self, feishu_channel):
        feishu_channel.notifier.send_text.return_value = MagicMock(success=True)

        result = feishu_channel.send_text("hello feishu")

        feishu_channel.notifier.send_text.assert_called_once_with(
            webhook_url=FEISHU_WEBHOOK,
            content="hello feishu",
            async_send=True,
        )
        assert result is True

    def test_send_text_with_custom_webhook(self, feishu_channel):
        custom_webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/custom"
        feishu_channel.notifier.send_text.return_value = MagicMock(success=True)

        feishu_channel.send_text("hello", webhook=custom_webhook)

        feishu_channel.notifier.send_text.assert_called_once_with(
            webhook_url=custom_webhook,
            content="hello",
            async_send=True,
        )

    def test_send_text_disabled_returns_false(self):
        with patch(f"{MODULE}.load_config", return_value={"feishu": {}, "wechat": {}}), \
             patch(f"{MODULE}._get_global_feishu_notifier", return_value=MagicMock()):
            ch = FeishuChannel()

        assert ch.send_text("hello") is False

    def test_send_text_empty_content_returns_false(self, feishu_channel):
        assert feishu_channel.send_text("") is False
        assert feishu_channel.send_text("   ") is False

    def test_send_text_exception_returns_false(self, feishu_channel):
        feishu_channel.notifier.send_text.side_effect = RuntimeError("network error")
        assert feishu_channel.send_text("hello") is False


# ============================================================
# send_rich (send_card mapping)
# ============================================================

class TestFeishuSendRich:
    """Tests for FeishuChannel.send_rich — maps to FeishuNotifier.send_card."""

    def test_send_rich_calls_send_card(self, feishu_channel):
        feishu_channel.notifier.send_card.return_value = MagicMock(success=True)

        result = feishu_channel.send_rich("## Title\n\nContent")

        feishu_channel.notifier.send_card.assert_called_once_with(
            webhook_url=FEISHU_WEBHOOK,
            content="## Title\n\nContent",
            title="通知",
            async_send=True,
        )
        assert result is True

    def test_send_rich_with_custom_title(self, feishu_channel):
        feishu_channel.notifier.send_card.return_value = MagicMock(success=True)

        feishu_channel.send_rich("content", title="Custom Title")

        feishu_channel.notifier.send_card.assert_called_once_with(
            webhook_url=FEISHU_WEBHOOK,
            content="content",
            title="Custom Title",
            async_send=True,
        )

    def test_send_rich_disabled_returns_false(self):
        with patch(f"{MODULE}.load_config", return_value={"feishu": {}, "wechat": {}}), \
             patch(f"{MODULE}._get_global_feishu_notifier", return_value=MagicMock()):
            ch = FeishuChannel()

        assert ch.send_rich("content") is False

    def test_send_rich_exception_returns_false(self, feishu_channel):
        feishu_channel.notifier.send_card.side_effect = RuntimeError("network error")
        assert feishu_channel.send_rich("content") is False


# ============================================================
# Oversized card splitting
# ============================================================

class TestFeishuCardSplitting:
    """send_rich must split cards over FEISHU_CARD_MAX_BYTES budget."""

    def _oversized_content(self) -> str:
        paragraphs = ["✅ [#abcdef] 标题", "一行说明"] + ["测" * 100 for _ in range(100)]
        return "\n\n".join(paragraphs)

    def test_within_budget_sends_single_card(self, feishu_channel):
        feishu_channel.notifier.send_card.return_value = MagicMock(success=True)
        content = "✅ [#abcdef] 标题\n\n" + "测" * 3400
        assert len(content.encode("utf-8")) > 10000

        result = feishu_channel.send_rich(content, title="原标题")

        feishu_channel.notifier.send_card.assert_called_once_with(
            webhook_url=FEISHU_WEBHOOK,
            content=content,
            title="原标题",
            async_send=True,
        )
        assert result is True

    def test_oversized_content_splits_into_budgeted_cards(self, feishu_channel):
        feishu_channel.notifier.send_card.return_value = MagicMock(success=True)
        content = self._oversized_content()
        assert len(content.encode("utf-8")) > 30000

        result = feishu_channel.send_rich(content, title="✅ [#abcdef] 标题")

        assert result is True
        calls = feishu_channel.notifier.send_card.call_args_list
        n = len(calls)
        assert n >= 2
        contents = [call.kwargs["content"] for call in calls]
        for part in contents:
            assert len(part.encode("utf-8")) <= 18000
        assert "".join(contents) == content
        assert contents[0].startswith("✅ [#abcdef]")
        assert calls[1].kwargs["title"].endswith(f" (2/{n})")

    def test_single_oversized_paragraph_hard_cut_on_char_boundaries(self, feishu_channel):
        feishu_channel.notifier.send_card.return_value = MagicMock(success=True)
        content = "文" * 7000
        assert len(content.encode("utf-8")) > 18000

        result = feishu_channel.send_rich(content, title="标题")

        assert result is True
        calls = feishu_channel.notifier.send_card.call_args_list
        assert len(calls) >= 2
        contents = [call.kwargs["content"] for call in calls]
        for part in contents:
            assert len(part.encode("utf-8")) <= 18000
            assert part.encode("utf-8").decode("utf-8") == part
        assert "".join(contents) == content

    def test_second_chunk_failure_returns_false(self, feishu_channel):
        feishu_channel.notifier.send_card.side_effect = [
            MagicMock(success=True),
            RuntimeError("network error"),
        ]

        result = feishu_channel.send_rich(self._oversized_content(), title="标题")

        assert result is False
        assert feishu_channel.notifier.send_card.call_count == 2


# ============================================================
# notify_task_status
# ============================================================

class TestFeishuNotifyTaskStatus:
    """Tests for FeishuChannel.notify_task_status."""

    def test_basic_notification(self, feishu_channel):
        with patch.object(feishu_channel, "send_rich", return_value=True) as mock_send:
            result = feishu_channel.notify_task_status(
                url="https://youtube.com/watch?v=test",
                status="【任务完成】",
                title="Test Video",
                task_id="task_abcdef123456",
            )

        assert result is True
        sent_content = mock_send.call_args[0][0]
        assert sent_content.splitlines()[0] == "✅ [#abcdef] Test Video"
        assert mock_send.call_args.kwargs["title"] == sent_content.splitlines()[0]
        assert "youtube.com" in sent_content
        assert "【任务完成】" in sent_content

    def test_notification_with_error(self, feishu_channel):
        with patch.object(feishu_channel, "send_rich", return_value=True) as mock_send:
            feishu_channel.notify_task_status(
                url="https://example.com",
                status="failed",
                error="Download timeout",
            )

        sent_content = mock_send.call_args[0][0]
        assert "Download timeout" in sent_content

    def test_notification_with_title_and_author(self, feishu_channel):
        with patch.object(feishu_channel, "send_rich", return_value=True) as mock_send:
            feishu_channel.notify_task_status(
                url="https://example.com",
                status="completed",
                title="Test Video",
                author="Test Author",
            )

        sent_content = mock_send.call_args[0][0]
        assert "Test Video" in sent_content
        assert "Test Author" in sent_content


# ============================================================
# WeComChannel wrapper
# ============================================================

WECHAT_MODULE = "src.video_transcript_api.utils.notifications.wechat"


class TestWeComChannel:
    """Tests for WeComChannel — thin wrapper around WechatNotifier."""

    def test_wecom_channel_name(self):
        with patch(f"{WECHAT_MODULE}.load_config", return_value={}), \
             patch(f"{WECHAT_MODULE}._get_global_notifier", return_value=MagicMock()):
            ch = WeComChannel(webhook="https://qyapi.weixin.qq.com/test")
        assert ch.name == "wechat"

    def test_wecom_channel_delegates_send_text(self):
        with patch(f"{WECHAT_MODULE}.load_config", return_value={}), \
             patch(f"{WECHAT_MODULE}._get_global_notifier", return_value=MagicMock()):
            ch = WeComChannel(webhook="https://qyapi.weixin.qq.com/test")

        with patch.object(ch._notifier, "send_text", return_value=True) as mock_send:
            result = ch.send_text("hello")

        mock_send.assert_called_once()
        assert result is True

    def test_wecom_channel_delegates_send_rich(self):
        with patch(f"{WECHAT_MODULE}.load_config", return_value={}), \
             patch(f"{WECHAT_MODULE}._get_global_notifier", return_value=MagicMock()):
            ch = WeComChannel(webhook="https://qyapi.weixin.qq.com/test")

        with patch.object(ch._notifier, "send_markdown_v2", return_value=True) as mock_send:
            result = ch.send_rich("## content")

        mock_send.assert_called_once()
        assert result is True

    def test_wecom_channel_enabled_with_webhook(self):
        with patch(f"{WECHAT_MODULE}.load_config", return_value={}), \
             patch(f"{WECHAT_MODULE}._get_global_notifier", return_value=MagicMock()):
            ch = WeComChannel(webhook="https://qyapi.weixin.qq.com/test")
        assert ch.is_enabled is True

    def test_wecom_channel_disabled_without_webhook(self):
        with patch(f"{WECHAT_MODULE}.load_config", return_value={}), \
             patch(f"{WECHAT_MODULE}._get_global_notifier", return_value=MagicMock()):
            ch = WeComChannel()
        assert ch.is_enabled is False
