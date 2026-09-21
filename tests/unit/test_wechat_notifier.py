"""
Unit tests for WechatNotifier.

Covers:
- URL cleaning (_clean_url) for various platforms
- URL protection/restoration (_protect_urls, _restore_urls)
- Risk control integration (_apply_risk_control_safe)
- Status emoji selection (_get_status_emoji)
- format_llm_config_markdown helper
- send_markdown_v2 edge cases
"""

from unittest.mock import patch, MagicMock

import pytest

from src.video_transcript_api.utils.notifications.wechat import (
    WechatNotifier,
    format_llm_config_markdown,
)


@pytest.fixture
def notifier():
    """Create a WechatNotifier with mocked dependencies."""
    with patch("src.video_transcript_api.utils.notifications.wechat.load_config", return_value={}), \
         patch("src.video_transcript_api.utils.notifications.wechat._get_global_notifier", return_value=MagicMock()):
        n = WechatNotifier(webhook="https://qyapi.weixin.qq.com/test")
    return n


# ============================================================
# URL Cleaning Tests
# ============================================================

class TestCleanURL:
    """Tests for WechatNotifier._clean_url."""

    def test_generic_url_strips_query(self, notifier):
        """Generic URLs should have query parameters removed."""
        url = "https://example.com/page?utm_source=twitter&ref=123"
        assert notifier._clean_url(url) == "https://example.com/page"

    def test_generic_url_without_query_unchanged(self, notifier):
        """Generic URLs without query params are returned as-is."""
        url = "https://example.com/page"
        assert notifier._clean_url(url) == url

    def test_youtube_preserves_v_param(self, notifier):
        """YouTube URLs should keep v= parameter only."""
        url = "https://www.youtube.com/watch?v=abc123&list=PLx&t=42"
        assert notifier._clean_url(url) == "https://www.youtube.com/watch?v=abc123"

    def test_youtube_no_v_param(self, notifier):
        """YouTube URL without v= should strip all params."""
        url = "https://www.youtube.com/channel?sub=1"
        assert notifier._clean_url(url) == "https://www.youtube.com/channel"

    def test_youtu_be_preserved(self, notifier):
        """youtu.be short URLs with v= should preserve it."""
        url = "https://youtu.be/watch?v=xyz"
        assert notifier._clean_url(url) == "https://youtu.be/watch?v=xyz"

    def test_xiaohongshu_preserves_xsec_token(self, notifier):
        """Xiaohongshu URLs should keep xsec_token only."""
        url = "https://www.xiaohongshu.com/explore/abc?xsec_token=tok123&xsec_source=pc"
        assert notifier._clean_url(url) == "https://www.xiaohongshu.com/explore/abc?xsec_token=tok123"

    def test_xiaohongshu_no_xsec_token(self, notifier):
        """Xiaohongshu URL without xsec_token should strip all params."""
        url = "https://www.xiaohongshu.com/explore/abc?source=pc"
        assert notifier._clean_url(url) == "https://www.xiaohongshu.com/explore/abc"

    def test_xhslink_preserves_xsec_token(self, notifier):
        """xhslink.com URLs should also preserve xsec_token."""
        url = "https://xhslink.com/a/abc?xsec_token=tok456&ref=share"
        assert notifier._clean_url(url) == "https://xhslink.com/a/abc?xsec_token=tok456"

    def test_xhslink_cn_preserves_xsec_token(self, notifier):
        """xhslink.cn URLs should also preserve xsec_token."""
        url = "https://xhslink.cn/o/abc?xsec_token=tok456&ref=share"
        assert notifier._clean_url(url) == "https://xhslink.cn/o/abc?xsec_token=tok456"

    def test_xiaohongshu_without_query(self, notifier):
        """Xiaohongshu URL without query should be returned as-is."""
        url = "https://www.xiaohongshu.com/explore/abc"
        assert notifier._clean_url(url) == url


# ============================================================
# URL Protection Tests
# ============================================================

