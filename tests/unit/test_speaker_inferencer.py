"""Unit tests for SpeakerInferencer: per-speaker sampling, confidence gating,
cache coverage validation, and prompt group rendering.

Covers the "per-speaker sampling + confidence downgrade" refactor:
- Late-appearing speakers still get sampled with first-appearance context.
- Per-speaker sample count / char caps are enforced.
- Low-confidence mappings are downgraded to a placeholder label ("SpeakerN"),
  not applied as a real name.
- Missing/malformed confidence defaults to 1.0 (applied).
- Cache is only reused when it covers the current speaker set; legacy
  (flat dict) cache format is treated as fully-confident.
- Prompt sample groups are ordered by first-appearance time.
"""

import unittest
from unittest.mock import MagicMock

from video_transcript_api.llm.core.speaker_inferencer import SpeakerInferencer


def _make_dialog(speaker, text, start_time=None):
    return {"speaker": speaker, "text": text, "start_time": start_time}


def _make_inferencer(**kwargs):
    llm_client = MagicMock()
    cache_manager = kwargs.pop("cache_manager", None)
    return SpeakerInferencer(
        llm_client=llm_client,
        cache_manager=cache_manager,
        model="test-model",
        **kwargs,
    )


def _mock_llm_response(speaker_mapping, confidence, reasoning="because"):
    response = MagicMock()
    response.structured_output = {
        "speaker_mapping": speaker_mapping,
        "confidence": confidence,
        "reasoning": reasoning,
    }
    return response


class TestPerSpeakerSampling(unittest.TestCase):
    """Sampling must be per-speaker across the whole timeline, not a global head truncation."""

    def test_late_appearing_speaker_gets_samples_and_context(self):
        """A speaker whose first line is deep in the transcript (index 15 of 20+)
        must still receive its own samples plus the context that precedes it."""
        inferencer = _make_inferencer(samples_per_speaker=3, context_dialogs=2)

        dialogs = []
        # Speaker1 dominates the first 15 turns.
        for i in range(15):
            dialogs.append(_make_dialog("Speaker1", f"Speaker1 line {i}", start_time=float(i)))
        # Someone name-drops Speaker2 right before they show up (identity signal).
        dialogs[13] = _make_dialog("Speaker1", "接下来有请王老师发言", start_time=13.0)
        dialogs[14] = _make_dialog("Speaker1", "王老师你先来", start_time=14.0)
        # Speaker2 finally appears at index 15.
        dialogs.append(_make_dialog("Speaker2", "大家好我是王老师", start_time=15.0))
        for i in range(16, 22):
            dialogs.append(_make_dialog("Speaker1", f"Speaker1 line {i}", start_time=float(i)))

        samples = inferencer._extract_sample_dialogs(dialogs, ["Speaker1", "Speaker2"])

        self.assertIn("Speaker2", samples)
        speaker2 = samples["Speaker2"]
        self.assertEqual(speaker2["first_seen_index"], 15)
        self.assertEqual(speaker2["first_seen_time"], "00:00:15")
        self.assertTrue(speaker2["own_samples"], "late speaker must still get its own samples")
        self.assertIn("大家好我是王老师", speaker2["own_samples"][0])

        # Context must be the 2 dialogs immediately preceding Speaker2's entrance,
        # in chronological order, and must NOT include Speaker2's own lines.
        context = speaker2["context_before"]
        self.assertEqual(len(context), 2)
        self.assertEqual([c[0] for c in context], ["Speaker1", "Speaker1"])
        self.assertIn("接下来有请王老师发言", context[0][1])
        self.assertIn("王老师你先来", context[1][1])

    def test_speaker_with_no_dialogs_is_excluded(self):
        inferencer = _make_inferencer()
        dialogs = [_make_dialog("Speaker1", "hello", start_time=0.0)]

        samples = inferencer._extract_sample_dialogs(dialogs, ["Speaker1", "Speaker2"])

        self.assertIn("Speaker1", samples)
        self.assertNotIn("Speaker2", samples)


