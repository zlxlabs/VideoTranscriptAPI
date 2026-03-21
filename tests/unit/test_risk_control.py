"""
Unit tests for risk_control module.

Covers SensitiveWordsManager (word loading, parsing, caching)
and TextSanitizer (detection, sanitization by text_type, URL protection).
"""

import os
import pytest
from unittest.mock import patch, MagicMock, mock_open

from src.video_transcript_api.risk_control.sensitive_words_manager import SensitiveWordsManager
from src.video_transcript_api.risk_control.text_sanitizer import TextSanitizer, RISK_WARNING


# ---------------------------------------------------------------------------
# SensitiveWordsManager tests
# ---------------------------------------------------------------------------

class TestSensitiveWordsManagerInit:
    """Tests for SensitiveWordsManager.__init__."""

    @patch("os.makedirs")
    def test_init_creates_cache_dir(self, mock_makedirs):
        """__init__ should create the cache directory if it does not exist."""
        config = {
            "sensitive_word_urls": ["https://example.com/words.txt"],
            "cache_file": "/tmp/test_risk/sensitive_words.txt",
        }
        manager = SensitiveWordsManager(config)

        mock_makedirs.assert_called_once_with("/tmp/test_risk", exist_ok=True)
        assert manager.urls == ["https://example.com/words.txt"]
        assert manager.cache_file == "/tmp/test_risk/sensitive_words.txt"
        assert manager.sensitive_words == set()

    @patch("os.makedirs")
    def test_init_uses_default_cache_file(self, mock_makedirs):
        """When cache_file is not provided, the default path is used."""
        manager = SensitiveWordsManager({})

        assert manager.cache_file == "./data/risk_control/sensitive_words.txt"
        assert manager.urls == []


class TestSensitiveWordsManagerParseWords:
    """Tests for SensitiveWordsManager._parse_words."""

    @patch("os.makedirs")
    def setup_method(self, method, mock_makedirs=None):
        self.manager = SensitiveWordsManager({})

    def test_parse_normal_words(self):
        """Normal words are lowercased and collected."""
        content = "BadWord\nAnother\nThird"
        result = self.manager._parse_words(content)
        assert result == {"badword", "another", "third"}

    def test_parse_skips_comments(self):
        """Lines starting with # are treated as comments and skipped."""
        content = "# this is a comment\nactualword\n# another comment"
        result = self.manager._parse_words(content)
        assert result == {"actualword"}

    def test_parse_skips_empty_lines(self):
        """Empty and whitespace-only lines are skipped."""
        content = "word1\n\n   \nword2\n\n"
        result = self.manager._parse_words(content)
        assert result == {"word1", "word2"}

    def test_parse_lowercases_words(self):
        """Words are converted to lowercase for case-insensitive matching."""
        content = "UPPER\nMiXeD\nlower"
        result = self.manager._parse_words(content)
        assert result == {"upper", "mixed", "lower"}

    def test_parse_strips_whitespace(self):
        """Leading/trailing whitespace is stripped from each line."""
        content = "  padded  \n\tword\t"
        result = self.manager._parse_words(content)
        assert result == {"padded", "word"}

    def test_parse_empty_content(self):
        """Empty content returns an empty set."""
        assert self.manager._parse_words("") == set()


class TestSensitiveWordsManagerLoadWords:
    """Tests for SensitiveWordsManager.load_words (download + cache fallback)."""

    @patch("os.makedirs")
    def _make_manager(self, urls, cache_file="/tmp/test/cache.txt", mock_makedirs=None):
        config = {"sensitive_word_urls": urls, "cache_file": cache_file}
        return SensitiveWordsManager(config)

    @patch("requests.get")
    def test_load_words_from_url_success(self, mock_get):
        """Successful URL download populates sensitive_words and saves cache."""
        mock_response = MagicMock()
        mock_response.text = "word1\nword2\n# comment\n"
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        manager = self._make_manager(["https://example.com/words.txt"])

        with patch.object(manager, "_save_to_cache") as mock_save:
            result = manager.load_words()

        assert result == {"word1", "word2"}
        assert manager.sensitive_words == {"word1", "word2"}
        mock_save.assert_called_once_with({"word1", "word2"})

    @patch("requests.get")
    def test_load_words_fallback_to_cache(self, mock_get):
        """When URL download fails, words are loaded from cache."""
        mock_get.side_effect = Exception("network error")

        manager = self._make_manager(["https://example.com/words.txt"])

        with patch.object(manager, "_load_from_cache", return_value={"cached1", "cached2"}):
            result = manager.load_words()

        assert result == {"cached1", "cached2"}
        assert manager.sensitive_words == {"cached1", "cached2"}

    @patch("requests.get")
    def test_load_words_empty_cache_returns_empty_set(self, mock_get):
        """When URL fails and cache is empty, returns empty set."""
        mock_get.side_effect = Exception("network error")

        manager = self._make_manager(["https://example.com/words.txt"])

        with patch.object(manager, "_load_from_cache", return_value=set()):
            result = manager.load_words()

        assert result == set()
        assert manager.sensitive_words == set()

    @patch("requests.get")
    def test_load_words_multiple_urls(self, mock_get):
        """Words from multiple URLs are merged and deduplicated."""
        resp1 = MagicMock()
        resp1.text = "word1\nshared"
        resp1.raise_for_status = MagicMock()

        resp2 = MagicMock()
        resp2.text = "word2\nshared"
        resp2.raise_for_status = MagicMock()

        mock_get.side_effect = [resp1, resp2]

        manager = self._make_manager([
            "https://example.com/1.txt",
            "https://example.com/2.txt",
        ])

        with patch.object(manager, "_save_to_cache"):
            result = manager.load_words()

        assert result == {"word1", "word2", "shared"}

    @patch("requests.get")
    def test_load_words_partial_url_failure(self, mock_get):
        """If one URL fails but another succeeds, words from the successful one are used."""
        resp_ok = MagicMock()
        resp_ok.text = "goodword"
        resp_ok.raise_for_status = MagicMock()

        mock_get.side_effect = [Exception("fail"), resp_ok]

        manager = self._make_manager([
            "https://example.com/bad.txt",
            "https://example.com/good.txt",
        ])

        with patch.object(manager, "_save_to_cache"):
            result = manager.load_words()

        assert result == {"goodword"}


