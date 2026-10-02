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


@pytest.mark.parametrize("max_chunk_length", [1500])
def test_short_dialog_is_not_reshaped(max_chunk_length: int):
    segmenter = _make_segmenter(max_chunk_length)
    chunks = _chunks_for(segmenter, "短对话")
    assert len(chunks) == 1
    assert chunks[0][0]["text"] == "短对话"
