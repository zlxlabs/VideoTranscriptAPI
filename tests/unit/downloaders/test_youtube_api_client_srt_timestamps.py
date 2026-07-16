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


def test_malformed_timestamp_format_keeps_cue_in_segments_with_none_time():
    """Malformed timestamp (single-digit hour) is not recognized as a valid
    range, but it still contains the "-->" arrow, so it is recognized as a
    (broken) cue boundary. Per the "text is never lost" invariant, the cue's
    text must still land in segments, just with start_time/end_time = None.
    Text extraction itself proceeds exactly like the legacy parser (the
    unrecognized timestamp line is treated as literal text, unchanged)."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_BAD_TIMESTAMP_FORMAT)

    assert result.segments == [
        {"start_time": None, "end_time": None, "text": "Hello world"},
    ]
    assert result.text == "0:00:01,000 --> 00:00:04,000 Hello world"
    assert YouTubeApiClient.parse_srt_to_text(SRT_BAD_TIMESTAMP_FORMAT) == result.text


def test_mixed_valid_and_broken_timeline_keeps_all_cue_text_in_segments():
    """Mixed SRT: a valid cue, a cue with a corrupted timeline, and another
    valid cue. All three cues' text must appear in segments (the corrupted
    one with start_time/end_time = None) -- a downstream consumer iterating
    segments must never silently lose the corrupted cue's text. The plain
    text output is completely unaffected (byte-identical to legacy)."""
    result = YouTubeApiClient.parse_srt_to_subtitle_result(SRT_MIXED_VALID_AND_BROKEN_TIMELINE)

    assert result.text == (
        "Hello world 0:00:05,000 --> 00:00:08,000 "
        "Broken timestamp cue Trailing valid cue"
    )
    assert YouTubeApiClient.parse_srt_to_text(SRT_MIXED_VALID_AND_BROKEN_TIMELINE) == result.text

    assert result.segments == [
        {"start_time": 1.0, "end_time": 4.0, "text": "Hello world"},
        {"start_time": None, "end_time": None, "text": "Broken timestamp cue"},
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
