"""Regression tests for Issue #179: ThreadPoolExecutor(max_workers=0) crashes on empty input.

Ensures that:
1. PlainTextProcessor._calibrate_segments([]) returns ([], []) directly without
   launching a ThreadPoolExecutor or issuing LLM calls.
2. PlainTextProcessor.process(text="") completes cleanly with 0 segments and
   honest calibration_status=none.
3. SpeakerAwareProcessor._calibrate_chunks(chunks=[]) returns empty chunks and
   zeroed calibration stats without launching a ThreadPoolExecutor.
4. SpeakerAwareProcessor.process(dialogs=[]) completes cleanly with empty output
   and honest calibration_status=none.
5. NotesProcessor with empty chapter slices returns a failed NotesResult rather
   than crashing on max_workers=0.
"""

from unittest.mock import MagicMock, Mock
import pytest

from video_transcript_api.llm.processors.plain_text_processor import PlainTextProcessor
from video_transcript_api.llm.processors.speaker_aware_processor import (
    SpeakerAwareProcessor,
)
from video_transcript_api.llm.processors.notes_processor import (
    NotesProcessor,
    NotesStatus,
)
from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.core.key_info_extractor import KeyInfo
from video_transcript_api.utils.llm_status import CalibrationStatus


@pytest.fixture
def mock_config():
    config = Mock(spec=LLMConfig)
    config.enable_threshold = 5000
    config.min_calibrate_ratio = 0.8
    config.concurrent_workers = 10
    config.calibration_concurrent_limit = 10
    config.segment_size = 2000
    config.max_segment_size = 3000
    config.calibrate_model = "mock-model"
    config.calibrate_reasoning_effort = "medium"
    config.notes_model = "mock-notes-model"
    config.notes_reasoning_effort = "medium"
    config.notes_concurrency = 4
    config.segmentation_pass_ratio = 0.7
    config.segmentation_force_retry_ratio = 0.5
    config.segmentation_fallback_strategy = "best_quality"
    config.segmentation_validation_enabled = False
    config.paragraphization_target_chars = 400
    config.paragraphization_hard_max_chars = 600
    config.paragraphization_pause_threshold_seconds = 2.0
    config.plain_structured_preferred_chunk_length = 2000
    config.plain_structured_max_chunk_length = 3000
    return config


class TestEmptyInputProcessors:
    def test_plain_text_calibrate_segments_empty_input(self, mock_config):
        llm_client = Mock()
        processor = PlainTextProcessor(
            config=mock_config,
            llm_client=llm_client,
            key_info_extractor=Mock(),
            quality_validator=Mock(),
        )

        calibrated_segments, segment_statuses = processor._calibrate_segments(
            segments=[],
            key_info=Mock(spec=KeyInfo),
            title="Empty Test",
            description="",
            selected_models=None,
        )

        assert calibrated_segments == []
        assert segment_statuses == []
        llm_client.call.assert_not_called()

    def test_plain_text_process_empty_text(self, mock_config):
        llm_client = Mock()
        key_info_mock = Mock(spec=KeyInfo)
        key_info_mock.to_dict.return_value = {}
        key_info_mock.format_for_prompt.return_value = ""
        key_info_extractor = Mock()
        key_info_extractor.extract.return_value = key_info_mock

        processor = PlainTextProcessor(
            config=mock_config,
            llm_client=llm_client,
            key_info_extractor=key_info_extractor,
            quality_validator=Mock(),
        )

        result = processor.process(
            text="",
            title="Empty Recording",
            author="Author",
            description="",
            platform="test",
            media_id="m1",
        )

        assert result["calibrated_text"] == ""
        stats = result["stats"]
        assert stats["original_length"] == 0
        assert stats["calibrated_length"] == 0
        assert stats["segment_count"] == 0
        assert stats["total_segments"] == 0
        assert stats["calibrated_segments"] == 0
        assert stats["fallback_segments"] == 0
        assert stats["calibration_status"] == CalibrationStatus.NONE
        llm_client.call.assert_not_called()

    def test_speaker_aware_calibrate_chunks_empty_input(self, mock_config):
        llm_client = Mock()
        processor = SpeakerAwareProcessor(
            config=mock_config,
            llm_client=llm_client,
            key_info_extractor=Mock(),
            speaker_inferencer=Mock(),
            quality_validator=Mock(),
        )

        chunks, stats = processor._calibrate_chunks(
            chunks=[],
            original_chunks=[],
            key_info=Mock(spec=KeyInfo),
            speaker_mapping={},
            title="Empty Test",
            description="",
            selected_models=None,
        )

        assert chunks == []
        assert stats["total_chunks"] == 0
        assert stats["success_count"] == 0
        assert stats["failed_count"] == 0
        assert stats["calibration_status"] == CalibrationStatus.NONE
        llm_client.call.assert_not_called()

    def test_speaker_aware_process_empty_dialogs(self, mock_config):
        llm_client = Mock()
        key_info_mock = Mock(spec=KeyInfo)
        key_info_mock.to_dict.return_value = {}
        key_info_mock.format_for_prompt.return_value = ""
        key_info_extractor = Mock()
        key_info_extractor.extract.return_value = key_info_mock

        processor = SpeakerAwareProcessor(
            config=mock_config,
            llm_client=llm_client,
            key_info_extractor=key_info_extractor,
            speaker_inferencer=Mock(),
            quality_validator=Mock(),
        )

        result = processor.process(
            dialogs=[],
            title="12s Silent Recording",
            author="Author",
            description="",
            platform="test",
            media_id="m2",
        )

        assert result["calibrated_text"] == ""
        assert result["structured_data"]["dialogs"] == []
        stats = result["stats"]
        assert stats["original_length"] == 0
        assert stats["calibrated_length"] == 0
        assert stats["dialog_count"] == 0
        assert stats["calibration_stats"]["total_chunks"] == 0
        assert stats["calibration_stats"]["calibration_status"] == CalibrationStatus.NONE
        llm_client.call.assert_not_called()

    def test_notes_processor_empty_slices_returns_failed_result(self, mock_config):
        llm_client = Mock()
        processor = NotesProcessor(llm_client, mock_config)

        result = processor.process(
            chapters={"chapters": [], "source": {"fingerprint": "abc"}},
            source_segments=[{"text": "some text", "start_time": 0.0, "end_time": 1.0}],
            selected_models=None,
        )

        assert result.status == NotesStatus.FAILED
        assert result.text is None
        assert result.chapter_count == 0
        llm_client.call.assert_not_called()

    def test_coordinator_handles_empty_string_and_empty_list(self, tmp_path):
        from video_transcript_api.llm.coordinator import LLMCoordinator

        cfg = {
            "llm": {
                "api_key": "test-key",
                "base_url": "http://localhost",
                "calibrate_model": "mock-model",
                "summary_model": "mock-model",
            }
        }
        coord = LLMCoordinator(cfg, cache_dir=str(tmp_path))

        res_str = coord.process(
            content="",
            title="Empty String",
            author="Author",
            description="",
            skip_summary=True,
            skip_chapters=True,
        )
        assert res_str["calibrated_text"] == ""
        assert res_str["stats"]["calibration_status"] == CalibrationStatus.NONE

        res_list = coord.process(
            content=[],
            title="Empty List",
            author="Author",
            description="",
            skip_summary=True,
            skip_chapters=True,
        )
        assert res_list["calibrated_text"] == ""
        assert res_list["stats"]["calibration_status"] == CalibrationStatus.NONE
