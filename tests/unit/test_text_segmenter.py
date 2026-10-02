"""
TextSegmenter unit tests.

Covers:
- Standard sentence-based segmentation
- CapsWriter format detection (low punctuation density)
- Segment size limits (segment_size, max_segment_size)
- Empty/short text handling

All console output must be in English only (no emoji, no Chinese).
"""

import re
import threading

import pytest
from unittest.mock import Mock
from video_transcript_api.llm.segmenters.text_segmenter import TextSegmenter
from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.transcriber.segments import parse_time_to_seconds


@pytest.fixture
def config():
    """Create a minimal LLMConfig for segmenter testing."""
    return Mock(
        spec=LLMConfig,
        segment_size=100,
        max_segment_size=200,
    )


@pytest.fixture
def segmenter(config):
    return TextSegmenter(config)


class TestTextSegmenter:
    """Test text segmentation logic."""

    def test_empty_text(self, segmenter):
        """Empty text should return empty segments."""
        result = segmenter.segment("")
        assert result == []

    def test_short_text_single_segment(self, segmenter):
        """Short text should produce a single segment."""
        result = segmenter.segment("short text here")
        assert len(result) == 1

    def test_sentence_segmentation(self, segmenter):
        """Text with punctuation should be split by sentences when exceeding segment_size."""
        # Need enough text to exceed segment_size (100 chars)
        text = "first sentence with enough words here。second sentence also with many words。third sentence is quite long too。fourth one also long enough。fifth sentence to push way over the size limit definitely。"
        result = segmenter.segment(text)
        assert len(result) >= 2
        # Each segment should be within max_segment_size
        for seg in result:
            assert len(seg) <= segmenter.max_segment_size

    def test_capswriter_format_detection(self, segmenter):
        """Text with low punctuation density (no periods) should be detected as CapsWriter."""
        # CapsWriter format: many lines, no punctuation
        lines = [f"line {i} with some words here" for i in range(20)]
        text = "\n".join(lines)
        result = segmenter.segment(text)
        assert len(result) >= 1
        for seg in result:
            assert len(seg) <= segmenter.max_segment_size

    def test_max_segment_size_respected(self):
        """Segments should never exceed max_segment_size."""
        config = Mock(spec=LLMConfig, segment_size=50, max_segment_size=100)
        seg = TextSegmenter(config)
        text = "a" * 500 + "。" + "b" * 500 + "。"
        result = seg.segment(text)
        for segment in result:
            assert len(segment) <= 100

    def test_chinese_text_segmentation(self, segmenter):
        """Chinese text with standard punctuation should segment correctly."""
        text = "这是第一句话。这是第二句话！这是第三句话？" * 5
        result = segmenter.segment(text)
        assert len(result) >= 1


def _call_with_watchdog(func, *args, timeout=10.0):
    """Run ``func`` in a daemon thread and turn a hang into a failed assertion.

    ``_append_fragment`` loops while the remainder is non-empty; without the
    ``max_segment_size > 0`` guard that loop never terminates and the segments
    list grows without bound. A bare call under ``pytest.raises`` would hang the
    whole test process instead of reporting a failure.
    """
    box = {}

    def _run():
        try:
            box["value"] = func(*args)
        except BaseException as exc:  # re-raised on the calling thread
            box["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), (
        f"{getattr(func, '__name__', func)} did not return within {timeout}s "
        f"(infinite loop?) args={args[1:]!r}"
    )
    if "error" in box:
        raise box["error"]
    return box["value"]


class TestMaxSegmentSizeFailFast:
    """P1-A: a non-positive ``max_segment_size`` must be rejected at construction.

    The old code clamped nothing and checked nothing, so ``while fragment:`` never
    shortened ``fragment`` and the worker hung with an unbounded ``segments`` list.
    """

    @pytest.mark.parametrize("bad_max_segment_size", [0, -1])
    def test_non_positive_max_segment_size_fails_fast(self, bad_max_segment_size):
        config = Mock(spec=LLMConfig, segment_size=100, max_segment_size=bad_max_segment_size)

        with pytest.raises(ValueError) as excinfo:
            _call_with_watchdog(TextSegmenter, config)

        assert "TEXT_SEGMENT_INVALID_MAX_SIZE" in str(excinfo.value)
        assert "condition=max_len_not_positive" in str(excinfo.value)

    @pytest.mark.parametrize("bad_max_segment_size", [0, -1])
    def test_non_positive_max_segment_size_never_segments(self, bad_max_segment_size):
        """The whole pipeline (constructor + segment) must refuse to spin."""
        config = Mock(spec=LLMConfig, segment_size=100, max_segment_size=bad_max_segment_size)

        def build_and_run():
            return TextSegmenter(config).segment("hello world。")

        with pytest.raises(ValueError):
            _call_with_watchdog(build_and_run)

    @pytest.mark.parametrize("segment_size", [0, -1])
    def test_non_positive_segment_size_still_works(self, segment_size):
        """segment_size only drives flush decisions, so it is not validated."""
        config = Mock(spec=LLMConfig, segment_size=segment_size, max_segment_size=200)
        segmenter = TextSegmenter(config)

        result = _call_with_watchdog(segmenter.segment, "hello world。")

        assert "".join(result) == "hello world。"