class TestSamplingCaps(unittest.TestCase):
    """Per-speaker sample count and char-budget caps must be enforced."""

    def test_sample_count_cap(self):
        inferencer = _make_inferencer(samples_per_speaker=2, max_chars_per_speaker=10000)
        dialogs = [_make_dialog("A", f"line {i}", start_time=float(i)) for i in range(10)]

        samples = inferencer._extract_sample_dialogs(dialogs, ["A"])

        self.assertEqual(len(samples["A"]["own_samples"]), 2)

    def test_total_char_cap(self):
        inferencer = _make_inferencer(samples_per_speaker=10, max_chars_per_speaker=50)
        # Each line is 30 chars; with a 50-char total budget, at most 1 full line fits
        # plus a truncated remainder of the second.
        long_line = "x" * 30
        dialogs = [_make_dialog("A", long_line, start_time=float(i)) for i in range(5)]

        samples = inferencer._extract_sample_dialogs(dialogs, ["A"])
        total_chars = sum(len(s) for s in samples["A"]["own_samples"])

        self.assertLessEqual(total_chars, 50)

    def test_single_sample_truncated_to_120_chars(self):
        inferencer = _make_inferencer(samples_per_speaker=1, max_chars_per_speaker=10000)
        very_long = "y" * 500
        dialogs = [_make_dialog("A", very_long, start_time=0.0)]

        samples = inferencer._extract_sample_dialogs(dialogs, ["A"])

        self.assertEqual(len(samples["A"]["own_samples"][0]), 120)


class TestPromptGroupRendering(unittest.TestCase):
    """Sample groups must render sorted by first-appearance order with clear structure."""

    def test_groups_ordered_by_first_appearance(self):
        inferencer = _make_inferencer()
        dialogs = [
            _make_dialog("Speaker2", "second speaker first line", start_time=5.0),
            _make_dialog("Speaker1", "first speaker first line", start_time=10.0),
        ]

        samples = inferencer._extract_sample_dialogs(dialogs, ["Speaker1", "Speaker2"])
        text = inferencer._format_sample_dialogs(samples)

        # Speaker2 appeared first in time (index 0), so its group must render first.
        self.assertLess(text.index("[Speaker2]"), text.index("[Speaker1]"))
        self.assertIn("首次出现于 00:00:05", text)
        self.assertIn("首次出现于 00:00:10", text)

    def test_missing_timestamp_is_omitted(self):
        inferencer = _make_inferencer()
        dialogs = [_make_dialog("Speaker1", "hello", start_time=None)]

        samples = inferencer._extract_sample_dialogs(dialogs, ["Speaker1"])
        text = inferencer._format_sample_dialogs(samples)

        self.assertIn("[Speaker1]", text)
        self.assertNotIn("首次出现于", text)

    def test_context_and_own_sample_sections_present(self):
        inferencer = _make_inferencer(context_dialogs=1)
        dialogs = [
            _make_dialog("Speaker1", "介绍一下王老师", start_time=0.0),
            _make_dialog("Speaker2", "大家好", start_time=1.0),
        ]

        samples = inferencer._extract_sample_dialogs(dialogs, ["Speaker1", "Speaker2"])
        text = inferencer._format_sample_dialogs(samples)

        self.assertIn("上下文（出场前他人发言，可能包含称呼线索）：", text)
        self.assertIn("本人发言样本：", text)