class TestURLProtection:
    """Tests for _protect_urls and _restore_urls."""

    def test_protect_single_url(self, notifier):
        """Single URL should be replaced with placeholder."""
        text = "Visit https://example.com for details"
        protected, url_map = notifier._protect_urls(text)

        assert "https://example.com" not in protected
        assert "__URL_PLACEHOLDER_0__" in protected
        assert url_map["__URL_PLACEHOLDER_0__"] == "https://example.com"

    def test_protect_multiple_urls(self, notifier):
        """Multiple URLs should each get unique placeholders."""
        text = "See https://a.com and https://b.com here"
        protected, url_map = notifier._protect_urls(text)

        assert len(url_map) == 2
        assert "__URL_PLACEHOLDER_0__" in protected
        assert "__URL_PLACEHOLDER_1__" in protected

    def test_protect_no_urls(self, notifier):
        """Text without URLs returns empty url_map."""
        text = "No URLs here at all"
        protected, url_map = notifier._protect_urls(text)

        assert protected == text
        assert url_map == {}

    def test_protect_empty_text(self, notifier):
        """Empty text returns empty text and empty map."""
        protected, url_map = notifier._protect_urls("")
        assert protected == ""
        assert url_map == {}

    def test_protect_none_text(self, notifier):
        """None text returns None and empty map."""
        protected, url_map = notifier._protect_urls(None)
        assert protected is None
        assert url_map == {}

    def test_restore_urls(self, notifier):
        """Restored text should have original URLs back."""
        url_map = {
            "__URL_PLACEHOLDER_0__": "https://example.com",
            "__URL_PLACEHOLDER_1__": "https://other.com",
        }
        protected = "Visit __URL_PLACEHOLDER_0__ and __URL_PLACEHOLDER_1__"
        restored = notifier._restore_urls(protected, url_map)

        assert restored == "Visit https://example.com and https://other.com"

    def test_restore_empty_map(self, notifier):
        """Restoring with empty map returns text unchanged."""
        text = "no placeholders"
        assert notifier._restore_urls(text, {}) == text

    def test_roundtrip(self, notifier):
        """Protect then restore should return original text."""
        original = "Check https://example.com/path?q=1 for more"
        protected, url_map = notifier._protect_urls(original)
        restored = notifier._restore_urls(protected, url_map)
        assert restored == original


# ============================================================
# Risk Control Integration Tests
# ============================================================

class TestApplyRiskControlSafe:
    """Tests for _apply_risk_control_safe."""

    def test_empty_content_returns_as_is(self, notifier):
        """Empty or whitespace content is returned unchanged."""
        assert notifier._apply_risk_control_safe("") == ""
        assert notifier._apply_risk_control_safe("   ") == "   "
        assert notifier._apply_risk_control_safe(None) is None

    @patch("src.video_transcript_api.utils.notifications.wechat._get_risk_control")
    def test_risk_control_disabled(self, mock_get_rc, notifier):
        """When risk control is disabled, content passes through."""
        mock_rc = MagicMock()
        mock_rc.is_enabled.return_value = False
        mock_get_rc.return_value = mock_rc

        result = notifier._apply_risk_control_safe("some content")
        assert result == "some content"

    @patch("src.video_transcript_api.utils.notifications.wechat._get_risk_control")
    def test_risk_control_none(self, mock_get_rc, notifier):
        """When risk control module is None, content passes through."""
        mock_get_rc.return_value = None

        result = notifier._apply_risk_control_safe("some content")
        assert result == "some content"

    @patch("src.video_transcript_api.utils.notifications.wechat._get_risk_control")
    def test_risk_control_sanitizes(self, mock_get_rc, notifier):
        """When risk control detects sensitive words, sanitized text is returned."""
        mock_rc = MagicMock()
        mock_rc.is_enabled.return_value = True
        mock_rc.sanitize_text.return_value = {
            "has_sensitive": True,
            "sensitive_words": ["bad"],
            "sanitized_text": "cleaned content",
        }
        mock_get_rc.return_value = mock_rc

        result = notifier._apply_risk_control_safe("bad content")
        assert result == "cleaned content"

    @patch("src.video_transcript_api.utils.notifications.wechat._get_risk_control")
    def test_risk_control_exception_fallback(self, mock_get_rc, notifier):
        """If risk control raises, original protected content is returned."""
        mock_rc = MagicMock()
        mock_rc.is_enabled.return_value = True
        mock_rc.sanitize_text.side_effect = RuntimeError("risk control crash")
        mock_get_rc.return_value = mock_rc

        result = notifier._apply_risk_control_safe("some content without urls")
        # Should fall back to protected content (same as original when no URLs)
        assert result == "some content without urls"


