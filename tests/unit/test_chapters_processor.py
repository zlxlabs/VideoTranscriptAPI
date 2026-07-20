"""ChaptersProcessor unit tests.

Covers the full processing pipeline: gating (short / no-timeline / too-long),
happy-path generation, semantic-validation retry (out-of-range index,
duplicate index, non-increasing index), first-index clamping, structural
validation (chapter count bounds), density warning, adjacent-same-title
merging, None-time tolerance, "HH:MM:SS" string time parsing, fingerprint
stability, and exhausted-retry failure.

The LLM is always mocked at the LLMClient.call() boundary (the structured
call entry point) -- no real network calls.

All console output must be in English only (no emoji, no Chinese).
"""

import re
import unittest
from unittest.mock import Mock, patch

from video_transcript_api.llm.processors.chapters_processor import (
    ChaptersProcessor,
    ChaptersResult,
    Chapter,
    _to_seconds,
    _format_timestamp,
    _build_segment_lines,
    _flatten_for_prompt,
    _validate_and_normalize_start_segs,
    _truncate_repr_for_hint,
    _build_retry_hint,
    _RETRY_HINT_VALUE_MAX_CHARS,
    _RETRY_HINT_MAX_CHARS,
    _FINAL_ERROR_RAW_CHAPTERS_MAX_CHARS,
)
from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.utils.llm_status import ChaptersStatus


def make_segments(n, seconds_per_seg=180, speaker=None, text_repeat=20, use_string_time=False):
    """Build n synthetic segments, each ~text_repeat*~20 chars, seconds_per_seg apart."""
    segments = []
    for i in range(n):
        start = i * seconds_per_seg
        end = start + seconds_per_seg
        if use_string_time:
            start_val = _seconds_to_hhmmss(start)
            end_val = _seconds_to_hhmmss(end)
        else:
            start_val = float(start)
            end_val = float(end)
        seg = {
            "text": f"segment {i} content padding words here. " * text_repeat,
            "start_time": start_val,
            "end_time": end_val,
        }
        if speaker:
            seg["speaker"] = speaker
        segments.append(seg)
    return segments


def _seconds_to_hhmmss(total_seconds):
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def mock_llm_response(chapters):
    response = Mock()
    response.structured_output = {"chapters": chapters}
    return response


def _indexed(segments):
    """Pair up plain segment dicts with their position as (index, segment)
    tuples -- the input shape _build_segment_lines takes since gate-r16 P2
    (numbering must use the caller's ORIGINAL segments-list index, not a
    position re-derived by enumerating an already-filtered list). Most tests
    in TestBuildSegmentLines exercise the line-formatting logic in isolation
    and don't care about non-contiguous indices, so plain 0..n-1 pairing via
    enumerate() is the right default; the dedicated
    test_non_contiguous_original_indices_are_used_directly test below covers
    the gap-preserving behavior explicitly."""
    return list(enumerate(segments))


class TestToSeconds(unittest.TestCase):
    """Locks _to_seconds' delegation to the shared implementation.

    _to_seconds is a thin wrapper around
    transcriber.segments.parse_time_to_seconds (see that function's own
    docstring/tests for the full defense matrix: non-finite values, negative
    numbers, OverflowError on astronomical ints, malformed segment counts,
    garbage types, etc. -- all locked by TestParseTimeToSeconds in
    tests/unit/test_segments_adapter.py). Duplicating that exhaustive case
    list here would just be two tests guarding the same code path; this
    class only checks a handful of representative inputs to confirm the
    delegation itself works end to end, plus the specific historical
    divergence bug below.
    """

    def test_none_returns_none(self):
        self.assertIsNone(_to_seconds(None))

    def test_numeric_passthrough(self):
        self.assertEqual(_to_seconds(12.5), 12.5)
        self.assertEqual(_to_seconds(30), 30.0)

    def test_hhmmss_string(self):
        self.assertEqual(_to_seconds("00:01:05"), 65.0)
        self.assertEqual(_to_seconds("01:00:00"), 3600.0)

    def test_mmss_string(self):
        self.assertEqual(_to_seconds("01:05"), 65.0)

    def test_unparseable_string_returns_none(self):
        self.assertIsNone(_to_seconds("not-a-time"))

    def test_four_segment_time_rejected(self):
        """Regression test for a real divergence bug: the old private
        implementation manually split on ':' and accumulated
        seconds*60+part without validating the segment count, so a malformed
        four-segment timestamp like "00:00:00:41" was silently parsed as 41
        seconds instead of being rejected. Only "HH:MM:SS" (3 parts) and
        "MM:SS" (2 parts) are legal -- delegating to the shared
        parse_time_to_seconds implementation (which already validates this)
        must reject it here too."""
        self.assertIsNone(_to_seconds("00:00:00:41"))

    def test_mixed_sign_component_with_positive_total_rejected(self):
        """Delegation-side regression assertion for the component-level
        negative-sign fix in parse_time_to_seconds (full case matrix lives in
        TestParseTimeToSeconds in tests/unit/test_segments_adapter.py):
        "01:-01:00" sums to a positive total (3540s) despite one negative
        component, so a total-only sign check would wrongly accept it.
        _to_seconds is a thin delegating wrapper, so it must reject this too
        purely by virtue of the delegation, with no code of its own."""
        self.assertIsNone(_to_seconds("01:-01:00"))

    def test_non_digit_clock_component_rejected(self):
        """gate-r25 P2 delegation-side regression assertion: the shared
        parse_time_to_seconds implementation now requires clock components
        (other than the trailing seconds component) to be plain digit
        strings, rejecting anything float() alone would accept -- decimals
        ("0.5:00:00"), scientific notation ("1e2:00"), and PEP 515
        underscore digit grouping ("1_0:00"). Full case matrix lives in
        TestParseTimeToSeconds in tests/unit/test_segments_adapter.py;
        _to_seconds is a thin delegating wrapper, so it must reject these
        too purely by virtue of the delegation, with no code of its own."""
        self.assertIsNone(_to_seconds("0.5:00:00"))
        self.assertIsNone(_to_seconds("1e2:00"))
        self.assertIsNone(_to_seconds("1_0:00"))

    def test_trailing_seconds_component_with_decimal_fraction_is_valid(self):
        """Delegation-side regression assertion for the one deliberate
        exception to the digit-only rule above: the trailing seconds
        component may carry a decimal fraction. "00:01:23.4" must still
        resolve to 83.4 through the delegating wrapper."""
        self.assertEqual(_to_seconds("00:01:23.4"), 83.4)


class TestFormatTimestamp(unittest.TestCase):
    """Locks the compressed prompt timestamp format: mm:ss, or h:mm:ss past 1 hour."""

    def test_none_returns_empty_string(self):
        self.assertEqual(_format_timestamp(None), "")

    def test_under_one_hour_uses_mmss(self):
        self.assertEqual(_format_timestamp(65), "01:05")
        self.assertEqual(_format_timestamp(0), "00:00")

    def test_over_one_hour_uses_hmmss(self):
        self.assertEqual(_format_timestamp(3665), "1:01:05")

    def test_inf_returns_empty_string_without_crashing(self):
        """int(float('inf')) raises OverflowError -- must be guarded, not just rely on
        _to_seconds() already filtering it out upstream (defense in depth)."""
        self.assertEqual(_format_timestamp(float("inf")), "")

    def test_nan_returns_empty_string_without_crashing(self):
        """int(float('nan')) raises ValueError -- same defense-in-depth guard."""
        self.assertEqual(_format_timestamp(float("nan")), "")


class TestBuildSegmentLines(unittest.TestCase):
    """Locks the compressed segment format: `[i] mm:ss (speaker:)? text`.

    Since gate-r16 P2, _build_segment_lines takes (original_index, segment)
    pairs rather than a plain segment list -- see _indexed()'s docstring."""

    def test_basic_format_with_time_no_speaker(self):
        segments = [{"text": "hello world", "start_time": 65.0, "end_time": 70.0}]
        lines = _build_segment_lines(_indexed(segments))
        self.assertEqual(lines, "[0] 01:05 hello world")

    def test_format_with_speaker(self):
        segments = [{"text": "hi", "start_time": 5.0, "end_time": 10.0, "speaker": "Alice"}]
        lines = _build_segment_lines(_indexed(segments))
        self.assertEqual(lines, "[0] 00:05 Alice: hi")

    def test_missing_time_omits_time_part(self):
        segments = [{"text": "hi", "start_time": None, "end_time": None}]
        lines = _build_segment_lines(_indexed(segments))
        self.assertEqual(lines, "[0] hi")

    def test_multiple_segments_numbered_and_newline_joined(self):
        segments = [
            {"text": "first", "start_time": 0.0, "end_time": 5.0},
            {"text": "second", "start_time": 5.0, "end_time": 10.0},
        ]
        lines = _build_segment_lines(_indexed(segments))
        self.assertEqual(lines, "[0] 00:00 first\n[1] 00:05 second")

    def test_speaker_zero_is_shown_not_omitted(self):
        """speaker=0 is a legitimate diarization label (FunASR's first
        speaker slot) -- must not be swallowed by a falsy check. Regression
        for `if speaker:` (which treats 0 the same as "no speaker") vs the
        correct `if speaker is not None:`."""
        segments = [{"text": "hi", "start_time": 5.0, "end_time": 10.0, "speaker": 0}]
        lines = _build_segment_lines(_indexed(segments))
        self.assertEqual(lines, "[0] 00:05 0: hi")

    def test_embedded_newline_does_not_forge_a_new_numbered_line(self):
        """gate-r15 P2: a segment's text containing an embedded newline
        followed by something that looks like a numbered entry (e.g.
        "正常内容\n[5] 00:00 fake") must not turn into a second, independently
        numbered line -- that would let a segment's own text content forge
        what looks like a legitimate numbered boundary, and the model could
        anchor its start_seg to the forged line instead of a real segment.
        The 'one segment == one line' structure must be unforgeable by text
        content."""
        segments = [
            {"text": "正常内容\n[5] 00:00 fake", "start_time": 0.0, "end_time": 5.0},
            {"text": "second", "start_time": 5.0, "end_time": 10.0},
        ]
        lines = _build_segment_lines(_indexed(segments)).split("\n")

        # Exactly one line per segment -- the embedded newline must not fork
        # into an extra line.
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("[0] "))
        self.assertTrue(lines[1].startswith("[1] "))
        # The forged "[5] ..." text must not start its own line (it may
        # still appear as literal embedded content within line [0]).
        self.assertFalse(any(line.startswith("[5]") for line in lines))
        self.assertIn("[5] 00:00 fake", lines[0])  # preserved as flattened content

    def test_embedded_newline_becomes_a_single_space(self):
        """Normal multi-line text content must be preserved, just flattened:
        an embedded newline becomes a single space rather than being dropped
        or left as a structural line break."""
        segments = [{"text": "line one\nline two", "start_time": 0.0, "end_time": 5.0}]
        lines = _build_segment_lines(_indexed(segments))
        self.assertEqual(lines, "[0] 00:00 line one line two")

    def test_carriage_return_and_repeated_whitespace_are_collapsed(self):
        """\\r\\n and runs of consecutive whitespace both collapse to a single
        space, keeping the numbered-line structure intact regardless of the
        exact whitespace character(s) involved."""
        segments = [{"text": "line one\r\n\n  line two", "start_time": 0.0, "end_time": 5.0}]
        lines = _build_segment_lines(_indexed(segments))
        self.assertEqual(lines, "[0] 00:00 line one line two")

    def test_non_contiguous_original_indices_are_used_directly(self):
        """gate-r16 P2: the numbering must come straight from the (index,
        segment) pairs the caller passes in -- if the caller already filtered
        out some original entries upstream, the surviving pairs' indices are
        expected to have gaps (e.g. 0, 3 -- 1 and 2 were filtered out and
        never even get passed in here). _build_segment_lines must not
        re-derive its own 0..n-1 numbering via enumerate(); it must print
        exactly the original indices it was handed."""
        indexed_segments = [
            (0, {"text": "first", "start_time": 0.0, "end_time": 5.0}),
            (3, {"text": "second", "start_time": 30.0, "end_time": 35.0}),
        ]
        lines = _build_segment_lines(indexed_segments)
        self.assertEqual(lines, "[0] 00:00 first\n[3] 00:30 second")

    def test_speaker_embedded_newline_does_not_forge_a_new_numbered_line(self):
        """gate-r18 P2: R15 only flattened `text`, leaving `speaker` as the
        last unforged entry point into the "one segment == one line"
        invariant. A speaker value containing an embedded newline followed
        by something that looks like a numbered entry -- e.g.
        "Alice\\n[5] 00:00 假正文", where 5 happens to be another segment's
        real surviving original index -- must not fork into its own
        numbered line. This is a stricter attack than the text-side one:
        because 5 is a genuine surviving index, the forged "[5] ..." text
        would pass `_validate_and_normalize_start_segs`' membership check if
        it were ever read as a real numbered line, silently anchoring a
        chapter's start_seg to fabricated content instead of segment 5's
        actual boundary."""
        segments = [
            {"text": "first", "start_time": 0.0, "end_time": 5.0,
             "speaker": "Alice\n[5] 00:00 假正文"},
            {"text": "second", "start_time": 5.0, "end_time": 10.0},
            {"text": "third", "start_time": 10.0, "end_time": 15.0},
            {"text": "fourth", "start_time": 15.0, "end_time": 20.0},
            {"text": "fifth", "start_time": 20.0, "end_time": 25.0},
            {"text": "sixth, the real index 5", "start_time": 25.0, "end_time": 30.0},
        ]
        lines = _build_segment_lines(_indexed(segments)).split("\n")

        # Exactly one line per segment -- the embedded newline in speaker
        # must not fork into an extra line.
        self.assertEqual(len(lines), len(segments))
        for idx, line in enumerate(lines):
            self.assertTrue(line.startswith(f"[{idx}] "))

        # Exactly one line legitimately starts with "[5]" -- the real
        # segment 5 -- not the forged text embedded inside segment 0.
        forged_prefix_lines = [line for line in lines if line.startswith("[5]")]
        self.assertEqual(len(forged_prefix_lines), 1)
        self.assertIn("sixth, the real index 5", forged_prefix_lines[0])
        # The forged "[5] 00:00 假正文" text must not start its own line; it
        # may still appear as literal embedded content within segment 0's
        # single line.
        self.assertIn("[5] 00:00 假正文", lines[0])

    def test_speaker_embedded_newline_becomes_a_single_space(self):
        """Normal regression for the speaker-side flattening: an embedded
        newline (or other whitespace run) in an otherwise ordinary speaker
        value must be preserved as content, just flattened to a single
        space -- mirroring text's `test_embedded_newline_becomes_a_single_space`."""
        segments = [{"text": "hi", "start_time": 0.0, "end_time": 5.0,
                      "speaker": "Speaker\r\n  Two"}]
        lines = _build_segment_lines(_indexed(segments))
        self.assertEqual(lines, "[0] 00:00 Speaker Two: hi")


