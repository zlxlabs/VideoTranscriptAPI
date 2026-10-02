"""
Integration tests for Feishu notification with real webhook.

Sends actual messages to a Feishu bot webhook to verify end-to-end delivery.
These tests require network access and a valid Feishu webhook.

The webhook is NOT stored in this repository. Export it before running:

    FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/<token>" \
        VTAPI_TESTS_MANUAL=1 uv run pytest tests/manual/test_feishu_real.py -v -s

Without FEISHU_WEBHOOK set, every test in this module is skipped.

Run: VTAPI_TESTS_MANUAL=1 uv run pytest tests/manual/test_feishu_real.py -v -s
"""

import os
import time

import pytest

from wecom_notifier import FeishuNotifier


FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")


@pytest.fixture(scope="module")
def webhook():
    """Read the real webhook from the environment; skip if not provided."""
    if not FEISHU_WEBHOOK:
        pytest.skip("FEISHU_WEBHOOK is not set; skipping real-webhook tests")
    return FEISHU_WEBHOOK


@pytest.fixture(scope="module")
def notifier():
    n = FeishuNotifier()
    yield n
    n.stop_all()


class TestFeishuRealWebhook:
    """Integration tests against real Feishu webhook."""

    def test_send_text(self, notifier, webhook):
        """Send a plain text message to Feishu."""
        result = notifier.send_text(
            webhook_url=webhook,
            content="[Integration Test] send_text from VideoTranscriptAPI",
            async_send=False,
        )
        assert result.success is True, f"send_text failed: {result.error}"

    def test_send_card_markdown(self, notifier, webhook):
        """Send a card (markdown) message to Feishu."""
        content = (
            "## Integration Test\n\n"
            "**Project:** VideoTranscriptAPI\n\n"
            "**Status:** send_card works\n\n"
            "| Column A | Column B |\n"
            "|----------|----------|\n"
            "| row 1    | value 1  |\n"
            "| row 2    | value 2  |"
        )
        result = notifier.send_card(
            webhook_url=webhook,
            content=content,
            title="VideoTranscriptAPI Integration Test",
            template="green",
            async_send=False,
        )
        assert result.success is True, f"send_card failed: {result.error}"

    def test_feishu_channel_send_text(self, webhook):
        """Test FeishuChannel.send_text with real webhook."""
        from src.video_transcript_api.utils.notifications.channel import FeishuChannel

        ch = FeishuChannel(webhook=webhook)
        assert ch.is_enabled is True
        result = ch.send_text("[FeishuChannel] send_text integration test")
        # send_text is async by default, just verify submission
        assert result is True

    def test_feishu_channel_send_rich(self, webhook):
        """Test FeishuChannel.send_rich with real webhook."""
        from src.video_transcript_api.utils.notifications.channel import FeishuChannel

        ch = FeishuChannel(webhook=webhook)
        content = (
            "## FeishuChannel Integration Test\n\n"
            "**Method:** send_rich (send_card)\n\n"
            "This verifies that markdown content renders correctly as a Feishu card."
        )
        result = ch.send_rich(content, title="FeishuChannel Test")
        assert result is True

    def test_notification_router_with_feishu(self, webhook):
        """Test NotificationRouter dispatching to Feishu."""
        from unittest.mock import patch
        from src.video_transcript_api.utils.notifications.channel import FeishuChannel
        from src.video_transcript_api.utils.notifications.router import NotificationRouter

        with patch(
            "src.video_transcript_api.utils.notifications.router.load_config",
            return_value={},
        ):
            router = NotificationRouter()

        # Manually inject a real FeishuChannel
        ch = FeishuChannel(webhook=webhook)
        router.channels = [ch]

        assert router.is_enabled is True

        results = router.send_text("[Router] Multi-channel dispatch test (feishu only)")
        assert results.get("feishu") is True

        # Wait for async sends to complete
        time.sleep(2)
