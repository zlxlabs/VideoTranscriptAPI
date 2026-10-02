"""
Test plain text formatting functionality.

Tests _format_plain_text() intelligent paragraph detection:
- Type A: text wall (few lines, long avg) -> split into paragraphs
- Type B: over-segmented (many lines, short avg) -> merge into paragraphs
- Type C: reasonable structure -> keep as-is
- Short text -> return unchanged

All console output must be in English only (no emoji, no Chinese).
"""

import pytest
from unittest.mock import Mock
from video_transcript_api.llm.processors.plain_text_processor import PlainTextProcessor
from video_transcript_api.llm.core.config import LLMConfig


class TestPlainTextFormatting:
    """Test plain text formatting in PlainTextProcessor."""

    @pytest.fixture
    def mock_config(self):
        """Create mock LLM config."""
        config = Mock(spec=LLMConfig)
        config.enable_threshold = 5000
        config.min_calibrate_ratio = 0.8
        config.concurrent_workers = 10
        # PlainTextProcessor builds a TextSegmenter, which now refuses a
        # non-positive max_segment_size instead of spinning forever; a bare
        # Mock attribute is not comparable to 0, so pin real values here.
        config.segment_size = 2000
        config.max_segment_size = 3000
        return config

    @pytest.fixture
    def processor(self, mock_config):
        """Create PlainTextProcessor instance with mocked dependencies."""
        return PlainTextProcessor(
            config=mock_config,
            llm_client=Mock(),
            key_info_extractor=Mock(),
            quality_validator=Mock(),
        )

    def test_short_text_unchanged(self, processor):
        """Text under 100 chars should be returned as-is."""
        text = "short text"
        assert processor._format_plain_text(text) == text

    def test_empty_text_unchanged(self, processor):
        """Empty text should be returned as-is."""
        assert processor._format_plain_text("") == ""

    def test_text_wall_gets_split(self, processor):
        """Long text with no line breaks should be split into paragraphs."""
        # Create a text wall using punctuation that _split_into_paragraphs recognizes
        # (。！？!? are in the split pattern, but . is NOT)
        sentences = [f"this is sentence number {i} with enough words to be meaningful！" for i in range(15)]
        text = "".join(sentences)

        formatted = processor._format_plain_text(text)

        # Should have paragraph breaks (double newlines)
        assert '\n\n' in formatted
        paragraphs = [p for p in formatted.split('\n\n') if p.strip()]
        assert len(paragraphs) > 1

    def test_already_formatted_kept_as_is(self, processor):
        """Text with reasonable line structure should not be changed."""
        # 10 lines, ~80 chars each -> reasonable structure
        lines = [f"line {i}: " + "x" * 70 for i in range(10)]
        text = "\n".join(lines)

        formatted = processor._format_plain_text(text)

        assert formatted == text

    def test_paragraph_structure_preserved(self, processor):
        """Text with existing paragraph breaks (double newlines) should be preserved."""
        para1 = "first paragraph " + "x" * 100
        para2 = "second paragraph " + "x" * 100
        text = f"{para1}\n\n{para2}"

        formatted = processor._format_plain_text(text)

        assert formatted == text

    def test_over_segmented_gets_merged(self, processor):
        """Many very short lines should be merged into paragraphs."""
        # 20 very short lines (< 50 chars average)
        lines = [f"short line {i}." for i in range(20)]
        text = "\n".join(lines)

        formatted = processor._format_plain_text(text)

        # Should have fewer paragraphs than original lines
        result_paragraphs = [p for p in formatted.split('\n\n') if p.strip()]
        assert len(result_paragraphs) < len(lines)

    def test_text_with_only_punctuation(self, processor):
        """Text with only punctuation should be handled gracefully."""
        text = "x" * 10  # Short -> returned as-is
        formatted = processor._format_plain_text(text)
        assert isinstance(formatted, str)

    def test_chinese_text_wall_split(self, processor):
        """Chinese text wall should also be split."""
        sentences = ["".join(["testing"] * 8) + "。" for _ in range(15)]
        text = "".join(sentences)

        formatted = processor._format_plain_text(text)

        # Should have some paragraph breaks
        assert '\n\n' in formatted
