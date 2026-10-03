"""DialogSegmenter max_chunk_length fallback tests (issue #142).

Covers the case where the sentence-level split cannot enforce the cap: text
without CJK sentence punctuation (English monologue) or with a single sentence
longer than the cap must still come back as chunks within max_chunk_length,
with the body text preserved and the timeline monotonic.

All console output must be English only (no emoji, no Chinese).
"""

from __future__ import annotations

import re
from unittest.mock import Mock

import pytest

from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.segmenters.dialog_segmenter import DialogSegmenter
from video_transcript_api.transcriber.segments import parse_time_to_seconds


def _make_segmenter(max_chunk_length: int = 1500) -> DialogSegmenter:
    config = Mock(
        spec=LLMConfig,
        min_chunk_length=50,
        max_chunk_length=max_chunk_length,
        preferred_chunk_length=800,
    )
    return DialogSegmenter(config)


def _chunks_for(segmenter: DialogSegmenter, text: str) -> list[list[dict]]:
    return segmenter.segment(
        [
            {
                "id": "1",
                "speaker_id": "A",
                "text": text,
                "start_time": "00:00:00",
                "end_time": "00:10:00",
                "duration": "00:10:00",
            }
        ]
    )


class TestDialogSegmenterChunkCap:
    """max_chunk_length is an input-size bound for a single LLM call, so the
    producer must not silently give up when sentences cannot be found."""

    def test_english_without_punctuation_is_split_within_cap(self):
        text = " ".join(["this is a spoken monologue sentence without any comma"] * 40)
        assert len(text) > 1500
        segmenter = _make_segmenter()

        chunks = _chunks_for(segmenter, text)

        assert len(chunks) > 1
        for chunk in chunks:
            total = sum(len(dialog.get("text", "")) for dialog in chunk)
            assert total <= segmenter.max_chunk_length
        fragments = [dialog for chunk in chunks for dialog in chunk]
        assert "".join(dialog["text"] for dialog in fragments).replace(
            " ", ""
        ) == text.replace(" ", "")

    def test_single_sentence_longer_than_cap_is_hard_split(self):
        text = "字" * 3200
        segmenter = _make_segmenter()

        chunks = _chunks_for(segmenter, text)

        assert [len(chunk[0]["text"]) for chunk in chunks] == [1500, 1500, 200]
        fragments = [dialog for chunk in chunks for dialog in chunk]
        assert "".join(dialog["text"] for dialog in fragments) == text

    def test_fallback_fragments_keep_dialog_identity_and_timeline(self):
        text = "字" * 3200
        segmenter = _make_segmenter()

        chunks = _chunks_for(segmenter, text)
        fragments = [dialog for chunk in chunks for dialog in chunk]

        assert len(fragments) > 1

        # No id rewriting: calibration anchors on the id set, so hard-cut
        # fragments must reuse the source dialog id unchanged.
        assert {fragment["id"] for fragment in fragments} == {"1"}
        assert all(fragment["time_estimated"] is True for fragment in fragments)

        starts = [parse_time_to_seconds(f["start_time"]) for f in fragments]
        ends = [parse_time_to_seconds(f["end_time"]) for f in fragments]
        assert all(value is not None for value in starts + ends)
        assert starts[0] == parse_time_to_seconds("00:00:00")
        assert ends[-1] == parse_time_to_seconds("00:10:00")
        assert starts == sorted(starts)
        for index in range(1, len(fragments)):
            assert starts[index] == ends[index - 1]
        for start, end in zip(starts, ends):
            assert start <= end
        assert all(
            re.fullmatch(r"\d{2}:\d{2}:\d{2}", fragment["start_time"])
            for fragment in fragments
        )

    def test_punctuation_split_behaviour_is_unchanged(self):
        sentence = "中性测试句子。"
        segmenter = _make_segmenter(max_chunk_length=200)

        chunks = _chunks_for(segmenter, sentence * 200)
        fragments = [dialog for chunk in chunks for dialog in chunk]

        assert [fragment["text"] for fragment in fragments] == [sentence * 28] * 7 + [
            sentence * 4
        ]

    def test_invalid_times_still_degrade_to_none(self):
        text = "字" * 3200
        segmenter = _make_segmenter()

        chunks = segmenter.segment(
            [
                {
                    "id": "1",
                    "speaker_id": "A",
                    "text": text,
                    "start_time": "not-a-time",
                    "end_time": "00:10:00",
                }
            ]
        )
        fragments = [dialog for chunk in chunks for dialog in chunk]

        assert len(fragments) > 1
        assert all(len(fragment["text"]) <= 1500 for fragment in fragments)
        assert "".join(fragment["text"] for fragment in fragments) == text
        assert all(fragment["start_time"] is None for fragment in fragments)
        assert all(fragment["end_time"] is None for fragment in fragments)
        assert all(fragment["duration"] is None for fragment in fragments)