# ---------------------------------------------------------------------------
# TextSanitizer tests
# ---------------------------------------------------------------------------

class TestTextSanitizerDetection:
    """Tests for TextSanitizer.sanitize detection logic."""

    def test_detects_sensitive_word_case_insensitive(self):
        """Sensitive words are matched case-insensitively."""
        sanitizer = TextSanitizer({"badword"})
        result = sanitizer.sanitize("This contains BADWORD here")

        assert result["has_sensitive"] is True
        assert len(result["sensitive_words"]) == 1
        # Original casing is preserved in the detection list
        assert result["sensitive_words"][0] == "BADWORD"

    def test_no_detection_for_clean_text(self):
        """Text without sensitive words returns has_sensitive=False."""
        sanitizer = TextSanitizer({"forbidden"})
        result = sanitizer.sanitize("This is perfectly clean text")

        assert result["has_sensitive"] is False
        assert result["sensitive_words"] == []
        assert result["sanitized_text"] == "This is perfectly clean text"

    def test_empty_text_returns_no_detections(self):
        """Empty string input returns has_sensitive=False immediately."""
        sanitizer = TextSanitizer({"badword"})
        result = sanitizer.sanitize("")

        assert result["has_sensitive"] is False
        assert result["sensitive_words"] == []
        assert result["sanitized_text"] == ""

    def test_none_text_returns_no_detections(self):
        """None input returns has_sensitive=False (falsy value)."""
        sanitizer = TextSanitizer({"badword"})
        result = sanitizer.sanitize(None)

        assert result["has_sensitive"] is False
        assert result["sanitized_text"] is None

    def test_multiple_sensitive_words_detected(self):
        """Multiple different sensitive words are all detected."""
        sanitizer = TextSanitizer({"bad", "evil"})
        result = sanitizer.sanitize("bad things and evil plans")

        assert result["has_sensitive"] is True
        assert set(result["sensitive_words"]) == {"bad", "evil"}


class TestTextSanitizerURLProtection:
    """Tests for URL protection - sensitive words inside URLs should NOT be flagged."""

    def test_sensitive_word_in_url_not_flagged(self):
        """A sensitive word appearing only inside a URL is not detected."""
        sanitizer = TextSanitizer({"badword"})
        text = "Check this link: https://example.com/badword/page"
        result = sanitizer.sanitize(text)

        assert result["has_sensitive"] is False
        assert result["sanitized_text"] == text

    def test_sensitive_word_outside_url_still_flagged(self):
        """A sensitive word outside a URL is detected even when URLs are present."""
        sanitizer = TextSanitizer({"badword"})
        text = "badword appears here and https://example.com/safe"
        result = sanitizer.sanitize(text)

        assert result["has_sensitive"] is True
        assert "badword" in result["sensitive_words"]

    def test_sensitive_word_both_in_and_outside_url(self):
        """Word in URL is ignored, but same word outside URL is detected."""
        sanitizer = TextSanitizer({"secret"})
        text = "this is secret info https://example.com/secret/path"
        result = sanitizer.sanitize(text)

        assert result["has_sensitive"] is True
        assert "secret" in result["sensitive_words"]


