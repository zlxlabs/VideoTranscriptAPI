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
- Malformed/missing timestamps never break text extraction: text stays
  normal regardless (fault tolerance rule).
- "Text is never lost" invariant: once segments is non-None, every cue's
  text must appear in it. A cue whose timeline is malformed still gets a
  segment entry (start_time/end_time = None, text preserved); segments only
  falls back to None when the whole SRT has no recognizable cue at all.
- Empty subtitle content: text == "" and segments is None.

All console output must be in English only (no emoji, no Chinese).
"""

from video_transcript_api.downloaders.subtitle_types import SubtitleResult, sanitize_time_pair
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

SRT_MIXED_VALID_AND_BROKEN_TIMELINE = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "0:00:05,000 --> 00:00:08,000\n"
    "Broken timestamp cue\n"
    "\n"
    "3\n"
    "00:00:09,000 --> 00:00:10,000\n"
    "Trailing valid cue\n"
)

SRT_BODY_TEXT_WITH_ARROW = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Go to Settings --> Privacy to change\n"
)

SRT_BODY_TEXT_WITH_ARROW_THEN_NEXT_CUE = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Go to Settings --> Privacy to change\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "Next cue text\n"
)

SRT_BODY_TEXT_WITH_STANDALONE_DIGIT_LINE = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "The answer is\n"
    "42\n"
    "more text after the number\n"
)

SRT_BODY_TEXT_WITH_STANDALONE_DIGIT_LINE_THEN_NEXT_CUE = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "The answer is\n"
    "42\n"
    "more text after the number\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "Next cue text\n"
)

SRT_BODY_ENDS_WITH_STANDALONE_DIGIT_LINE = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "The final line is\n"
    "42\n"
)

SRT_MIDDLE_CUE_MISSING_TIMELINE_ROW = (
    "1\n"
    "00:00:01,000 --> 00:00:02,000\n"
    "First line\n"
    "\n"
    "42\n"
    "Orphan body text\n"
    "\n"
    "3\n"
    "00:00:05,000 --> 00:00:06,000\n"
    "Third line\n"
)

SRT_REVERSED_TIMELINE = (
    "1\n"
    "00:00:10,000 --> 00:00:05,000\n"
    "Reversed timeline\n"
)

SRT_REVERSED_TIMELINE_THEN_NEXT_CUE = (
    "1\n"
    "00:00:10,000 --> 00:00:05,000\n"
    "Reversed timeline\n"
    "\n"
    "2\n"
    "00:00:06,000 --> 00:00:09,000\n"
    "Next cue text\n"
)

SRT_ALL_NUMERIC_BODY = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "42\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "100\n"
)

SRT_ORPHAN_ARROW_BODY_NO_TIME_STYLE = (
    "1\n"
    "00:00:01,000 --> 00:00:02,000\n"
    "First line\n"
    "\n"
    "42\n"
    "Settings --> Privacy to change\n"
    "\n"
    "3\n"
    "00:00:05,000 --> 00:00:06,000\n"
    "Third line\n"
)

SRT_CORRUPTED_TIMELINE_WITH_LETTER = (
    "1\n"
    "00:00:0X --> 00:00:04\n"
    "Hello world\n"
)

# gate-r17 P2: syntactically well-formed (two digits per field) but
# semantically bogus -- "99" seconds is not a valid clock value (seconds
# must be < 60). Before that fix, the regex only checked digit COUNT, not
# clock-VALIDITY, so this silently converted to 99 seconds via
# hours*3600+minutes*60+seconds -- the same "corrupted time masquerading as
# real time" bug fixed for parse_time_to_seconds in transcriber/segments.py.
#
# gate-r26 P2: start/end are now validated independently. Only the START
# side is malformed here -- the END side ("00:00:04,000") is perfectly
# well formed, so end_time must be preserved (4.0), not nulled out just
# because its sibling side is corrupted.
SRT_INVALID_SECONDS_COMPONENT = (
    "1\n"
    "00:00:99,000 --> 00:00:04,000\n"
    "Hello world\n"
)

# Same rule applied to the minutes component: "00:99:00,000" claims 99
# minutes, also invalid. gate-r26 P2: only start_time is nulled; end_time
# (4.0) is preserved.
SRT_INVALID_MINUTES_COMPONENT = (
    "1\n"
    "00:99:00,000 --> 00:00:04,000\n"
    "Hello world\n"
)

# gate-r24 P2: _SRT_TIMESTAMP_RANGE_PATTERN is used with .match(), which only
# anchors the START of the line -- a fourth trailing millisecond digit past
# the well-formed 3-digit field is simply left unconsumed and ignored, so the
# loose match still "succeeds" and extracts a start/end time as if the line
# were perfectly well-formed. The loose match still recognizes the line as a
# cue boundary (so the cue's text is not lost, and legacy parse_srt_to_text's
# output is completely unaffected -- that path is untouched), but the time
# VALUES must come from a strict, fully-anchored match and fall back to None
# for whichever side isn't exactly a well-formed timestamp.
#
# gate-r26 P2: only the corrupted END side (the extra trailing ms digit) is
# nulled; the well-formed START side ("00:00:01,000") must be preserved.
SRT_TRAILING_EXTRA_MS_DIGIT = (
    "1\n"
    "00:00:01,000 --> 00:00:04,0000\n"
    "Hello world\n"
)

# Same bug, different shape: arbitrary trailing garbage characters after an
# otherwise perfectly well-formed END timestamp. gate-r26 P2: only end_time
# is nulled; start_time (1.0) is preserved.
SRT_TRAILING_GARBAGE_AFTER_TIMESTAMP = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000 garbage\n"
    "Hello world\n"
)

# gate-r25 P2: _SRT_TIMESTAMP_STRICT_PATTERN's millisecond groups were
# `\d{1,3}` (1-3 digits), while the standard SRT millisecond field is always
# exactly 3 digits (zero-padded, e.g. "000"). A non-standard digit count
# (1 or 2 digits) is a corrupted timestamp, not a legitimately-formatted
# alternative -- it must be treated the same as any other corrupted side
# (that side's time = None), not silently accepted as a real time.
#
# gate-r26 P2: the malformed side here is START ("00:00:01,1"); the
# well-formed END side ("00:00:04,000") must be preserved (4.0), not
# nulled out just because its sibling side is corrupted.
SRT_SINGLE_DIGIT_MILLISECOND = (
    "1\n"
    "00:00:01,1 --> 00:00:04,000\n"
    "Hello world\n"
)

# Same rule for a 2-digit millisecond field ("...,12") on the START side.
SRT_TWO_DIGIT_MILLISECOND = (
    "1\n"
    "00:00:01,12 --> 00:00:04,000\n"
    "Hello world\n"
)

# gate-r26 P2: dedicated fixtures for the independent-side-validation fix
# itself (the fixtures above predate the fix and happen to also exercise it
# once their expectations are updated; these are the motivating examples
# from the fix description).
#
# Only the END side's minutes component is invalid ("99" minutes); the
# START side is perfectly well formed. A legitimate start time must not be
# discarded just because the end of the same cue is corrupted -- start is
# what chapter anchoring needs most.
SRT_END_MINUTES_INVALID = (
    "1\n"
    "00:00:01,000 --> 00:99:04,000\n"
    "Hello world\n"
)

# Only the END side's millisecond field is malformed (2 digits instead of
# the standard 3); the START side is perfectly well formed.
SRT_END_MS_NOT_THREE_DIGITS = (
    "1\n"
    "00:00:01,000 --> 00:00:04,12\n"
    "Hello world\n"
)

# BOTH sides malformed (invalid minutes component on each side) -- this must
# degrade to the same result as full corruption: both start_time and
# end_time are None.
SRT_BOTH_SIDES_INVALID = (
    "1\n"
    "00:99:01,000 --> 00:99:04,000\n"
    "Hello world\n"
)

# gate-r27 P2: a cue whose timeline row is entirely missing (mirrors
# SRT_ORPHAN_ARROW_BODY_NO_TIME_STYLE's structure), where the orphaned body
# text itself contains "-->" AND happens to have a "12:34"-style time
# fragment on exactly one side ("12:30" on the left of "Meet at 12:30 -->
# lobby"; "lobby" on the right has no digits at all). Before the AND fix,
# the old "at least one side" (OR) rule misclassified this as a corrupted
# timeline declaration, silently swallowing the body text itself (the
# text-collection loop for the "next cue" starts scanning *after* this
# line, so the line's own content is never captured anywhere).
SRT_CUE_BODY_ONE_SIDED_TIME_STYLE_THEN_GARBAGE_SIDE = (
    "1\n"
    "00:00:01,000 --> 00:00:02,000\n"
    "First line\n"
    "\n"
    "42\n"
    "Meet at 12:30 --> lobby\n"
    "\n"
    "3\n"
    "00:00:05,000 --> 00:00:06,000\n"
    "Third line\n"
)

# gate-r27 P2: dedicated non-regression fixture for the AND fix -- a
# genuinely corrupted timeline row where BOTH sides still contain a
# "12:34"-style digit+colon fragment ("00:00" / "00:00:04") must keep being
# recognized as a damaged timeline boundary (not reclassified as body text
# just because the AND guard was tightened).
SRT_GATE_R27_BOTH_SIDES_TIME_STYLE_STILL_BROKEN = (
    "1\n"
    "00:00:0Y --> 00:00:04\n"
    "Hello world\n"
)

# gate-r27 P2: consistency fixture for a "-->" row where one side is a
# complete, well-formed timestamp and the other side is arbitrary garbage
# with no digit+colon fragment at all ("00:00:01,000 --> garbage"). Placed
# after a normal, fully recognized cue so `found_any_cue` is already True,
# letting us directly observe how this specific row is classified (rather
# than the whole SRT collapsing to segments=None for having no recognizable
# cue at all).
SRT_ONE_SIDE_FULL_TIMESTAMP_OTHER_SIDE_GARBAGE = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:01,000 --> garbage\n"
    "Second cue text\n"
)

SRT_EMPTY = ""

# gate-r16 P2: a UTF-8 BOM (U+FEFF) prepended to the file content (as many
# editors/exporters do) lands on the very first index line, turning "1" into
# "﻿1". str.strip() does NOT remove U+FEFF (it is not whitespace by
# Python's definition), so the segments-extraction path's index-row
# recognition ("﻿1".isdigit() is False) used to misclassify it as
# ordinary orphan body text instead of a real index row -- producing a bogus
# leading segment (text="﻿1", start_time/end_time=None) ahead of the
# real first cue.
SRT_WITH_BOM = (
    "﻿1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "This is a test\n"
)


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


def test_malformed_timestamp_format_keeps_cue_in_segments_with_start_nulled():
    """Malformed timestamp (single-digit hour on the START side, "0:00:01,000"
    instead of "00:00:01,000") is not recognized as a valid range, but it
    still contains the "-->" arrow, so it is recognized as a (broken) cue
    boundary. Per the "text is never lost" invariant, the cue's text must
    still land in segments.

    gate-r26 P2: only the malformed START side is nulled -- the well-formed
    END side ("00:00:04,000") must be preserved as 4.0. Text extraction
    itself proceeds exactly like the legacy parser (the unrecognized
    timestamp line is treated as literal text, unchanged)."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_BAD_TIMESTAMP_FORMAT)

    assert result.segments == [
        {"start_time": None, "end_time": 4.0, "text": "Hello world"},
    ]
    assert result.text == "0:00:01,000 --> 00:00:04,000 Hello world"
    assert YouTubeApiClient.parse_srt_to_text(SRT_BAD_TIMESTAMP_FORMAT) == result.text