# ---------------------------------------------------------------------------
# issue #156: ASCII "!" / "?" are sentence ends for oversized dialogs.
#
# Scope lock (see design note #156): only the oversized-dialog path splits, and
# ASCII "." stays out of the sentence set on purpose. Tests below pin the split
# shape, verbatim conservation of the direct split, and the pre-existing
# whitespace-tail contract (a whitespace-only fragment is still filtered out).
# ---------------------------------------------------------------------------

_ASCII_SENTENCES = (
    "Is the recording still running?",
    "Please confirm the microphone level!",
    "Did the calibration finish in time?",
    "Great, we can start the export!",
)

_BARE_MARKS = {"!", "?", "!?", "?!", "。", "！", "？"}


def _long_ascii_dialog_text(repeats: int = 12) -> str:
    """English-only text whose sentence ends are ASCII ! and ?."""
    return " ".join(_ASCII_SENTENCES * repeats)


def _plain_style_segmenter(max_chunk_length: int = 200) -> DialogSegmenter:
    """Mirror the no-speaker construction in speaker_aware_processor.py:247."""
    config = Mock(
        spec=LLMConfig,
        min_chunk_length=50,
        max_chunk_length=1500,
        preferred_chunk_length=800,
    )
    return DialogSegmenter(
        config,
        preferred_chunk_length=1000,
        max_chunk_length=max_chunk_length,
    )


