"""Test calibration_status/stats tracking in PlainTextProcessor.

Mirrors tests/unit/test_calibration_stats.py (which covers the speaker-aware
/ chunk-based path). This file covers the plain-text / segment-based path,
which previously had NO visibility into per-segment fallback at all.

Three states covered end to end via process():
- full:    every segment cleanly passed the length-ratio/validation gate
- partial: some segments fell back (raw original) or degraded to low_quality
- none:    every segment fell back to the formatted original (LLM produced
           nothing usable)

Plus the "选较长低质输出" case: when both LLM attempts fail the length-ratio
gate and best_quality strategy still picks an LLM candidate (not the raw
formatted original), it must be counted as low_quality_segments and must NOT
be silently reported as a clean success (calibration_status must not be FULL).

All console output must be in English only (no emoji, no Chinese).
"""

import pytest
from unittest.mock import Mock

from video_transcript_api.llm.processors.plain_text_processor import PlainTextProcessor
from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.core.key_info_extractor import KeyInfo
from video_transcript_api.utils.llm_status import CalibrationStatus


@pytest.fixture
def mock_config():
    """Mock LLM config with all attributes PlainTextProcessor reads."""
    config = Mock(spec=LLMConfig)
    config.enable_threshold = 5000
    config.min_calibrate_ratio = 0.8
    config.concurrent_workers = 10
    config.segment_size = 2000
    config.max_segment_size = 3000
    config.calibrate_model = "mock-model"
    config.calibrate_reasoning_effort = "medium"
    config.segmentation_pass_ratio = 0.7
    config.segmentation_force_retry_ratio = 0.5
    config.segmentation_fallback_strategy = "best_quality"
    config.segmentation_validation_enabled = False
    return config


@pytest.fixture
def processor(mock_config):
    return PlainTextProcessor(
        config=mock_config,
        llm_client=Mock(),
        key_info_extractor=Mock(),
        quality_validator=Mock(),
    )


def _key_info_mock(processor):
    mock_key_info = Mock(spec=KeyInfo)
    mock_key_info.to_dict.return_value = {}
    mock_key_info.format_for_prompt.return_value = ""
    processor.key_info_extractor.extract.return_value = mock_key_info


class TestCalibrateSegmentsStatusTracking:
    """Directly exercise _calibrate_segments to control per-segment outcomes."""

    def test_all_segments_pass_cleanly(self, processor):
        """Every segment's first LLM attempt clears the pass_ratio gate -> all 'success'."""
        _key_info_mock(processor)
        original = "A" * 100
        segments = [original, original]

        # calibrated text long enough to clear pass_ratio (0.7) for every call
        response = Mock()
        response.text = "B" * 90
        processor.llm_client.call = Mock(return_value=response)

        calibrated, statuses = processor._calibrate_segments(
            segments=segments,
            key_info=processor.key_info_extractor.extract.return_value,
            title="t",
            description="d",
            selected_models=None,
            language="zh",
        )

        assert statuses == ["success", "success"]
        assert calibrated == ["B" * 90, "B" * 90]

    def test_exception_marks_fallback(self, processor):
        """LLM exception -> _format_plain_text(original) -> status 'fallback'."""
        _key_info_mock(processor)
        segments = ["A" * 100]
        processor.llm_client.call = Mock(side_effect=Exception("boom"))

        calibrated, statuses = processor._calibrate_segments(
            segments=segments,
            key_info=processor.key_info_extractor.extract.return_value,
            title="t",
            description="d",
            selected_models=None,
            language="zh",
        )

        assert statuses == ["fallback"]

    def test_best_quality_picks_llm_candidate_marks_low_quality(self, processor, mock_config):
        """Both attempts stay under force_retry_ratio (red zone) so no natural pass;
        best_quality picks the longer LLM candidate (not the formatted original) ->
        status must be 'low_quality', NOT 'success' and NOT 'fallback'."""
        _key_info_mock(processor)
        original = "A" * 100
        segments = [original]

        # Both attempts return short text -> ratio < force_retry_ratio (0.5) -> fallback path,
        # but best_quality still picks the longer of the two LLM candidates over raw original.
        short_response = Mock()
        short_response.text = "short but not empty text output"  # non-trivial LLM text, ratio<0.5
        processor.llm_client.call = Mock(return_value=short_response)

        calibrated, statuses = processor._calibrate_segments(
            segments=segments,
            key_info=processor.key_info_extractor.extract.return_value,
            title="t",
            description="d",
            selected_models=None,
            language="zh",
        )

        assert statuses == ["low_quality"]
        # The LLM's own output was kept (not replaced with the reformatted original)
        assert calibrated[0] == "short but not empty text output"

    def test_formatted_original_strategy_marks_fallback(self, processor, mock_config):
        """formatted_original strategy always returns _format_plain_text(original) ->
        even though the LLM was called, the final text is the raw original -> 'fallback'."""
        mock_config.segmentation_fallback_strategy = "formatted_original"
        _key_info_mock(processor)
        original = "A" * 100
        segments = [original]

        short_response = Mock()
        short_response.text = "short"
        processor.llm_client.call = Mock(return_value=short_response)

        calibrated, statuses = processor._calibrate_segments(
            segments=segments,
            key_info=processor.key_info_extractor.extract.return_value,
            title="t",
            description="d",
            selected_models=None,
            language="zh",
        )

        assert statuses == ["fallback"]


class TestProcessCalibrationStatsAggregation:
    """End-to-end via process(): verify stats dict aggregation and calibration_status."""

    def test_full_status_when_all_segments_succeed(self, processor):
        _key_info_mock(processor)
        response = Mock()
        response.text = "B" * 90
        processor.llm_client.call = Mock(return_value=response)

        result = processor.process(
            text="A" * 100,
            title="t",
            author="a",
            platform="test",
            media_id="m1",
        )

        stats = result["stats"]
        assert stats["total_segments"] == 1
        assert stats["fallback_segments"] == 0
        assert stats["low_quality_segments"] == 0
        assert stats["calibrated_segments"] == 1
        assert stats["calibration_status"] == CalibrationStatus.FULL

    def test_none_status_when_all_segments_fallback(self, processor):
        _key_info_mock(processor)
        processor.llm_client.call = Mock(side_effect=Exception("boom"))

        result = processor.process(
            text="A" * 100,
            title="t",
            author="a",
            platform="test",
            media_id="m2",
        )

        stats = result["stats"]
        assert stats["fallback_segments"] == 1
        assert stats["calibrated_segments"] == 0
        assert stats["calibration_status"] == CalibrationStatus.NONE

    def test_partial_status_does_not_lie_about_low_quality_segment(self, processor, mock_config):
        """A single low_quality segment must NOT be reported as calibration_status=full."""
        _key_info_mock(processor)
        short_response = Mock()
        short_response.text = "short but not empty text output"
        processor.llm_client.call = Mock(return_value=short_response)

        result = processor.process(
            text="A" * 100,
            title="t",
            author="a",
            platform="test",
            media_id="m3",
        )

        stats = result["stats"]
        assert stats["low_quality_segments"] == 1
        assert stats["fallback_segments"] == 0
        # A segment used LLM output but never cleanly passed -> calibrated but not "full"
        assert stats["calibrated_segments"] == 1
        assert stats["calibration_status"] != CalibrationStatus.FULL
        assert stats["calibration_status"] == CalibrationStatus.PARTIAL