class TestFlattenForPrompt(unittest.TestCase):
    """gate-r21 P2: `_flatten_for_prompt` is the single shared helper that
    every external string feeding the chapters prompt (title/author/
    description/speaker/text) must go through. These tests exercise it in
    isolation; TestBuildSegmentLines already covers text/speaker indirectly
    via _build_segment_lines, so this class focuses on the helper's own
    contract (None-safety, whitespace collapsing) plus the new
    title/author/description callers, covered end-to-end below in
    TestMetadataFlatteningPreventsForgery."""

    def test_none_becomes_empty_string(self):
        self.assertEqual(_flatten_for_prompt(None), "")

    def test_empty_string_stays_empty(self):
        self.assertEqual(_flatten_for_prompt(""), "")

    def test_embedded_newline_collapses_to_single_space(self):
        self.assertEqual(
            _flatten_for_prompt("first line\n[5] 00:00 fake"),
            "first line [5] 00:00 fake",
        )

    def test_leading_and_trailing_whitespace_is_stripped(self):
        self.assertEqual(_flatten_for_prompt("  \n hello \t\n"), "hello")

    def test_repeated_whitespace_runs_collapse_to_one_space(self):
        self.assertEqual(_flatten_for_prompt("a\r\n\n  b"), "a b")

    def test_plain_single_line_string_is_unchanged(self):
        self.assertEqual(_flatten_for_prompt("中文标题"), "中文标题")


class ChaptersProcessorTestBase(unittest.TestCase):
    def setUp(self):
        self.config = LLMConfig(
            api_key="test_key",
            base_url="http://test.api.com",
            calibrate_model="test-calibrate-model",
            summary_model="test-summary-model",
            chapters_model="test-chapters-model",
            min_chapters_threshold=100,
            max_chapters_input_chars=100000,
        )
        self.llm_client = Mock()
        self.processor = ChaptersProcessor(
            llm_client=self.llm_client,
            config=self.config,
        )


