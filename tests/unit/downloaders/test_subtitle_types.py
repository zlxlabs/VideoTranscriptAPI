"""
Unit tests for SubtitleResult data structure and the shared sanitize_time_pair
helper used by all three YouTube subtitle timestamp-extraction paths.

Covers:
- Basic construction with text + segments
- segments defaults to None when omitted
- segments item shape convention: start_time/end_time/text (no speaker field,
  since subtitles have no speaker information)
- sanitize_time_pair(): negative start -> None, negative end -> None,
  end < start (reversed interval) -> end None, all other pairs untouched.

All console output must be in English only (no emoji, no Chinese).
"""

from video_transcript_api.downloaders.subtitle_types import (
    SubtitleResult,
    sanitize_time_pair,
)


def test_subtitle_result_requires_text_only():
    """segments should default to None when not provided."""
    result = SubtitleResult(text="hello world")

    assert result.text == "hello world"
    assert result.segments is None


def test_subtitle_result_holds_segments_with_expected_shape():
    """segments is a list of dicts with start_time/end_time/text (seconds, no speaker)."""
    segments = [
        {"start_time": 0.0, "end_time": 1.5, "text": "hello"},
        {"start_time": 1.5, "end_time": 3.0, "text": "world"},
    ]
    result = SubtitleResult(text="hello world", segments=segments)

    assert result.text == "hello world"
    assert result.segments == segments
    for segment in result.segments:
        assert set(segment.keys()) == {"start_time", "end_time", "text"}
        assert isinstance(segment["start_time"], float)
        assert isinstance(segment["end_time"], float)


# ---------------------------------------------------------------------------
# sanitize_time_pair()
# ---------------------------------------------------------------------------

def test_sanitize_time_pair_negative_start_becomes_none():
    """A negative start has no physical meaning; start is nulled, end (still
    valid on its own) is left untouched since it no longer has a start to be
    compared against."""
    start, end = sanitize_time_pair(-5.0, -5.0 + 2.0)
    assert start is None
    # end (-3.0) is itself negative -> also nulled by the negative-end rule.
    assert end is None


def test_sanitize_time_pair_negative_end_becomes_none():
    """A negative end (e.g. produced by start=0 + a large negative duration)
    is nulled even though start itself is valid."""
    start, end = sanitize_time_pair(0.0, -1.0)
    assert start == 0.0
    assert end is None


def test_sanitize_time_pair_reversed_interval_nulls_end_only():
    """end < start (interval reversed, e.g. negative duration whose sum is
    still non-negative, or a reversed SRT timeline) nulls only end_time;
    start_time -- which is valid on its own -- is preserved."""
    start, end = sanitize_time_pair(5.0, 3.0)
    assert start == 5.0
    assert end is None


def test_sanitize_time_pair_valid_pair_untouched():
    start, end = sanitize_time_pair(1.0, 4.0)
    assert start == 1.0
    assert end == 4.0


def test_sanitize_time_pair_none_values_untouched():
    assert sanitize_time_pair(None, None) == (None, None)
    assert sanitize_time_pair(1.0, None) == (1.0, None)
    assert sanitize_time_pair(None, 4.0) == (None, 4.0)


def test_sanitize_time_pair_zero_values_are_not_negative():
    """0.0 is a legitimate, non-negative timestamp -- must not be nulled."""
    start, end = sanitize_time_pair(0.0, 0.0)
    assert start == 0.0
    assert end == 0.0
