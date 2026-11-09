"""Single-source-of-truth tests for calibration status derivation.

Locked contract (card vta-fsm-C3-calibration-status):
- total units == 0                         -> NONE
- no unit got effective LLM output         -> NONE
  (all fallback/failed, or structured corrections all empty applied==0)
- any fallback/failed/partial/low_quality  -> PARTIAL
- otherwise                                -> FULL

All four production call sites must route through
video_transcript_api.utils.llm_status.derive_calibration_status (directly or
via derive_calibration_status_from_stats). The real-caller test classes below
pin each call site through its public entry point:
- SpeakerAwareProcessor._calibrate_chunks (structured / chunk shape)
- PlainTextProcessor.process (plain-text / segment shape)
- views._derive_legacy_calibration_status (persisted legacy chunk shape)
- llm_ops._build_calibration_warning (notification wording trigger)

All console output must be in English only (no emoji, no Chinese).
"""

from unittest.mock import Mock

import pytest

from video_transcript_api.utils.llm_status import (
    CalibrationStatus,
    derive_calibration_status,
    derive_calibration_status_from_stats,
)


class TestDeriveCalibrationStatusTruthTable:
    """Truth table for the single derivation function (count inputs only)."""

    def test_zero_total_units_is_none(self):
        assert (
            derive_calibration_status(
                total_units=0, llm_output_units=0, degraded_units=0, partial_units=0
            )
            is CalibrationStatus.NONE
        )

    def test_zero_total_beats_degraded_and_partial(self):
        assert (
            derive_calibration_status(
                total_units=0, llm_output_units=0, degraded_units=5, partial_units=5
            )
            is CalibrationStatus.NONE
        )

    def test_all_units_degraded_is_none(self):
        assert (
            derive_calibration_status(
                total_units=3, llm_output_units=0, degraded_units=3, partial_units=0
            )
            is CalibrationStatus.NONE
        )

    def test_no_llm_output_units_is_none(self):
        """Structured corrections all empty (applied==0): chunks were accepted
        as 'partial' but not a single unit got an effective LLM correction."""
        assert (
            derive_calibration_status(
                total_units=2, llm_output_units=0, degraded_units=0, partial_units=2
            )
            is CalibrationStatus.NONE
        )

    def test_any_degraded_unit_is_partial(self):
        assert (
            derive_calibration_status(
                total_units=5, llm_output_units=4, degraded_units=1, partial_units=0
            )
            is CalibrationStatus.PARTIAL
        )

    def test_any_partial_unit_is_partial(self):
        """Low coverage / low quality units demote the status even when every
        unit kept some LLM output (contract change vs the old FULL rule)."""
        assert (
            derive_calibration_status(
                total_units=2, llm_output_units=2, degraded_units=0, partial_units=1
            )
            is CalibrationStatus.PARTIAL
        )

    def test_clean_success_is_full(self):
        assert (
            derive_calibration_status(
                total_units=4, llm_output_units=4, degraded_units=0, partial_units=0
            )
            is CalibrationStatus.FULL
        )


class TestDeriveCalibrationStatusFromStats:
    """Dict adapter used by llm_ops notifications and views legacy reads."""

    def test_chunk_shape_applied_zero_is_none(self):
        """dialog_counts.applied==0 is the explicit 'corrections all empty'
        signal -> NONE even though chunk statuses were accepted as partial."""
        cal_stats = {
            "total_chunks": 2,
            "success_count": 0,
            "partial_count": 2,
            "fallback_count": 0,
            "failed_count": 0,
            "dialog_counts": {"applied": 0},
        }
        assert derive_calibration_status_from_stats(cal_stats) is CalibrationStatus.NONE

    def test_chunk_shape_clean_is_full(self):
        cal_stats = {
            "total_chunks": 3,
            "success_count": 3,
            "partial_count": 0,
            "fallback_count": 0,
            "failed_count": 0,
            "dialog_counts": {"applied": 7},
        }
        assert derive_calibration_status_from_stats(cal_stats) is CalibrationStatus.FULL

    def test_chunk_shape_degraded_is_partial(self):
        cal_stats = {
            "total_chunks": 5,
            "success_count": 3,
            "partial_count": 0,
            "fallback_count": 1,
            "failed_count": 1,
            "dialog_counts": {"applied": 3},
        }
        assert (
            derive_calibration_status_from_stats(cal_stats) is CalibrationStatus.PARTIAL
        )

    def test_chunk_shape_missing_dialog_counts_defaults_to_accepted(self):
        """Legacy persisted stats may lack dialog_counts; without the explicit
        applied==0 signal the accepted units (total - degraded) count as output."""
        cal_stats = {
            "total_chunks": 3,
            "success_count": 3,
            "fallback_count": 0,
            "failed_count": 0,
        }
        assert derive_calibration_status_from_stats(cal_stats) is CalibrationStatus.FULL

    def test_segment_shape_low_quality_is_partial(self):
        cal_stats = {
            "total_segments": 4,
            "calibrated_segments": 4,
            "fallback_segments": 0,
            "low_quality_segments": 1,
        }
        assert (
            derive_calibration_status_from_stats(cal_stats) is CalibrationStatus.PARTIAL
        )

    def test_segment_shape_all_fallback_is_none(self):
        cal_stats = {
            "total_segments": 3,
            "calibrated_segments": 0,
            "fallback_segments": 3,
            "low_quality_segments": 0,
        }
        assert derive_calibration_status_from_stats(cal_stats) is CalibrationStatus.NONE

    def test_unrecognized_shape_returns_none(self):
        assert derive_calibration_status_from_stats({"unrelated": 1}) is None