def test_mixed_valid_and_broken_timeline_keeps_all_cue_text_in_segments():
    """Mixed SRT: a valid cue, a cue with a corrupted START side (single-digit
    hour, "0:00:05,000"), and another valid cue. All three cues' text must
    appear in segments -- a downstream consumer iterating segments must
    never silently lose the corrupted cue's text. The plain text output is
    completely unaffected (byte-identical to legacy).

    gate-r26 P2: the broken cue's START side is nulled, but its well-formed
    END side ("00:00:08,000") is preserved as 8.0."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_MIXED_VALID_AND_BROKEN_TIMELINE)

    assert result.text == (
        "Hello world 0:00:05,000 --> 00:00:08,000 "
        "Broken timestamp cue Trailing valid cue"
    )
    assert YouTubeApiClient.parse_srt_to_text(SRT_MIXED_VALID_AND_BROKEN_TIMELINE) == result.text

    assert result.segments == [
        {"start_time": 1.0, "end_time": 4.0, "text": "Hello world"},
        {"start_time": None, "end_time": 8.0, "text": "Broken timestamp cue"},
        {"start_time": 9.0, "end_time": 10.0, "text": "Trailing valid cue"},
    ]
    # Every cue's text must be present in segments -- nothing silently dropped.
    segment_texts = {seg["text"] for seg in result.segments}
    assert segment_texts == {"Hello world", "Broken timestamp cue", "Trailing valid cue"}


def test_body_text_containing_arrow_stays_in_cue_text_and_segments():
    """A plain-text line that merely contains "-->" (e.g. a UI navigation hint
    like "Settings --> Privacy") must not be mistaken for a (corrupted)
    timeline boundary. The loose "-->"-based fallback added to recognize
    corrupted timelines only applies to lines in the expected timeline
    position (immediately following a pure-digit index line) -- a "-->" line
    encountered while collecting a cue's body text, whose previous line is
    not a pure-digit index line, is always treated as ordinary text."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_BODY_TEXT_WITH_ARROW)

    assert result.text == "Go to Settings --> Privacy to change"
    assert result.segments == [
        {"start_time": 1.0, "end_time": 4.0, "text": "Go to Settings --> Privacy to change"},
    ]
    assert YouTubeApiClient.parse_srt_to_text(SRT_BODY_TEXT_WITH_ARROW) == result.text