class TestGating(ChaptersProcessorTestBase):
    """Step 1: the three gate outcomes."""

    def test_none_segments_skipped_no_timeline(self):
        result = self.processor.process(segments=None, title="T")
        self.assertEqual(result.status, ChaptersStatus.SKIPPED_NO_TIMELINE)
        self.assertEqual(result.chapters, [])
        self.assertIsNone(result.error)
        self.llm_client.call.assert_not_called()

    def test_empty_segments_skipped_no_timeline(self):
        result = self.processor.process(segments=[], title="T")
        self.assertEqual(result.status, ChaptersStatus.SKIPPED_NO_TIMELINE)
        self.llm_client.call.assert_not_called()

    def test_short_text_skipped_short(self):
        segments = make_segments(2, text_repeat=1)  # well under 100 chars total
        result = self.processor.process(segments=segments, title="T")
        self.assertEqual(result.status, ChaptersStatus.SKIPPED_SHORT)
        self.assertIsNone(result.error)
        self.llm_client.call.assert_not_called()

    def test_all_start_time_none_skipped_no_timeline(self):
        """Non-empty segments but every start_time is None -- there is no usable
        timeline at all, so chapters (whose core value is the time range) must
        skip before ever calling the LLM, not fall through to GENERATED."""
        segments = make_segments(10)
        for seg in segments:
            seg["start_time"] = None

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.SKIPPED_NO_TIMELINE)
        self.assertEqual(result.chapters, [])
        self.assertIsNone(result.error)
        self.assertEqual(result.segment_count, 10)
        self.llm_client.call.assert_not_called()

    def test_all_start_time_unparseable_skipped_no_timeline(self):
        """Unparseable strings normalize to None via _to_seconds -- must count as
        'no usable timeline' too, not just literal None."""
        segments = make_segments(10)
        for seg in segments:
            seg["start_time"] = "not-a-time"

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.SKIPPED_NO_TIMELINE)
        self.llm_client.call.assert_not_called()

    def test_all_four_segment_time_skipped_no_timeline(self):
        """Malformed four-segment timestamps (e.g. "00:00:00:41") must not be
        silently accepted as a usable timeline -- when every segment uses this
        illegal format, has_any_start_time must resolve False and the LLM must
        never be called, same as the all-None / all-unparseable-string cases."""
        segments = make_segments(10)
        for seg in segments:
            seg["start_time"] = "00:00:00:41"

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.SKIPPED_NO_TIMELINE)
        self.assertEqual(result.chapters, [])
        self.llm_client.call.assert_not_called()

    def test_one_start_time_present_still_generates(self):
        """A single parseable start_time among many None's is enough to proceed --
        the rule is 'at least one', not 'all or nothing'."""
        segments = make_segments(10)
        for seg in segments[1:]:
            seg["start_time"] = None
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.llm_client.call.assert_called_once()

    def test_all_negative_start_time_skipped_no_timeline(self):
        """A fully negative timeline (e.g. a corrupt/relative-offset upstream source)
        must be treated the same as 'no usable timeline' -- not proceed to call the
        LLM and produce a chapter with a negative start_time that contradicts the
        00:00 timestamp shown in the prompt (_format_timestamp clamps to 0)."""
        segments = make_segments(10)
        for i, seg in enumerate(segments):
            seg["start_time"] = -(i + 1)  # every value negative, none valid

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.SKIPPED_NO_TIMELINE)
        self.assertEqual(result.chapters, [])
        self.llm_client.call.assert_not_called()

    def test_single_segment_over_threshold_skipped_no_timeline(self):
        """gate-r14 P2: filtering can leave exactly one usable segment whose
        text alone already exceeds min_chapters_threshold -- structurally
        that can never produce >= _MIN_CHAPTER_COUNT (2) chapters (a single
        block has no internal boundary to split on), so calling the LLM
        would just burn a call on a guaranteed structural-validation FAILED
        (see step 3's chapter-count-bounds check). FAILED is retryable
        under the future tiered-reprocessing semantics, so without this
        gate a single over-long segment would get retried forever for
        nothing. A lone segment offers no navigable structure at all, which
        is semantically equivalent to "no usable timeline" -- so this must
        resolve to SKIPPED_NO_TIMELINE (not FAILED), and the LLM must never
        be called."""
        segments = make_segments(1, text_repeat=50)  # single segment, well over threshold
        self.assertGreater(len(segments[0]["text"]), self.config.min_chapters_threshold)

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.SKIPPED_NO_TIMELINE)
        self.assertEqual(result.chapters, [])
        self.assertEqual(result.segment_count, 1)
        self.assertIsNotNone(result.error)
        self.assertIn("timeline", result.error.lower())
        self.llm_client.call.assert_not_called()

    def test_two_segments_over_threshold_generates_normally(self):
        """Boundary check for the same gate: exactly two usable segments
        must NOT be treated as structurally insufficient -- two segments
        can in principle be split into two chapters, so generation proceeds
        as normal, unaffected by the new gate."""
        segments = make_segments(2, text_repeat=50)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "gist a", "start_seg": 0},
            {"title": "B", "gist": "gist b", "start_seg": 1},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.llm_client.call.assert_called_once()

    def test_too_long_text_failed(self):
        tiny_config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="m", summary_model="s",
            chapters_model="chap-model",
            min_chapters_threshold=10,
            max_chapters_input_chars=50,
        )
        processor = ChaptersProcessor(llm_client=Mock(), config=tiny_config)
        segments = make_segments(5, text_repeat=5)  # far more than 50 chars total
        result = processor.process(segments=segments, title="T")
        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertIsNotNone(result.error)
        self.assertIn("too long", result.error.lower())

    def test_numbering_overhead_pushes_over_max_when_bare_text_is_under(self):
        """max_chapters_input_chars must gate on the actual numbered/timestamped
        prompt text built by _build_segment_lines (what's really sent to the LLM),
        not the bare concatenated segment text. Many short segments make the
        '[i] mm:ss ' prefix overhead dominate: bare text can stay comfortably
        under the cap while the real prompt blows past it."""
        tiny_config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="m", summary_model="s",
            chapters_model="chap-model",
            min_chapters_threshold=50,
            max_chapters_input_chars=1000,
        )
        llm_client = Mock()
        processor = ChaptersProcessor(llm_client=llm_client, config=tiny_config)

        # 200 segments, 2 chars of text each -> bare full_text is only 400 chars,
        # well under the 1000 cap. But each numbered/timestamped line is far
        # longer than the 2-char payload it wraps, so the real prompt text
        # blows well past 1000 chars.
        segments = [
            {"text": "hi", "start_time": float(i * 100), "end_time": float(i * 100 + 50)}
            for i in range(200)
        ]
        full_text_len = sum(len(seg["text"]) for seg in segments)
        self.assertLess(full_text_len, 1000)  # sanity: bare text alone would pass

        result = processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertIsNotNone(result.error)
        self.assertIn("too long", result.error.lower())
        llm_client.call.assert_not_called()

    def test_gate_counts_metadata_length_not_just_segment_lines(self):
        """gate-r21 P3: max_chapters_input_chars previously only measured
        segment_lines, completely ignoring title/author/description -- a
        combination where segment_lines alone fits comfortably under the cap
        but segment_lines + metadata does not must now be caught (before the
        fix, this input would have slipped past the gate and been sent
        straight to the LLM)."""
        segments = make_segments(2, text_repeat=3)
        bare_segment_lines_len = len(_build_segment_lines(_indexed(segments)))
        # A description alone comfortably fits under a cap sized just above
        # segment_lines -- but combined, the two together must not.
        description = "d" * 200
        cap = bare_segment_lines_len + 50
        self.assertLess(bare_segment_lines_len, cap)  # segment_lines alone fits
        self.assertGreater(bare_segment_lines_len + len(description), cap)  # combined does not

        tiny_config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="m", summary_model="s",
            chapters_model="chap-model",
            min_chapters_threshold=10,
            max_chapters_input_chars=cap,
        )
        llm_client = Mock()
        processor = ChaptersProcessor(llm_client=llm_client, config=tiny_config)

        result = processor.process(segments=segments, title="T", description=description)

        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertIsNotNone(result.error)
        self.assertIn("too long", result.error.lower())
        self.assertIn("metadata", result.error.lower())
        llm_client.call.assert_not_called()

    def test_huge_description_is_truncated_before_gate_and_prompt(self):
        """gate-r21 P3: description can come from an external platform and be
        arbitrarily large (real-world descriptions have been observed in the
        hundreds-of-KB range). It must be defensively truncated to a bounded
        length (with a warning logged) BEFORE it participates in the gate-4
        length measurement or gets built into the prompt -- otherwise a
        single oversized description would either make the gate measurement
        meaningless or blow the actual prompt size far past the cap. This
        test picks a cap that comfortably fits segment_lines + a *truncated*
        description, but is far smaller than segment_lines + the raw 600k-char
        description -- isolating the truncation behavior from the
        "metadata is counted at all" behavior covered by the sibling test
        above."""
        segments = make_segments(5)
        bare_segment_lines_len = len(_build_segment_lines(_indexed(segments)))
        cap = bare_segment_lines_len + 3000  # room for title + a truncated (<=2000 char) description
        huge_description = "d" * 600_000
        self.assertGreater(bare_segment_lines_len + len(huge_description), cap)  # raw would fail

        tiny_config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="m", summary_model="s",
            chapters_model="chap-model",
            min_chapters_threshold=10,
            max_chapters_input_chars=cap,
        )
        llm_client = Mock()
        llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "gist a", "start_seg": 0},
            {"title": "B", "gist": "gist b", "start_seg": 3},
        ])
        processor = ChaptersProcessor(llm_client=llm_client, config=tiny_config)

        with patch("video_transcript_api.llm.processors.chapters_processor.logger") as mock_logger:
            result = processor.process(segments=segments, title="T", description=huge_description)

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        llm_client.call.assert_called_once()
        # the actual prompt sent to the LLM must not carry the full 600k-char
        # description verbatim
        user_prompt = llm_client.call.call_args[1]["user_prompt"]
        self.assertLess(len(user_prompt), len(huge_description))
        warning_messages = [str(call.args[0]) for call in mock_logger.warning.call_args_list]
        self.assertTrue(
            any("description" in msg.lower() and "truncat" in msg.lower() for msg in warning_messages),
            f"expected a description truncation warning, got: {warning_messages}",
        )

    def test_description_under_2000_chars_sent_in_full_not_cut_at_old_500_limit(self):
        """gate-r22 P2 regression: build_chapters_user_prompt used to carry its
        own leftover 500-char description truncation from an earlier
        implementation, independent of (and inconsistent with)
        _truncate_description's 2000-char cap and gate 4's length
        measurement (which counts the *post-truncation* description). For a
        501-2000 char description, gate 4 would correctly count the full
        length and let it through, but build_chapters_user_prompt would then
        silently drop everything past char 500 when actually building the
        prompt -- gate measured one thing, the LLM received another. A
        1500-char description must now reach the LLM in full, with no
        internal re-truncation."""
        segments = make_segments(5)
        description = "x" * 1500

        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "gist a", "start_seg": 0},
            {"title": "B", "gist": "gist b", "start_seg": 3},
        ])
        # self.config.max_chapters_input_chars defaults to 100000 -- comfortably
        # fits segment_lines + title + a 1500-char description.
        result = self.processor.process(segments=segments, title="T", description=description)

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        user_prompt = self.llm_client.call.call_args[1]["user_prompt"]
        # all 1500 'x' characters must reach the LLM -- not just the first 500
        self.assertEqual(user_prompt.count("x"), 1500)
        self.assertNotIn("...", user_prompt)

    def test_description_over_2000_chars_truncated_and_gate_counts_truncated_length(self):
        """gate-r22 P2 regression (single-truncation-point invariant): a
        description beyond _DESCRIPTION_MAX_CHARS (2000) must be truncated to
        exactly 2000 chars by _truncate_description -- the *only* truncation
        point -- and gate 4's length measurement must match that same
        2000-char boundary exactly, not the pre-truncation 2500-char raw
        length and not an internal 500-char cut inside
        build_chapters_user_prompt. Proven two ways: (1) a cap sized to fit
        exactly segment_lines + title + a 2000-char description passes and
        the LLM receives exactly 2000 'x' characters; (2) a cap one char
        smaller than that fails the gate -- showing the gate is counting the
        truncated 2000-char length precisely, not something smaller (500) or
        larger (2500)."""
        segments = make_segments(5)
        bare_segment_lines_len = len(_build_segment_lines(_indexed(segments)))
        title = "T"
        description = "x" * 2500
        exact_cap = bare_segment_lines_len + len(title) + 2000

        fitting_llm_client = Mock()
        fitting_llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "gist a", "start_seg": 0},
            {"title": "B", "gist": "gist b", "start_seg": 3},
        ])
        fitting_config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="m", summary_model="s",
            chapters_model="chap-model",
            min_chapters_threshold=10,
            max_chapters_input_chars=exact_cap,
        )
        fitting_processor = ChaptersProcessor(llm_client=fitting_llm_client, config=fitting_config)
        result = fitting_processor.process(segments=segments, title=title, description=description)

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        user_prompt = fitting_llm_client.call.call_args[1]["user_prompt"]
        self.assertEqual(user_prompt.count("x"), 2000)

        too_small_llm_client = Mock()
        too_small_config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="m", summary_model="s",
            chapters_model="chap-model",
            min_chapters_threshold=10,
            max_chapters_input_chars=exact_cap - 1,
        )
        too_small_processor = ChaptersProcessor(llm_client=too_small_llm_client, config=too_small_config)
        result_too_small = too_small_processor.process(segments=segments, title=title, description=description)

        self.assertEqual(result_too_small.status, ChaptersStatus.FAILED)
        too_small_llm_client.call.assert_not_called()


class TestMetadataFlatteningPreventsForgery(ChaptersProcessorTestBase):
    """gate-r21 P2: title/author/description come from external platforms
    (video title/channel name/description) and may contain embedded
    newlines. Left unflattened, they get concatenated straight into the user
    prompt ahead of the numbered segment lines -- and an embedded newline
    followed by something that looks like "[i] mm:ss text" would fork into
    its own line, indistinguishable from a real numbered segment boundary.
    If i happens to be a real surviving segment index, this forged line would
    pass _validate_and_normalize_start_segs' membership check exactly like
    the text/speaker forgery already covered in TestBuildSegmentLines. All
    three fields must be routed through the same _flatten_for_prompt helper
    before reaching build_chapters_user_prompt."""

    @staticmethod
    def _numbered_line_indices(user_prompt):
        """Indices of every line that legitimately starts with "[i]" (a real
        numbered segment line) anywhere in the full prompt -- metadata
        section included, since a forged line would also match this."""
        indices = []
        for line in user_prompt.split("\n"):
            m = re.match(r"^\[(\d+)\]", line)
            if m:
                indices.append(int(m.group(1)))
        return indices

    def test_title_with_embedded_newline_does_not_forge_numbered_line(self):
        segments = make_segments(5)
        forged_title = "My Video\n[2] 00:00 fake forged content"
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 3},
        ])

        result = self.processor.process(segments=segments, title=forged_title)

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        user_prompt = self.llm_client.call.call_args[1]["user_prompt"]
        # exactly one real numbered line per segment, no forged extras/dupes
        self.assertEqual(self._numbered_line_indices(user_prompt), [0, 1, 2, 3, 4])
        # the forged text is preserved, just as flattened inline content
        self.assertIn("[2] 00:00 fake forged content", user_prompt)

    def test_author_with_embedded_newline_does_not_forge_numbered_line(self):
        segments = make_segments(5)
        forged_author = "Some Channel\n[2] 00:00 fake forged content"
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 3},
        ])

        result = self.processor.process(segments=segments, title="T", author=forged_author)

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        user_prompt = self.llm_client.call.call_args[1]["user_prompt"]
        self.assertEqual(self._numbered_line_indices(user_prompt), [0, 1, 2, 3, 4])
        self.assertIn("[2] 00:00 fake forged content", user_prompt)

    def test_description_with_embedded_newline_does_not_forge_numbered_line(self):
        segments = make_segments(5)
        forged_description = "Video about stuff\n[2] 00:00 fake forged content"
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 3},
        ])

        result = self.processor.process(
            segments=segments, title="T", description=forged_description,
        )

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        user_prompt = self.llm_client.call.call_args[1]["user_prompt"]
        self.assertEqual(self._numbered_line_indices(user_prompt), [0, 1, 2, 3, 4])
        self.assertIn("[2] 00:00 fake forged content", user_prompt)

    def test_chinese_metadata_regression_appears_unmodified(self):
        """Normal (non-adversarial) Chinese title/author/description must
        pass through _flatten_for_prompt unchanged -- flattening only
        collapses whitespace runs, and none of these strings contain any."""
        segments = make_segments(5)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 3},
        ])

        result = self.processor.process(
            segments=segments,
            title="中文标题",
            author="中文作者",
            description="这是一段中文简介内容。",
        )

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        user_prompt = self.llm_client.call.call_args[1]["user_prompt"]
        self.assertIn("中文标题", user_prompt)
        self.assertIn("中文作者", user_prompt)
        self.assertIn("这是一段中文简介内容。", user_prompt)
        self.assertEqual(self._numbered_line_indices(user_prompt), [0, 1, 2, 3, 4])


class TestHappyPath(ChaptersProcessorTestBase):
    def test_generates_chapters(self):
        segments = make_segments(10)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "Intro", "gist": "Opening remarks and topic setup.", "start_seg": 0},
            {"title": "Deep Dive", "gist": "Detailed discussion of the topic.", "start_seg": 4},
            {"title": "Wrap Up", "gist": "Summary and closing thoughts.", "start_seg": 8},
        ])

        result = self.processor.process(segments=segments, title="My Video", author="Me", description="desc")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertIsNone(result.error)
        self.assertEqual(len(result.chapters), 3)
        self.assertEqual(result.segment_count, 10)

        ch0, ch1, ch2 = result.chapters
        self.assertEqual(ch0.index, 0)
        self.assertEqual(ch0.start_seg, 0)
        self.assertEqual(ch0.end_seg, 3)
        self.assertEqual(ch1.start_seg, 4)
        self.assertEqual(ch1.end_seg, 7)
        self.assertEqual(ch2.start_seg, 8)
        self.assertEqual(ch2.end_seg, 9)
        # start_time/end_time derived by looking up segments, not from the LLM
        self.assertEqual(ch0.start_time, segments[0]["start_time"])
        self.assertEqual(ch0.end_time, segments[3]["end_time"])
        self.assertEqual(ch2.end_time, segments[9]["end_time"])

        self.llm_client.call.assert_called_once()
        call_kwargs = self.llm_client.call.call_args[1]
        self.assertEqual(call_kwargs.get("task_type"), "chapters")
        self.assertEqual(call_kwargs.get("model"), "test-chapters-model")
        self.assertEqual(call_kwargs.get("force_json_mode"), "json_object")

    def test_selected_models_override_config(self):
        segments = make_segments(10)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "gist a", "start_seg": 0},
            {"title": "B", "gist": "gist b", "start_seg": 5},
        ])

        self.processor.process(
            segments=segments,
            title="T",
            selected_models={"chapters_model": "override-model", "chapters_reasoning_effort": "high"},
        )

        call_kwargs = self.llm_client.call.call_args[1]
        self.assertEqual(call_kwargs.get("model"), "override-model")
        self.assertEqual(call_kwargs.get("reasoning_effort"), "high")