# ============================================================
# Status Emoji Tests
# ============================================================

class TestGetStatusEmoji:
    """Tests for _get_status_emoji."""

    def test_error_status(self, notifier):
        """Error-related statuses should return error emoji."""
        assert notifier._get_status_emoji("processing", error="timeout") == "❌"
        assert notifier._get_status_emoji("任务失败") == "❌"
        assert notifier._get_status_emoji("处理异常") == "❌"
        assert notifier._get_status_emoji("发生错误") == "❌"

    def test_download_statuses(self, notifier):
        """Download-related statuses should return appropriate emojis."""
        assert notifier._get_status_emoji("正在下载") == "⬇️"
        assert notifier._get_status_emoji("下载完成") == "✅"
        assert notifier._get_status_emoji("下载中") == "📥"

    def test_transcription_statuses(self, notifier):
        """Transcription statuses should return appropriate emojis."""
        assert notifier._get_status_emoji("正在转录") == "🎤"
        assert notifier._get_status_emoji("转录完成") == "✅"
        assert notifier._get_status_emoji("转录中") == "📝"

    def test_completion_status(self, notifier):
        """Completion statuses should return success emoji."""
        # Note: "处理完成" matches "处理" first (returns "🔄"), not "完成"
        # because the if-elif chain checks "处理" before "完成"
        assert notifier._get_status_emoji("任务完成") == "✅"
        assert notifier._get_status_emoji("成功") == "✅"

    def test_processing_status(self, notifier):
        """Processing statuses should return processing emoji."""
        assert notifier._get_status_emoji("开始处理") == "🔄"

    def test_waiting_status(self, notifier):
        """Waiting statuses should return hourglass emoji."""
        assert notifier._get_status_emoji("等待队列") == "⏳"

    def test_cache_status(self, notifier):
        """Cache-related status returns floppy emoji."""
        assert notifier._get_status_emoji("缓存命中") == "💾"

    def test_platform_subtitle_status(self, notifier):
        """Platform subtitle status returns document emoji."""
        assert notifier._get_status_emoji("平台字幕") == "📄"

    def test_default_status(self, notifier):
        """Unknown status returns processing emoji."""
        assert notifier._get_status_emoji("unknown state") == "🔄"


# ============================================================
# send_markdown_v2 Edge Cases
# ============================================================

class TestSendMarkdownV2:
    """Tests for send_markdown_v2 edge cases."""

    def test_no_webhook_returns_false(self):
        """Should return False when webhook is not configured."""
        with patch("src.video_transcript_api.utils.notifications.wechat.load_config", return_value={}), \
             patch("src.video_transcript_api.utils.notifications.wechat._get_global_notifier", return_value=MagicMock()):
            n = WechatNotifier(webhook=None)
        assert n.send_markdown_v2("content") is False

    def test_empty_content_returns_false(self, notifier):
        """Should return False for empty or whitespace-only content."""
        assert notifier.send_markdown_v2("") is False
        assert notifier.send_markdown_v2("   ") is False

    def test_send_text_delegates_to_send_markdown_v2(self, notifier):
        """send_text should call send_markdown_v2."""
        with patch.object(notifier, "send_markdown_v2", return_value=True) as mock_send:
            result = notifier.send_text("hello")
            mock_send.assert_called_once_with("hello", skip_risk_control=False)
            assert result is True

    def test_successful_send(self, notifier):
        """Successful send should return True."""
        notifier.notifier.send_markdown.return_value = MagicMock()

        with patch("src.video_transcript_api.utils.notifications.wechat._get_risk_control", return_value=None):
            result = notifier.send_markdown_v2("hello world")

        assert result is True
        notifier.notifier.send_markdown.assert_called_once()

    def test_send_exception_returns_false(self, notifier):
        """Exception during send should return False."""
        notifier.notifier.send_markdown.side_effect = RuntimeError("network error")

        with patch("src.video_transcript_api.utils.notifications.wechat._get_risk_control", return_value=None):
            result = notifier.send_markdown_v2("hello")

        assert result is False

    def test_skip_risk_control_flag(self, notifier):
        """When skip_risk_control=True, risk control should not be called."""
        notifier.notifier.send_markdown.return_value = MagicMock()

        with patch.object(notifier, "_apply_risk_control_safe") as mock_rc:
            notifier.send_markdown_v2("content", skip_risk_control=True)
            mock_rc.assert_not_called()