def test_body_text_containing_arrow_does_not_break_next_cue_boundary():
    """The same stray "-->" body line must not throw off detection of the
    following, genuinely well-formed cue either."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_BODY_TEXT_WITH_ARROW_THEN_NEXT_CUE)

    assert result.segments == [
        {"start_time": 1.0, "end_time": 4.0, "text": "Go to Settings --> Privacy to change"},
        {"start_time": 5.0, "end_time": 8.0, "text": "Next cue text"},
    ]
    assert YouTubeApiClient.parse_srt_to_text(SRT_BODY_TEXT_WITH_ARROW_THEN_NEXT_CUE) == result.text


def test_standalone_digit_line_in_cue_body_is_kept_as_text_not_treated_as_index():
    """A pure-digit line that is genuine cue body content (e.g. a lyric/line
    of dialogue that is literally the number "42") must not be mistaken for
    the next cue's index line -- doing so would prematurely end text
    collection and silently drop everything after it from segments (even
    though it still shows up in the plain-text output, since that extraction
    is a separate, cue-unaware pass). A digit line only counts as a real
    index line when the line immediately following it is a timeline (or a
    corrupted-timeline attempt); here the next line is ordinary text, so "42"
    and everything after it must stay inside this cue's segment."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(
        SRT_BODY_TEXT_WITH_STANDALONE_DIGIT_LINE
    )

    assert result.segments == [
        {
            "start_time": 1.0,
            "end_time": 4.0,
            "text": "The answer is 42 more text after the number",
        },
    ]
    # Backward-compatible plain-text extraction is untouched by this fix --
    # its cue-unaware, line-by-line algorithm has always dropped a standalone
    # digit line (treating it as an index line), unrelated to this bug.
    assert result.text == "The answer is more text after the number"
    assert YouTubeApiClient.parse_srt_to_text(
        SRT_BODY_TEXT_WITH_STANDALONE_DIGIT_LINE
    ) == result.text


