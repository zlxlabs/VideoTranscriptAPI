"""测试 LLM Coordinator 的内容路由功能"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from video_transcript_api.llm.coordinator import LLMCoordinator


@pytest.fixture
def mock_config_dict():
    """Mock configuration dictionary"""
    return {
        "llm": {
            "api_key": "test-key",
            "base_url": "http://test",
            "calibrate_model": "test-model",
            "summary_model": "test-model",
            "max_retries": 3,
            "retry_delay": 5,
            "min_calibrate_ratio": 0.8,
            "min_summary_threshold": 500,
            "quality_validation": {
                "score_weights": {
                    "accuracy": 0.4,
                    "completeness": 0.3,
                    "fluency": 0.2,
                    "format": 0.1,
                },
                "quality_threshold": {
                    "overall_score": 8.0,
                    "minimum_single_score": 7.0,
                },
            },
            "segmentation": {
                "enable_threshold": 5000,
                "segment_size": 2000,
                "max_segment_size": 3000,
                "concurrent_workers": 10,
            },
            "structured_calibration": {
                "min_chunk_length": 300,
                "max_chunk_length": 1500,
                "preferred_chunk_length": 800,
                "max_calibration_retries": 2,
                "calibration_concurrent_limit": 3,
                "quality_validation": {"enabled": True, "fallback_strategy": "best_quality"},
            },
        }
    }


@pytest.fixture
def coordinator(mock_config_dict, tmp_path):
    """Create a coordinator instance with mocked dependencies"""
    with patch(
        "video_transcript_api.llm.coordinator.PlainTextProcessor"
    ) as MockPlainProcessor, patch(
        "video_transcript_api.llm.coordinator.SpeakerAwareProcessor"
    ) as MockSpeakerProcessor, patch(
        "video_transcript_api.llm.coordinator.SummaryProcessor"
    ):
        coordinator = LLMCoordinator(
            config_dict=mock_config_dict, cache_dir=str(tmp_path)
        )

        # Mock processor results
        coordinator.plain_text_processor.process = Mock(
            return_value={
                "calibrated_text": "Calibrated plain text",
                "key_info": {},
                "stats": {"original_length": 100, "calibrated_length": 100},
            }
        )

        coordinator.speaker_aware_processor.process = Mock(
            return_value={
                "calibrated_text": "Calibrated dialog text",
                "key_info": {},
                "stats": {"original_length": 100, "calibrated_length": 100},
                "structured_data": {"speaker_mapping": {"Speaker1": "Alice"}},
            }
        )

        coordinator.summary_processor.process = Mock(return_value=None)

        yield coordinator


def test_route_plain_text_to_plain_processor(coordinator):
    """Test routing plain text (str) to PlainTextProcessor"""
    content = "This is a plain text transcript"

    coordinator.process(
        content=content,
        title="Test Video",
        author="Test Author",
        description="Test Description",
    )

    # Verify PlainTextProcessor was called
    coordinator.plain_text_processor.process.assert_called_once()
    coordinator.speaker_aware_processor.process.assert_not_called()


def test_route_dialog_list_to_speaker_processor(coordinator):
    """Test routing dialog list to SpeakerAwareProcessor"""
    content = [
        {"speaker": "Speaker1", "text": "Hello", "start_time": 0.0},
        {"speaker": "Speaker2", "text": "Hi there", "start_time": 1.5},
    ]

    coordinator.process(
        content=content,
        title="Test Video",
        author="Test Author",
        description="Test Description",
    )

    # Verify SpeakerAwareProcessor was called
    coordinator.speaker_aware_processor.process.assert_called_once()
    coordinator.plain_text_processor.process.assert_not_called()

    # Verify correct dialogs were passed
    call_args = coordinator.speaker_aware_processor.process.call_args
    assert call_args.kwargs["dialogs"] == content


def test_route_dict_with_segments_to_speaker_processor(coordinator):
    """Test routing dict with 'segments' key to SpeakerAwareProcessor"""
    segments = [
        {"speaker": "Speaker1", "text": "Hello", "start_time": 0.0},
        {"speaker": "Speaker2", "text": "Hi there", "start_time": 1.5},
    ]
    content = {"segments": segments, "speakers": ["Speaker1", "Speaker2"]}

    coordinator.process(
        content=content,
        title="Test Video",
        author="Test Author",
        description="Test Description",
    )

    # Verify SpeakerAwareProcessor was called
    coordinator.speaker_aware_processor.process.assert_called_once()

    # Verify segments were extracted correctly
    call_args = coordinator.speaker_aware_processor.process.call_args
    assert call_args.kwargs["dialogs"] == segments


def test_route_dict_without_segments_raises_error(coordinator):
    """Test that dict without 'segments' key raises ValueError"""
    content = {"invalid_key": "value"}

    with pytest.raises(ValueError, match="dict without 'segments' key"):
        coordinator.process(
            content=content,
            title="Test Video",
            author="Test Author",
            description="Test Description",
        )


def test_route_invalid_type_raises_error(coordinator):
    """Test that invalid content type raises ValueError"""
    content = 123  # Invalid type

    with pytest.raises(
        ValueError, match="Unsupported content type.*Expected str.*or list"
    ):
        coordinator.process(
            content=content,
            title="Test Video",
            author="Test Author",
            description="Test Description",
        )


def test_extract_speaker_count_from_plain_text(coordinator):
    """Test speaker count extraction from plain text"""
    content = "Plain text"
    calibration_result = {
        "calibrated_text": "Calibrated",
        "key_info": {},
        "stats": {},
    }

    speaker_count = coordinator._extract_speaker_count(content, calibration_result)

    assert speaker_count == 0


def test_extract_speaker_count_from_dialogs(coordinator):
    """Test speaker count extraction from dialog result"""
    content = [{"speaker": "Speaker1", "text": "Hello"}]
    calibration_result = {
        "calibrated_text": "Calibrated",
        "key_info": {},
        "stats": {},
        "structured_data": {
            "speaker_mapping": {"Speaker1": "Alice", "Speaker2": "Bob"}
        },
    }

    speaker_count = coordinator._extract_speaker_count(content, calibration_result)

    assert speaker_count == 2


class TestSpeakerCountHintOverridesAutoInference:
    """codex-review R5 #3: the layered-cache "summary-only backfill" path
    (transcription.py) forces content to plain text (transcription_data=None)
    to avoid re-running speaker-aware calibration -- but plain text always
    makes _extract_speaker_count() return 0 (single-speaker), even when the
    underlying media genuinely has multiple speakers. speaker_count_hint lets
    a caller that already knows the real count (read from the cached
    llm_processed.json) override that misjudgment without re-inferring
    speakers, so SummaryProcessor still gets a correct multi-speaker
    speaker_count and picks the right prompt / structured context.
    """

    def _make_long_enough_calibrated_text(self):
        # mock_config_dict's min_summary_threshold is 500 -- must clear it or
        # _generate_summary_if_needed short-circuits before ever calling
        # summary_processor.process().
        return "x" * 600

    def test_hint_is_passed_to_summary_processor(self, coordinator):
        long_text = self._make_long_enough_calibrated_text()
        coordinator.plain_text_processor.process = Mock(
            return_value={
                "calibrated_text": long_text,
                "key_info": {},
                "stats": {
                    "original_length": len(long_text),
                    "calibrated_length": len(long_text),
                    "calibration_status": "disabled",
                },
            }
        )
        summary_result = MagicMock()
        summary_result.text = "a" * 100
        summary_result.status = "generated"
        coordinator.summary_processor.process = Mock(return_value=summary_result)

        # content is plain text -- exactly what the forced plain-text
        # downgrade in transcription.py's summary-only backfill produces,
        # even though the media actually has 3 speakers.
        coordinator.process(
            content=long_text,
            title="Test Video",
            speaker_count_hint=3,
        )

        coordinator.summary_processor.process.assert_called_once()
        call_kwargs = coordinator.summary_processor.process.call_args.kwargs
        assert call_kwargs["speaker_count"] == 3

    def test_hint_skips_auto_inference_entirely(self, coordinator):
        """Not just overridden -- _extract_speaker_count must not even run,
        since re-deriving from plain-text content would always be wrong."""
        long_text = self._make_long_enough_calibrated_text()
        coordinator.plain_text_processor.process = Mock(
            return_value={
                "calibrated_text": long_text,
                "key_info": {},
                "stats": {
                    "original_length": len(long_text),
                    "calibrated_length": len(long_text),
                },
            }
        )
        summary_result = MagicMock()
        summary_result.text = "a" * 100
        summary_result.status = "generated"
        coordinator.summary_processor.process = Mock(return_value=summary_result)

        with patch.object(coordinator, "_extract_speaker_count") as mock_extract:
            coordinator.process(
                content=long_text,
                title="Test Video",
                speaker_count_hint=3,
            )
            mock_extract.assert_not_called()

    def test_no_hint_falls_back_to_auto_inference(self, coordinator):
        """Backward compatibility: omitting speaker_count_hint (every
        existing call site) must reproduce the pre-existing auto-inference
        behavior exactly -- plain text still yields speaker_count=0."""
        long_text = self._make_long_enough_calibrated_text()
        coordinator.plain_text_processor.process = Mock(
            return_value={
                "calibrated_text": long_text,
                "key_info": {},
                "stats": {
                    "original_length": len(long_text),
                    "calibrated_length": len(long_text),
                },
            }
        )
        summary_result = MagicMock()
        summary_result.text = "a" * 100
        summary_result.status = "generated"
        coordinator.summary_processor.process = Mock(return_value=summary_result)

        coordinator.process(content=long_text, title="Test Video")

        call_kwargs = coordinator.summary_processor.process.call_args.kwargs
        assert call_kwargs["speaker_count"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
