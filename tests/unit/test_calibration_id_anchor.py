"""Tests for ID-anchored calibration merge (_apply_corrections_by_id).

The ID-anchored redesign makes timestamps/speakers/turn-count ground truth owned
by the deterministic pipeline. The LLM only returns {id, text} corrections; merge
is by-id lookup, so a structure mismatch can never discard a whole chunk.

All assertions are deterministic (no real LLM call).
"""

import unittest
from unittest.mock import MagicMock

from video_transcript_api.llm.processors.speaker_aware_processor import (
    SpeakerAwareProcessor,
)
from video_transcript_api.llm.core.config import LLMConfig


def _make_processor():
    config = MagicMock(spec=LLMConfig)
    config.calibrate_model = "test-model"
    config.calibrate_reasoning_effort = None
    config.max_calibration_retries = 0
    config.structured_fallback_strategy = "original"
    config.structured_validation_enabled = False
    config.calibration_concurrent_limit = 1
    config.min_calibrate_ratio = 0.8
    config.chunk_time_budget = 300
    config.min_correction_coverage = 0.5

    return SpeakerAwareProcessor(
        config=config,
        llm_client=MagicMock(),
        key_info_extractor=MagicMock(),
        speaker_inferencer=MagicMock(),
        quality_validator=MagicMock(),
    )


def _chunk(n):
    return [
        {
            "speaker": f"S{i % 2}",
            "text": f"raw {i}",
            "start_time": f"00:00:0{i}",
            "end_time": f"00:00:1{i}",
            "duration": 10.0,
        }
        for i in range(n)
    ]


class TestApplyCorrectionsById(unittest.TestCase):
    def test_all_hit(self):
        """Every id returned -> every text replaced, all counted applied."""
        p = _make_processor()
        chunk = _chunk(3)
        corr = [{"id": i, "text": f"fixed {i}"} for i in range(3)]
        merged, counts = p._apply_corrections_by_id(corr, chunk)
        self.assertEqual([d["text"] for d in merged], ["fixed 0", "fixed 1", "fixed 2"])
        self.assertEqual(counts["applied"], 3)
        self.assertEqual(counts["kept_original"], 0)

    def test_missing_id_keeps_original(self):
        """REGRESSION (VOL.170): id 1 missing -> only dialog 1 keeps raw text,
        the rest are still corrected. Never whole-chunk loss."""
        p = _make_processor()
        chunk = _chunk(3)
        corr = [{"id": 0, "text": "fixed 0"}, {"id": 2, "text": "fixed 2"}]
        merged, counts = p._apply_corrections_by_id(corr, chunk)
        self.assertEqual([d["text"] for d in merged], ["fixed 0", "raw 1", "fixed 2"])
        self.assertEqual(counts["applied"], 2)
        self.assertEqual(counts["kept_original"], 1)

    def test_unknown_id_ignored(self):
        """id out of range -> ignored, counted, does not crash."""
        p = _make_processor()
        chunk = _chunk(2)
        corr = [{"id": 0, "text": "fixed 0"}, {"id": 99, "text": "ghost"}]
        merged, counts = p._apply_corrections_by_id(corr, chunk)
        self.assertEqual([d["text"] for d in merged], ["fixed 0", "raw 1"])
        self.assertEqual(counts["applied"], 1)
        self.assertEqual(counts["unknown_id"], 1)

    def test_duplicate_id_first_wins(self):
        """Duplicate id -> first wins, second counted as duplicate."""
        p = _make_processor()
        chunk = _chunk(2)
        corr = [{"id": 0, "text": "first"}, {"id": 0, "text": "second"}]
        merged, counts = p._apply_corrections_by_id(corr, chunk)
        self.assertEqual(merged[0]["text"], "first")
        self.assertEqual(counts["duplicate_id"], 1)
        self.assertEqual(counts["applied"], 1)

    def test_empty_corrections_keeps_all_original(self):
        """Empty list -> whole chunk keeps original text (graceful, not a crash)."""
        p = _make_processor()
        chunk = _chunk(3)
        merged, counts = p._apply_corrections_by_id([], chunk)
        self.assertEqual([d["text"] for d in merged], ["raw 0", "raw 1", "raw 2"])
        self.assertEqual(counts["applied"], 0)
        self.assertEqual(counts["kept_original"], 3)

    def test_speaker_and_time_always_from_original(self):
        """LLM cannot affect speaker/time even if it tries to send them."""
        p = _make_processor()
        chunk = _chunk(2)
        corr = [
            {"id": 0, "text": "fixed 0", "speaker": "HACKED", "start_time": "99:99:99"},
            {"id": 1, "text": "fixed 1"},
        ]
        merged, _ = p._apply_corrections_by_id(corr, chunk)
        for idx, d in enumerate(merged):
            self.assertEqual(d["speaker"], chunk[idx]["speaker"])
            self.assertEqual(d["start_time"], chunk[idx]["start_time"])
            self.assertEqual(d["end_time"], chunk[idx]["end_time"])

    def test_malformed_text_rejected(self):
        """Empty/whitespace text, echoed [id][spk] tag, or non-str -> malformed,
        original kept for that dialog."""
        p = _make_processor()
        chunk = _chunk(4)
        corr = [
            {"id": 0, "text": ""},                 # empty
            {"id": 1, "text": "   "},              # whitespace only
            {"id": 2, "text": "[2][S0]: echoed"},  # echoed prompt format
            {"id": 3, "text": 12345},              # non-str
        ]
        merged, counts = p._apply_corrections_by_id(corr, chunk)
        self.assertEqual([d["text"] for d in merged], ["raw 0", "raw 1", "raw 2", "raw 3"])
        self.assertEqual(counts["malformed"], 4)
        self.assertEqual(counts["applied"], 0)

    def test_malformed_id_rejected(self):
        """Non-integer / non-coercible id -> malformed; '3' digit-string coerces."""
        p = _make_processor()
        chunk = _chunk(4)
        corr = [
            {"id": "3", "text": "coerced"},   # digit string -> 3
            {"id": 1.5, "text": "floaty"},    # non-integral float -> malformed
            {"id": "abc", "text": "nope"},    # junk -> malformed
            {"text": "no id"},                # missing id -> malformed
        ]
        merged, counts = p._apply_corrections_by_id(corr, chunk)
        self.assertEqual(merged[3]["text"], "coerced")
        self.assertEqual(counts["applied"], 1)
        self.assertEqual(counts["malformed"], 3)


if __name__ == "__main__":
    unittest.main()