def test_standalone_digit_line_in_cue_body_does_not_break_next_cue_boundary():
    """The same in-body digit line must not throw off detection of the
    following, genuinely well-formed cue -- mirrors the existing stray
    "-->" body-text regression test for the digit case."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(
        SRT_BODY_TEXT_WITH_STANDALONE_DIGIT_LINE_THEN_NEXT_CUE
    )

    assert result.segments == [
        {
            "start_time": 1.0,
            "end_time": 4.0,
            "text": "The answer is 42 more text after the number",
        },
        {"start_time": 5.0, "end_time": 8.0, "text": "Next cue text"},
    ]
    assert YouTubeApiClient.parse_srt_to_text(
        SRT_BODY_TEXT_WITH_STANDALONE_DIGIT_LINE_THEN_NEXT_CUE
    ) == result.text


def test_standalone_digit_line_at_end_of_cue_with_no_following_line_is_kept():
    """A digit line with nothing after it (end of file) has no lookahead
    line to prove it is a real index -- it must default to being treated as
    ordinary text rather than silently vanishing from the cue's segment."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(
        SRT_BODY_ENDS_WITH_STANDALONE_DIGIT_LINE
    )

    assert result.segments == [
        {"start_time": 1.0, "end_time": 4.0, "text": "The final line is 42"},
    ]
    assert YouTubeApiClient.parse_srt_to_text(
        SRT_BODY_ENDS_WITH_STANDALONE_DIGIT_LINE
    ) == result.text


def test_middle_cue_missing_timeline_row_lands_as_orphan_segment():
    """A cue whose timeline row is entirely absent (index line followed
    directly by body text, no "-->" line at all -- e.g. "42\\nOrphan body
    text\\n\\n") must not silently vanish from segments even though its text
    still lands in result.text. Per the "text is never lost" invariant, once
    segments is non-None (because the file does contain other real cues),
    this orphan text must land as its own segment with start_time/end_time
    both None, in its correct position between the surrounding cues."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(
        SRT_MIDDLE_CUE_MISSING_TIMELINE_ROW
    )

    assert result.segments == [
        {"start_time": 1.0, "end_time": 2.0, "text": "First line"},
        {"start_time": None, "end_time": None, "text": "Orphan body text"},
        {"start_time": 5.0, "end_time": 6.0, "text": "Third line"},
    ]
    assert result.text == "First line Orphan body text Third line"
    assert YouTubeApiClient.parse_srt_to_text(
        SRT_MIDDLE_CUE_MISSING_TIMELINE_ROW
    ) == result.text


def test_reversed_timeline_yields_none_end_time_start_kept():
    """A well-formed but reversed SRT timeline (end before start, e.g. an
    authoring mistake) must null end_time via the shared sanitize_time_pair
    rule -- start_time (valid on its own) and the cue's text are preserved.

    Note: negative start/end values are NOT separately tested here because
    the SRT timestamp regex (\\d{2}:\\d{2}:\\d{2}[,.]\\d{3}) only ever
    captures digits -- a negative timestamp cannot survive a successful
    regex match in the first place. That branch of sanitize_time_pair is
    covered directly in test_subtitle_types.py."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_REVERSED_TIMELINE)

    assert result.text == "Reversed timeline"
    assert result.segments == [
        {"start_time": 10.0, "end_time": None, "text": "Reversed timeline"},
    ]