def _make_chunk(n_dialogs=3):
    return [
        {"speaker": "A", "text": f"dialog {i}", "start_time": "00:00:00",
         "end_time": "00:00:10", "duration": 10.0}
        for i in range(n_dialogs)
    ]


def _make_speaker_aware_processor():
    from video_transcript_api.llm.processors.speaker_aware_processor import (
        SpeakerAwareProcessor,
    )
    from video_transcript_api.llm.core.config import LLMConfig
    from video_transcript_api.llm.core.key_info_extractor import KeyInfo

    config = Mock(spec=LLMConfig)
    config.calibrate_model = "test-model"
    config.calibrate_reasoning_effort = None
    config.max_calibration_retries = 0
    config.structured_fallback_strategy = "original"
    config.structured_validation_enabled = False
    config.calibration_concurrent_limit = 1
    config.min_calibrate_ratio = 0.8
    config.chunk_time_budget = 300
    config.min_correction_coverage = 0.5

    processor = SpeakerAwareProcessor(
        config=config,
        llm_client=Mock(),
        key_info_extractor=Mock(),
        speaker_inferencer=Mock(),
        quality_validator=Mock(),
    )
    key_info = KeyInfo(
        names=[], places=[], technical_terms=[], brands=[],
        abbreviations=[], foreign_terms=[], other_entities=[],
    )
    return processor, key_info


def _run_calibrate_chunks(processor, key_info, chunks, llm_side_effect):
    processor.llm_client.call = Mock(side_effect=llm_side_effect)
    _, cal_stats = processor._calibrate_chunks(
        chunks=chunks,
        original_chunks=chunks,
        key_info=key_info,
        speaker_mapping={"A": "Alice"},
        title="test",
        description="test",
        selected_models={
            "calibrate_model": "test-model",
            "calibrate_reasoning_effort": None,
        },
        language="zh",
    )
    return cal_stats


class TestSpeakerAwareProcessorCallSite:
    """Real caller: SpeakerAwareProcessor._calibrate_chunks."""

    def test_empty_corrections_is_none(self):
        """LLM answers with an empty corrections list for the only chunk:
        applied==0 -> no unit got an effective correction -> NONE.
        (Base formula ignored partial-only stats and reported FULL.)"""
        processor, key_info = _make_speaker_aware_processor()
        result = Mock()
        result.structured_output = {"corrections": []}
        cal_stats = _run_calibrate_chunks(
            processor, key_info, [_make_chunk(3)], [result]
        )
        assert cal_stats["total_chunks"] == 1
        assert cal_stats["dialog_counts"]["applied"] == 0
        assert cal_stats["calibration_status"] is CalibrationStatus.NONE

    def test_low_coverage_partial_chunk_is_partial(self):
        """One chunk, coverage 1/3 < 0.5, no retries left: accepted as partial
        with applied=1. Contract change: status is PARTIAL, not FULL."""
        processor, key_info = _make_speaker_aware_processor()
        result = Mock()
        result.structured_output = {"corrections": [{"id": 0, "text": "fixed zero"}]}
        cal_stats = _run_calibrate_chunks(
            processor, key_info, [_make_chunk(3)], [result]
        )
        assert cal_stats["partial_count"] == 1
        assert cal_stats["fallback_count"] == 0
        assert cal_stats["failed_count"] == 0
        assert cal_stats["calibration_status"] is CalibrationStatus.PARTIAL

    def test_clean_success_is_full(self):
        processor, key_info = _make_speaker_aware_processor()

        def make_result(n):
            r = Mock()
            r.structured_output = {
                "corrections": [{"id": i, "text": f"calibrated {i}"} for i in range(n)]
            }
            return r

        cal_stats = _run_calibrate_chunks(
            processor,
            key_info,
            [_make_chunk(2), _make_chunk(2)],
            [make_result(2), make_result(2)],
        )
        assert cal_stats["calibration_status"] is CalibrationStatus.FULL


