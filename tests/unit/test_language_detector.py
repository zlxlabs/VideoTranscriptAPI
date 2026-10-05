"""Unit tests for language_detector module."""

import pytest

from video_transcript_api.llm.utils.language_detector import detect_language


class TestDetectLanguage:
    """Tests for detect_language function."""

    def test_pure_chinese_text(self):
        """Pure Chinese text should be detected as 'zh'."""
        text = "今天天气很好，我们一起去公园散步吧。这是一段纯中文的测试文本，用于验证语言检测功能。"
        assert detect_language(text) == "zh"

    def test_pure_english_text(self):
        """Pure English text should be detected as 'en'."""
        text = (
            "The quick brown fox jumps over the lazy dog. "
            "This is a sample English text for testing the language detection feature."
        )
        assert detect_language(text) == "en"

    def test_mixed_chinese_dominant(self):
        """Mixed text with Chinese dominant should be detected as 'zh'."""
        text = "今天我学习了Python编程，感觉非常有趣。Machine Learning是一个很热门的领域。"
        assert detect_language(text) == "zh"

    def test_mixed_english_dominant(self):
        """Mixed text with English dominant should be detected as 'en'."""
        text = (
            "Today I learned about machine learning and deep learning frameworks. "
            "The instructor mentioned some Chinese concepts like 深度学习 briefly."
        )
        assert detect_language(text) == "en"

    def test_empty_string(self):
        """Empty string should default to 'zh'."""
        assert detect_language("") == "zh"

    def test_whitespace_only(self):
        """Whitespace-only string should default to 'zh'."""
        assert detect_language("   \n\t  ") == "zh"

    def test_numbers_and_symbols_only(self):
        """Text with only numbers/symbols should default to 'zh'."""
        assert detect_language("12345 !@#$% 67890") == "zh"

    def test_long_english_text(self):
        """Long English text should be detected as 'en'."""
        text = (
            "In this video, we will discuss the fundamentals of artificial intelligence "
            "and how it is transforming the modern world. Machine learning algorithms "
            "have become increasingly sophisticated, enabling computers to perform tasks "
            "that were once thought to be exclusively human. From natural language processing "
            "to computer vision, the applications are vast and continue to grow every day."
        )
        assert detect_language(text) == "en"

    def test_long_chinese_text(self):
        """Long Chinese text should be detected as 'zh'."""
        text = (
            "在这个视频中，我们将讨论人工智能的基础知识以及它如何改变现代世界。"
            "机器学习算法变得越来越复杂，使计算机能够执行曾经被认为只有人类才能完成的任务。"
            "从自然语言处理到计算机视觉，应用范围广泛，并且每天都在不断增长。"
        )
        assert detect_language(text) == "zh"

    def test_english_with_technical_terms(self):
        """English text with technical terms should be detected as 'en'."""
        text = (
            "The API endpoint returns a JSON response with the following fields: "
            "status, message, and data. You can configure the timeout parameter "
            "to control how long the request waits before timing out."
        )
        assert detect_language(text) == "en"

    def test_sampling_limit(self):
        """Text longer than sample size should still work correctly."""
        # Create a text longer than 2000 chars, English at start, Chinese at end
        english_part = "Hello world. " * 200  # ~2600 chars
        chinese_part = "你好世界。" * 200
        text = english_part + chinese_part
        # Only first 2000 chars are sampled, which is English
        assert detect_language(text) == "en"
