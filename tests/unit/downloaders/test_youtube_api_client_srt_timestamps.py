"""
Unit tests for YouTubeApiClient SRT subtitle parsing with timestamps.

Covers the third of the three YouTube subtitle parsing paths described in
the task: YouTubeApiClient.parse_srt_to_text() used to discard the SRT
timestamp lines entirely. These tests lock down:

- parse_srt_to_text() keeps returning byte-identical plain text (backward
  compatible public contract, still used by youtube.py and
  YouTubeApiClient.fetch_transcript()).
- parse_srt_to_subtitle_result() (new) returns a SubtitleResult whose text
  matches parse_srt_to_text() exactly, plus segments with start_time /
  end_time (seconds) / text extracted from the SRT timestamp lines.
- Malformed/missing timestamps never break text extraction: segments falls
  back to None while text stays normal (fault tolerance rule).
- Empty subtitle content: text == "" and segments is None.

All console output must be in English only (no emoji, no Chinese).
"""

from video_transcript_api.downloaders.subtitle_types import SubtitleResult
from video_transcript_api.downloaders.youtube_api_client import YouTubeApiClient


SRT_NORMAL = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "This is a test\n"
)

SRT_MULTILINE_WITH_HTML_TAGS = (
    "1\n"
    "00:00:10,500 --> 00:00:12,750\n"
    "<i>Hello</i>\n"
    "world\n"
)

SRT_MISSING_TIMESTAMP = "1\nHello world without any timestamp\n"

SRT_BAD_TIMESTAMP_FORMAT = (
    "1\n"
    "0:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
)

SRT_EMPTY = ""


def test_parse_srt_to_text_unchanged_for_normal_srt():
    """parse_srt_to_text keeps its historical plain-text output."""
    text = YouTubeApiClient.parse_srt_to_text(SRT_NORMAL)
    assert text == "Hello world This is a test"


def test_parse_srt_to_subtitle_result_normal_srt():
    """New entry returns text identical to parse_srt_to_text plus segments."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_NORMAL)

    assert isinstance(result, SubtitleResult)
    assert result.text == YouTubeApiClient.parse_srt_to_text(SRT_NORMAL)
    assert result.text == "Hello world This is a test"
    assert result.segments == [
        {"start_time": 1.0, "end_time": 4.0, "text": "Hello world"},
        {"start_time": 5.0, "end_time": 8.0, "text": "This is a test"},
    ]


def test_parse_srt_to_subtitle_result_multiline_block_strips_html_tags():
    """Multi-line subtitle blocks merge into a single segment; HTML tags stripped."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_MULTILINE_WITH_HTML_TAGS)

    assert result.text == "Hello world"
    assert result.segments == [
        {"start_time": 10.5, "end_time": 12.75, "text": "Hello world"},
    ]


def test_missing_timestamp_falls_back_to_none_segments_text_unaffected():
    """No timestamp line at all: segments is None, text extraction unaffected."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_MISSING_TIMESTAMP)

    assert result.text == "Hello world without any timestamp"
    assert result.segments is None
    # Backward-compatible entry must agree with the new one.
    assert YouTubeApiClient.parse_srt_to_text(SRT_MISSING_TIMESTAMP) == result.text


def test_malformed_timestamp_format_falls_back_to_none_segments_text_unaffected():
    """Malformed timestamp (single-digit hour) is not recognized: segments None,
    text extraction proceeds exactly like the legacy parser (the unrecognized
    timestamp line is treated as literal text, same as before this change)."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_BAD_TIMESTAMP_FORMAT)

    assert result.segments is None
    assert result.text == "0:00:01,000 --> 00:00:04,000 Hello world"
    assert YouTubeApiClient.parse_srt_to_text(SRT_BAD_TIMESTAMP_FORMAT) == result.text


def test_empty_srt_content():
    """Empty subtitle content: text is empty string, segments is None."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_EMPTY)

    assert result.text == ""
    assert result.segments is None
    assert YouTubeApiClient.parse_srt_to_text(SRT_EMPTY) == ""
