"""Integration test for plain text formatting when calibration fails"""

import pytest
from unittest.mock import Mock, MagicMock
from src.video_transcript_api.llm.processors.plain_text_processor import PlainTextProcessor
from src.video_transcript_api.llm.core.config import LLMConfig
from src.video_transcript_api.llm.core.key_info_extractor import KeyInfo


class TestPlainTextFallbackFormatting:
    """Test formatting when calibration falls back to original text"""

    @pytest.fixture
    def mock_config(self):
        """Create mock LLM config"""
        config = Mock(spec=LLMConfig)
        config.enable_threshold = 5000  # High threshold, no segmentation for small text
        config.min_calibrate_ratio = 0.8  # Require 80% of original length
        config.concurrent_workers = 10
        config.segment_size = 2000
        config.max_segment_size = 3000
        config.calibrate_model = "mock-model"
        config.calibrate_reasoning_effort = "medium"
        return config

    @pytest.fixture
    def processor(self, mock_config):
        """Create PlainTextProcessor with mocked dependencies"""
        mock_llm_client = Mock()
        mock_key_info_extractor = Mock()
        mock_quality_validator = Mock()

        return PlainTextProcessor(
            config=mock_config,
            llm_client=mock_llm_client,
            key_info_extractor=mock_key_info_extractor,
            quality_validator=mock_quality_validator,
        )

    def test_calibration_failure_formats_original_text(self, processor, mock_config):
        """Test that when calibration fails, original text is formatted"""
        # Setup: Create long text without line breaks
        original_text = (
            "这是第一句话，内容很长，足够满足长度要求。"
            "这是第二句话，也很长，包含很多信息。"
            "这是第三句话！内容丰富，详细描述了很多内容。"
            "这是第四句话？问题很多，需要详细解答。"
        )

        # Mock key info extractor to return empty key info
        mock_key_info = Mock(spec=KeyInfo)
        mock_key_info.to_dict.return_value = {}
        mock_key_info.format_for_prompt.return_value = ""
        processor.key_info_extractor.extract.return_value = mock_key_info

        # Mock LLM client to return text that's too short (< 80% of original)
        # This simulates calibration failure
        mock_response = Mock()
        mock_response.text = "短文本"  # Much shorter than required
        processor.llm_client.call.return_value = mock_response

        # Execute: Process the text
        result = processor.process(
            text=original_text,
            title="Test Video",
            author="Test Author",
            description="Test Description",
            platform="test",
            media_id="test123",
        )

        # Verify: The result should be formatted with line breaks
        calibrated = result["calibrated_text"]

        # Should have line breaks after each sentence
        assert '\n' in calibrated
        lines = calibrated.split('\n')

        # Should have at least 4 lines (one per sentence)
        assert len(lines) >= 4

        # Each line should end with punctuation
        for line in lines:
            if line.strip():  # Skip empty lines
                assert line.strip()[-1] in '。！？；.!?;'

    def test_exception_during_calibration_formats_text(self, processor, mock_config):
        """Test that when exception occurs, text is formatted before fallback"""
        # Original text without line breaks
        original_text = "第一句。第二句！第三句？第四句；第五句。"

        # Mock key info extractor
        mock_key_info = Mock(spec=KeyInfo)
        mock_key_info.to_dict.return_value = {}
        mock_key_info.format_for_prompt.return_value = ""
        processor.key_info_extractor.extract.return_value = mock_key_info

        # Mock LLM client to raise exception
        processor.llm_client.call.side_effect = Exception("API Error")

        # Execute: Process the text (should handle exception gracefully)
        result = processor.process(
            text=original_text,
            title="Test Video",
            author="Test Author",
        )

        # Verify: Text should still be formatted despite exception
        calibrated = result["calibrated_text"]
        lines = calibrated.split('\n')

        # Should have 5 lines (one per sentence)
        assert len(lines) == 5
        assert lines[0] == "第一句。"
        assert lines[1] == "第二句！"
        assert lines[2] == "第三句？"

    def test_retry_failure_formats_text(self, processor, mock_config):
        """Test that after retry failure, text is properly formatted"""
        # Long text requiring formatting
        original_text = (
            "这是一个很长的句子，包含了很多信息和内容。"
            "这是另一个很长的句子，描述了更多细节。"
            "第三句话也很长，提供了额外的上下文信息。"
        )

        # Mock key info
        mock_key_info = Mock(spec=KeyInfo)
        mock_key_info.to_dict.return_value = {}
        mock_key_info.format_for_prompt.return_value = ""
        processor.key_info_extractor.extract.return_value = mock_key_info

        # Mock LLM to return short text both times (initial + retry)
        mock_response = Mock()
        mock_response.text = "太短"  # Way too short
        processor.llm_client.call.return_value = mock_response

        # Execute
        result = processor.process(
            text=original_text,
            title="Test Video",
        )

        # Verify: Should be formatted
        calibrated = result["calibrated_text"]
        lines = calibrated.split('\n')

        # Should have at least 3 lines
        assert len(lines) >= 3

        # Each non-empty line should end with punctuation
        for line in lines:
            if line.strip():
                assert line.strip()[-1] in '。！？；.!?;'

    def test_long_unformatted_text_gets_formatted(self, processor, mock_config):
        """Test formatting of very long text without any line breaks"""
        # Create a long text (500+ chars) without line breaks
        sentences = [
            "这是关于人工智能的详细讨论。",
            "机器学习是人工智能的一个重要分支！",
            "深度学习在近年来取得了巨大进展？",
            "自然语言处理也是研究热点之一；",
            "计算机视觉应用广泛。",
        ]
        # Repeat to make it long enough
        original_text = "".join(sentences * 5)  # ~250 chars * 5 = ~1250 chars

        # Mock components
        mock_key_info = Mock(spec=KeyInfo)
        mock_key_info.to_dict.return_value = {}
        mock_key_info.format_for_prompt.return_value = ""
        processor.key_info_extractor.extract.return_value = mock_key_info

        # Mock calibration to fail
        mock_response = Mock()
        mock_response.text = "失败"
        processor.llm_client.call.return_value = mock_response

        # Execute
        result = processor.process(
            text=original_text,
            title="AI Discussion",
        )

        # Verify formatting
        calibrated = result["calibrated_text"]

        # Should have many line breaks (5 sentences * 5 repetitions = 25 lines)
        lines = [l for l in calibrated.split('\n') if l.strip()]
        assert len(lines) == 25

        # Each line should end with a sentence-ending punctuation
        for line in lines:
            assert line.strip()[-1] in '。！？；'