class TestConfidenceGating(unittest.TestCase):
    """Low-confidence mappings must be downgraded to a placeholder label, not applied."""

    def _run_infer(self, speaker_mapping, confidence, threshold=0.6):
        inferencer = _make_inferencer(confidence_threshold=threshold)
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm_response(speaker_mapping, confidence)
        )
        dialogs = [
            _make_dialog("Speaker1", "hello there", start_time=0.0),
            _make_dialog("Speaker2", "hi back", start_time=1.0),
        ]
        return inferencer.infer(
            speakers=["Speaker1", "Speaker2"],
            dialogs=dialogs,
            title="t",
        )

    def test_low_confidence_downgraded_to_placeholder(self):
        result = self._run_infer(
            speaker_mapping={"Speaker1": "张三", "Speaker2": "李四"},
            confidence={"Speaker1": 0.9, "Speaker2": 0.3},
        )

        self.assertEqual(result["mapping"]["Speaker1"], "张三")
        self.assertEqual(result["mapping"]["Speaker2"], "说话人2")
        self.assertTrue(result["meta"]["Speaker1"]["applied"])
        self.assertFalse(result["meta"]["Speaker2"]["applied"])
        self.assertEqual(result["meta"]["Speaker2"]["confidence"], 0.3)
        # The originally inferred (but unapplied) name is still recorded for audit purposes.
        self.assertEqual(result["meta"]["Speaker2"]["name"], "李四")
        self.assertEqual(result["low_confidence"], ["Speaker2"])

    def test_fallback_label_uses_numeric_suffix_of_original_label(self):
        """The placeholder number must come from the label's own digit (Speaker7 -> 7),
        not from its position in the speakers list (which would be 2nd -> 2)."""
        inferencer = _make_inferencer(confidence_threshold=0.6)
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm_response(
                speaker_mapping={"Speaker1": "张三", "Speaker7": "李四"},
                confidence={"Speaker1": 0.9, "Speaker7": 0.1},
            )
        )
        dialogs = [
            _make_dialog("Speaker1", "hello", start_time=0.0),
            _make_dialog("Speaker7", "hi", start_time=1.0),
        ]

        result = inferencer.infer(speakers=["Speaker1", "Speaker7"], dialogs=dialogs, title="t")

        self.assertEqual(result["mapping"]["Speaker7"], "说话人7")

    def test_fallback_label_falls_back_to_position_when_no_digit(self):
        inferencer = _make_inferencer(confidence_threshold=0.6)
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm_response(
                speaker_mapping={"alpha": "张三", "beta": "李四"},
                confidence={"alpha": 0.9, "beta": 0.1},
            )
        )
        dialogs = [
            _make_dialog("alpha", "hello", start_time=0.0),
            _make_dialog("beta", "hi", start_time=1.0),
        ]

        result = inferencer.infer(speakers=["alpha", "beta"], dialogs=dialogs, title="t")

        # "beta" has no digit -> falls back to its 1-based position in speakers list (2nd).
        self.assertEqual(result["mapping"]["beta"], "说话人2")

    def test_missing_confidence_defaults_to_one_and_applies(self):
        inferencer = _make_inferencer(confidence_threshold=0.6)
        response = MagicMock()
        response.structured_output = {
            "speaker_mapping": {"Speaker1": "张三", "Speaker2": "李四"},
            "reasoning": "no confidence field at all",
        }
        inferencer.llm_client.call = MagicMock(return_value=response)
        dialogs = [
            _make_dialog("Speaker1", "hello", start_time=0.0),
            _make_dialog("Speaker2", "hi", start_time=1.0),
        ]

        result = inferencer.infer(speakers=["Speaker1", "Speaker2"], dialogs=dialogs, title="t")

        self.assertEqual(result["meta"]["Speaker1"]["confidence"], 1.0)
        self.assertTrue(result["meta"]["Speaker1"]["applied"])
        self.assertEqual(result["mapping"]["Speaker1"], "张三")

    def test_malformed_confidence_value_defaults_to_one(self):
        result = self._run_infer(
            speaker_mapping={"Speaker1": "张三", "Speaker2": "李四"},
            confidence={"Speaker1": "not-a-number", "Speaker2": 0.9},
        )

        self.assertEqual(result["meta"]["Speaker1"]["confidence"], 1.0)
        self.assertTrue(result["meta"]["Speaker1"]["applied"])

    def test_scalar_confidence_applies_to_all_speakers(self):
        result = self._run_infer(
            speaker_mapping={"Speaker1": "张三", "Speaker2": "李四"},
            confidence=0.9,
        )

        self.assertEqual(result["meta"]["Speaker1"]["confidence"], 0.9)
        self.assertEqual(result["meta"]["Speaker2"]["confidence"], 0.9)
        self.assertTrue(result["meta"]["Speaker1"]["applied"])
        self.assertTrue(result["meta"]["Speaker2"]["applied"])