def test_reversed_timeline_does_not_break_next_cue_boundary():
    """The reversed-timeline cue's sanitized end_time must not throw off
    detection of the following, genuinely well-formed cue."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(
        SRT_REVERSED_TIMELINE_THEN_NEXT_CUE
    )

    assert result.segments == [
        {"start_time": 10.0, "end_time": None, "text": "Reversed timeline"},
        {"start_time": 6.0, "end_time": 9.0, "text": "Next cue text"},
    ]
    assert YouTubeApiClient.parse_srt_to_text(
        SRT_REVERSED_TIMELINE_THEN_NEXT_CUE
    ) == result.text


def test_all_numeric_body_cues_yield_empty_text_but_populated_segments():
    """When every cue's body is itself a bare digit line (e.g. a spoken
    number like "42"), the legacy line-by-line text extraction mistakes each
    body line for the *next* cue's index line (its `line.isdigit()` skip
    rule cannot tell the two apart) and drops all of them -- text ends up
    "" even though every cue had real content. The lookahead-based segments
    extraction correctly recognizes these as body text (their *next* line is
    not a timeline), so segments stays fully populated. This is exactly the
    case get_subtitle_result()'s validity check (text OR segments) must
    accept as "has subtitles" -- see test_youtube_get_subtitle_result.py."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_ALL_NUMERIC_BODY)

    assert result.text == ""
    assert YouTubeApiClient.parse_srt_to_text(SRT_ALL_NUMERIC_BODY) == ""
    assert result.segments == [
        {"start_time": 1.0, "end_time": 4.0, "text": "42"},
        {"start_time": 5.0, "end_time": 8.0, "text": "100"},
    ]


def test_orphan_body_text_containing_arrow_with_no_time_style_lands_as_none_time_segment():
    """Regression for gate-r14 P2: a cue whose timeline row is entirely
    missing (index line followed directly by body text, no "-->" timeline
    row at all -- mirrors SRT_MIDDLE_CUE_MISSING_TIMELINE_ROW), where that
    orphaned body text itself happens to contain "-->" (e.g. a UI hint like
    "Settings --> Privacy"), must NOT be misclassified as a (corrupted)
    timeline boundary just because it sits in the "expected timeline
    position" (right after a digit index line). Neither side of its "-->"
    looks like a time (no digit+colon fragment), so it must fall through to
    the orphan-text path (R6) and land in segments with start_time/end_time
    = None -- not be silently dropped (which is what happened before this
    fix, since the text-collection loop would then start scanning for body
    text *after* this very line, capturing nothing)."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(
        SRT_ORPHAN_ARROW_BODY_NO_TIME_STYLE
    )

    assert result.segments == [
        {"start_time": 1.0, "end_time": 2.0, "text": "First line"},
        {"start_time": None, "end_time": None, "text": "Settings --> Privacy to change"},
        {"start_time": 5.0, "end_time": 6.0, "text": "Third line"},
    ]
    assert result.text == "First line Settings --> Privacy to change Third line"
    assert YouTubeApiClient.parse_srt_to_text(
        SRT_ORPHAN_ARROW_BODY_NO_TIME_STYLE
    ) == result.text


def test_corrupted_timeline_with_letter_and_time_style_still_treated_as_broken_timeline():
    """A genuinely corrupted timeline row (e.g. a stray letter replacing a
    digit: "00:00:0X --> 00:00:04") must NOT be reclassified as body text by
    the new "looks like a time" guard -- both sides still contain a
    "12:34"-style digit+colon fragment ("00:00" / "00:00:04"), so it must
    keep being recognized as a damaged timeline boundary: the cue's text is
    preserved with start_time/end_time = None (the regex itself still fails
    to match, since "X" isn't a digit)."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(
        SRT_CORRUPTED_TIMELINE_WITH_LETTER
    )

    assert result.segments == [
        {"start_time": None, "end_time": None, "text": "Hello world"},
    ]
    assert result.text == "00:00:0X --> 00:00:04 Hello world"
    assert YouTubeApiClient.parse_srt_to_text(
        SRT_CORRUPTED_TIMELINE_WITH_LETTER
    ) == result.text