class TestBodyTextConservation:
    """P1-B: segmentation must cut, never rewrite the transcript body.

    Two rounds of the same defect, both silent and both reaching the delivered
    transcript (the proofediting fallback returns the segment verbatim):

    1. punctuation: every original break mark was dropped and a ``。`` injected,
       so ``3.14`` became ``3。14`` and ``https://a.b/c`` became ``https。//a。b/c``;
    2. whitespace: each piece was ``strip()``-ed, so ``Hello! World?`` became
       ``Hello!World?`` -- two words glued together, same harm, invisible to a
       criterion that compares non-whitespace sequences only.

    The criterion here is therefore verbatim equality: the pieces must
    concatenate back to the input, whitespace included. The previous
    ``_non_whitespace()`` helper stripped whitespace on both sides before
    comparing, so by construction it could never see defect 2 -- a comparison
    that cannot fail is not a lock, it is decoration.
    """

    #: Samples the sentence branch must reproduce character for character.
    SAMPLES = [
        # --- the four samples from the gate finding (codex-sub, run 37034223853) ---
        "Hello! World?",
        "你好。 世界。",
        "see item 3。 then item 4。",
        "A； B， C",
        # --- punctuation forms from the first round ---
        "圆周率是3.14！见 https://a.b/c 与 Mr. Smith 的 e.g. 例子。真的吗？！",
        "真的吗？！开头。结尾？！",
        "Mr. Smith says e.g. this。",
        # --- whitespace shapes ---
        "  前后空白保留 \n 换行与连续空格  ",
        "tab\tseparated\tfields with spaces。",
        "single-spaced words without any break char " * 30,
        "double  spaced  words  inside。",
        # --- length pressure: forces the max_segment_size hard cuts ---
        "这是一个没有任何句末标点的纯中文长句子用来测试长度兜底切分" * 20,
        "word " * 120,
        "长" * 600,
        "混合 mixed 内容 content " * 60,
    ]

    @pytest.mark.parametrize(
        "text",
        SAMPLES,
        ids=[
            "finding_ascii_bangs_space",
            "finding_cjk_space",
            "finding_sentence_space",
            "finding_cjk_semicolon_comma_space",
            "punctuation_mixed",
            "consecutive_break_chars",
            "dot_abbreviation",
            "leading_trailing_whitespace",
            "tabs",
            "single_spaces_no_break",
            "double_spaces",
            "long_cjk_no_punctuation",
            "long_english_no_punctuation",
            "long_over_max_segment_size",
            "long_mixed_over_max",
        ],
    )
    def test_sentence_split_is_verbatim(self, segmenter, text):
        """``''.join(_segment_by_sentences(text)) == text``, whitespace included."""
        result = segmenter._segment_by_sentences(text)

        assert result
        assert "".join(result) == text
        assert all(len(piece) <= segmenter.max_segment_size for piece in result)

    @pytest.mark.parametrize(
        "text",
        [
            # Single-line inputs reach the sentence branch through the
            # CapsWriter fallback in segment(), so the pipeline is verbatim too.
            "Hello! World?",
            "你好。 世界。",
            "圆周率是3.14！见 https://a.b/c。",
            "word " * 120,
            "长" * 600,
        ],
        ids=[
            "finding_ascii_bangs_space",
            "finding_cjk_space",
            "punctuation_mixed",
            "long_english_no_punctuation",
            "long_over_max_segment_size",
        ],
    )
    def test_segment_pipeline_is_verbatim(self, segmenter, text):
        result = segmenter.segment(text)

        assert result
        assert "".join(result) == text
        assert all(len(piece) <= segmenter.max_segment_size for piece in result)

    def test_verbatim_criterion_rejects_word_gluing(self):
        """Negative control: the criterion must fail on the lossy output.

        This is the shape the previous implementation returned for
        ``"Hello! World?"`` and the reason the gate flagged it as a major
        finding. A criterion that cannot reject this pair cannot lock the fix,
        so the pair is pinned here explicitly instead of being trusted to the
        property tests above.
        """
        original = "Hello! World?"
        lossy_output = ["Hello!World?"]  # what the stripping implementation returned

        assert "".join(lossy_output) != original

    def test_verbatim_criterion_rejects_whitespace_runs_and_gluing(self):
        """More known-wrong outputs the criterion has to reject."""
        cases = [
            ("Hello! World?", ["Hello!World?"]),          # word gluing
            ("你好。 世界。", ["你好。世界。"]),  # space after a break char
            ("a  b", ["a b"]),                        # collapsed interior run
            ("end with space ", ["end with space"]),     # dropped trailing space
            ("  leading", ["leading"]),               # dropped leading space
            ("3.14", ["3。14"]),                       # punctuation injection
        ]

        for original, lossy_output in cases:
            assert "".join(lossy_output) != original, (
                f"criterion cannot detect {lossy_output!r} for {original!r}"
            )

    def test_original_punctuation_is_kept_verbatim(self, segmenter):
        text = "圆周率是3.14！见 https://a.b/c 与 Mr. Smith 的 e.g. 例子。真的吗？！"

        joined = "".join(segmenter._segment_by_sentences(text))

        for literal in ("3.14", "https://a.b/c", "Mr. Smith", "e.g."):
            assert literal in joined, f"literal {literal!r} was rewritten"
        assert "3。14" not in joined
        assert "https。//" not in joined

    def test_no_punctuation_is_injected(self, segmenter):
        text = "这里没有句号也没有感叹号"

        result = segmenter._segment_by_sentences(text)

        assert "。" not in "".join(result)