class TestDialogSegmenterAsciiSentenceEnd:
    """ASCII !? must end a sentence when a single dialog is oversized."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "Is it ready? Yes, it is! Not yet? Maybe later.",
                ["Is it ready?", " Yes, it is!", " Not yet?", " Maybe later."],
            ),
            (
                "我们试试这个功能! Is it working? 当然可以。",
                ["我们试试这个功能!", " Is it working?", " 当然可以。"],
            ),
            (
                "Really?! I think so! Fine.",
                ["Really?", "!", " I think so!", " Fine."],
            ),
        ],
    )
    def test_direct_split_cuts_at_ascii_marks(self, text, expected):
        segmenter = _make_segmenter()

        assert segmenter._split_by_sentences(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "第一句话。第二句话！第三句话？结束",
                ["第一句话。", "第二句话！", "第三句话？", "结束"],
            ),
            (
                "他说：真的吗？好的。",
                ["他说：真的吗？", "好的。"],
            ),
            (
                "中文句子一；中文句子二。",
                ["中文句子一；中文句子二。"],
            ),
        ],
    )
    def test_cjk_sentence_split_is_item_for_item_unchanged(self, text, expected):
        segmenter = _make_segmenter()

        assert segmenter._split_by_sentences(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "Is it ready? Yes, it is! Not yet? Maybe later.",
            "我们试试这个功能! Is it working? 当然可以。",
            _long_ascii_dialog_text(),
        ],
    )
    def test_direct_split_joins_back_to_the_source_text(self, text):
        """No whitespace-only fragment in these samples, so join must be exact."""
        segmenter = _make_segmenter()

        assert "".join(segmenter._split_by_sentences(text)) == text

    def test_whitespace_only_tail_fragment_is_still_filtered(self):
        """Pre-existing contract: whitespace-only fragments never become sentences."""
        segmenter = _make_segmenter()

        assert segmenter._split_by_sentences("Ready to roll!   \n") == ["Ready to roll!"]

    def test_ascii_period_only_text_is_not_sentence_split(self):
        """Non-goal: "." is not a sentence end in this segmenter (#146 stays open)."""
        text = "This is a plain english sentence without any special mark. " * 12
        segmenter = _make_segmenter(max_chunk_length=200)

        assert len(text) > segmenter.max_chunk_length
        assert segmenter._split_by_sentences(text) == [text]

    @pytest.mark.parametrize("factory", [_make_segmenter, _plain_style_segmenter])
    def test_split_long_dialog_breaks_ascii_text_at_sentence_ends(self, factory):
        segmenter = factory(200)
        text = _long_ascii_dialog_text()

        fragments = segmenter._split_long_dialog(
            {
                "id": "7",
                "speaker_id": "A",
                "text": text,
                "start_time": "00:00:00",
                "end_time": "00:10:00",
            }
        )

        assert len(fragments) == 10
        assert all(len(fragment["text"]) <= segmenter.max_chunk_length for fragment in fragments)
        # Every fragment ends on a sentence mark: the mark rides along with the
        # sentence before it and never flies on its own.
        assert all(fragment["text"].endswith(("!", "?")) for fragment in fragments)
        assert all(fragment["text"] not in _BARE_MARKS for fragment in fragments)
        assert fragments[0]["text"] == (
            "Is the recording still running? Please confirm the microphone level! "
            "Did the calibration finish in time? Great, we can start the export! "
            "Is the recording still running?"
        )
        assert fragments[-1]["text"] == (
            "Please confirm the microphone level! "
            "Did the calibration finish in time? "
            "Great, we can start the export!"
        )
        assert "".join(fragment["text"] for fragment in fragments).replace(
            " ", ""
        ) == text.replace(" ", "")

    @pytest.mark.parametrize("factory", [_make_segmenter, _plain_style_segmenter])
    def test_full_ascii_dialog_chunking_stays_within_cap(self, factory):
        segmenter = factory(200)
        text = _long_ascii_dialog_text()

        chunks = _chunks_for(segmenter, text)
        fragments = [dialog for chunk in chunks for dialog in chunk]

        # Each fragment of an oversized dialog becomes its own single-dialog chunk.
        assert len(chunks) == len(fragments) > 1
        assert all(len(chunk) == 1 for chunk in chunks)
        assert all(
            len(dialog["text"]) <= segmenter.max_chunk_length for dialog in fragments
        )
        assert all(dialog["text"].endswith(("!", "?")) for dialog in fragments)
        assert all(dialog["text"] not in _BARE_MARKS for dialog in fragments)
        assert "".join(dialog["text"] for dialog in fragments).replace(
            " ", ""
        ) == text.replace(" ", "")

    @pytest.mark.parametrize("factory", [_make_segmenter, _plain_style_segmenter])
    def test_full_ascii_dialog_keeps_identity_and_timeline(self, factory):
        segmenter = factory(200)
        text = _long_ascii_dialog_text()

        chunks = _chunks_for(segmenter, text)
        fragments = [dialog for chunk in chunks for dialog in chunk]

        assert {fragment["id"] for fragment in fragments} == {"1"}
        assert all(fragment["time_estimated"] is True for fragment in fragments)

        starts = [parse_time_to_seconds(fragment["start_time"]) for fragment in fragments]
        ends = [parse_time_to_seconds(fragment["end_time"]) for fragment in fragments]
        assert all(value is not None for value in starts + ends)
        assert starts[0] == parse_time_to_seconds("00:00:00")
        assert ends[-1] == parse_time_to_seconds("00:10:00")
        assert starts == sorted(starts)
        for index in range(1, len(fragments)):
            assert starts[index] == ends[index - 1]
        for start, end in zip(starts, ends):
            assert start < end

    def test_mixed_cjk_ascii_dialog_keeps_both_mark_families(self):
        segmenter = _make_segmenter(max_chunk_length=200)
        unit = "我们试试这个功能! Is it working? 当然可以。"
        text = unit * 30

        chunks = _chunks_for(segmenter, text)
        fragments = [dialog for chunk in chunks for dialog in chunk]

        assert len(fragments) > 1
        assert all(len(dialog["text"]) <= segmenter.max_chunk_length for dialog in fragments)
        assert all(dialog["text"].endswith(("!", "?", "。")) for dialog in fragments)
        assert all(dialog["text"] not in _BARE_MARKS for dialog in fragments)
        assert "".join(dialog["text"] for dialog in fragments).replace(
            " ", ""
        ) == text.replace(" ", "")


@pytest.mark.parametrize("max_chunk_length", [1500])
def test_short_dialog_is_not_reshaped(max_chunk_length: int):
    segmenter = _make_segmenter(max_chunk_length)
    chunks = _chunks_for(segmenter, "短对话")
    assert len(chunks) == 1
    assert chunks[0][0]["text"] == "短对话"
