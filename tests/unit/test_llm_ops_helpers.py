"""
llm_ops helper function unit tests.

Covers:
- _generate_title_if_needed
- _prepare_llm_content
- _build_result_dict
- _build_calibration_warning
- _sanitize_title

All console output must be in English only (no emoji, no Chinese).
"""

import pytest
from video_transcript_api.api.services.llm_ops import (
    _prepare_llm_content,
    _build_result_dict,
    _build_calibration_warning,
    _should_backfill_summary,
)


class TestPrepareLLMContent:
    """Test _prepare_llm_content."""

    def test_plain_text_without_speaker(self):
        """No speaker recognition should return transcript as-is."""
        result = _prepare_llm_content(
            llm_task={"transcription_data": None},
            transcript="hello world",
            use_speaker_recognition=False,
        )
        assert result == "hello world"

    def test_speaker_with_dict_segments(self):
        """Dict transcription_data should extract segments."""
        result = _prepare_llm_content(
            llm_task={"transcription_data": {"segments": [{"text": "hi"}]}},
            transcript="fallback",
            use_speaker_recognition=True,
        )
        assert result == [{"text": "hi"}]

    def test_speaker_with_list_data(self):
        """List transcription_data should be used directly."""
        data = [{"text": "a"}, {"text": "b"}]
        result = _prepare_llm_content(
            llm_task={"transcription_data": data},
            transcript="fallback",
            use_speaker_recognition=True,
        )
        assert result == data

    def test_speaker_with_unexpected_type_falls_back(self):
        """Unexpected type should fall back to transcript text."""
        result = _prepare_llm_content(
            llm_task={"transcription_data": "unexpected string"},
            transcript="fallback text",
            use_speaker_recognition=True,
        )
        assert result == "fallback text"

    def test_speaker_without_data(self):
        """No transcription_data should return transcript."""
        result = _prepare_llm_content(
            llm_task={},
            transcript="text only",
            use_speaker_recognition=True,
        )
        assert result == "text only"


class TestBuildResultDict:
    """Test _build_result_dict."""

    def test_basic_result(self):
        """Should build result dict from coordinator result."""
        coordinator_result = {
            "calibrated_text": "calibrated",
            "summary_text": "summary",
            "stats": {"original_length": 100},
            "models_used": {"calibrate_model": "test"},
        }
        result = _build_result_dict(coordinator_result)
        assert result["calibrate_success"] is True
        assert result["summary_success"] is True
        assert result["skip_summary"] is False

    def test_no_summary(self):
        """None summary should set skip_summary=True."""
        coordinator_result = {
            "calibrated_text": "cal",
            "summary_text": None,
            "stats": {},
            "models_used": {},
        }
        result = _build_result_dict(coordinator_result)
        assert result["skip_summary"] is True
        assert result["summary_success"] is False

    def test_structured_data_included(self):
        """structured_data should be passed through."""
        coordinator_result = {
            "calibrated_text": "cal",
            "summary_text": "sum",
            "stats": {},
            "models_used": {},
            "structured_data": {"key": "value"},
        }
        result = _build_result_dict(coordinator_result)
        assert result["structured_data"] == {"key": "value"}


class TestBuildCalibrationWarning:
    """Test _build_calibration_warning."""

    def test_no_stats(self):
        """No calibration_stats should return empty string."""
        assert _build_calibration_warning({}) == ""

    def test_all_success(self):
        """All success should return empty string."""
        stats = {
            "calibration_stats": {
                "total_chunks": 5,
                "success_count": 5,
                "failed_count": 0,
                "fallback_count": 0,
            }
        }
        assert _build_calibration_warning(stats) == ""

    def test_total_failure(self):
        """All chunks failed should return total failure warning."""
        stats = {
            "calibration_stats": {
                "total_chunks": 3,
                "success_count": 0,
                "failed_count": 3,
                "fallback_count": 0,
            }
        }
        warning = _build_calibration_warning(stats)
        assert "completely failed" in warning.lower() or "完全失败" in warning

    def test_partial_failure(self):
        """Some failures should return partial warning."""
        stats = {
            "calibration_stats": {
                "total_chunks": 5,
                "success_count": 3,
                "failed_count": 1,
                "fallback_count": 1,
            }
        }
        warning = _build_calibration_warning(stats)
        assert "3/5" in warning
        assert "1" in warning  # fallback count


class TestShouldBackfillSummary:
    """Test _should_backfill_summary helper.

    Decides whether /api/recalibrate should also re-run summary generation
    when the cache is missing a usable llm_summary.txt.
    """

    def test_not_calibrate_only_returns_false(self, tmp_path):
        """Non calibrate-only path should never trigger backfill."""
        summary_file = tmp_path / "llm_summary.txt"
        summary_file.write_text("stale", encoding="utf-8")
        cache_data = {"file_path": str(tmp_path)}
        assert _should_backfill_summary(cache_data, calibrate_only=False) is False

    def test_summary_exists_non_empty_returns_false(self, tmp_path):
        """Existing non-empty summary should not trigger backfill."""
        (tmp_path / "llm_summary.txt").write_text("real summary", encoding="utf-8")
        cache_data = {"file_path": str(tmp_path)}
        assert _should_backfill_summary(cache_data, calibrate_only=True) is False

    def test_summary_missing_returns_true(self, tmp_path):
        """Missing summary file should trigger backfill."""
        cache_data = {"file_path": str(tmp_path)}
        assert _should_backfill_summary(cache_data, calibrate_only=True) is True

    def test_summary_empty_file_returns_true(self, tmp_path):
        """Zero-byte summary placeholder should trigger backfill."""
        (tmp_path / "llm_summary.txt").write_text("", encoding="utf-8")
        cache_data = {"file_path": str(tmp_path)}
        assert _should_backfill_summary(cache_data, calibrate_only=True) is True

    def test_no_file_path_returns_false(self):
        """Cache data without file_path cannot be inspected; be conservative."""
        assert _should_backfill_summary({}, calibrate_only=True) is False
        assert _should_backfill_summary({"file_path": None}, calibrate_only=True) is False
