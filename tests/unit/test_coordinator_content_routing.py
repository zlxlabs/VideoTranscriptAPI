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
                "enable_validation": True,
                "quality_threshold": {
                    "overall_score": 8.0,
                    "minimum_single_score": 7.0,
                },
            },
            "enable_risk_model_selection": False,
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