class TestSemanticRetry(ChaptersProcessorTestBase):
    def test_out_of_range_index_retries_then_succeeds(self):
        segments = make_segments(10)
        bad = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 999},  # out of range for N=10
        ])
        good = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        self.llm_client.call.side_effect = [bad, good]

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(len(result.chapters), 2)
        self.assertEqual(self.llm_client.call.call_count, 2)
        # retry prompt must carry the concrete error forward
        retry_call_kwargs = self.llm_client.call.call_args_list[1][1]
        self.assertIn("out of range", retry_call_kwargs["user_prompt"].lower())

    def test_duplicate_index_retry_still_bad_fails(self):
        """A literal duplicate start_seg that, even after dedup (keep-first), still
        leaves a non-increasing tail must fail -- and stay failed if the retry
        repeats the same mistake."""
        segments = make_segments(10)
        bad1 = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
            {"title": "B2", "gist": "g", "start_seg": 5},  # duplicate of B's start_seg
            {"title": "C", "gist": "g", "start_seg": 3},   # dedup -> [0,5,3]: not increasing
        ])
        bad2 = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 6},
            {"title": "B2", "gist": "g", "start_seg": 6},  # duplicate again
            {"title": "C", "gist": "g", "start_seg": 4},   # dedup -> [0,6,4]: still not increasing
        ])
        self.llm_client.call.side_effect = [bad1, bad2]

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertIsNotNone(result.error)
        self.assertEqual(self.llm_client.call.call_count, 2)
        self.assertEqual(result.chapters, [])

    def test_pure_duplicate_collapses_without_retry(self):
        """A duplicate start_seg that fully resolves to an increasing sequence after
        dedup (keep-first) is NOT a validation failure -- the duplicate entry is
        silently dropped, no retry needed."""
        segments = make_segments(10)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
            {"title": "B-dup", "gist": "g", "start_seg": 5},  # exact duplicate, dropped by dedup
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(len(result.chapters), 2)
        self.llm_client.call.assert_called_once()

    def test_non_increasing_retries_then_succeeds(self):
        segments = make_segments(10)
        bad = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 5},
            {"title": "B", "gist": "g", "start_seg": 2},  # decreasing
        ])
        good = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        self.llm_client.call.side_effect = [bad, good]

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(self.llm_client.call.call_count, 2)

    def test_first_index_above_zero_is_clamped_without_retry(self):
        segments = make_segments(10)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 2},  # should be clamped to 0
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(result.chapters[0].start_seg, 0)
        self.llm_client.call.assert_called_once()  # no retry needed, clamping isn't a violation

    def test_illegal_json_retry_exhausted_at_llm_layer_fails(self):
        """llm_client.call() itself raises (llm.py's Self-Correction already exhausted
        its retries and surfaced an error) -- the processor must not crash, just FAILED."""
        segments = make_segments(10)
        self.llm_client.call.side_effect = Exception("json_object call failed: JSON parse failed")

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.chapters, [])


