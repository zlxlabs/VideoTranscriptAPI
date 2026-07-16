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

import unittest
from unittest.mock import Mock, patch

from video_transcript_api.llm.processors.chapters_processor import (
    ChaptersProcessor,
    ChaptersResult,
    Chapter,
    _to_seconds,
    _format_timestamp,
    _build_segment_lines,
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
    """Locks the compressed segment format: `[i] mm:ss (speaker:)? text`."""

    def test_basic_format_with_time_no_speaker(self):
        segments = [{"text": "hello world", "start_time": 65.0, "end_time": 70.0}]
        lines = _build_segment_lines(segments)
        self.assertEqual(lines, "[0] 01:05 hello world")

    def test_format_with_speaker(self):
        segments = [{"text": "hi", "start_time": 5.0, "end_time": 10.0, "speaker": "Alice"}]
        lines = _build_segment_lines(segments)
        self.assertEqual(lines, "[0] 00:05 Alice: hi")

    def test_missing_time_omits_time_part(self):
        segments = [{"text": "hi", "start_time": None, "end_time": None}]
        lines = _build_segment_lines(segments)
        self.assertEqual(lines, "[0] hi")

    def test_multiple_segments_numbered_and_newline_joined(self):
        segments = [
            {"text": "first", "start_time": 0.0, "end_time": 5.0},
            {"text": "second", "start_time": 5.0, "end_time": 10.0},
        ]
        lines = _build_segment_lines(segments)
        self.assertEqual(lines, "[0] 00:00 first\n[1] 00:05 second")

    def test_speaker_zero_is_shown_not_omitted(self):
        """speaker=0 is a legitimate diarization label (FunASR's first
        speaker slot) -- must not be swallowed by a falsy check. Regression
        for `if speaker:` (which treats 0 the same as "no speaker") vs the
        correct `if speaker is not None:`."""
        segments = [{"text": "hi", "start_time": 5.0, "end_time": 10.0, "speaker": 0}]
        lines = _build_segment_lines(segments)
        self.assertEqual(lines, "[0] 00:05 0: hi")


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


if __name__ == "__main__":
    unittest.main()