# ============================================================
# format_llm_config_markdown Tests
# ============================================================

class TestFormatLLMConfigMarkdown:
    """Tests for format_llm_config_markdown helper."""

    def test_empty_input(self):
        """Empty or None input returns empty string."""
        assert format_llm_config_markdown({}) == ""
        assert format_llm_config_markdown(None) == ""

    def test_calibrate_model_only(self):
        """Only calibrate model should be shown."""
        result = format_llm_config_markdown({"calibrate_model": "gpt-4"})
        assert "gpt-4" in result
        assert "calibrate" in result.lower() or "\u6821\u5bf9" in result

    def test_summary_model_only(self):
        """Only summary model should be shown."""
        result = format_llm_config_markdown({"summary_model": "claude-3"})
        assert "claude-3" in result

    def test_both_models(self):
        """Both models should appear in output."""
        result = format_llm_config_markdown({
            "calibrate_model": "gpt-4",
            "summary_model": "claude-3",
        })
        assert "gpt-4" in result
        assert "claude-3" in result

    def test_reasoning_effort_shown(self):
        """Reasoning effort should appear when set."""
        result = format_llm_config_markdown({
            "calibrate_model": "gpt-4",
            "calibrate_reasoning_effort": "high",
        })
        assert "high" in result

    def test_no_risk_flag_in_output(self):
        """Models dict without has_risk should not show risk marker."""
        result = format_llm_config_markdown({
            "calibrate_model": "gpt-4",
        })
        assert "\u964d\u7ea7" not in result


# ============================================================
# notify_task_status Tests
# ============================================================

class TestNotifyTaskStatus:
    """Tests for WechatNotifier.notify_task_status."""

    def test_basic_notification(self, notifier):
        """Basic notification should include URL and status."""
        with patch.object(notifier, "send_text", return_value=True) as mock_send:
            result = notifier.notify_task_status(
                url="https://youtube.com/watch?v=test",
                status="started",
            )

        assert result is True
        sent_content = mock_send.call_args[0][0]
        assert "youtube.com" in sent_content
        assert "started" in sent_content

    def test_notification_with_error(self, notifier):
        """Error info should be included in notification."""
        with patch.object(notifier, "send_text", return_value=True) as mock_send:
            notifier.notify_task_status(
                url="https://example.com",
                status="failed",
                error="Download timeout",
            )

        sent_content = mock_send.call_args[0][0]
        assert "Download timeout" in sent_content

    def test_notification_with_title_and_author(self, notifier):
        """Title and author should be included when provided."""
        with patch.object(notifier, "send_text", return_value=True) as mock_send, \
             patch.object(notifier, "_apply_risk_control_safe", side_effect=lambda x, **kw: x):
            notifier.notify_task_status(
                url="https://example.com",
                status="completed",
                title="Test Video",
                author="Test Author",
            )

        sent_content = mock_send.call_args[0][0]
        assert "Test Video" in sent_content
        assert "Test Author" in sent_content

    def test_transcript_preview_on_completion(self, notifier):
        """Transcript preview should appear only for completion status."""
        with patch.object(notifier, "send_text", return_value=True) as mock_send:
            notifier.notify_task_status(
                url="https://example.com",
                status="\u8f6c\u5f55\u5b8c\u6210",
                transcript="This is a test transcript preview",
            )

        sent_content = mock_send.call_args[0][0]
        assert "test transcript" in sent_content

    def test_transcript_not_shown_for_other_statuses(self, notifier):
        """Transcript should NOT appear for non-completion statuses."""
        with patch.object(notifier, "send_text", return_value=True) as mock_send:
            notifier.notify_task_status(
                url="https://example.com",
                status="processing",
                transcript="This should not appear",
            )

        sent_content = mock_send.call_args[0][0]
        assert "This should not appear" not in sent_content