class TestDialogSegmenter:
    """Test dialog segmentation logic."""

    @pytest.fixture
    def dialog_config(self):
        return Mock(
            spec=LLMConfig,
            min_chunk_length=50,
            max_chunk_length=200,
            preferred_chunk_length=100,
        )

    @pytest.fixture
    def dialog_segmenter(self, dialog_config):
        from video_transcript_api.llm.segmenters.dialog_segmenter import DialogSegmenter
        return DialogSegmenter(dialog_config)

    def test_empty_dialogs(self, dialog_segmenter):
        """Empty dialog list should return empty chunks."""
        assert dialog_segmenter.segment([]) == []

    def test_single_short_dialog(self, dialog_segmenter):
        """Single short dialog should be one chunk."""
        dialogs = [{"speaker": "A", "text": "hello world", "start_time": 0}]
        result = dialog_segmenter.segment(dialogs)
        assert len(result) == 1
        assert len(result[0]) == 1

    def test_multiple_dialogs_chunking(self, dialog_segmenter):
        """Multiple dialogs should be chunked by preferred length."""
        dialogs = [
            {"speaker": f"S{i%2}", "text": f"dialog text number {i} " * 5, "start_time": i}
            for i in range(10)
        ]
        result = dialog_segmenter.segment(dialogs)
        assert len(result) >= 2
        # Each chunk total text should not exceed max
        for chunk in result:
            total = sum(len(d["text"]) for d in chunk)
            assert total <= dialog_segmenter.max_chunk_length + 100  # some tolerance for last merge

    def test_long_dialog_split(self, dialog_segmenter):
        """Single dialog exceeding max_chunk_length should be split."""
        # Text must have sentence punctuation for splitting to work
        long_text = "这是一段很长的话。" * 50  # ~450 chars with split points
        dialogs = [{"speaker": "A", "text": long_text, "start_time": 0}]
        result = dialog_segmenter.segment(dialogs)
        assert len(result) >= 2

    def test_long_dialog_interpolates_timestamps_without_copying(self, dialog_segmenter):
        """Long dialog fragments retain the original sentence split order and timeline."""
        sentence = "中性测试句子。"
        long_text = sentence * 200
        dialogs = [
            {
                "speaker": "A",
                "text": long_text,
                "start_time": "00:00:21",
                "end_time": "00:56:24",
            }
        ]

        result = dialog_segmenter.segment(dialogs)
        fragments = [dialog for chunk in result for dialog in chunk]
        expected_texts = [sentence * 28] * 7 + [sentence * 4]

        assert [dialog["text"] for dialog in fragments] == expected_texts
        assert len(fragments) == len(expected_texts)
        assert all(dialog["time_estimated"] is True for dialog in fragments)

        starts = [parse_time_to_seconds(dialog["start_time"]) for dialog in fragments]
        ends = [parse_time_to_seconds(dialog["end_time"]) for dialog in fragments]
        assert starts[0] == parse_time_to_seconds("00:00:21")
        assert ends[-1] == parse_time_to_seconds("00:56:24")
        assert all(
            re.fullmatch(r"\d{2}:\d{2}:\d{2}", dialog["start_time"])
            for dialog in fragments
        )
        assert all(
            re.fullmatch(r"\d{2}:\d{2}:\d{2}", dialog["end_time"])
            for dialog in fragments
        )
        assert all(start < end for start, end in zip(starts, ends))
        assert starts == sorted(starts)
        assert len(set(starts)) == len(starts)

        for index, dialog in enumerate(fragments):
            assert dialog["duration"] == pytest.approx(ends[index] - starts[index])
            if index:
                assert starts[index] == ends[index - 1]
                assert starts[index] >= ends[index - 1]

    @pytest.mark.parametrize(
        ("seconds", "template", "expected"),
        [
            (1.5, "00:00:00", "00:00:01"),
            (1.0, "00:00:00.0", "00:00:01.0"),
            (1.5, "00:00:00.00", "00:00:01.50"),
        ],
    )
    def test_dialog_timestamp_format_preserves_template_precision(
        self, dialog_segmenter, seconds, template, expected
    ):
        assert (
            dialog_segmenter._format_dialog_timestamp(seconds, template)
            == expected
        )

    def test_long_dialog_with_invalid_times_keeps_text_and_drops_timeline(
        self, dialog_segmenter
    ):
        sentence = "中性测试句子。"
        dialogs = [
            {
                "speaker": "A",
                "text": sentence * 100,
                "start_time": "not-a-time",
                "end_time": "00:10:00",
            }
        ]

        result = dialog_segmenter.segment(dialogs)
        fragments = [dialog for chunk in result for dialog in chunk]

        assert len(fragments) > 1
        assert "".join(dialog["text"] for dialog in fragments) == dialogs[0]["text"]
        assert all(dialog["time_estimated"] is True for dialog in fragments)
        assert all(dialog["start_time"] is None for dialog in fragments)
        assert all(dialog["end_time"] is None for dialog in fragments)
        assert all(dialog["duration"] is None for dialog in fragments)

    def test_mixed_precision_endpoints_keep_contiguous_timeline(
        self, dialog_segmenter
    ):
        """Different start/end decimal precision must not wipe the timeline."""
        sentence = "中性测试句子。"
        dialogs = [
            {
                "speaker": "A",
                "text": sentence * 100,
                "start_time": "00:00:00",
                "end_time": "00:00:10.0",
            }
        ]

        result = dialog_segmenter.segment(dialogs)
        fragments = [dialog for chunk in result for dialog in chunk]

        assert len(fragments) > 1
        assert all(dialog["start_time"] is not None for dialog in fragments)
        assert all(dialog["end_time"] is not None for dialog in fragments)
        assert all(dialog["duration"] is not None for dialog in fragments)
        assert all(dialog["time_estimated"] is True for dialog in fragments)

        assert parse_time_to_seconds(fragments[0]["start_time"]) == parse_time_to_seconds(
            "00:00:00"
        )
        assert parse_time_to_seconds(fragments[-1]["end_time"]) == parse_time_to_seconds(
            "00:00:10.0"
        )
        for index in range(1, len(fragments)):
            assert fragments[index - 1]["end_time"] == fragments[index]["start_time"]

    def test_short_tail_merged(self, dialog_segmenter):
        """Very short last chunk should be merged into previous."""
        dialogs = [
            {"speaker": "A", "text": "x" * 80, "start_time": 0},
            {"speaker": "B", "text": "y" * 80, "start_time": 1},
            {"speaker": "A", "text": "z" * 10, "start_time": 2},  # Short tail
        ]
        result = dialog_segmenter.segment(dialogs)
        # Short tail should be merged
        total_dialogs = sum(len(chunk) for chunk in result)
        assert total_dialogs == 3