class TestPlainTextProcessorCallSite:
    """Real caller: PlainTextProcessor.process (segment shape).

    NOTE (red-check not applicable): the old plain-text formula is behaviorally
    equivalent to the new contract for every input, so this behavior-lock test
    is green on base as well; it pins that the refactor does not change the
    plain-text path.
    """

    def test_low_quality_segment_is_partial(self):
        from video_transcript_api.llm.core.config import LLMConfig
        from video_transcript_api.llm.core.key_info_extractor import KeyInfo
        from video_transcript_api.llm.processors.plain_text_processor import (
            PlainTextProcessor,
        )

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

        processor = PlainTextProcessor(
            config=config, llm_client=Mock(), key_info_extractor=Mock(),
            quality_validator=Mock(),
        )
        key_info = Mock(spec=KeyInfo)
        key_info.to_dict.return_value = {}
        key_info.format_for_prompt.return_value = ""
        processor.key_info_extractor.extract.return_value = key_info

        # Short LLM output -> low_quality (kept LLM text, ratio below gate)
        response = Mock()
        response.text = "short but not empty text output"
        processor.llm_client.call = Mock(return_value=response)

        result = processor.process(
            text="A" * 100, title="t", author="a", platform="test", media_id="m1",
        )
        stats = result["stats"]
        assert stats["low_quality_segments"] == 1
        assert stats["fallback_segments"] == 0
        assert stats["calibration_status"] is CalibrationStatus.PARTIAL


class TestLegacyViewsCallSite:
    """Real caller: views._derive_legacy_calibration_status."""

    def test_partial_only_legacy_stats_is_partial(self):
        """Partial-only persisted stats used to derive FULL; the single
        derivation demotes them to PARTIAL."""
        from video_transcript_api.api.routes.views import (
            _derive_legacy_calibration_status,
        )

        cal_stats = {
            "total_chunks": 2,
            "success_count": 0,
            "partial_count": 2,
            "fallback_count": 0,
            "failed_count": 0,
        }
        assert (
            _derive_legacy_calibration_status(cal_stats) is CalibrationStatus.PARTIAL
        )

    def test_legacy_applied_zero_is_none(self):
        from video_transcript_api.api.routes.views import (
            _derive_legacy_calibration_status,
        )

        cal_stats = {
            "total_chunks": 2,
            "success_count": 0,
            "partial_count": 2,
            "fallback_count": 0,
            "failed_count": 0,
            "dialog_counts": {"applied": 0},
        }
        assert (
            _derive_legacy_calibration_status(cal_stats) is CalibrationStatus.NONE
        )

    def test_legacy_clean_stats_is_full(self):
        from video_transcript_api.api.routes.views import (
            _derive_legacy_calibration_status,
        )

        assert (
            _derive_legacy_calibration_status(
                {"total_chunks": 3, "failed_count": 0, "fallback_count": 0}
            )
            is CalibrationStatus.FULL
        )


class TestBuildCalibrationWarningCallSite:
    """Real caller: llm_ops._build_calibration_warning."""

    def test_partial_only_stats_surfaces_warning(self):
        """Partial-only chunk stats used to produce NO warning (the old
        trigger only counted failed/fallback); now PARTIAL must warn."""
        from video_transcript_api.api.services.llm_ops import (
            _build_calibration_warning,
        )

        stats = {
            "calibration_stats": {
                "total_chunks": 2,
                "success_count": 0,
                "partial_count": 2,
                "fallback_count": 0,
                "failed_count": 0,
            }
        }
        warning = _build_calibration_warning(stats)
        assert warning != ""
        assert "2/2" in warning

    def test_applied_zero_stats_surfaces_total_failure_warning(self):
        """Corrections all empty (applied==0) must surface the total-failure
        wording; the old counter check saw failed=0 and stayed silent."""
        from video_transcript_api.api.services.llm_ops import (
            _build_calibration_warning,
        )

        stats = {
            "calibration_stats": {
                "total_chunks": 2,
                "success_count": 0,
                "partial_count": 2,
                "fallback_count": 0,
                "failed_count": 0,
                "dialog_counts": {"applied": 0},
            }
        }
        warning = _build_calibration_warning(stats)
        assert "完全失败" in warning