def test_invalid_seconds_component_nulls_only_start_end_preserved():
    """A timestamp line can fully match the digit-count regex while still
    carrying an out-of-range clock component: "00:00:99,000" claims 99
    seconds, which is not a valid clock value. Per the same "corrupted clock
    components must not masquerade as a real time" policy enforced by
    parse_time_to_seconds (transcriber/segments.py, gate-r17 P2/P3), this
    must NOT be silently converted to 99 seconds.

    gate-r26 P2: only the malformed side (start) is nulled -- the
    well-formed end side ("00:00:04,000") must be preserved as 4.0, not
    discarded just because its sibling side is corrupted. Text preserved,
    plain text output byte-identical to the legacy parser (which never
    consumes the time value in the first place)."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_INVALID_SECONDS_COMPONENT)

    assert result.segments == [
        {"start_time": None, "end_time": 4.0, "text": "Hello world"},
    ]
    assert YouTubeApiClient.parse_srt_to_text(
        SRT_INVALID_SECONDS_COMPONENT
    ) == result.text


def test_invalid_minutes_component_nulls_only_start_end_preserved():
    """Same rule applied to the minutes component: "00:99:00,000" claims 99
    minutes, also invalid. gate-r26 P2: only start_time is nulled; end_time
    (4.0) is preserved."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_INVALID_MINUTES_COMPONENT)

    assert result.segments == [
        {"start_time": None, "end_time": 4.0, "text": "Hello world"},
    ]
    assert YouTubeApiClient.parse_srt_to_text(
        SRT_INVALID_MINUTES_COMPONENT
    ) == result.text


def test_trailing_extra_ms_digit_nulls_only_end_start_preserved():
    """gate-r24 P2: a fourth trailing millisecond digit ("...,0000" instead of
    the well-formed 3-digit "...,000") must not be silently accepted as a
    legal time -- the loose .match()-based cue-boundary detection still
    recognizes the line as a timeline row (so the cue text is preserved and
    legacy parse_srt_to_text output is unaffected), but the strict,
    fully-anchored check used to extract the actual time VALUE for that side
    must reject it, falling back to None.

    gate-r26 P2: the extra digit only corrupts the END side -- the
    well-formed START side ("00:00:01,000") must be preserved as 1.0, not
    discarded along with it."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_TRAILING_EXTRA_MS_DIGIT)

    assert result.segments == [
        {"start_time": 1.0, "end_time": None, "text": "Hello world"},
    ]
    # legacy text path is untouched by this fix -- byte-identical output
    assert YouTubeApiClient.parse_srt_to_text(SRT_TRAILING_EXTRA_MS_DIGIT) == result.text


def test_trailing_garbage_after_timestamp_nulls_only_end_start_preserved():
    """Same rule for arbitrary trailing garbage characters after an otherwise
    well-formed END timestamp -- the loose match ignores everything after the
    recognized prefix, but the strict per-side check must reject that side.
    gate-r26 P2: only end_time is nulled; start_time (1.0) is preserved."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(
        SRT_TRAILING_GARBAGE_AFTER_TIMESTAMP
    )

    assert result.segments == [
        {"start_time": 1.0, "end_time": None, "text": "Hello world"},
    ]
    assert YouTubeApiClient.parse_srt_to_text(
        SRT_TRAILING_GARBAGE_AFTER_TIMESTAMP
    ) == result.text


def test_single_digit_millisecond_nulls_only_start_end_preserved():
    """gate-r25 P2: a millisecond field with only 1 digit ("...,1" instead of
    the standard zero-padded 3-digit "...,000") must not be silently accepted
    as a legal time -- the loose cue-boundary detection (via the "-->"-based
    fallback, since the digit count also fails the fixed-width loose/range
    patterns) still recognizes the line as a timeline row, but the strict
    check used to extract that side's time VALUE must reject any millisecond
    field that isn't exactly 3 digits, falling back to None.

    gate-r26 P2: the malformed side is START -- the well-formed END side
    ("00:00:04,000") must be preserved as 4.0."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_SINGLE_DIGIT_MILLISECOND)

    assert result.segments == [
        {"start_time": None, "end_time": 4.0, "text": "Hello world"},
    ]
    # legacy text path is untouched by this fix -- byte-identical output
    assert YouTubeApiClient.parse_srt_to_text(SRT_SINGLE_DIGIT_MILLISECOND) == result.text


def test_two_digit_millisecond_nulls_only_start_end_preserved():
    """Same rule for a 2-digit millisecond field ("...,12") -- also
    non-standard, must be rejected the same way as the 1-digit case above.
    gate-r26 P2: only start_time is nulled; end_time (4.0) is preserved."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_TWO_DIGIT_MILLISECOND)

    assert result.segments == [
        {"start_time": None, "end_time": 4.0, "text": "Hello world"},
    ]
    assert YouTubeApiClient.parse_srt_to_text(SRT_TWO_DIGIT_MILLISECOND) == result.text


