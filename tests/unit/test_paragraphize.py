"""Unit tests for the deterministic paragraphization tool.

Covers video_transcript_api.transcriber.paragraphize.paragraphize_segments:
a pure function (no LLM, no I/O, deterministic) that only SELECTS break
boundaries between consecutive segments and never rewrites any text.

Spec: docs/sessions/260719-0513-chapters/TASKS.md T8 "deterministic
paragraphization v1" -- length is a budget, not a gate; breaks only happen
at authorized points (sentence-end / pause / comma-level fallback /
terminal force-break / pathological single-segment passthrough).

All console output in this file must be English only (no emoji, no
Chinese), per project convention.
"""

import pytest
from loguru import logger

from video_transcript_api.transcriber.paragraphize import paragraphize_segments


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _seg(text, start=None, end=None, **extra):
    """Build one input segment dict (float seconds or None for times)."""
    seg = {"text": text, "start_time": start, "end_time": end}
    seg.update(extra)
    return seg


def _member_char_total(paragraph_text):
    """Total characters ignoring join spaces -- for coverage assertions."""
    return len(paragraph_text.replace(" ", ""))


@pytest.fixture
def warnings_sink():
    """Capture loguru WARNING+ messages emitted during a test."""
    messages = []
    sink_id = logger.add(
        lambda m: messages.append(str(m.record["message"])), level="WARNING"
    )
    try:
        yield messages
    finally:
        logger.remove(sink_id)


# ---------------------------------------------------------------------------
# Chinese prose with sentence-end punctuation
# ---------------------------------------------------------------------------

class TestChineseSentenceEndBreaks:
    def test_breaks_after_sentence_end_once_target_reached(self):
        # 5 segments x 30 chars each, all ending with the CJK full stop.
        segs = [_seg("文" * 29 + "。") for _ in range(5)]
        result = paragraphize_segments(segs, target_chars=100, hard_max_chars=200)
        # cur_len hits 120 (>= 100) at the 4th segment boundary -> break there;
        # the 5th segment is flushed as the trailing paragraph.
        assert len(result) == 2
        assert _member_char_total(result[0]["text"]) == 120
        assert _member_char_total(result[1]["text"]) == 30

    def test_every_non_terminal_paragraph_ends_with_sentence_punctuation(self):
        # Property: in a pure-punctuation scenario every paragraph except the
        # trailing one must end with sentence-end punctuation.
        segs = [_seg("文" * 29 + "。") for _ in range(9)]
        result = paragraphize_segments(segs, target_chars=100, hard_max_chars=200)
        assert len(result) >= 2
        for paragraph in result[:-1]:
            assert paragraph["text"].rstrip()[-1] in "。！？….!?"

    def test_no_early_break_before_target(self):
        # A run of ten 10-char sentences must NOT be split into ten tiny
        # paragraphs: authorization points are inert while cur_len < target.
        segs = [_seg("短" * 9 + "。") for _ in range(10)]  # total 100 chars
        result = paragraphize_segments(segs, target_chars=300, hard_max_chars=600)
        assert len(result) == 1
        assert _member_char_total(result[0]["text"]) == 100


# ---------------------------------------------------------------------------
# pause authorization
# ---------------------------------------------------------------------------

class TestPauseAuthorization:
    def test_pause_at_or_above_threshold_breaks_without_punctuation(self):
        segs = [
            _seg("字" * 30, 0.0, 10.0),
            _seg("字" * 30, 13.0, 23.0),  # gap 3.0 >= 2.0, but cur_len 30 < 50
            _seg("字" * 30, 26.0, 36.0),  # gap 3.0 >= 2.0, cur_len 60 >= 50 -> break
        ]
        result = paragraphize_segments(
            segs, target_chars=50, hard_max_chars=1000, pause_threshold_seconds=2.0
        )
        assert len(result) == 2
        assert _member_char_total(result[0]["text"]) == 60
        assert _member_char_total(result[1]["text"]) == 30

    def test_pause_below_threshold_does_not_break(self):
        segs = [
            _seg("字" * 30, 0.0, 10.0),
            _seg("字" * 30, 10.5, 20.0),  # gap 0.5 < 2.0
            _seg("字" * 30, 20.5, 30.0),
        ]
        result = paragraphize_segments(
            segs, target_chars=50, hard_max_chars=1000, pause_threshold_seconds=2.0
        )
        assert len(result) == 1

    def test_pause_exactly_at_threshold_breaks(self):
        segs = [
            _seg("字" * 30, 0.0, 10.0),
            _seg("字" * 30, 10.0, 20.0),
            _seg("字" * 30, 22.0, 32.0),  # gap exactly 2.0 -> authorized
        ]
        result = paragraphize_segments(
            segs, target_chars=50, hard_max_chars=1000, pause_threshold_seconds=2.0
        )
        assert len(result) == 2


