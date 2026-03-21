"""Integration test for plain text fallback formatting.

Tests PlainTextProcessor fallback behavior under different failure modes:
1. best_quality strategy: picks longest LLM attempt even if short
2. formatted_original strategy: formats original text into paragraphs
3. Exception path: formats original text via _format_plain_text
4. _format_plain_text logic: splits text walls into 2-3 sentence paragraphs
"""

import pytest
from unittest.mock import Mock
from src.video_transcript_api.llm.processors.plain_text_processor import PlainTextProcessor
from src.video_transcript_api.llm.core.config import LLMConfig
from src.video_transcript_api.llm.core.key_info_extractor import KeyInfo


class TestPlainTextFallbackFormatting:
    """Test formatting behavior under calibration failure scenarios"""

    @pytest.fixture
    def mock_config(self):
        """Create mock LLM config with all required attributes"""
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
    def processor(self, mock_config):
        """Create PlainTextProcessor with mocked dependencies"""
        return PlainTextProcessor(
            config=mock_config,
            llm_client=Mock(),
            key_info_extractor=Mock(),
            quality_validator=Mock(),
        )

    def _setup_mocks(self, processor, llm_text="short", llm_error=None):
        """Helper: configure key_info + llm_client mocks"""
        mock_key_info = Mock(spec=KeyInfo)
        mock_key_info.to_dict.return_value = {}
        mock_key_info.format_for_prompt.return_value = ""
        processor.key_info_extractor.extract.return_value = mock_key_info

        if llm_error:
            processor.llm_client.call.side_effect = llm_error
        else:
            mock_response = Mock()
            mock_response.text = llm_text
            processor.llm_client.call.return_value = mock_response

    def test_best_quality_picks_longest_attempt(self, processor, mock_config):
        """best_quality fallback picks the longest LLM candidate"""
        original_text = (
            "First sentence about technology."
            "Second sentence about science."
            "Third sentence about research!"
            "Fourth sentence about discovery?"
        )
        self._setup_mocks(processor, llm_text="short text")

        result = processor.process(
            text=original_text,
            title="Test",
            author="Author",
            platform="test",
            media_id="t1",
        )

        calibrated = result["calibrated_text"]
        # best_quality: returns max(candidates, key=len) = "short text" (10 chars)
        # Both attempts return same "short text", so result is "short text"
        assert calibrated == "short text"

    def test_formatted_original_strategy_formats_text(self, processor, mock_config):
        """formatted_original strategy calls _format_plain_text on original"""
        mock_config.segmentation_fallback_strategy = "formatted_original"
        original_text = (
            "First sentence about technology and its deep impact on society."
            "Second sentence about science and the way it shapes our world."
            "Third sentence about cutting-edge research breakthroughs!"
            "Fourth sentence about important scientific discovery?"
            "Fifth sentence with additional context and information."
            "Sixth sentence concluding the discussion."
        )
        self._setup_mocks(processor, llm_text="x")

        result = processor.process(
            text=original_text,
            title="Test",
            author="Author",
            platform="test",
            media_id="t2",
        )

        calibrated = result["calibrated_text"]
        # formatted_original -> _format_plain_text -> _split_into_paragraphs
        # Should produce paragraph structure with \n\n
        assert '\n\n' in calibrated
        paragraphs = [p for p in calibrated.split('\n\n') if p.strip()]
        assert len(paragraphs) >= 2

    def test_exception_falls_back_to_formatted_original(self, processor, mock_config):
        """When LLM raises exception, falls back to _format_plain_text(segment)"""
        original_text = (
            "First sentence about technology and innovation in modern world."
            "Second sentence about science and discovery across disciplines."
            "Third sentence about research and academic achievements!"
            "Fourth sentence about engineering and system design?"
            "Fifth sentence with more context and background information."
        )
        self._setup_mocks(processor, llm_error=Exception("API Error"))

        result = processor.process(
            text=original_text,
            title="Test",
            author="Author",
        )

        calibrated = result["calibrated_text"]
        # Exception path line 325: formatted_segment = self._format_plain_text(segment)
        assert calibrated is not None
        assert len(calibrated) > 0
        # For text wall (1 line, long), should be split into paragraphs
        assert '\n\n' in calibrated

    def test_format_plain_text_splits_text_wall(self, processor):
        """_format_plain_text splits text wall into 2-3 sentence paragraphs"""
        sentences = [
            "Artificial intelligence is transforming every industry.",
            "Machine learning enables automated decision making!",
            "Deep learning has achieved remarkable breakthroughs?",
            "NLP bridges human language and computer understanding.",
            "Computer vision has widespread applications.",
        ]
        # 5 repetitions = 25 sentences, single wall of text
        text_wall = "".join(sentences * 5)

        result = processor._format_plain_text(text_wall)

        # Should be split with \n\n
        assert '\n\n' in result
        paragraphs = [p for p in result.split('\n\n') if p.strip()]
        # 25 sentences / 2-3 per paragraph = ~8-12 paragraphs
        assert 5 <= len(paragraphs) <= 15