def test_end_minutes_invalid_nulls_only_end_start_preserved():
    """gate-r26 P2 motivating example: only the END side's minutes component
    is invalid ("00:99:04,000" claims 99 minutes) while the START side
    ("00:00:01,000") is perfectly well formed. A legitimate start time must
    not be discarded just because the end of the same cue is corrupted --
    start is what chapter anchoring needs most. Only end_time is nulled;
    start_time (1.0) is preserved."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_END_MINUTES_INVALID)

    assert result.segments == [
        {"start_time": 1.0, "end_time": None, "text": "Hello world"},
    ]
    assert YouTubeApiClient.parse_srt_to_text(SRT_END_MINUTES_INVALID) == result.text


def test_end_ms_not_three_digits_nulls_only_end_start_preserved():
    """gate-r26 P2 motivating example: only the END side's millisecond field
    is malformed (2 digits instead of the standard 3) while the START side is
    perfectly well formed. Only end_time is nulled; start_time (1.0) is
    preserved."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_END_MS_NOT_THREE_DIGITS)

    assert result.segments == [
        {"start_time": 1.0, "end_time": None, "text": "Hello world"},
    ]
    assert YouTubeApiClient.parse_srt_to_text(SRT_END_MS_NOT_THREE_DIGITS) == result.text


def test_both_sides_invalid_nulls_both_matches_prior_full_corruption_behavior():
    """gate-r26 P2: when BOTH sides are independently invalid (invalid
    minutes component on each side), the result must be identical to the
    pre-fix "whole timeline corrupted" behavior: both start_time and
    end_time are None, text preserved."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_BOTH_SIDES_INVALID)

    assert result.segments == [
        {"start_time": None, "end_time": None, "text": "Hello world"},
    ]
    assert YouTubeApiClient.parse_srt_to_text(SRT_BOTH_SIDES_INVALID) == result.text


def test_only_end_survives_sanitize_time_pair_keeps_it_not_nulled():
    """gate-r26 P2 lock: sanitize_time_pair's rule 3 (end < start -> end =
    None) only fires when BOTH start and end are non-None. When start is
    None (its side was corrupted) and end alone is a legitimate value, that
    end value must survive sanitize_time_pair untouched -- it must not be
    incorrectly nulled just because start is absent. This locks in the exact
    scenario from SRT_INVALID_SECONDS_COMPONENT at the sanitize_time_pair
    level, independent of SRT parsing."""
    assert sanitize_time_pair(None, 4.0) == (None, 4.0)


def test_normal_timeline_still_parses_after_strict_check_added():
    """Regression: a perfectly well-formed timestamp range must still parse
    to real start/end times under the new strict full-line check -- this is
    covered implicitly by test_parse_srt_to_subtitle_result_normal_srt, but
    is asserted explicitly here alongside the two adversarial cases above so
    the three tests read as one coherent before/after/regression trio."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_NORMAL)

    assert result.segments == [
        {"start_time": 1.0, "end_time": 4.0, "text": "Hello world"},
        {"start_time": 5.0, "end_time": 8.0, "text": "This is a test"},
    ]
    assert YouTubeApiClient.parse_srt_to_text(SRT_NORMAL) == result.text


def test_empty_srt_content():
    """Empty subtitle content: text is empty string, segments is None."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_EMPTY)

    assert result.text == ""
    assert result.segments is None
    assert YouTubeApiClient.parse_srt_to_text(SRT_EMPTY) == ""


def test_bom_prefixed_srt_first_cue_parses_cleanly_with_no_bogus_segment():
    """gate-r16 P2 regression: a BOM on the first index line ("﻿1") must not
    produce a leading bogus segment (text="﻿1", start_time/end_time=None)
    ahead of the real first cue. The segments-extraction path must recognize
    "﻿1" as a genuine index row (BOM-tolerant isdigit check) and skip it,
    same as an ordinary "1" -- leaving segments with exactly the two real
    cues, correct start/end times, no extra entry."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_WITH_BOM)

    assert result.segments == [
        {"start_time": 1.0, "end_time": 4.0, "text": "Hello world"},
        {"start_time": 5.0, "end_time": 8.0, "text": "This is a test"},
    ]