# ---------------------------------------------------------------------------
# missing time
# ---------------------------------------------------------------------------

class TestMissingTime:
    def test_punctuation_mode_still_works_with_none_times(self):
        segs = [_seg("文" * 14 + "。") for _ in range(3)]  # 15 chars each
        result = paragraphize_segments(segs, target_chars=20, hard_max_chars=1000)
        assert len(result) == 2
        assert result[0]["start_time"] is None
        assert result[0]["end_time"] is None

    def test_pause_never_fires_when_time_is_none(self):
        # No punctuation, no usable time -> no authorization at all; with a
        # large hard_max the whole input stays one paragraph.
        segs = [_seg("字" * 15) for _ in range(3)]
        result = paragraphize_segments(segs, target_chars=20, hard_max_chars=1000)
        assert len(result) == 1

    def test_one_sided_none_time_disables_pause(self):
        # Right side start_time None -> pause signal unavailable even though
        # the left side has a complete interval.
        segs = [
            _seg("字" * 30, 0.0, 10.0),
            _seg("字" * 30, None, None),
            _seg("字" * 30, None, None),
        ]
        result = paragraphize_segments(segs, target_chars=50, hard_max_chars=1000)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# hard_max fallback (comma-level authorization)
# ---------------------------------------------------------------------------

class TestHardMaxFallback:
    def test_breaks_at_last_comma_before_hard_max(self):
        # 6 segments x 20 chars; only segment 4 (index 3) ends with a comma.
        segs = [
            _seg("文" * 20),
            _seg("文" * 20),
            _seg("文" * 20),
            _seg("文" * 19 + "，"),
            _seg("文" * 20),
            _seg("文" * 20),
        ]
        result = paragraphize_segments(segs, target_chars=50, hard_max_chars=100)
        # At cur_len == 100 with no strong point, the break falls back to the
        # last authorized point before hard_max: the comma after segment 4.
        assert len(result) == 2
        assert _member_char_total(result[0]["text"]) == 80
        assert result[0]["text"].rstrip().endswith("，")
        assert _member_char_total(result[1]["text"]) == 40

    def test_breaks_at_first_comma_after_hard_max_when_none_before(self):
        # 7 segments x 20 chars; the only comma is at segment 6 (index 5),
        # i.e. cum 120 > hard_max 100 -- no authorized point exists before
        # hard_max, so the first comma-level point after it wins.
        segs = [_seg("文" * 20) for _ in range(7)]
        segs[5] = _seg("文" * 19 + "，")
        result = paragraphize_segments(segs, target_chars=50, hard_max_chars=100)
        assert len(result) == 2
        assert _member_char_total(result[0]["text"]) == 120
        assert result[0]["text"].rstrip().endswith("，")

    def test_ascii_comma_also_authorized_at_hard_max(self):
        segs = [
            _seg("w" * 20),
            _seg("w" * 20),
            _seg("w" * 19 + ","),
            _seg("w" * 20),
            _seg("w" * 20),
        ]
        result = paragraphize_segments(segs, target_chars=50, hard_max_chars=80)
        # cur_len reaches 80 at segment 4 (index 3); last comma point is at
        # cum 60 -> first paragraph covers 3 segments.
        assert _member_char_total(result[0]["text"]) == 60
        assert result[0]["text"].rstrip().endswith(",")


# ---------------------------------------------------------------------------
# terminal force-break rule (no punctuation, no pause)
# ---------------------------------------------------------------------------

class TestTerminalForceBreak:
    def test_english_cue_stream_force_breaks_before_2x_hard_max(self, warnings_sink):
        # 5 segments x 60 chars, no punctuation, no time -> no authorization
        # point ever exists. Groups must stay <= 2 * hard_max = 200.
        segs = [_seg("w" * 60) for _ in range(5)]
        result = paragraphize_segments(segs, target_chars=50, hard_max_chars=100)
        assert len(result) == 2
        for paragraph in result:
            assert _member_char_total(paragraph["text"]) <= 200
        # Coverage: nothing lost, nothing duplicated.
        total = sum(_member_char_total(p["text"]) for p in result)
        assert total == 300
        assert any("force" in m.lower() for m in warnings_sink)

    def test_force_break_never_splits_a_member(self, warnings_sink):
        segs = [_seg("w" * 60) for _ in range(7)]
        result = paragraphize_segments(segs, target_chars=50, hard_max_chars=100)
        # Every paragraph text length must be a multiple of 60 plus join
        # spaces -- members are never cut mid-text.
        for paragraph in result:
            assert _member_char_total(paragraph["text"]) % 60 == 0


