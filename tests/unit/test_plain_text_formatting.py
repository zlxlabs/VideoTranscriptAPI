"""Test plain text formatting functionality"""

import pytest
from unittest.mock import Mock, MagicMock
from src.video_transcript_api.llm.processors.plain_text_processor import PlainTextProcessor
from src.video_transcript_api.llm.core.config import LLMConfig


class TestPlainTextFormatting:
    """Test plain text formatting in PlainTextProcessor"""

    @pytest.fixture
    def mock_config(self):
        """Create mock LLM config"""
        config = Mock(spec=LLMConfig)
        config.enable_threshold = 5000
        config.min_calibrate_ratio = 0.8
        config.concurrent_workers = 10
        return config

    @pytest.fixture
    def processor(self, mock_config):
        """Create PlainTextProcessor instance with mocked dependencies"""
        mock_llm_client = Mock()
        mock_key_info_extractor = Mock()
        mock_quality_validator = Mock()

        return PlainTextProcessor(
            config=mock_config,
            llm_client=mock_llm_client,
            key_info_extractor=mock_key_info_extractor,
            quality_validator=mock_quality_validator,
        )

    def test_format_plain_text_with_no_line_breaks(self, processor):
        """Test formatting text without line breaks"""
        # Long text without line breaks
        text = "这是第一句话。这是第二句话！这是第三句话？这是第四句话；这是第五句话。"

        formatted = processor._format_plain_text(text)

        # Should have line breaks after punctuation
        assert '\n' in formatted
        lines = formatted.split('\n')
        assert len(lines) == 5
        assert lines[0] == "这是第一句话。"
        assert lines[1] == "这是第二句话！"
        assert lines[2] == "这是第三句话？"

    def test_format_plain_text_with_english_punctuation(self, processor):
        """Test formatting with English punctuation"""
        text = "This is sentence one.This is sentence two!This is sentence three?This is sentence four."

        formatted = processor._format_plain_text(text)

        lines = formatted.split('\n')
        assert len(lines) == 4
        assert lines[0] == "This is sentence one."
        assert lines[1] == "This is sentence two!"

    def test_format_plain_text_with_mixed_punctuation(self, processor):
        """Test formatting with mixed Chinese and English punctuation"""
        text = "中文句子。English sentence.另一个中文句子！Another English sentence!"

        formatted = processor._format_plain_text(text)

        lines = formatted.split('\n')
        assert len(lines) == 4

    def test_format_plain_text_already_formatted(self, processor):
        """Test that already formatted text is not changed"""
        # Text with sufficient line breaks (average < 200 chars per line)
        text = "第一行。\n第二行。\n第三行。\n第四行。\n第五行。"

        formatted = processor._format_plain_text(text)

        # Should return as-is since it already has enough line breaks
        assert formatted == text

    def test_format_plain_text_removes_excessive_line_breaks(self, processor):
        """Test that excessive line breaks are cleaned up"""
        text = "第一句。\n\n\n\n第二句。\n\n\n第三句。"

        formatted = processor._format_plain_text(text)

        # Should not have more than 2 consecutive newlines
        assert '\n\n\n' not in formatted

    def test_format_plain_text_with_multiple_punctuation(self, processor):
        """Test formatting with multiple consecutive punctuation marks"""
        text = "这是一句话...这是另一句话！！！这是第三句？？"

        formatted = processor._format_plain_text(text)

        lines = formatted.split('\n')
        assert len(lines) == 3
        assert lines[0] == "这是一句话..."
        assert lines[1] == "这是另一句话！！！"

    def test_format_plain_text_strips_whitespace(self, processor):
        """Test that leading and trailing whitespace is removed"""
        text = "   第一句。第二句。   "

        formatted = processor._format_plain_text(text)

        # Should not have leading or trailing whitespace
        assert not formatted.startswith(' ')
        assert not formatted.endswith(' ')

    def test_format_plain_text_with_semicolon(self, processor):
        """Test formatting with semicolon punctuation"""
        text = "这是主句；这是分句。另一个句子；第二分句。"

        formatted = processor._format_plain_text(text)

        lines = formatted.split('\n')
        assert len(lines) == 4

    def test_format_empty_text(self, processor):
        """Test formatting empty text"""
        text = ""

        formatted = processor._format_plain_text(text)

        assert formatted == ""

    def test_format_text_with_only_punctuation(self, processor):
        """Test formatting text with only punctuation"""
        text = "。！？；.!?;"

        formatted = processor._format_plain_text(text)

        # Should handle gracefully
        assert isinstance(formatted, str)