def test_bom_prefixed_srt_legacy_parse_srt_to_text_keeps_historical_quirk():
    """The BOM-tolerance fix is scoped to the segments-extraction path only.
    parse_srt_to_text() (and the `text` field of parse_srt_to_subtitle_result)
    must stay byte-identical to main's historical behavior, quirk included:
    main's line-by-line text loop never strips BOM, so "﻿1" fails its
    strict `line.isdigit()` check and gets kept as literal body text (the
    BOM character embedded in it) rather than being recognized and skipped
    as an index row. This divergence between the two paths (segments: BOM
    tolerant / text: BOM-naive legacy quirk) is intentional -- the task's
    hard constraint is that legacy text output must never change, even
    though it means text and segments briefly disagree on how cue 1's index
    line should be classified."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_WITH_BOM)

    assert result.text == "﻿1 Hello world This is a test"
    assert YouTubeApiClient.parse_srt_to_text(SRT_WITH_BOM) == result.text


def test_gate_r27_cue_body_first_line_with_one_sided_time_style_lands_as_text_not_broken_timeline():
    """gate-r27 P2: "Meet at 12:30 --> lobby" as a cue's orphaned body text
    (its real timeline row is missing entirely, index line followed
    directly by this body line) must NOT be misjudged as a damaged timeline
    just because its LEFT side ("12:30") happens to look like a time -- the
    RIGHT side ("lobby") has no digit+colon fragment at all. The old "at
    least one side" (OR) rule swallowed this line whole (treated as a
    corrupted timeline boundary, so the text-collection for whatever
    "follows" it starts scanning from the line *after* it, losing this
    line's own content everywhere). The tightened "both sides" (AND) rule
    correctly falls through to the orphan-text path (R6): the line lands in
    segments as its own entry with start_time/end_time = None, and text
    stays byte-identical to the legacy parser (which never recognized this
    line as a timeline row in the first place, since it doesn't match the
    full "HH:MM:SS,mmm --> HH:MM:SS,mmm" shape)."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(
        SRT_CUE_BODY_ONE_SIDED_TIME_STYLE_THEN_GARBAGE_SIDE
    )

    assert result.segments == [
        {"start_time": 1.0, "end_time": 2.0, "text": "First line"},
        {"start_time": None, "end_time": None, "text": "Meet at 12:30 --> lobby"},
        {"start_time": 5.0, "end_time": 6.0, "text": "Third line"},
    ]
    assert result.text == "First line Meet at 12:30 --> lobby Third line"
    assert YouTubeApiClient.parse_srt_to_text(
        SRT_CUE_BODY_ONE_SIDED_TIME_STYLE_THEN_GARBAGE_SIDE
    ) == result.text


def test_gate_r27_both_sides_time_style_corrupted_timeline_still_treated_as_broken():
    """gate-r27 P2 non-regression: tightening `_looks_like_timeline_attempt`
    from "at least one side" to "both sides must match" must not lose the
    ability to recognize a genuinely corrupted timeline row. "00:00:0Y -->
    00:00:04" has a stray letter breaking the strict digit-count regex, but
    BOTH sides still contain a "12:34"-style digit+colon fragment ("00:00"
    on the left, "00:00:04" on the right) -- it must keep being classified
    as a damaged timeline boundary: the cue text is preserved with
    start_time/end_time = None (not reclassified as plain body text)."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(
        SRT_GATE_R27_BOTH_SIDES_TIME_STYLE_STILL_BROKEN
    )

    assert result.segments == [
        {"start_time": None, "end_time": None, "text": "Hello world"},
    ]
    assert result.text == "00:00:0Y --> 00:00:04 Hello world"
    assert YouTubeApiClient.parse_srt_to_text(
        SRT_GATE_R27_BOTH_SIDES_TIME_STYLE_STILL_BROKEN
    ) == result.text


def test_gate_r27_one_side_time_style_other_side_garbage_matches_legacy_text_handling():
    """gate-r27 P2 consistency check: for a "-->" row where one side is a
    complete, well-formed timestamp and the other side is arbitrary garbage
    with no digit+colon fragment ("00:00:01,000 --> garbage"), the tightened
    AND rule no longer treats it as a (corrupted) timeline candidate -- it
    falls through to plain text/orphan-text handling.

    Consistency conclusion (verified against legacy `parse_srt_to_text`):
    legacy's own timeline-line recognition (`_SRT_TIMESTAMP_LINE_PATTERN`,
    matched with a plain, non-strict regex) requires BOTH sides to be a
    fully-formed "HH:MM:SS,mmm" timestamp to match at all -- "garbage" on
    the right never matches, so legacy's `.match()` fails and legacy keeps
    this entire line as literal body text (never consumed as a timeline
    row). After the AND fix, the segments path reaches exactly the same
    verdict: this row is not a recognized timeline row, so it is collected
    as literal text into the orphan-text buffer alongside the following
    body line. Both paths agree -- "preserve as text, never consume as a
    timeline" -- so text and segments stay consistent (the hard invariant:
    segments-side text must never be lost, and legacy text must stay
    byte-identical)."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(
        SRT_ONE_SIDE_FULL_TIMESTAMP_OTHER_SIDE_GARBAGE
    )

    assert result.segments == [
        {"start_time": 1.0, "end_time": 4.0, "text": "Hello world"},
        {
            "start_time": None,
            "end_time": None,
            "text": "00:00:01,000 --> garbage Second cue text",
        },
    ]
    assert (
        result.text == "Hello world 00:00:01,000 --> garbage Second cue text"
    )
    assert YouTubeApiClient.parse_srt_to_text(
        SRT_ONE_SIDE_FULL_TIMESTAMP_OTHER_SIDE_GARBAGE
    ) == result.text
