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
            # speaker_id is the raw, pre-mapping label SpeakerAwareProcessor.
            # _normalize_dialog always attaches alongside "speaker" in real
            # pipeline output (distinct value here so tests can tell the two
            # fields apart instead of accidentally passing via a coincidental
            # match).
            "speaker_id": f"raw_S{i % 2}",
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

    def test_speaker_id_preserved_through_merge(self):
        """Local codex review round 5, F4: _apply_corrections_by_id rebuilds
        a brand-new dialog dict per merged row, copying only time/speaker/
        text/original_text -- it used to silently drop "speaker_id" (the
        raw, pre-mapping label SpeakerAwareProcessor._normalize_dialog
        attaches to every dialog it produces). Any real calibration run
        (which always goes through this function) therefore emitted
        dialogs indistinguishable from the pre-schema-migration legacy
        format, and llm_ops.py's
        _refresh_speaker_names_in_existing_structured_artifact would treat
        a freshly-produced, real artifact as "no raw label to key off",
        skip the name refresh, and log a stale warning about legacy data --
        even though the schema had already moved on. Locks in that
        speaker_id survives verbatim through the merge for every id bucket
        (applied via correction AND kept-original), not just the "text"
        field the pre-existing tests above already cover."""
        p = _make_processor()
        chunk = _chunk(3)
        # id 1 is intentionally omitted so this also covers the
        # kept_original branch, not just the corrected/applied one.
        corr = [{"id": 0, "text": "fixed 0"}, {"id": 2, "text": "fixed 2"}]
        merged, counts = p._apply_corrections_by_id(corr, chunk)
        self.assertEqual(
            [d["speaker_id"] for d in merged],
            [c["speaker_id"] for c in chunk],
        )
        self.assertEqual(counts["applied"], 2)
        self.assertEqual(counts["kept_original"], 1)

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


class TestVol170Regression(unittest.TestCase):
    """End-to-end (deterministic mock) regression for the VOL.170 bug:
    key_info has the correct '威皇小海鲜' but the ASR chunk says '微煌'. Under the
    OLD whole-chunk-revert design, any dialog-count drift discarded the whole
    chunk and the correction was lost. With ID anchoring the correction survives
    even when the LLM omits some ids."""

    def _run(self, corrections):
        from video_transcript_api.llm.core.key_info_extractor import KeyInfo

        p = _make_processor()
        # 3 dialogs mirroring the real funasr segments around 00:49:54
        chunk = [
            {"speaker": "惠子", "text": "微煌小海鲜？", "start_time": "00:49:54",
             "end_time": "00:49:56", "duration": 2.0},
            {"speaker": "肥姐", "text": "威煌小海鲜是叫这个名字吗？", "start_time": "00:49:56",
             "end_time": "00:50:01", "duration": 5.0},
            {"speaker": "惠子", "text": "微煌小海鲜，特别有特色的花雕鸡肉锅底", "start_time": "00:50:01",
             "end_time": "00:50:09", "duration": 8.0},
        ]
        result = MagicMock()
        result.structured_output = {"corrections": corrections}
        p.llm_client.call = MagicMock(return_value=result)
        key_info = KeyInfo(
            names=[], places=[], technical_terms=[],
            brands=["威皇小海鲜"], abbreviations=[], foreign_terms=[], other_entities=[],
        )
        calibrated_chunks, stats = p._calibrate_chunks(
            chunks=[chunk], original_chunks=[chunk], key_info=key_info,
            speaker_mapping={}, title="越山吃海", description="威皇小海鲜的花雕鸡火锅",
            selected_models={"calibrate_model": "test-model", "calibrate_reasoning_effort": None},
            language="zh",
        )
        return calibrated_chunks[0], stats

    def test_full_coverage_applies_brand_fix(self):
        """LLM returns all 3 ids with 微煌/威煌 -> 威皇: every dialog corrected."""
        merged, stats = self._run([
            {"id": 0, "text": "威皇小海鲜？"},
            {"id": 1, "text": "威皇小海鲜是叫这个名字吗？"},
            {"id": 2, "text": "威皇小海鲜，特别有特色的花雕鸡锅底"},
        ])
        joined = " ".join(d["text"] for d in merged)
        self.assertIn("威皇小海鲜", joined)
        self.assertNotIn("微煌", joined)
        self.assertEqual(stats["success_count"], 1)

    def test_partial_coverage_still_keeps_brand_fix(self):
        """Even if the LLM omits id=1, the 威皇 fix on ids 0/2 survives — the
        whole chunk is NOT discarded (the exact old-design failure)."""
        merged, stats = self._run([
            {"id": 0, "text": "威皇小海鲜？"},
            {"id": 2, "text": "威皇小海鲜，特别有特色的花雕鸡锅底"},
        ])
        self.assertEqual(merged[0]["text"], "威皇小海鲜？")
        self.assertEqual(merged[2]["text"], "威皇小海鲜，特别有特色的花雕鸡锅底")
        # id=1 omitted -> keeps original ASR text, not discarded
        self.assertEqual(merged[1]["text"], "威煌小海鲜是叫这个名字吗？")
        self.assertEqual(stats["fallback_count"], 0)
        self.assertEqual(stats["dialog_counts"]["applied"], 2)


if __name__ == "__main__":
    unittest.main()
