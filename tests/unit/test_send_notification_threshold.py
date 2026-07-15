"""
Unit tests for _send_notification text length threshold behavior.

When summary generation fails (skip_summary=True):
- calibrated_text <= 5000 chars: send full text in notification
- calibrated_text > 5000 chars: send only link, not full text

All console output must be in English only (no emoji, no Chinese).
"""

import pytest
from unittest.mock import patch, MagicMock

from video_transcript_api.api.services.llm_ops import _send_notification


NOTIFICATION_TEXT_THRESHOLD = 5000


@pytest.fixture
def mock_dependencies():
    """Mock external dependencies for _send_notification."""
    with (
        patch(
            "video_transcript_api.api.services.llm_ops.get_notification_router"
        ) as mock_router_fn,
        patch(
            "video_transcript_api.api.services.llm_ops.cache_manager"
        ) as mock_cache,
        patch(
            "video_transcript_api.api.services.llm_ops.get_base_url",
            return_value="http://localhost:8000",
        ),
        patch(
            "video_transcript_api.api.services.llm_ops.format_llm_config_markdown",
            return_value="model: test",
        ),
        patch("video_transcript_api.api.services.llm_ops.time"),
    ):
        mock_router = MagicMock()
        mock_router_fn.return_value = mock_router
        mock_cache.get_task_by_id.return_value = {
            "view_token": "test-token-123"
        }
        yield {
            "router": mock_router,
            "cache": mock_cache,
        }


class TestSendNotificationSummaryStatusLabel:
    """Test the summary status label text shown in the notification body:
    'disabled' (user turned off summarize) must read distinctly from
    'failed' (a genuine LLM/processing failure) -- conflating the two would
    misreport a deliberate user choice as an error."""

    def test_disabled_status_shows_not_enabled_label(self, mock_dependencies):
        result_dict = {
            "校对文本": "some calibrated text",
            "内容总结": None,
            "skip_summary": True,
            "stats": {
                "original_length": 100,
                "calibrated_length": 100,
                "summary_length": 0,
                "summary_status": "disabled",
            },
            "models_used": {},
        }

        _send_notification(
            task_id="task_disabled",
            video_title="Disabled Summary Video",
            display_url="https://example.com/video-disabled",
            use_speaker_recognition=False,
            result_dict=result_dict,
        )

        router = mock_dependencies["router"]
        call_kwargs = router.send_long_text.call_args[1]
        assert "未启用" in call_kwargs["text"]
        assert "生成失败" not in call_kwargs["text"]

    def test_failed_status_still_shows_generation_failed_label(self, mock_dependencies):
        """Regression: the pre-existing 'failed' label must be unaffected by
        adding the new 'disabled' branch."""
        result_dict = {
            "校对文本": "some calibrated text",
            "内容总结": None,
            "skip_summary": True,
            "stats": {
                "original_length": 100,
                "calibrated_length": 100,
                "summary_length": 0,
                "summary_status": "failed",
            },
            "models_used": {},
        }

        _send_notification(
            task_id="task_failed",
            video_title="Failed Summary Video",
            display_url="https://example.com/video-failed",
            use_speaker_recognition=False,
            result_dict=result_dict,
        )

        router = mock_dependencies["router"]
        call_kwargs = router.send_long_text.call_args[1]
        assert "生成失败" in call_kwargs["text"]
        assert "未启用" not in call_kwargs["text"]


class TestSendNotificationCalibrationDisabledDisclosure:
    """ci-gate review: calibrate=False, summarize=True combination must
    disclose that the summary was built from uncalibrated ASR text, not
    just report the summary status. Full end-to-end through
    _send_notification() (not just the _build_calibration_warning() helper
    in isolation) to lock down the real assembled notification text."""

    def test_calibration_disabled_with_summary_generated_discloses_in_text(
        self, mock_dependencies
    ):
        result_dict = {
            "校对文本": "raw ASR text, never LLM-calibrated",
            "内容总结": "a summary built from that raw text",
            "skip_summary": False,
            "stats": {
                "original_length": 100,
                "calibrated_length": 100,
                "summary_length": 40,
                "summary_status": "generated",
                "calibration_status": "disabled",
                "calibration_stats": None,
            },
            "models_used": {},
        }

        _send_notification(
            task_id="task_calibration_disabled",
            video_title="Calibration Disabled Video",
            display_url="https://example.com/video-calib-disabled",
            use_speaker_recognition=False,
            result_dict=result_dict,
        )

        router = mock_dependencies["router"]
        call_kwargs = router.send_long_text.call_args[1]
        assert "未启用" in call_kwargs["text"]

    def test_normal_calibration_does_not_show_disabled_disclosure(
        self, mock_dependencies
    ):
        """Regression guard: a genuinely successful calibration must not
        show the 'calibration disabled' disclosure."""
        result_dict = {
            "校对文本": "properly calibrated text",
            "内容总结": "a real summary",
            "skip_summary": False,
            "stats": {
                "original_length": 100,
                "calibrated_length": 100,
                "summary_length": 20,
                "summary_status": "generated",
                "calibration_status": "full",
                "calibration_stats": {
                    "total_chunks": 2, "success_count": 2,
                    "failed_count": 0, "fallback_count": 0,
                },
            },
            "models_used": {},
        }

        _send_notification(
            task_id="task_calibration_full",
            video_title="Normal Calibration Video",
            display_url="https://example.com/video-calib-full",
            use_speaker_recognition=False,
            result_dict=result_dict,
        )

        router = mock_dependencies["router"]
        call_kwargs = router.send_long_text.call_args[1]
        assert "未启用" not in call_kwargs["text"]