class TestTextSanitizerTextTypes:
    """Tests for sanitization behavior based on text_type."""

    def test_summary_replaces_with_risk_warning(self):
        """text_type='summary' replaces entire text with RISK_WARNING."""
        sanitizer = TextSanitizer({"forbidden"})
        result = sanitizer.sanitize("This forbidden content is long", text_type="summary")

        assert result["has_sensitive"] is True
        assert result["sanitized_text"] == RISK_WARNING

    def test_title_removes_word_and_truncates(self):
        """text_type='title' removes sensitive word then truncates to 6 chars."""
        sanitizer = TextSanitizer({"bad"})
        result = sanitizer.sanitize("bad hello world title", text_type="title")

        assert result["has_sensitive"] is True
        # After removing "bad": " hello world title" -> first 6 chars: " hello"
        assert len(result["sanitized_text"]) <= 6

    def test_title_short_result(self):
        """text_type='title' with short remaining text keeps all of it."""
        sanitizer = TextSanitizer({"longword"})
        result = sanitizer.sanitize("longword hi", text_type="title")

        assert result["has_sensitive"] is True
        # After removing "longword": " hi" -> 3 chars, under 6 limit
        assert result["sanitized_text"] == " hi"

    def test_general_removes_word_from_text(self):
        """text_type='general' removes the sensitive word from the text."""
        sanitizer = TextSanitizer({"evil"})
        result = sanitizer.sanitize("an evil plan unfolds", text_type="general")

        assert result["has_sensitive"] is True
        assert "evil" not in result["sanitized_text"].lower()
        assert "plan" in result["sanitized_text"]

    def test_general_is_default_text_type(self):
        """When text_type is not specified, general behavior is used."""
        sanitizer = TextSanitizer({"bad"})
        result = sanitizer.sanitize("bad stuff here")

        assert result["has_sensitive"] is True
        assert "bad" not in result["sanitized_text"].lower()
        assert "stuff" in result["sanitized_text"]

    def test_author_type_truncates_like_title(self):
        """text_type='author' behaves the same as 'title' (remove + truncate to 6)."""
        sanitizer = TextSanitizer({"bad"})
        result = sanitizer.sanitize("bad author name is long", text_type="author")

        assert result["has_sensitive"] is True
        assert len(result["sanitized_text"]) <= 6


class TestTextSanitizerExtractURLRanges:
    """Tests for TextSanitizer._extract_url_ranges."""

    def test_extracts_single_url(self):
        """A single URL is correctly identified with start and end positions."""
        sanitizer = TextSanitizer(set())
        text = "visit https://example.com today"
        ranges = sanitizer._extract_url_ranges(text)

        assert len(ranges) == 1
        start, end = ranges[0]
        assert text[start:end] == "https://example.com"

    def test_extracts_multiple_urls(self):
        """Multiple URLs are all identified."""
        sanitizer = TextSanitizer(set())
        text = "see https://a.com and http://b.com here"
        ranges = sanitizer._extract_url_ranges(text)

        assert len(ranges) == 2

    def test_no_urls_returns_empty(self):
        """Text without URLs returns an empty list."""
        sanitizer = TextSanitizer(set())
        ranges = sanitizer._extract_url_ranges("no urls in this text")

        assert ranges == []

    def test_url_with_path(self):
        """URL with path components is captured correctly."""
        sanitizer = TextSanitizer(set())
        text = "go to https://example.com/path/to/page end"
        ranges = sanitizer._extract_url_ranges(text)

        assert len(ranges) == 1
        start, end = ranges[0]
        extracted = text[start:end]
        assert extracted.startswith("https://example.com/path")


class TestTextSanitizerIsInURLRange:
    """Tests for TextSanitizer._is_in_url_range boundary checks."""

    def setup_method(self):
        self.sanitizer = TextSanitizer(set())

    def test_completely_inside_url(self):
        """Position fully within a URL range returns True."""
        url_ranges = [(10, 30)]
        assert self.sanitizer._is_in_url_range(15, 20, url_ranges) is True

    def test_completely_outside_url(self):
        """Position fully outside any URL range returns False."""
        url_ranges = [(10, 30)]
        assert self.sanitizer._is_in_url_range(0, 5, url_ranges) is False

    def test_overlapping_start_boundary(self):
        """Position overlapping the start of a URL range returns True."""
        url_ranges = [(10, 30)]
        assert self.sanitizer._is_in_url_range(5, 15, url_ranges) is True

    def test_overlapping_end_boundary(self):
        """Position overlapping the end of a URL range returns True."""
        url_ranges = [(10, 30)]
        assert self.sanitizer._is_in_url_range(25, 35, url_ranges) is True

    def test_adjacent_before_url_not_in_range(self):
        """Position ending exactly at URL start is NOT in range (end <= url_start)."""
        url_ranges = [(10, 30)]
        assert self.sanitizer._is_in_url_range(5, 10, url_ranges) is False

    def test_adjacent_after_url_not_in_range(self):
        """Position starting exactly at URL end is NOT in range (start >= url_end)."""
        url_ranges = [(10, 30)]
        assert self.sanitizer._is_in_url_range(30, 35, url_ranges) is False

    def test_empty_url_ranges(self):
        """Empty URL ranges always returns False."""
        assert self.sanitizer._is_in_url_range(0, 10, []) is False

    def test_multiple_url_ranges(self):
        """Check against multiple URL ranges - match in second range."""
        url_ranges = [(5, 10), (20, 30)]
        assert self.sanitizer._is_in_url_range(22, 25, url_ranges) is True
        assert self.sanitizer._is_in_url_range(12, 18, url_ranges) is False
