"""
Unit tests for YoutubeDownloader._parse_youtube_subtitle_xml() with timestamps.

This is the second of the three YouTube subtitle parsing paths described in
the task (TikHub API XML subtitle fallback). It used to parse start/dur
attributes, sort by start time, merge into plain text, then discard the
timing information entirely. These tests lock down:

- The merged text output stays byte-identical to the historical algorithm
  (including sorting by start time).
- The new return type is a SubtitleResult carrying segments with
  start_time / end_time (seconds) / text.
- Timestamp parsing failures (malformed "start"/"dur" attributes) never
  break text extraction, and never drop that entry from segments either
  ("text is never lost" invariant): only the offending entry's
  start_time/end_time is nulled out; the rest of the batch is untouched.
- Empty subtitle XML (no <text> elements): text == "" and segments is None.
- Fully invalid XML still returns None (unchanged from before).

All console output must be in English only (no emoji, no Chinese).
"""

from video_transcript_api.downloaders.subtitle_types import SubtitleResult
from video_transcript_api.downloaders.youtube import YoutubeDownloader


def _make_downloader() -> YoutubeDownloader:
    return YoutubeDownloader()


XML_NORMAL_UNSORTED = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    "<transcript>"
    '<text start="5.0" dur="3.0">World second</text>'
    '<text start="0.0" dur="2.5">Hello first</text>'
    "</transcript>"
)

XML_MISSING_DUR = (
    '<transcript><text start="1.0">No duration</text></transcript>'
)

XML_BAD_START_ATTR = (
    '<transcript><text start="not-a-number" dur="2.0">Bad start segment</text></transcript>'
)

XML_MIXED_VALID_AND_BROKEN = (
    '<transcript>'
    '<text start="0.0" dur="2.5">Hello first</text>'
    '<text start="not-a-number" dur="2.0">Bad start segment</text>'
    '<text start="5.0" dur="3.0">World second</text>'
    '</transcript>'
)

XML_EMPTY = "<transcript></transcript>"

XML_INVALID = "<transcript><text start=not-closed"


def test_parses_and_sorts_by_start_time_text_unchanged():
    """Text output stays byte-identical to the legacy algorithm (sorted by start)."""
    downloader = _make_downloader()

    result = downloader._parse_youtube_subtitle_xml(XML_NORMAL_UNSORTED)

    assert isinstance(result, SubtitleResult)
    assert result.text == "Hello first World second"
    assert result.segments == [
        {"start_time": 0.0, "end_time": 2.5, "text": "Hello first"},
        {"start_time": 5.0, "end_time": 8.0, "text": "World second"},
    ]


def test_missing_dur_attribute_defaults_to_zero_duration():
    downloader = _make_downloader()

    result = downloader._parse_youtube_subtitle_xml(XML_MISSING_DUR)

    assert result.text == "No duration"
    assert result.segments == [
        {"start_time": 1.0, "end_time": 1.0, "text": "No duration"},
    ]


def test_malformed_start_attribute_keeps_entry_with_none_time():
    """A malformed 'start' attribute must not break text extraction, and the
    entry must still land in segments (text is never lost) with both time
    fields nulled out (end_time depends on a valid start)."""
    downloader = _make_downloader()

    result = downloader._parse_youtube_subtitle_xml(XML_BAD_START_ATTR)

    assert result.text == "Bad start segment"
    assert result.segments == [
        {"start_time": None, "end_time": None, "text": "Bad start segment"},
    ]


def test_mixed_valid_and_broken_start_attribute_keeps_all_text_in_segments():
    """Mixed batch: two valid entries and one with a malformed 'start'. All
    three must appear in segments -- the broken one with both time fields
    None -- and text stays byte-identical to the legacy sorted-merge join."""
    downloader = _make_downloader()

    result = downloader._parse_youtube_subtitle_xml(XML_MIXED_VALID_AND_BROKEN)

    assert result.text == "Hello first Bad start segment World second"
    assert result.segments == [
        {"start_time": 0.0, "end_time": 2.5, "text": "Hello first"},
        {"start_time": None, "end_time": None, "text": "Bad start segment"},
        {"start_time": 5.0, "end_time": 8.0, "text": "World second"},
    ]


def test_empty_subtitle_xml():
    downloader = _make_downloader()

    result = downloader._parse_youtube_subtitle_xml(XML_EMPTY)

    assert result.text == ""
    assert result.segments is None


def test_invalid_xml_returns_none_unchanged():
    """Fully unparseable XML keeps returning None (unchanged from before)."""
    downloader = _make_downloader()

    result = downloader._parse_youtube_subtitle_xml(XML_INVALID)

    assert result is None
