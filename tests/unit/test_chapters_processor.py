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
    """Locks _to_seconds' handling of the two upstream time formats."""

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

    def test_bool_returns_none(self):
        """bool is a subclass of int in Python -- must not be silently treated as seconds."""
        self.assertIsNone(_to_seconds(True))

    def test_inf_returns_none(self):
        """Non-finite numeric input must not silently become a fake timestamp."""
        self.assertIsNone(_to_seconds(float("inf")))
        self.assertIsNone(_to_seconds(float("-inf")))

    def test_nan_returns_none(self):
        self.assertIsNone(_to_seconds(float("nan")))

    def test_inf_string_returns_none(self):
        """float() happily parses "inf"/"nan" strings -- the ':'-split path must
        also reject non-finite results instead of passing them through."""
        self.assertIsNone(_to_seconds("inf"))


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


if __name__ == "__main__":
    unittest.main()
