"""
Unit tests for SubtitleResult data structure.

Covers:
- Basic construction with text + segments
- segments defaults to None when omitted
- segments item shape convention: start_time/end_time/text (no speaker field,
  since subtitles have no speaker information)

All console output must be in English only (no emoji, no Chinese).
"""

from video_transcript_api.downloaders.subtitle_types import SubtitleResult


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