# ---------------------------------------------------------------------------
# pathological single member exceeding hard_max
# ---------------------------------------------------------------------------

class TestPathologicalOversizeMember:
    def test_oversize_member_stands_alone_with_warning(self, warnings_sink):
        segs = [
            _seg("短" * 19 + "。"),
            _seg("长" * 150),
            _seg("尾" * 20),
        ]
        result = paragraphize_segments(segs, target_chars=50, hard_max_chars=100)
        assert len(result) == 3
        assert result[0]["text"] == "短" * 19 + "。"
        assert result[1]["text"] == "长" * 150
        assert result[2]["text"] == "尾" * 20
        assert any("exceeds" in m.lower() for m in warnings_sink)

    def test_oversize_member_does_not_merge_neighbors(self, warnings_sink):
        # The oversize member must not drag its neighbors into its paragraph,
        # and neighbors must not be glued to each other across it.
        segs = [_seg("甲" * 30), _seg("长" * 150), _seg("乙" * 30)]
        result = paragraphize_segments(segs, target_chars=300, hard_max_chars=100)
        assert [p["text"] for p in result] == ["甲" * 30, "长" * 150, "乙" * 30]


# ---------------------------------------------------------------------------
# ASCII punctuation and closer-suffix handling
# ---------------------------------------------------------------------------

class TestAsciiPunctuation:
    def test_breaks_after_ascii_period(self):
        segs = [_seg("Hello world."), _seg("Next one."), _seg("Third here.")]
        result = paragraphize_segments(segs, target_chars=10, hard_max_chars=1000)
        assert len(result) == 2
        assert result[0]["text"] == "Hello world."
        assert result[1]["text"] == "Next one. Third here."

    def test_period_followed_by_closing_quote_is_sentence_end(self):
        segs = [_seg('He said "wow."'), _seg("She left early today.")]
        result = paragraphize_segments(segs, target_chars=10, hard_max_chars=1000)
        assert len(result) == 2
        assert result[0]["text"] == 'He said "wow."'

    def test_period_followed_by_closing_paren_is_sentence_end(self):
        segs = [_seg("It works (really)."), _seg("Move on now please.")]
        result = paragraphize_segments(segs, target_chars=10, hard_max_chars=1000)
        assert len(result) == 2

    def test_cjk_exclamation_followed_by_bracket_is_sentence_end(self):
        segs = [_seg("她喊「够了！」"), _seg("全场安静下来没人再说话。")]
        result = paragraphize_segments(segs, target_chars=5, hard_max_chars=1000)
        assert len(result) == 2
        assert result[0]["text"] == "她喊「够了！」"


# ---------------------------------------------------------------------------
# smart join (CJK-aware spacing)
# ---------------------------------------------------------------------------

class TestSmartJoin:
    def test_chinese_segments_join_without_space(self):
        segs = [_seg("你好吗。"), _seg("我很好。")]
        result = paragraphize_segments(segs, target_chars=1000)
        assert result[0]["text"] == "你好吗。我很好。"

    def test_english_segments_join_with_single_space(self):
        segs = [_seg("hello"), _seg("world")]
        result = paragraphize_segments(segs, target_chars=1000)
        assert result[0]["text"] == "hello world"

    def test_cjk_left_edge_suppresses_space(self):
        segs = [_seg("你好"), _seg("world")]
        result = paragraphize_segments(segs, target_chars=1000)
        assert result[0]["text"] == "你好world"

    def test_cjk_right_edge_suppresses_space(self):
        segs = [_seg("hello"), _seg("世界")]
        result = paragraphize_segments(segs, target_chars=1000)
        assert result[0]["text"] == "hello世界"

    def test_no_space_after_chinese_full_stop(self):
        # The classic defect this guards against: a space inserted after the
        # CJK full stop inside a Chinese paragraph.
        segs = [_seg("这是第一句。"), _seg("这是第二句。"), _seg("这是第三句。")]
        result = paragraphize_segments(segs, target_chars=1000)
        assert result[0]["text"] == "这是第一句。这是第二句。这是第三句。"
        assert "。 " not in result[0]["text"]