class TestSendNotificationThreshold:
    """Test notification text length threshold when summary is skipped."""

    def test_short_text_sends_full_calibrated_text(self, mock_dependencies):
        """When calibrated text <= 5000 chars and summary skipped, send full text."""
        short_text = "A" * 3000
        result_dict = {
            "校对文本": short_text,
            "内容总结": None,
            "skip_summary": True,
            "stats": {
                "original_length": 3500,
                "calibrated_length": 3000,
                "summary_length": 0,
            },
            "models_used": {},
        }

        _send_notification(
            task_id="task_short",
            video_title="Short Video",
            display_url="https://example.com/video1",
            use_speaker_recognition=False,
            result_dict=result_dict,
        )

        router = mock_dependencies["router"]
        router.send_long_text.assert_called_once()
        call_kwargs = router.send_long_text.call_args[1]
        # Full calibrated text should be present in the message
        assert short_text in call_kwargs["text"]

    def test_long_text_does_not_send_full_calibrated_text(self, mock_dependencies):
        """When calibrated text > 5000 chars and summary skipped, do NOT send full text."""
        long_text = "B" * 6000
        result_dict = {
            "校对文本": long_text,
            "内容总结": None,
            "skip_summary": True,
            "stats": {
                "original_length": 7000,
                "calibrated_length": 6000,
                "summary_length": 0,
            },
            "models_used": {},
        }

        _send_notification(
            task_id="task_long",
            video_title="Long Podcast",
            display_url="https://example.com/video2",
            use_speaker_recognition=False,
            result_dict=result_dict,
        )

        router = mock_dependencies["router"]
        router.send_long_text.assert_called_once()
        call_kwargs = router.send_long_text.call_args[1]
        # Full calibrated text should NOT be in the message
        assert long_text not in call_kwargs["text"]
        # But view URL should still be present
        assert "http://localhost:8000/view/test-token-123" in call_kwargs["text"]

    def test_exactly_threshold_sends_full_text(self, mock_dependencies):
        """When calibrated text == 5000 chars exactly, still send full text."""
        exact_text = "C" * NOTIFICATION_TEXT_THRESHOLD
        result_dict = {
            "校对文本": exact_text,
            "内容总结": None,
            "skip_summary": True,
            "stats": {
                "original_length": 5500,
                "calibrated_length": 5000,
                "summary_length": 0,
            },
            "models_used": {},
        }

        _send_notification(
            task_id="task_exact",
            video_title="Exact Threshold",
            display_url="https://example.com/video3",
            use_speaker_recognition=False,
            result_dict=result_dict,
        )

        router = mock_dependencies["router"]
        call_kwargs = router.send_long_text.call_args[1]
        assert exact_text in call_kwargs["text"]

    def test_summary_success_always_sends_summary(self, mock_dependencies):
        """When summary succeeds, always send summary regardless of length."""
        summary = "This is a summary"
        result_dict = {
            "校对文本": "D" * 10000,
            "内容总结": summary,
            "skip_summary": False,
            "stats": {
                "original_length": 12000,
                "calibrated_length": 10000,
                "summary_length": len(summary),
            },
            "models_used": {},
        }

        _send_notification(
            task_id="task_summary",
            video_title="Summary OK",
            display_url="https://example.com/video4",
            use_speaker_recognition=False,
            result_dict=result_dict,
        )

        router = mock_dependencies["router"]
        call_kwargs = router.send_long_text.call_args[1]
        assert summary in call_kwargs["text"]

    def test_long_text_notification_includes_stats(self, mock_dependencies):
        """When text is too long, notification should still include stats info."""
        long_text = "E" * 8000
        result_dict = {
            "校对文本": long_text,
            "内容总结": None,
            "skip_summary": True,
            "stats": {
                "original_length": 9000,
                "calibrated_length": 8000,
                "summary_length": 0,
            },
            "models_used": {},
        }

        _send_notification(
            task_id="task_stats",
            video_title="Stats Check",
            display_url="https://example.com/video5",
            use_speaker_recognition=False,
            result_dict=result_dict,
        )

        router = mock_dependencies["router"]
        call_kwargs = router.send_long_text.call_args[1]
        text = call_kwargs["text"]
        # Should contain stats
        assert "9,000" in text
        assert "8,000" in text
        # Should mention summary not generated
        assert "view/test-token-123" in text
