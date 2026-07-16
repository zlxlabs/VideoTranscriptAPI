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
- Missing "start"/"dur" attributes must NOT be faked as 0 -- that would
  fabricate a bogus "starts at 0" or "zero duration" timestamp, violating
  the "bad/missing time -> None" invariant. A missing "start" nulls
  start_time (and therefore end_time, which depends on it); a missing
  "dur" (with a valid start) nulls only end_time. Text is unaffected either
  way, and the "start" sort key still treats a missing attribute as 0 for
  ordering purposes only (unchanged legacy behavior).
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

XML_MISSING_START = (
    '<transcript><text dur="3.0">No start</text></transcript>'
)

XML_MISSING_BOTH = (
    '<transcript><text>No timing at all</text></transcript>'
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


def test_missing_dur_attribute_yields_none_end_time():
    """A missing "dur" attribute must not be faked as a zero-length duration
    (end_time == start_time). start_time is still valid (parsed from
    "start"), but end_time depends on a real duration -- with none
    available, it must be None rather than a fabricated zero-length cue."""
    downloader = _make_downloader()

    result = downloader._parse_youtube_subtitle_xml(XML_MISSING_DUR)

    assert result.text == "No duration"
    assert result.segments == [
        {"start_time": 1.0, "end_time": None, "text": "No duration"},
    ]


def test_missing_start_attribute_yields_none_start_and_end_time():
    """A missing "start" attribute must not be faked as "starts at 0" --
    start_time must be None. end_time depends on start_time, so it must be
    None too even though "dur" is present and valid. Text is unaffected,
    and the item still sorts (via the 0-fallback sort key, unchanged legacy
    behavior for ordering purposes only) without raising."""
    downloader = _make_downloader()

    result = downloader._parse_youtube_subtitle_xml(XML_MISSING_START)

    assert result.text == "No start"
    assert result.segments == [
        {"start_time": None, "end_time": None, "text": "No start"},
    ]


def test_missing_both_start_and_dur_attributes_yields_none_times_text_kept():
    """Both attributes missing: both time fields are None, but the cue's
    text is still preserved in segments (text is never lost)."""
    downloader = _make_downloader()

    result = downloader._parse_youtube_subtitle_xml(XML_MISSING_BOTH)

    assert result.text == "No timing at all"
    assert result.segments == [
        {"start_time": None, "end_time": None, "text": "No timing at all"},
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


XML_START_PLUS_DUR_OVERFLOWS = (
    '<transcript>'
    '<text start="1e308" dur="1e308">Overflowing</text>'
    '<text start="1.5" dur="2.0">World</text>'
    '</transcript>'
)


def test_start_plus_dur_sum_overflow_yields_none_end_time():
    """"start" and "dur" are each individually finite (1e308 parses fine and
    passes math.isfinite() on its own), so neither raises ValueError/TypeError.
    But float addition does not raise OverflowError like int->float
    conversion does -- it silently saturates to inf (1e308 + 1e308 == inf).
    Before the fix, end = start + duration was never re-checked after the
    sum, producing an end_time of inf and violating the "time field is None
    or finite non-negative" invariant. After the fix, the sum itself is
    validated with math.isfinite(): start_time is kept (valid on its own),
    end_time is nulled out, text is preserved, and the rest of the batch
    (including its own end_time) is untouched.

    Entries are sorted by start time (legacy behavior), so the start=1.5
    entry ("World") sorts before the start=1e308 entry ("Overflowing")."""
    downloader = _make_downloader()

    result = downloader._parse_youtube_subtitle_xml(XML_START_PLUS_DUR_OVERFLOWS)

    assert result.text == "World Overflowing"
    assert result.segments == [
        {"start_time": 1.5, "end_time": 3.5, "text": "World"},
        {"start_time": 1e308, "end_time": None, "text": "Overflowing"},
    ]