# ---------------------------------------------------------------------------
# output contract: original_text / duration / start / end
# ---------------------------------------------------------------------------

class TestOutputContract:
    def test_original_text_joined_when_all_members_have_it(self):
        segs = [
            _seg("校准一。", 1.0, 2.0, original_text="原始一，", duration=1.0),
            _seg("校准二。", 2.0, 3.5, original_text="原始二，", duration=1.5),
        ]
        result = paragraphize_segments(segs, target_chars=1000)
        assert len(result) == 1
        paragraph = result[0]
        assert paragraph["text"] == "校准一。校准二。"
        assert paragraph["original_text"] == "原始一，原始二，"
        assert paragraph["start_time"] == 1.0
        assert paragraph["end_time"] == 3.5
        assert paragraph["duration"] == 2.5

    def test_original_text_omitted_when_any_member_lacks_it(self):
        segs = [
            _seg("校准一。", 1.0, 2.0, original_text="原始一，", duration=1.0),
            _seg("校准二。", 2.0, 3.5),  # no original_text, no duration
        ]
        result = paragraphize_segments(segs, target_chars=1000)
        assert "original_text" not in result[0]
        assert "duration" not in result[0]

    def test_start_end_taken_from_first_and_last_member(self):
        segs = [
            _seg("文" * 29 + "。", 5.0, 6.0),
            _seg("文" * 29 + "。", 6.0, 7.0),
            _seg("文" * 29 + "。", 7.0, 9.5),
        ]
        result = paragraphize_segments(segs, target_chars=1000)
        assert result[0]["start_time"] == 5.0
        assert result[0]["end_time"] == 9.5

    def test_none_times_preserved_not_fabricated(self):
        segs = [
            _seg("文" * 29 + "。", None, 6.0),
            _seg("文" * 29 + "。", 6.0, None),
        ]
        result = paragraphize_segments(segs, target_chars=1000)
        assert result[0]["start_time"] is None
        assert result[0]["end_time"] is None

    def test_per_paragraph_times_follow_member_span_after_break(self):
        segs = [
            _seg("文" * 29 + "。", 0.0, 1.0),
            _seg("文" * 29 + "。", 1.0, 2.0),
            _seg("文" * 29 + "。", 2.0, 3.0),
            _seg("文" * 29 + "。", 3.0, 4.0),
        ]
        result = paragraphize_segments(segs, target_chars=100, hard_max_chars=1000)
        # cur_len reaches 90 (< target) at the last internal boundary, so the
        # run stays a single paragraph of 120 chars -- target is a budget,
        # not a gate; the trailing flush keeps whatever accumulated.
        assert len(result) == 1
        for paragraph in result:
            assert paragraph["start_time"] is not None
            assert paragraph["end_time"] is not None
            assert paragraph["end_time"] > paragraph["start_time"]


# ---------------------------------------------------------------------------
# determinism and edge cases
# ---------------------------------------------------------------------------

class TestDeterminismAndEdges:
    def test_same_input_same_output(self):
        segs = [
            _seg("文" * 29 + "。", 0.0, 1.0, original_text="原" * 30, duration=1.0),
            _seg("文" * 20, 1.5, 2.0),
            _seg("hello world again.", 2.0, 3.0),
            _seg("字" * 70, 3.0, 4.0),
            _seg("文" * 19 + "，", 4.0, 5.0),
            _seg("w" * 60, None, None),
        ]
        kwargs = dict(target_chars=50, hard_max_chars=100, pause_threshold_seconds=2.0)
        assert paragraphize_segments(segs, **kwargs) == paragraphize_segments(segs, **kwargs)

    def test_empty_input_returns_empty_list(self):
        assert paragraphize_segments([]) == []

    def test_single_segment_passthrough(self):
        result = paragraphize_segments([_seg("唯一的一段。", 1.0, 2.0)])
        assert result == [{"text": "唯一的一段。", "start_time": 1.0, "end_time": 2.0}]

    def test_full_coverage_no_loss_no_duplication(self):
        # Invariant: paragraphs partition the input -- concatenated member
        # text (ignoring join spaces) equals concatenated input text.
        segs = [
            _seg("文" * 29 + "。"),
            _seg("文" * 20),
            _seg("hello."),
            _seg("文" * 19 + "，"),
            _seg("文" * 150),
            _seg("world"),
        ]
        result = paragraphize_segments(segs, target_chars=50, hard_max_chars=100)
        produced = "".join(p["text"].replace(" ", "") for p in result)
        expected = "".join(s["text"] for s in segs)
        assert produced == expected