class TestCacheCoverageValidation(unittest.TestCase):
    """Cached mapping must cover the current speaker set to be reused."""

    def test_cache_hit_when_covers_current_speakers(self):
        cache_manager = MagicMock()
        cache_manager.get_speaker_mapping.return_value = {
            "mapping": {"Speaker1": "张三", "Speaker2": "李四"},
            "meta": {
                "Speaker1": {"name": "张三", "confidence": 0.95, "applied": True},
                "Speaker2": {"name": "李四", "confidence": 0.95, "applied": True},
            },
            "low_confidence": [],
        }
        inferencer = _make_inferencer(cache_manager=cache_manager)

        result = inferencer.infer(
            speakers=["Speaker1", "Speaker2"],
            dialogs=[_make_dialog("Speaker1", "hi", 0.0)],
            title="t",
            platform="p",
            media_id="m",
        )

        inferencer.llm_client.call.assert_not_called()
        self.assertEqual(result["mapping"], {"Speaker1": "张三", "Speaker2": "李四"})

    def test_cache_miss_when_missing_a_speaker_triggers_reinference(self):
        cache_manager = MagicMock()
        # Legacy flat-dict cache only covers Speaker1; Speaker2 is new this run.
        cache_manager.get_speaker_mapping.return_value = {"Speaker1": "张三"}
        inferencer = _make_inferencer(cache_manager=cache_manager)
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm_response(
                speaker_mapping={"Speaker1": "张三", "Speaker2": "李四"},
                confidence={"Speaker1": 0.9, "Speaker2": 0.9},
            )
        )

        result = inferencer.infer(
            speakers=["Speaker1", "Speaker2"],
            dialogs=[
                _make_dialog("Speaker1", "hi", 0.0),
                _make_dialog("Speaker2", "hello", 1.0),
            ],
            title="t",
            platform="p",
            media_id="m",
        )

        inferencer.llm_client.call.assert_called_once()
        self.assertEqual(result["mapping"]["Speaker2"], "李四")
        # Re-inferred result must be persisted back (overwriting the stale cache).
        cache_manager.save_speaker_mapping.assert_called_once()
        saved_args = cache_manager.save_speaker_mapping.call_args[0]
        self.assertEqual(saved_args[0], "p")
        self.assertEqual(saved_args[1], "m")
        self.assertIn("mapping", saved_args[2])
        self.assertIn("meta", saved_args[2])

    def test_legacy_flat_cache_treated_as_full_confidence(self):
        cache_manager = MagicMock()
        cache_manager.get_speaker_mapping.return_value = {
            "Speaker1": "张三",
            "Speaker2": "李四",
        }
        inferencer = _make_inferencer(cache_manager=cache_manager)

        result = inferencer.infer(
            speakers=["Speaker1", "Speaker2"],
            dialogs=[_make_dialog("Speaker1", "hi", 0.0)],
            title="t",
            platform="p",
            media_id="m",
        )

        inferencer.llm_client.call.assert_not_called()
        self.assertEqual(result["meta"]["Speaker1"]["confidence"], 1.0)
        self.assertTrue(result["meta"]["Speaker1"]["applied"])


class TestNoSamplesAndFailureFallback(unittest.TestCase):
    def test_empty_speakers_returns_empty_result(self):
        inferencer = _make_inferencer()
        result = inferencer.infer(speakers=[], dialogs=[], title="t")
        self.assertEqual(result, {"mapping": {}, "meta": {}, "low_confidence": []})

    def test_no_valid_samples_falls_back_to_identity(self):
        inferencer = _make_inferencer()
        result = inferencer.infer(
            speakers=["Speaker1"],
            dialogs=[{"speaker": "Speaker1", "text": ""}],  # empty text -> no sample
            title="t",
        )
        self.assertEqual(result["mapping"], {"Speaker1": "Speaker1"})
        self.assertFalse(result["meta"]["Speaker1"]["applied"])

    def test_llm_call_exception_falls_back_to_identity(self):
        inferencer = _make_inferencer()
        inferencer.llm_client.call = MagicMock(side_effect=Exception("boom"))

        result = inferencer.infer(
            speakers=["Speaker1"],
            dialogs=[_make_dialog("Speaker1", "hello", 0.0)],
            title="t",
        )

        self.assertEqual(result["mapping"], {"Speaker1": "Speaker1"})
        self.assertFalse(result["meta"]["Speaker1"]["applied"])


if __name__ == "__main__":
    unittest.main()
