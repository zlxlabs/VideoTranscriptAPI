"""Test calibration_stats tracking in speaker_aware_processor._calibrate_chunks"""

import unittest
from unittest.mock import MagicMock, patch
from video_transcript_api.llm.processors.speaker_aware_processor import (
    SpeakerAwareProcessor,
)
from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.core.key_info_extractor import KeyInfo


def _make_chunk(n_dialogs=3):
    """Create a simple chunk of dialogs for testing."""
    return [
        {"speaker": "A", "text": f"dialog {i}", "start_time": "00:00:00",
         "end_time": "00:00:10", "duration": 10.0}
        for i in range(n_dialogs)
    ]


def _make_processor():
    """Create a SpeakerAwareProcessor with mocked dependencies."""
    config = MagicMock(spec=LLMConfig)
    config.calibrate_model = "test-model"
    config.calibrate_reasoning_effort = None
    config.max_calibration_retries = 0  # no retries, keep test fast
    config.structured_fallback_strategy = "original"
    config.structured_validation_enabled = False
    config.calibration_concurrent_limit = 2
    config.min_calibrate_ratio = 0.8
    config.chunk_time_budget = 300
    config.min_correction_coverage = 0.5

    llm_client = MagicMock()
    key_info_extractor = MagicMock()
    speaker_inferencer = MagicMock()
    quality_validator = MagicMock()

    processor = SpeakerAwareProcessor(
        config=config,
        llm_client=llm_client,
        key_info_extractor=key_info_extractor,
        speaker_inferencer=speaker_inferencer,
        quality_validator=quality_validator,
    )
    return processor


class TestCalibrationStats(unittest.TestCase):
    """Verify calibration_stats are correctly tracked."""

    def test_all_chunks_succeed(self):
        """When all chunks calibrate successfully, stats reflect that."""
        processor = _make_processor()
        # Force sequential for deterministic order
        processor.config.calibration_concurrent_limit = 1
        chunks = [_make_chunk(2), _make_chunk(3)]

        # Return matching dialog count for each chunk
        def make_result(n):
            result = MagicMock()
            result.structured_output = {
                "corrections": [
                    {"id": i, "text": f"calibrated {i}"}
                    for i in range(n)
                ]
            }
            return result

        processor.llm_client.call = MagicMock(
            side_effect=[make_result(2), make_result(3)]
        )

        key_info = KeyInfo(
            names=[], places=[], technical_terms=[], brands=[],
            abbreviations=[], foreign_terms=[], other_entities=[],
        )
        speaker_mapping = {"A": "Alice"}

        calibrated_chunks, cal_stats = processor._calibrate_chunks(
            chunks=chunks,
            original_chunks=chunks,
            key_info=key_info,
            speaker_mapping=speaker_mapping,
            title="test",
            description="test",
            selected_models={"calibrate_model": "test-model", "calibrate_reasoning_effort": None},
            language="zh",
        )

        self.assertEqual(cal_stats["total_chunks"], 2)
        self.assertEqual(cal_stats["success_count"], 2)
        self.assertEqual(cal_stats["fallback_count"], 0)
        self.assertEqual(cal_stats["failed_count"], 0)

    def test_all_chunks_fail(self):
        """When LLM raises exception for all chunks, stats show all failed."""
        processor = _make_processor()
        chunks = [_make_chunk(2), _make_chunk(2), _make_chunk(2)]

        # Mock LLM to always raise
        processor.llm_client.call = MagicMock(
            side_effect=Exception("API timeout")
        )

        key_info = KeyInfo(
            names=[], places=[], technical_terms=[], brands=[],
            abbreviations=[], foreign_terms=[], other_entities=[],
        )

        calibrated_chunks, cal_stats = processor._calibrate_chunks(
            chunks=chunks,
            original_chunks=chunks,
            key_info=key_info,
            speaker_mapping={"A": "Alice"},
            title="test",
            description="test",
            selected_models={"calibrate_model": "test-model", "calibrate_reasoning_effort": None},
            language="zh",
        )

        self.assertEqual(cal_stats["total_chunks"], 3)
        self.assertEqual(cal_stats["success_count"], 0)
        self.assertEqual(cal_stats["failed_count"], 3)

    def test_mixed_success_and_failure(self):
        """Some chunks succeed, some fail."""
        processor = _make_processor()
        # Force sequential execution for deterministic ordering
        processor.config.calibration_concurrent_limit = 1
        chunks = [_make_chunk(2), _make_chunk(2)]

        # First chunk succeeds, second chunk fails
        success_result = MagicMock()
        success_result.structured_output = {
            "corrections": [
                {"id": 0, "text": "ok 0"},
                {"id": 1, "text": "ok 1"},
            ]
        }
        processor.llm_client.call = MagicMock(
            side_effect=[success_result, Exception("API timeout")]
        )

        key_info = KeyInfo(
            names=[], places=[], technical_terms=[], brands=[],
            abbreviations=[], foreign_terms=[], other_entities=[],
        )

        calibrated_chunks, cal_stats = processor._calibrate_chunks(
            chunks=chunks,
            original_chunks=chunks,
            key_info=key_info,
            speaker_mapping={"A": "Alice"},
            title="test",
            description="test",
            selected_models={"calibrate_model": "test-model", "calibrate_reasoning_effort": None},
            language="zh",
        )

        self.assertEqual(cal_stats["total_chunks"], 2)
        self.assertEqual(cal_stats["success_count"], 1)
        self.assertEqual(cal_stats["failed_count"], 1)

    def test_partial_coverage_keeps_applied_corrections(self):
        """REGRESSION: when the LLM returns corrections for only some ids,
        the ID-anchor merge KEEPS those corrections (missing ids fall back to
        original) instead of discarding the whole chunk. This is the exact bug
        that made "威皇小海鲜" revert under the old whole-chunk-revert design."""
        processor = _make_processor()  # max_calibration_retries=0
        chunks = [_make_chunk(3)]

        # LLM only returns id=0 (1 of 3) -> coverage 33% < 0.5 threshold.
        # With no retries left, it is accepted as "partial", NOT discarded.
        result = MagicMock()
        result.structured_output = {
            "corrections": [
                {"id": 0, "text": "fixed zero"},
            ]
        }
        processor.llm_client.call = MagicMock(return_value=result)

        key_info = KeyInfo(
            names=[], places=[], technical_terms=[], brands=[],
            abbreviations=[], foreign_terms=[], other_entities=[],
        )

        calibrated_chunks, cal_stats = processor._calibrate_chunks(
            chunks=chunks,
            original_chunks=chunks,
            key_info=key_info,
            speaker_mapping={"A": "Alice"},
            title="test",
            description="test",
            selected_models={"calibrate_model": "test-model", "calibrate_reasoning_effort": None},
            language="zh",
        )

        self.assertEqual(cal_stats["total_chunks"], 1)
        self.assertEqual(cal_stats["fallback_count"], 0)  # NOT discarded
        self.assertEqual(cal_stats["partial_count"], 1)
        # The applied correction survives; missing ids keep original text.
        texts = [d["text"] for d in calibrated_chunks[0]]
        self.assertEqual(texts[0], "fixed zero")
        self.assertEqual(texts[1], "dialog 1")
        self.assertEqual(cal_stats["dialog_counts"]["applied"], 1)
        self.assertEqual(cal_stats["dialog_counts"]["kept_original"], 2)


if __name__ == "__main__":
    unittest.main()