class TestTitleGistValidation(ChaptersProcessorTestBase):
    """title/gist must be non-empty strings after stripping -- a missing/non-string/
    blank value used to be silently coerced to "" and still became a chapter. Now
    it is treated the same as a bad start_seg: retry once with the error, FAILED
    if still bad."""

    def test_missing_title_retries_then_fails_if_still_bad(self):
        segments = make_segments(10)
        bad = mock_llm_response([
            {"gist": "g", "start_seg": 0},  # title key missing entirely
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        self.llm_client.call.side_effect = [bad, bad]

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertEqual(self.llm_client.call.call_count, 2)
        self.assertEqual(result.chapters, [])
        retry_prompt = self.llm_client.call.call_args_list[1][1]["user_prompt"]
        self.assertIn("title", retry_prompt.lower())

    def test_blank_gist_retries_then_fails_if_still_bad(self):
        segments = make_segments(10)
        bad = mock_llm_response([
            {"title": "A", "gist": "   ", "start_seg": 0},  # whitespace-only gist
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        self.llm_client.call.side_effect = [bad, bad]

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertEqual(self.llm_client.call.call_count, 2)
        self.assertEqual(result.chapters, [])

    def test_numeric_title_retries_then_fails_if_still_bad(self):
        """title must be a str type, not merely str()-coercible."""
        segments = make_segments(10)
        bad = mock_llm_response([
            {"title": 123, "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        self.llm_client.call.side_effect = [bad, bad]

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertEqual(self.llm_client.call.call_count, 2)
        self.assertEqual(result.chapters, [])

    def test_bad_title_gist_retry_succeeds_when_corrected(self):
        segments = make_segments(10)
        bad = mock_llm_response([
            {"title": "", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        good = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        self.llm_client.call.side_effect = [bad, good]

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(self.llm_client.call.call_count, 2)
        self.assertEqual(result.chapters[0].title, "A")


class TestRetryHintBounding(ChaptersProcessorTestBase):
    """gate-r24 P2: the semantic-validation error message echoes the LLM's own
    illegal title/gist value verbatim (via repr) -- this message becomes
    retry_hint on the second attempt. Gate 4 (max_chapters_input_chars) only
    measures the FIRST prompt; if the LLM's first response returns a huge
    garbage value there, the echoed value can make the second prompt balloon
    past the configured cap, silently bypassing the input-size safety limit.
    Both the per-value repr (_truncate_repr_for_hint) and the overall hint
    (_build_retry_hint) must now be bounded and flattened."""

    def test_huge_blank_title_does_not_blow_up_retry_prompt(self):
        """A first response whose title is 500k whitespace characters (blank
        after strip, so it fails _validate_and_normalize_start_segs' title
        check) must not let that 500k-char value ride along into the retry
        prompt -- the retry prompt must stay close to the first prompt's
        size, not balloon by ~500,000 chars."""
        segments = make_segments(10)
        huge_garbage_title = " " * 500_000  # blank after strip -> invalid
        bad = mock_llm_response([
            {"title": huge_garbage_title, "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        good = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        self.llm_client.call.side_effect = [bad, good]

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(self.llm_client.call.call_count, 2)

        first_prompt = self.llm_client.call.call_args_list[0][1]["user_prompt"]
        retry_prompt = self.llm_client.call.call_args_list[1][1]["user_prompt"]

        # bounded to first_prompt length + a small fixed hint budget (well
        # under a 500,000-char blowup) -- proves gate 4's input-size cap on
        # the first prompt cannot be bypassed via the retry hint.
        self.assertLess(len(retry_prompt), len(first_prompt) + 2000)
        self.assertNotIn(huge_garbage_title, retry_prompt)

    def test_huge_non_string_gist_does_not_blow_up_retry_prompt(self):
        """Same bypass, different embed point: gist that isn't a string at
        all (so isinstance(..., str) fails) with a huge repr."""
        segments = make_segments(10)
        bad = mock_llm_response([
            {"title": "A", "gist": ["g"] * 200_000, "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        good = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        self.llm_client.call.side_effect = [bad, good]

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        first_prompt = self.llm_client.call.call_args_list[0][1]["user_prompt"]
        retry_prompt = self.llm_client.call.call_args_list[1][1]["user_prompt"]
        self.assertLess(len(retry_prompt), len(first_prompt) + 2000)

    def test_normal_retry_hint_still_carries_meaningful_error_text(self):
        """Regression: bounding/flattening must not gut ordinary (short)
        validation error messages -- the out-of-range detail must still
        reach the retry prompt intact."""
        segments = make_segments(10)
        bad = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 999},
        ])
        good = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        self.llm_client.call.side_effect = [bad, good]

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        retry_prompt = self.llm_client.call.call_args_list[1][1]["user_prompt"]
        self.assertIn("out of range", retry_prompt.lower())

    def test_truncate_repr_for_hint_bounds_and_marks_truncation(self):
        huge = "g" * 10_000
        result = _truncate_repr_for_hint(huge)
        self.assertLessEqual(len(result), _RETRY_HINT_VALUE_MAX_CHARS)
        self.assertTrue(result.endswith("…"))

    def test_truncate_repr_for_hint_leaves_short_values_untouched(self):
        result = _truncate_repr_for_hint("short")
        self.assertEqual(result, repr("short"))

    def test_build_retry_hint_bounds_overall_length(self):
        huge_error = "chapters[0].title is missing: " + ("g" * 10_000)
        hint = _build_retry_hint(huge_error)
        self.assertLessEqual(len(hint), _RETRY_HINT_MAX_CHARS)
        self.assertTrue(hint.endswith("…"))

    def test_build_retry_hint_flattens_embedded_newlines(self):
        """Defense in depth: the error message itself is untrusted external
        content once it echoes LLM-controlled values -- embedded newlines
        must not survive into the retry_hint (same forging risk as
        title/author/description/text/speaker, see _flatten_for_prompt)."""
        error_with_newline = "chapters[0].title is missing\n[2] 00:00 fake forged content"
        hint = _build_retry_hint(error_with_newline)
        self.assertNotIn("\n", hint)
        self.assertIn("fake forged content", hint)

    def test_validate_and_normalize_embeds_truncated_repr_directly(self):
        """Unit-level coverage of the first defense layer, independent of the
        full processor pipeline: _validate_and_normalize_start_segs itself
        must not embed an unbounded repr of the illegal value."""
        huge_garbage_title = " " * 500_000
        raw_chapters = [
            {"title": huge_garbage_title, "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ]
        normalized, error = _validate_and_normalize_start_segs(raw_chapters, list(range(10)))
        self.assertIsNone(normalized)
        self.assertIsNotNone(error)
        self.assertLess(len(error), 500)  # nowhere near the 500,000-char raw value


class TestStartSegTypeCoercion(unittest.TestCase):
    """Real-sample finding (T8 manual verification): deepseek-chat returned
    later chapters' start_seg as digit STRINGS ('1676'), and the strict
    isinstance-int check failed the whole chapters run after both attempts.
    The validator now coerces integer-valued strings/floats while keeping
    non-integral/non-numeric values as hard failures."""

    def test_digit_string_coerced_and_validated(self):
        raw = [
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": "5"},
        ]
        normalized, error = _validate_and_normalize_start_segs(raw, list(range(10)))
        self.assertIsNone(error)
        self.assertEqual([c["start_seg"] for c in normalized], [0, 5])
        self.assertIs(type(normalized[1]["start_seg"]), int)

    def test_integral_float_coerced(self):
        raw = [
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 3.0},
        ]
        normalized, error = _validate_and_normalize_start_segs(raw, list(range(10)))
        self.assertIsNone(error)
        self.assertEqual([c["start_seg"] for c in normalized], [0, 3])
        self.assertIs(type(normalized[1]["start_seg"]), int)

    def test_bool_still_rejected(self):
        raw = [{"title": "A", "gist": "g", "start_seg": True}]
        normalized, error = _validate_and_normalize_start_segs(raw, list(range(10)))
        self.assertIsNone(normalized)
        self.assertIn("not an int", error)

    def test_non_numeric_string_still_rejected(self):
        for bad in ("abc", "12.5", "", None):
            raw = [{"title": "A", "gist": "g", "start_seg": bad}]
            normalized, error = _validate_and_normalize_start_segs(raw, list(range(10)))
            self.assertIsNone(normalized, f"should reject {bad!r}")
            self.assertIn("not an int", error)

    def test_coerced_value_still_checked_against_survived_indices(self):
        raw = [{"title": "A", "gist": "g", "start_seg": "7"}]
        normalized, error = _validate_and_normalize_start_segs(raw, [0, 2, 4])
        self.assertIsNone(normalized)
        self.assertIn("out of range", error)


class TestFinalErrorRawChaptersBounding(ChaptersProcessorTestBase):
    """gate-r27 P3: when both attempts (first + the single semantic retry)
    exhaust without producing a valid chapters list, `_process_impl` writes
    the LAST attempt's raw_chapters into the FAILED result's error field
    (and the accompanying error log) to aid debugging. This is a separate
    code path from TestRetryHintBounding above -- that class covers the
    *validation_error* string embedded mid-pipeline (bounded per-value via
    _truncate_repr_for_hint inside _validate_and_normalize_start_segs, and
    it always returns on the FIRST illegal field so it only ever carries one
    truncated value). This class covers the outer `_process_impl` call site
    that used to concatenate the ENTIRE raw_chapters repr with no bound at
    all -- if the LLM returns the same huge garbage value on both attempts,
    the untruncated repr used to make result.error (and the error log)
    balloon to the same size as the garbage payload."""

    def test_two_huge_garbage_attempts_yield_bounded_final_error_and_log(self):
        """Both attempts return a chapters list containing a 500k-char blank
        title (invalid -- fails the non-empty-after-strip check both times).
        The retry is exhausted, so the processor gives up with FAILED. The
        resulting error string (and the error-level log line built from it)
        must stay bounded to roughly the _FINAL_ERROR_RAW_CHAPTERS_MAX_CHARS
        budget, nowhere near the 500,000-char raw payload -- and must not
        contain the full garbage text verbatim."""
        segments = make_segments(10)
        huge_garbage_title = " " * 500_000  # blank after strip -> invalid every time
        bad = mock_llm_response([
            {"title": huge_garbage_title, "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        self.llm_client.call.side_effect = [bad, bad]

        with patch("video_transcript_api.llm.processors.chapters_processor.logger") as mock_logger:
            result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertEqual(self.llm_client.call.call_count, 2)
        self.assertEqual(result.chapters, [])
        self.assertIsNotNone(result.error)

        # Nowhere near the 500,000-char garbage payload -- bounded to the
        # validation-error prefix (already <500 chars, see
        # TestRetryHintBounding.test_validate_and_normalize_embeds_truncated_repr_directly)
        # plus the truncated raw-chapters sample (<= _FINAL_ERROR_RAW_CHAPTERS_MAX_CHARS).
        self.assertLess(
            len(result.error), _FINAL_ERROR_RAW_CHAPTERS_MAX_CHARS + 500
        )
        self.assertNotIn(huge_garbage_title, result.error)

        error_log_messages = [str(call.args[0]) for call in mock_logger.error.call_args_list]
        self.assertTrue(error_log_messages)
        for msg in error_log_messages:
            self.assertLess(len(msg), _FINAL_ERROR_RAW_CHAPTERS_MAX_CHARS + 500)
            self.assertNotIn(huge_garbage_title, msg)


class TestTitleTruncation(ChaptersProcessorTestBase):
    """Prompt declares title <=20 chars but this is not enforced upstream by
    the LLM. Rather than treating an overlong title as a semantic-validation
    failure (triggering a retry, or FAILED if the retry is also overlong --
    not worth it for a cosmetic length issue), it is soft-normalized locally:
    stripped, then truncated to 23 chars + "..." if still over 24 chars.
    Status/retry behavior is unaffected; only a warning is logged."""

    def test_overlong_title_truncated_and_logs_warning(self):
        segments = make_segments(10)
        long_title = "x" * 50
        self.llm_client.call.return_value = mock_llm_response([
            {"title": long_title, "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        with patch("video_transcript_api.llm.processors.chapters_processor.logger") as mock_logger:
            result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(self.llm_client.call.call_count, 1)  # no retry triggered
        truncated = result.chapters[0].title
        self.assertEqual(truncated, ("x" * 23) + "…")
        self.assertEqual(len(truncated), 24)
        warning_messages = [str(call.args[0]) for call in mock_logger.warning.call_args_list]
        self.assertTrue(
            any("title" in msg.lower() and "truncat" in msg.lower() for msg in warning_messages),
            f"expected a title truncation warning, got: {warning_messages}",
        )

    def test_title_at_boundary_not_truncated(self):
        """Exactly 24 chars after strip is within bounds -- must pass through
        unchanged (only strictly-greater-than-24 triggers truncation)."""
        segments = make_segments(10)
        boundary_title = "y" * 24
        self.llm_client.call.return_value = mock_llm_response([
            {"title": boundary_title, "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(result.chapters[0].title, boundary_title)

    def test_different_long_titles_sharing_truncated_prefix_are_not_merged(self):
        """Two DIFFERENT full-length titles that happen to share the same
        first 23 chars must be compared (for adjacent-same-title merging)
        BEFORE truncation, not after. If truncation ran first, both would
        collapse to the identical 24-char string and get wrongly merged --
        losing one chapter's worth of content (and potentially dropping the
        result below the 2-chapter minimum, forcing a bogus FAILED)."""
        segments = make_segments(10)
        common_prefix = "X" * 23
        title1 = common_prefix + "11"  # 25 chars, first 23 == common_prefix
        title2 = common_prefix + "22"  # 25 chars, first 23 == common_prefix, differs after
        self.llm_client.call.return_value = mock_llm_response([
            {"title": title1, "gist": "part one", "start_seg": 0},
            {"title": title2, "gist": "part two", "start_seg": 5},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(len(result.chapters), 2)  # must NOT be merged into 1
        expected_truncated = common_prefix + "…"
        self.assertEqual(result.chapters[0].title, expected_truncated)
        self.assertEqual(result.chapters[1].title, expected_truncated)
        # Each chapter keeps its own gist -- proof they were never merged.
        self.assertEqual(result.chapters[0].gist, "part one")
        self.assertEqual(result.chapters[1].gist, "part two")
        self.assertEqual(result.chapters[0].start_seg, 0)
        self.assertEqual(result.chapters[1].start_seg, 5)

    def test_truly_identical_long_titles_still_merge_after_truncation(self):
        """Sanity check for the other direction: two adjacent chapters with the
        exact same overlong title must still merge normally -- truncation
        happening after merge (instead of before) must not break this case.
        A third, distinct chapter keeps the post-merge count at 2 (at the
        minimum), isolating this from the below-minimum-after-merge case
        covered separately in TestStructuralValidation."""
        segments = make_segments(12)
        long_title = "z" * 30
        self.llm_client.call.return_value = mock_llm_response([
            {"title": long_title, "gist": "part one", "start_seg": 0},
            {"title": long_title, "gist": "part two", "start_seg": 4},
            {"title": "Topic B", "gist": "different topic", "start_seg": 8},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(len(result.chapters), 2)  # the first two merged into one
        self.assertEqual(result.chapters[0].title, ("z" * 23) + "…")
        self.assertIn("part one", result.chapters[0].gist)
        self.assertIn("part two", result.chapters[0].gist)
        self.assertEqual(result.chapters[1].title, "Topic B")


class TestStructuralValidation(ChaptersProcessorTestBase):
    def test_too_few_chapters_fails(self):
        segments = make_segments(10)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "Only", "gist": "g", "start_seg": 0},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertIn("count", result.error.lower())

    def test_too_many_chapters_fails(self):
        segments = make_segments(150, text_repeat=2)
        chapters = [{"title": f"C{i}", "gist": "g", "start_seg": i} for i in range(101)]
        self.llm_client.call.return_value = mock_llm_response(chapters)

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertIn("count", result.error.lower())

    def test_merge_collapsing_below_minimum_fails(self):
        """2 chapters pass the pre-merge count check ([2, 100]), but they share the
        same title so adjacent-merge collapses them into 1 -- the 'at least 2
        chapters' invariant must be re-checked AFTER merging, not just before."""
        segments = make_segments(10)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "Same Topic", "gist": "part one", "start_seg": 0},
            {"title": "Same Topic", "gist": "part two", "start_seg": 5},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertEqual(result.chapters, [])
        self.assertIsNotNone(result.error)
        self.assertIn("合并后不足两章", result.error)


class TestDensityWarning(ChaptersProcessorTestBase):
    def test_short_average_duration_logs_warning_but_keeps_result(self):
        # 10 segments, 30s apart, one chapter per segment -> avg chapter duration
        # is exactly 30s, below the 60s floor. Logger is loguru's process-wide
        # singleton (setup_logger always returns the same object regardless of
        # `name`), so it's patched at the module reference rather than captured
        # via stdlib logging/caplog (see test_llm_ops_title_generation.py for
        # the same pattern used elsewhere in this codebase).
        segments = make_segments(10, seconds_per_seg=30)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": f"C{i}", "gist": "g", "start_seg": i} for i in range(10)
        ])

        with patch("video_transcript_api.llm.processors.chapters_processor.logger") as mock_logger:
            result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        warning_messages = [str(call.args[0]) for call in mock_logger.warning.call_args_list]
        self.assertTrue(
            any("duration" in msg.lower() for msg in warning_messages),
            f"expected a duration warning, got: {warning_messages}",
        )


class TestMergeAdjacentSameTitle(ChaptersProcessorTestBase):
    def test_adjacent_same_title_merged(self):
        segments = make_segments(12)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "Topic A", "gist": "part one", "start_seg": 0},
            {"title": "Topic A", "gist": "part two", "start_seg": 4},
            {"title": "Topic B", "gist": "different topic", "start_seg": 8},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(len(result.chapters), 2)
        self.assertEqual(result.chapters[0].title, "Topic A")
        self.assertEqual(result.chapters[0].start_seg, 0)
        self.assertEqual(result.chapters[0].end_seg, 7)  # extended to absorb the merged chapter
        self.assertIn("part one", result.chapters[0].gist)
        self.assertIn("part two", result.chapters[0].gist)
        self.assertEqual(result.chapters[1].title, "Topic B")


class TestTimeHandling(ChaptersProcessorTestBase):
    def test_none_time_is_tolerated(self):
        segments = make_segments(10)
        segments[0]["start_time"] = None
        segments[9]["end_time"] = None
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertIsNone(result.chapters[0].start_time)
        self.assertIsNone(result.chapters[1].end_time)

    def test_process_does_not_raise_on_overflow_time_value(self):
        """A JSON-legal but astronomically large integer time value (e.g.
        10**400 surviving upstream deserialization) must not blow up process()
        with an uncaught OverflowError -- it degrades to None like any other
        unparseable time, same as the negative/None cases in this class."""
        segments = make_segments(10)
        segments[3]["start_time"] = 10 ** 400
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        result = self.processor.process(segments=segments, title="T")  # must not raise

        self.assertEqual(result.status, ChaptersStatus.GENERATED)

    def test_mixed_negative_and_valid_time_negative_entry_becomes_none(self):
        """One negative start_time among otherwise-valid segments must not abort
        the whole run (has_any_start_time is still True via the valid entries),
        but the negative entry itself must resolve to None rather than a bogus
        negative timestamp."""
        segments = make_segments(10)
        segments[0]["start_time"] = -5.0  # segment 0 is chapter 0's start_seg
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertIsNone(result.chapters[0].start_time)

    def test_hhmmss_string_time_is_converted(self):
        segments = make_segments(10, use_string_time=True)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        # segment 0 start = "00:00:00" -> 0.0 seconds
        self.assertEqual(result.chapters[0].start_time, 0.0)
        # chapter 0 end_seg = 4 (start_seg[1]=5 -> 5-1=4) -> segments[4]["end_time"]
        # = "00:15:00" (4*180+180=900s), confirming HH:MM:SS strings parse correctly.
        self.assertEqual(result.chapters[0].end_time, 900.0)

    def test_end_time_before_start_time_becomes_none_with_warning(self):
        """Out-of-order / corrupt upstream segment timestamps can make a
        derived chapter's end_time earlier than its own start_time -- an
        illegal interval. Honest degradation: discard end_time (set to None)
        and log a warning, but keep status GENERATED and do not reorder
        segments (index order stays the body's order)."""
        segments = make_segments(10)
        segments[0]["start_time"] = 500.0  # chapter 0's start_seg (index 0)
        segments[4]["end_time"] = 100.0    # chapter 0's end_seg (index 4) -- earlier
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        with patch(
            "video_transcript_api.llm.processors.chapters_processor.logger"
        ) as mock_logger:
            result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(result.chapters[0].start_time, 500.0)
        self.assertIsNone(result.chapters[0].end_time)
        # segments themselves must not be reordered -- start_seg/end_seg
        # indices are unchanged from what the LLM/derivation produced.
        self.assertEqual(result.chapters[0].start_seg, 0)
        self.assertEqual(result.chapters[0].end_seg, 4)
        warning_messages = [str(call.args[0]) for call in mock_logger.warning.call_args_list]
        self.assertTrue(
            any("end_time" in msg and "start_time" in msg for msg in warning_messages),
            f"expected an end_time-before-start_time warning, got: {warning_messages}",
        )

    def test_ascending_times_not_affected_by_end_before_start_guard(self):
        """Sanity check for the other direction: normal ascending times must
        pass through completely unaffected by the new guard."""
        segments = make_segments(10)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(result.chapters[0].start_time, 0.0)
        self.assertEqual(result.chapters[0].end_time, 900.0)
        self.assertIsNotNone(result.chapters[1].end_time)


class TestSegmentLevelTimeInversion(ChaptersProcessorTestBase):
    """gate-r23 P2: the processor may receive dialogs straight from a caller
    that never ran them through transcriber.segments.normalize_segments --
    so a single surviving segment's OWN start_time/end_time can be inverted
    (e.g. start=240, end=200) even though every OTHER segment (including the
    chapter's start_seg segment) is perfectly ordered. Before the fix, only
    the chapter-level combination (start_seg's start_time vs end_seg's
    end_time) was checked by _sanitize_end_time -- so a chapter starting at
    a small time (e.g. 0.0) would let a later segment's already-self-broken
    end_time (200) sail through, because "0 < 200" alone looks fine. The fix
    runs the shared sanitize_time_pair per surviving segment right at the
    entry, so a segment's own end_time is already None by the time any
    chapter looks it up."""

    def test_segment_self_inverted_end_time_becomes_none_even_when_chapter_level_check_would_pass(self):
        segments = make_segments(10)
        # segment 4 is internally inverted: its own end_time (200) is before
        # its own start_time (240). Chapter 0 (start_seg=0, start_time=0.0)
        # uses segment 4 as its end_seg -- the OLD chapter-level check
        # ("0.0 < 200" passes) would have let end_time=200.0 through.
        segments[4]["start_time"] = 240.0
        segments[4]["end_time"] = 200.0
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        with patch(
            "video_transcript_api.llm.processors.chapters_processor.logger"
        ) as mock_logger:
            result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(result.chapters[0].start_seg, 0)
        self.assertEqual(result.chapters[0].end_seg, 4)
        self.assertEqual(result.chapters[0].start_time, 0.0)
        # Must be None, NOT 200.0 -- segment 4's own end_time was already
        # discarded at the entry sanitization step before any chapter-level
        # check ever ran.
        self.assertIsNone(result.chapters[0].end_time)
        warning_messages = [str(call.args[0]) for call in mock_logger.warning.call_args_list]
        self.assertTrue(
            any("end_time" in msg and "start_time" in msg for msg in warning_messages),
            f"expected a segment-level time inversion warning, got: {warning_messages}",
        )

    def test_normal_ascending_segment_times_regression_unaffected(self):
        """Regression check: entry-level per-segment sanitize_time_pair must
        be a no-op for well-formed, ascending segment times -- chapters
        derive exactly the same start_time/end_time as before this fix."""
        segments = make_segments(10)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(result.chapters[0].start_time, 0.0)
        self.assertEqual(result.chapters[0].end_time, 900.0)  # segments[4]["end_time"]
        self.assertEqual(result.chapters[1].start_time, 900.0)  # segments[5]["start_time"]
        self.assertEqual(result.chapters[1].end_time, 1800.0)  # segments[9]["end_time"]


class TestFingerprint(ChaptersProcessorTestBase):
    def test_fingerprint_is_stable_across_calls(self):
        segments = make_segments(10)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        result1 = self.processor.process(segments=segments, title="T1")
        result2 = self.processor.process(segments=segments, title="T2 (different title, same segments)")

        self.assertIsNotNone(result1.fingerprint)
        self.assertEqual(result1.fingerprint, result2.fingerprint)

    def test_fingerprint_changes_with_content(self):
        segments_a = make_segments(10)
        segments_b = make_segments(10)
        segments_b[0]["text"] = "completely different opening text here " * 20

        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        result_a = self.processor.process(segments=segments_a, title="T")
        result_b = self.processor.process(segments=segments_b, title="T")

        self.assertNotEqual(result_a.fingerprint, result_b.fingerprint)

    def test_fingerprint_uses_separator_between_segment_texts(self):
        """Fingerprint must not be a naive concatenation of segment texts --
        ["ab", "c"] and ["a", "bc"] concatenate to the same string "abc" and
        would collide under plain "".join(). An invisible separator (e.g.
        "\\x1f") between segments makes the two groupings distinguishable."""
        segs_ab_c = [
            {"text": "ab", "start_time": 0.0, "end_time": 1.0},
            {"text": "c", "start_time": 1.0, "end_time": 2.0},
        ]
        segs_a_bc = [
            {"text": "a", "start_time": 0.0, "end_time": 1.0},
            {"text": "bc", "start_time": 1.0, "end_time": 2.0},
        ]

        result1 = self.processor.process(segments=segs_ab_c, title="T")
        result2 = self.processor.process(segments=segs_a_bc, title="T")

        self.assertIsNotNone(result1.fingerprint)
        self.assertIsNotNone(result2.fingerprint)
        self.assertNotEqual(result1.fingerprint, result2.fingerprint)

    def test_fingerprint_stable_across_calls_with_separator(self):
        """Same segment grouping (not just same concatenated text) must still
        produce a stable fingerprint across repeated calls."""
        segs = [
            {"text": "ab", "start_time": 0.0, "end_time": 1.0},
            {"text": "c", "start_time": 1.0, "end_time": 2.0},
        ]

        result1 = self.processor.process(segments=segs, title="T1")
        result2 = self.processor.process(segments=segs, title="T2 (different title)")

        self.assertEqual(result1.fingerprint, result2.fingerprint)

    def test_fingerprint_changes_when_only_timestamps_differ(self):
        """Same segment text but a corrected start_time/end_time (e.g. a
        timestamp-fixup pass ran, text untouched) must still change the
        fingerprint. If the fingerprint only hashed text, this would collide
        and a downstream cache keyed on fingerprint could keep serving
        chapters annotated with the stale start_time/end_time."""
        segs_original = [
            {"text": "same text content here", "start_time": 0.0, "end_time": 10.0},
        ]
        segs_time_fixed = [
            {"text": "same text content here", "start_time": 3.0, "end_time": 10.0},
        ]

        result_original = self.processor.process(segments=segs_original, title="T")
        result_time_fixed = self.processor.process(segments=segs_time_fixed, title="T")

        self.assertIsNotNone(result_original.fingerprint)
        self.assertIsNotNone(result_time_fixed.fingerprint)
        self.assertNotEqual(result_original.fingerprint, result_time_fixed.fingerprint)

    def test_fingerprint_stable_for_identical_input_including_times(self):
        """Fully identical input (same text AND same start_time/end_time)
        across repeated calls must still produce the exact same fingerprint
        now that timestamps are part of the hashed input."""
        segs = [
            {"text": "stable text", "start_time": 12.5, "end_time": 20.0},
            {"text": "more stable text", "start_time": 20.0, "end_time": 30.0},
        ]

        result1 = self.processor.process(segments=segs, title="T1")
        result2 = self.processor.process(segments=segs, title="T2 (different title)")

        self.assertIsNotNone(result1.fingerprint)
        self.assertEqual(result1.fingerprint, result2.fingerprint)

    def test_fingerprint_distinguishes_none_time_from_zero_time(self):
        """start_time/end_time of None must not collide with start_time/
        end_time of 0 -- the two carry very different meaning ("no usable
        timeline at all" vs "timeline starts at second 0") and must hash to
        different fingerprints. This locks in the fixed placeholder used for
        None instead of e.g. an empty string, which could otherwise coincide
        with some numeric string in degenerate cases."""
        segs_none_time = [
            {"text": "same text content here", "start_time": None, "end_time": None},
        ]
        segs_zero_time = [
            {"text": "same text content here", "start_time": 0.0, "end_time": 0.0},
        ]

        result_none_time = self.processor.process(segments=segs_none_time, title="T")
        result_zero_time = self.processor.process(segments=segs_zero_time, title="T")

        self.assertIsNotNone(result_none_time.fingerprint)
        self.assertIsNotNone(result_zero_time.fingerprint)
        self.assertNotEqual(result_none_time.fingerprint, result_zero_time.fingerprint)

    def test_fingerprint_changes_with_speaker_correction(self):
        """Same text and same start_time/end_time, but a corrected speaker
        label (e.g. "Speaker 2" fixed to a real name after diarization
        correction) must still change the fingerprint. Speaker is part of
        what gets sent to the LLM (`[i] mm:ss speaker: text`), so if the
        fingerprint only hashed text + times, a speaker-only correction
        would collide and a downstream cache keyed on fingerprint could
        keep serving chapter gists that reference the stale speaker."""
        segs_original = [
            {"text": "same text content here", "start_time": 0.0, "end_time": 10.0,
             "speaker": "Speaker 2"},
        ]
        segs_speaker_fixed = [
            {"text": "same text content here", "start_time": 0.0, "end_time": 10.0,
             "speaker": "Alice"},
        ]

        result_original = self.processor.process(segments=segs_original, title="T")
        result_speaker_fixed = self.processor.process(segments=segs_speaker_fixed, title="T")

        self.assertIsNotNone(result_original.fingerprint)
        self.assertIsNotNone(result_speaker_fixed.fingerprint)
        self.assertNotEqual(result_original.fingerprint, result_speaker_fixed.fingerprint)

    def test_fingerprint_uses_raw_speaker_unaffected_by_prompt_flattening(self):
        """gate-r18 P2: `_build_segment_lines` now flattens embedded
        whitespace in `speaker` before it goes into the prompt, but the
        fingerprint must keep hashing the raw, un-flattened speaker value --
        per this module's own docstring, the fingerprint "如实反映输入内容
        本身，不应因展示形态的处理而变化". A speaker with an embedded
        whitespace run must therefore still produce a *different*
        fingerprint than a speaker whose value is already the flattened
        result -- if the fingerprint went through the same flattening, the
        two would collide and a downstream cache would be unable to tell
        "speaker was corrected from a messy raw value" from "no change"."""
        segs_raw_whitespace_speaker = [
            {"text": "same text content here", "start_time": 0.0, "end_time": 10.0,
             "speaker": "Alice\n\n  Bob"},
        ]
        segs_already_flattened_speaker = [
            {"text": "same text content here", "start_time": 0.0, "end_time": 10.0,
             "speaker": "Alice Bob"},
        ]

        result_raw = self.processor.process(segments=segs_raw_whitespace_speaker, title="T")
        result_flattened = self.processor.process(segments=segs_already_flattened_speaker, title="T")

        self.assertIsNotNone(result_raw.fingerprint)
        self.assertIsNotNone(result_flattened.fingerprint)
        self.assertNotEqual(result_raw.fingerprint, result_flattened.fingerprint)

    def test_fingerprint_distinguishes_missing_speaker_from_empty_speaker(self):
        """A segment with no "speaker" key at all must not collide with one
        whose speaker is an explicit empty string -- same rationale as the
        None-time vs zero-time distinction: "no speaker info" and "speaker
        is blank" are different states and must hash to different
        fingerprints (locks in the fixed placeholder for missing speaker)."""
        segs_missing_speaker = [
            {"text": "same text content here", "start_time": 0.0, "end_time": 10.0},
        ]
        segs_empty_speaker = [
            {"text": "same text content here", "start_time": 0.0, "end_time": 10.0,
             "speaker": ""},
        ]

        result_missing = self.processor.process(segments=segs_missing_speaker, title="T")
        result_empty = self.processor.process(segments=segs_empty_speaker, title="T")

        self.assertIsNotNone(result_missing.fingerprint)
        self.assertIsNotNone(result_empty.fingerprint)
        self.assertNotEqual(result_missing.fingerprint, result_empty.fingerprint)

    def test_fingerprint_distinguishes_speaker_zero_from_missing_speaker(self):
        """speaker=0 (a legitimate first-speaker diarization label, not
        "no speaker") must produce a different fingerprint than a segment
        with no speaker key at all -- same falsy-vs-absent pitfall as
        `_build_segment_lines`'s `if speaker:` bug, but on the fingerprint
        side. `_compute_fingerprint` already guards this correctly with
        `if speaker is not None:`; this test locks that in."""
        segs_missing_speaker = [
            {"text": "same text content here", "start_time": 0.0, "end_time": 10.0},
        ]
        segs_speaker_zero = [
            {"text": "same text content here", "start_time": 0.0, "end_time": 10.0,
             "speaker": 0},
        ]

        result_missing = self.processor.process(segments=segs_missing_speaker, title="T")
        result_speaker_zero = self.processor.process(segments=segs_speaker_zero, title="T")

        self.assertIsNotNone(result_missing.fingerprint)
        self.assertIsNotNone(result_speaker_zero.fingerprint)
        self.assertNotEqual(result_missing.fingerprint, result_speaker_zero.fingerprint)

    def test_fingerprint_distinguishes_explicit_null_byte_speaker_from_missing(self):
        """The old fixed placeholder for "missing speaker" was literally the
        string "\\x00" -- a segment with an actual speaker value of "\\x00"
        (adversarial/corrupted upstream data) would collide with an entry
        that has no speaker key at all, since both serialized to the exact
        same placeholder token. The fix must keep these distinguishable by
        construction (e.g. presence/absence of a dict key), not by picking a
        different placeholder string that just moves the same class of bug
        to a new unlucky value."""
        segs_null_byte_speaker = [
            {"text": "same text content here", "start_time": 0.0, "end_time": 10.0,
             "speaker": "\x00"},
        ]
        segs_missing_speaker = [
            {"text": "same text content here", "start_time": 0.0, "end_time": 10.0},
        ]

        result_null_byte = self.processor.process(segments=segs_null_byte_speaker, title="T")
        result_missing = self.processor.process(segments=segs_missing_speaker, title="T")

        self.assertIsNotNone(result_null_byte.fingerprint)
        self.assertIsNotNone(result_missing.fingerprint)
        self.assertNotEqual(result_null_byte.fingerprint, result_missing.fingerprint)

    def test_fingerprint_field_separator_injection_no_longer_collides(self):
        """Old bug: raw delimiter-joined fingerprint strings have no
        escaping, so a single segment whose speaker embeds both internal
        separator characters (entry-sep "\\x1f", field-sep "\\x1e") can
        produce a byte-identical fingerprint source to an unrelated
        two-segment sequence -- the field boundaries "blur" into what looks
        like a second entry:

            1 segment, speaker="X\\x1fworld\\x1e2.0\\x1e3.0\\x1eY"
            -> old source: "hello\\x1e0.0\\x1e1.0\\x1eX\\x1fworld\\x1e2.0\\x1e3.0\\x1eY"

            2 segments ("hello"/speaker X, "world"/speaker Y)
            -> old source: "hello\\x1e0.0\\x1e1.0\\x1eX" + "\\x1f" +
                            "world\\x1e2.0\\x1e3.0\\x1eY"
            -> byte-identical to the line above.

        A structured JSON array (each segment its own escaped array element)
        cannot collide this way -- must no longer produce the same
        fingerprint."""
        segs_single_crafted = [
            {"text": "hello", "start_time": 0.0, "end_time": 1.0,
             "speaker": "X\x1fworld\x1e2.0\x1e3.0\x1eY"},
        ]
        segs_two_clean = [
            {"text": "hello", "start_time": 0.0, "end_time": 1.0, "speaker": "X"},
            {"text": "world", "start_time": 2.0, "end_time": 3.0, "speaker": "Y"},
        ]

        result_crafted = self.processor.process(segments=segs_single_crafted, title="T")
        result_clean = self.processor.process(segments=segs_two_clean, title="T")

        self.assertIsNotNone(result_crafted.fingerprint)
        self.assertIsNotNone(result_clean.fingerprint)
        self.assertNotEqual(result_crafted.fingerprint, result_clean.fingerprint)

    def test_fingerprint_stable_for_identical_input_including_speaker(self):
        """Fully identical input (same text, times, AND speaker) across
        repeated calls must still produce the exact same fingerprint now
        that speaker is part of the hashed input."""
        segs = [
            {"text": "stable text", "start_time": 12.5, "end_time": 20.0, "speaker": "Alice"},
            {"text": "more stable text", "start_time": 20.0, "end_time": 30.0, "speaker": "Bob"},
        ]

        result1 = self.processor.process(segments=segs, title="T1")
        result2 = self.processor.process(segments=segs, title="T2 (different title)")

        self.assertIsNotNone(result1.fingerprint)
        self.assertEqual(result1.fingerprint, result2.fingerprint)

    def test_fingerprint_changes_when_leading_blank_entry_shifts_survivor_indices(self):
        """gate-r17 P2: a leading blank-text entry is filtered out before
        fingerprinting (it has no retention value), but the *original*
        index of every surviving segment still shifts by one (0,1,2,... ->
        1,2,3,...). The survivors' text/start_time/end_time are completely
        unchanged, so a fingerprint that only hashes (text, start_time,
        end_time, speaker) is blind to this shift -- yet the cached
        chapters' start_seg/end_seg anchors (which key off these very
        original indices, see gate-r16 P2) now point at entirely different
        segments. The fingerprint must change so a caching layer keyed on
        it cannot silently keep serving chapters with misaligned anchors."""
        segments_no_blank = make_segments(5)
        segments_with_leading_blank = [
            {"text": "   ", "start_time": 0.0, "end_time": 0.0},
        ] + make_segments(5)

        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 3},
        ])

        result_no_blank = self.processor.process(segments=segments_no_blank, title="T")
        result_with_blank = self.processor.process(
            segments=segments_with_leading_blank, title="T"
        )

        self.assertIsNotNone(result_no_blank.fingerprint)
        self.assertIsNotNone(result_with_blank.fingerprint)
        self.assertNotEqual(result_no_blank.fingerprint, result_with_blank.fingerprint)

    def test_fingerprint_stable_when_no_entries_are_filtered(self):
        """Sanity check for the other direction: when nothing gets filtered
        (no non-dict / blank-text entries), every survivor's original index
        equals its position, so adding the "i" field to the fingerprint
        must not make an otherwise-identical input produce a different
        fingerprint across repeated calls."""
        segments = make_segments(6)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 3},
        ])

        result1 = self.processor.process(segments=segments, title="T1")
        result2 = self.processor.process(
            segments=segments, title="T2 (different title, same segments)"
        )

        self.assertIsNotNone(result1.fingerprint)
        self.assertEqual(result1.fingerprint, result2.fingerprint)

    def test_fingerprint_uses_raw_segment_times_not_entry_sanitized_values(self):
        """gate-r23 P2: _compute_fingerprint must keep hashing the RAW,
        pre-sanitization start_time/end_time of each segment -- per this
        module's own docstring, fingerprint "如实反映输入内容本身". If the
        fingerprint instead hashed the entry-sanitized values (see
        `_sanitize_segment_time_fields`), a segment with a raw self-inverted
        time pair (start=240, end=200) would produce the SAME fingerprint as
        a segment that already carries the post-sanitization result
        (start=240, end=None) -- because sanitize_time_pair maps both inputs
        to the identical (240, None) output. The fingerprint must therefore
        distinguish the two, even though both resolve to the exact same
        downstream chapter behavior (end_time=None)."""
        segments_raw_inverted = make_segments(10)
        segments_raw_inverted[4]["start_time"] = 240.0
        segments_raw_inverted[4]["end_time"] = 200.0  # raw self-inversion

        segments_already_sanitized = make_segments(10)
        segments_already_sanitized[4]["start_time"] = 240.0
        segments_already_sanitized[4]["end_time"] = None  # sanitize_time_pair's own output

        chapters_response = [
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ]
        self.llm_client.call.return_value = mock_llm_response(chapters_response)
        result_raw = self.processor.process(segments=segments_raw_inverted, title="T")

        self.llm_client.call.return_value = mock_llm_response(chapters_response)
        result_sanitized = self.processor.process(segments=segments_already_sanitized, title="T")

        self.assertIsNotNone(result_raw.fingerprint)
        self.assertIsNotNone(result_sanitized.fingerprint)
        self.assertNotEqual(result_raw.fingerprint, result_sanitized.fingerprint)
        # Both inputs still resolve to the SAME downstream chapter behavior
        # (end_time discarded) even though their fingerprints differ.
        self.assertEqual(result_raw.status, ChaptersStatus.GENERATED)
        self.assertEqual(result_sanitized.status, ChaptersStatus.GENERATED)
        self.assertIsNone(result_raw.chapters[0].end_time)
        self.assertIsNone(result_sanitized.chapters[0].end_time)


class TestIntegerSpeaker(ChaptersProcessorTestBase):
    """FunASR raw dicts can carry `speaker` as a bare int (diarization label,
    e.g. 2) rather than a string like "Speaker 2" -- process() must not crash
    on this before it even gets a chance to return an honest status."""

    def test_integer_speaker_does_not_raise_and_generates(self):
        segments = make_segments(10, speaker=2)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        result = self.processor.process(segments=segments, title="T")  # must not raise

        self.assertEqual(result.status, ChaptersStatus.GENERATED)

    def test_integer_speaker_fingerprint_is_stable(self):
        segments = make_segments(10, speaker=2)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        result1 = self.processor.process(segments=segments, title="T1")
        result2 = self.processor.process(
            segments=segments, title="T2 (different title, same segments)"
        )

        self.assertIsNotNone(result1.fingerprint)
        self.assertEqual(result1.fingerprint, result2.fingerprint)


class TestModelFallback(unittest.TestCase):
    """LLMConfig() constructed directly (bypassing from_dict, whose own defaulting
    logic falls chapters_model back to calibrate_model) leaves chapters_model=None.
    The processor must resolve a usable model at call time instead of passing
    None straight through to the LLM client."""

    def _make_processor(self, config):
        llm_client = Mock()
        llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])
        processor = ChaptersProcessor(llm_client=llm_client, config=config)
        return processor, llm_client

    def test_missing_chapters_model_falls_back_to_summary_model(self):
        config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="calib-model",
            summary_model="summary-model",
            min_chapters_threshold=10,
        )
        self.assertIsNone(config.chapters_model)  # sanity: bare constructor, no from_dict default
        processor, llm_client = self._make_processor(config)

        result = processor.process(segments=make_segments(10), title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        call_kwargs = llm_client.call.call_args[1]
        self.assertEqual(call_kwargs.get("model"), "summary-model")

    def test_missing_chapters_and_summary_model_falls_back_to_calibrate_model(self):
        config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="calib-model",
            summary_model="",  # falsy -- chain must keep walking
            min_chapters_threshold=10,
        )
        processor, llm_client = self._make_processor(config)

        result = processor.process(segments=make_segments(10), title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        call_kwargs = llm_client.call.call_args[1]
        self.assertEqual(call_kwargs.get("model"), "calib-model")

    def test_configured_chapters_model_is_not_overridden(self):
        """The fallback chain must only kick in when chapters_model is unset --
        a configured value always wins."""
        config = LLMConfig(
            api_key="k", base_url="u",
            calibrate_model="calib-model",
            summary_model="summary-model",
            chapters_model="chapters-model",
            min_chapters_threshold=10,
        )
        processor, llm_client = self._make_processor(config)

        result = processor.process(segments=make_segments(10), title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        call_kwargs = llm_client.call.call_args[1]
        self.assertEqual(call_kwargs.get("model"), "chapters-model")


class TestNonDictSegmentFiltering(ChaptersProcessorTestBase):
    """Regression tests for the gate-r13 P2 finding: a segments list mixed with
    non-dict entries (e.g. JSON null -> None, or a bare string) must not crash
    process() with AttributeError -- it must filter them out up front, the same
    way transcriber.segments.normalize_segments skips non-dict entries."""

    def test_non_dict_entries_are_filtered_then_generation_succeeds(self):
        good = make_segments(2)  # two well-formed dict segments
        # original indices: 0=None(filtered), 1=good[0], 2="junk"(filtered), 3=good[1]
        # surviving original indices: [1, 3] -- gate-r16 P2: start_seg/end_seg
        # must reference these ORIGINAL indices, not a compacted [0, 1].
        segments = [None, good[0], "junk", good[1]]
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "gist a", "start_seg": 1},
            {"title": "B", "gist": "gist b", "start_seg": 3},
        ])

        with patch("video_transcript_api.llm.processors.chapters_processor.logger") as mock_logger:
            result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(result.segment_count, 2)
        self.llm_client.call.assert_called_once()
        # start_seg/end_seg must index the ORIGINAL segments list handed to
        # process(), not the filtered/compacted list.
        self.assertEqual(result.chapters[0].start_seg, 1)
        self.assertEqual(result.chapters[0].end_seg, 1)
        self.assertEqual(result.chapters[1].start_seg, 3)
        self.assertEqual(result.chapters[1].end_seg, 3)

        # A warning naming the filtered count (2: None + "junk") must be logged.
        warning_messages = [str(call.args[0]) for call in mock_logger.warning.call_args_list]
        self.assertTrue(
            any("2" in msg and "non-dict" in msg.lower() for msg in warning_messages),
            f"expected a filtered-count warning, got: {warning_messages}",
        )

    def test_all_non_dict_entries_skipped_no_timeline(self):
        segments = [None, "junk", 123, ["nested"]]

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.SKIPPED_NO_TIMELINE)
        self.assertEqual(result.chapters, [])
        self.assertIsNone(result.error)
        self.llm_client.call.assert_not_called()


class TestBlankTextSegmentFiltering(ChaptersProcessorTestBase):
    """Regression tests for the gate-r15 P2 finding: a blank/whitespace-only
    (or missing/non-string) 'text' segment must not count toward
    segment_count -- otherwise "one long real segment + one blank cue" slips
    past the gate-r14 '< 2 usable segments' gate and could even seed an
    empty chapter. Filtering must match transcriber.segments.normalize_segments'
    'blank text has no retention value' rule, and happen before any gate is
    evaluated."""

    def test_blank_segment_plus_one_real_segment_is_skipped_no_timeline(self):
        long_segment = make_segments(1, text_repeat=50)[0]
        blank_segment = {"text": "   ", "start_time": 60.0, "end_time": 65.0}
        segments = [long_segment, blank_segment]

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.SKIPPED_NO_TIMELINE)
        self.assertEqual(result.chapters, [])
        self.assertEqual(result.segment_count, 1)
        self.llm_client.call.assert_not_called()

    def test_blank_segment_among_two_normal_segments_generates_normally(self):
        good = make_segments(2)
        blank_segment = {"text": "", "start_time": 30.0, "end_time": 35.0}
        # original indices: 0=blank(filtered), 1=good[0], 2=good[1]
        # surviving original indices: [1, 2] -- gate-r16 P2: start_seg/end_seg
        # must reference these ORIGINAL indices, not a compacted [0, 1].
        segments = [blank_segment, good[0], good[1]]
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "gist a", "start_seg": 1},
            {"title": "B", "gist": "gist b", "start_seg": 2},
        ])

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(result.segment_count, 2)
        self.llm_client.call.assert_called_once()
        self.assertEqual(result.chapters[0].start_seg, 1)
        self.assertEqual(result.chapters[1].start_seg, 2)

        # The numbered prompt text sent to the LLM must contain exactly the
        # two real segments, numbered by their ORIGINAL indices (1 and 2) --
        # the blank segment must not occupy a numbered slot (index 0 must not
        # appear), and the surviving segments must not be renumbered from 0.
        expected_segment_lines = _build_segment_lines([(1, good[0]), (2, good[1])])
        user_prompt = self.llm_client.call.call_args.kwargs["user_prompt"]
        self.assertTrue(user_prompt.endswith(expected_segment_lines))

    def test_blank_text_filtering_logs_warning_with_count(self):
        """Missing text key, non-string text, and whitespace-only text all
        count as 'blank' and must all be filtered -- with a single warning
        naming the total count, the same shape as the non-dict filtering
        warning."""
        good = make_segments(2)
        # original indices: 0,1,2 filtered (blank/missing/whitespace-only text)
        # 3=good[0], 4=good[1] -- surviving original indices: [3, 4].
        segments = [
            {"text": None, "start_time": 1.0, "end_time": 2.0},
            {"start_time": 1.0, "end_time": 2.0},  # text key missing entirely
            {"text": "   \n\t  ", "start_time": 1.0, "end_time": 2.0},  # whitespace only
            good[0],
            good[1],
        ]
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "gist a", "start_seg": 3},
            {"title": "B", "gist": "gist b", "start_seg": 4},
        ])

        with patch("video_transcript_api.llm.processors.chapters_processor.logger") as mock_logger:
            result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(result.segment_count, 2)
        self.assertEqual(result.chapters[0].start_seg, 3)
        self.assertEqual(result.chapters[1].start_seg, 4)

        warning_messages = [str(call.args[0]) for call in mock_logger.warning.call_args_list]
        self.assertTrue(
            any("3" in msg and "blank" in msg.lower() for msg in warning_messages),
            f"expected a filtered-blank-text-count warning, got: {warning_messages}",
        )


class TestOriginalIndexPreservation(ChaptersProcessorTestBase):
    """Regression tests for the gate-r16 P2 finding: entry filtering (blank-text /
    non-dict segments) used to renumber the *filtered* list from 0, so a returned
    Chapter.start_seg/end_seg indexed the filtered list, not the original segments
    list the caller actually holds. A future page anchor like '#dlg-{start_seg}'
    is built against the ORIGINAL list, so this misalignment would silently point
    at the wrong dialogue line whenever any segment gets filtered out.

    The fix threads (original_index, segment) pairs through the whole pipeline
    instead of re-enumerating the filtered list, so start_seg/end_seg always
    index the caller's original, unfiltered segments list -- gaps and all."""

    def test_blank_segment_in_middle_does_not_shift_start_seg_end_seg(self):
        """A blank segment sitting between real ones must not shift the indices
        of everything after it. original_segments has 5 entries; index 1 is
        blank and gets filtered, leaving surviving original indices [0, 2, 3, 4].
        The (mocked) LLM response uses those surviving ORIGINAL indices directly
        (0 and 3) -- exactly what a well-behaved model would do when reading the
        numbered prompt text, which is now built from original indices too.

        Before the fix, the filtered list would get compacted and re-enumerated
        as [0, 1, 2, 3] (good0, good1, good2, good3), so end_seg/start_time/
        end_time lookups would silently resolve against the WRONG original
        entry (e.g. chapter 0's end_seg would wrongly land on good2's original
        slot -- index 3 -- instead of good1's original slot -- index 2)."""
        good = make_segments(4, seconds_per_seg=100)
        blank_segment = {"text": "   ", "start_time": 30.0, "end_time": 35.0}
        original_segments = [good[0], blank_segment, good[1], good[2], good[3]]
        # original indices: 0=good0, 1=blank(filtered), 2=good1, 3=good2, 4=good3
        # surviving original indices: [0, 2, 3, 4]
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "gist a", "start_seg": 0},
            {"title": "B", "gist": "gist b", "start_seg": 3},
        ])

        result = self.processor.process(segments=original_segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.GENERATED)
        self.assertEqual(result.segment_count, 4)  # 4 surviving segments
        ch0, ch1 = result.chapters

        self.assertEqual(ch0.start_seg, 0)
        # end_seg must be the surviving original index immediately before the
        # next chapter's start (3) in the surviving sequence [0, 2, 3, 4] -- i.e.
        # 2 (good1's ORIGINAL slot), not 1 (what a naive "next_start - 1" or a
        # compacted-list position would wrongly give).
        self.assertEqual(ch0.end_seg, 2)
        self.assertEqual(ch1.start_seg, 3)
        self.assertEqual(ch1.end_seg, 4)  # largest surviving original index

        # Anchor test: times must be looked up against the ORIGINAL list by
        # original index, not the compacted filtered-list position.
        self.assertEqual(ch0.start_time, original_segments[0]["start_time"])
        self.assertEqual(ch0.end_time, original_segments[2]["end_time"])  # good1, NOT good2
        self.assertEqual(ch1.start_time, original_segments[3]["start_time"])  # good2
        self.assertEqual(ch1.end_time, original_segments[4]["end_time"])  # good3

    def test_llm_returning_filtered_out_index_is_rejected_and_eventually_fails(self):
        """If the LLM (mistakenly) returns a start_seg that belongs to a segment
        that got filtered out (index 1, the blank one), semantic validation must
        reject it -- the same as any other out-of-range index -- triggering a
        retry with a concrete error; if the retry repeats the mistake, the final
        result is FAILED (never silently accepted as if index 1 still existed)."""
        good = make_segments(4, seconds_per_seg=100)
        blank_segment = {"text": "   ", "start_time": 30.0, "end_time": 35.0}
        segments = [good[0], blank_segment, good[1], good[2], good[3]]
        bad = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 1},  # filtered-out original index
        ])
        self.llm_client.call.side_effect = [bad, bad]

        result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertEqual(self.llm_client.call.call_count, 2)
        self.assertEqual(result.chapters, [])
        self.assertIsNotNone(result.error)
        retry_prompt = self.llm_client.call.call_args_list[1][1]["user_prompt"]
        self.assertIn("out of range", retry_prompt.lower())


class TestUnexpectedExceptionSafety(ChaptersProcessorTestBase):
    """Regression test for the gate-r13 P2 finding's second layer: process()
    must never let an unforeseen internal exception escape -- it is the
    processor contract ('honest status, never crash') that every caller
    relies on. This is a deliberately-injected failure in an internal helper
    (not reachable via any legitimate input) standing in for "some bug we
    haven't found yet"."""

    def test_internal_exception_is_caught_and_returns_failed(self):
        segments = make_segments(10)
        self.llm_client.call.return_value = mock_llm_response([
            {"title": "A", "gist": "g", "start_seg": 0},
            {"title": "B", "gist": "g", "start_seg": 5},
        ])

        with patch(
            "video_transcript_api.llm.processors.chapters_processor._derive_times",
            side_effect=RuntimeError("boom"),
        ):
            result = self.processor.process(segments=segments, title="T")

        self.assertEqual(result.status, ChaptersStatus.FAILED)
        self.assertEqual(result.chapters, [])
        self.assertIsNotNone(result.error)
        self.assertIn("boom", result.error)


if __name__ == "__main__":
    unittest.main()
