"""Unit tests for SpeakerInferencer: per-speaker sampling, confidence gating,
cache coverage validation, and prompt group rendering.

Covers the "per-speaker sampling + confidence downgrade" refactor:
- Late-appearing speakers still get sampled with first-appearance context.
- Per-speaker sample count / char caps are enforced.
- Low-confidence mappings are downgraded to a placeholder label ("SpeakerN"),
  not applied as a real name.
- Missing/malformed confidence defaults to 1.0 (applied).
- Cache is only reused when it covers the current speaker set AND carries
  real confidence data; legacy (flat dict, pre-confidence-gating) cache
  format is treated as unusable and forces re-inference (ci-gate review).
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


class TestGlobalSampleCharBudget(unittest.TestCase):
    """A global char budget guards against runaway prompt size when diarization
    produces many (possibly spurious) speaker labels -- P2 codex-review R12."""

    def test_many_speakers_total_sample_chars_capped(self):
        """50+ speakers, each with plenty of dialog, must not blow past the
        configured global character budget."""
        num_speakers = 60
        speakers = [f"Speaker{i}" for i in range(num_speakers)]
        dialogs = []
        t = 0.0
        for speaker in speakers:
            # 4 lines x 100 chars each so every speaker can fill its full
            # 400-char per-speaker quota (mirrors a real multi-turn transcript).
            for _ in range(4):
                dialogs.append(_make_dialog(speaker, "x" * 100, start_time=t))
                t += 1.0

        inferencer = _make_inferencer(
            samples_per_speaker=3, max_chars_per_speaker=400, max_total_sample_chars=8000
        )
        samples = inferencer._extract_sample_dialogs(dialogs, speakers)

        total_chars = sum(len(s) for info in samples.values() for s in info["own_samples"])
        self.assertLessEqual(total_chars, 8000)
        # With 60 speakers and 400 chars/speaker (~24000 raw), the cap must
        # actually have dropped some speakers, not just silently no-op.
        self.assertLess(len(samples), num_speakers)

    def test_normal_scenario_unaffected_by_global_budget(self):
        """A handful of speakers, well within the global budget, must behave
        identically to the pre-cap behavior: everyone gets their full quota."""
        inferencer = _make_inferencer(
            samples_per_speaker=3, max_chars_per_speaker=400, max_total_sample_chars=8000
        )
        speakers = ["Speaker1", "Speaker2", "Speaker3"]
        dialogs = []
        for i, speaker in enumerate(speakers):
            for j in range(3):
                dialogs.append(
                    _make_dialog(
                        speaker, f"{speaker} says something #{j}", start_time=float(i * 3 + j)
                    )
                )

        samples = inferencer._extract_sample_dialogs(dialogs, speakers)

        for speaker in speakers:
            self.assertIn(speaker, samples)
            self.assertEqual(len(samples[speaker]["own_samples"]), 3)

    def test_budget_is_configurable_and_triggers_earlier_when_smaller(self):
        """Shrinking the budget must cause later-appearing speakers to be
        dropped sooner (an acceptable degradation: latest arrivals lose out).

        context_dialogs=0 isolates this test to own_samples accounting only
        (each dialog line here is from a different speaker, so with the
        default context_dialogs=2 every speaker's context_before would pull
        in the preceding speaker's line too -- exercised separately and
        deliberately in test_context_before_counts_toward_global_budget)."""
        speakers = [f"Speaker{i}" for i in range(10)]
        dialogs = [
            _make_dialog(speaker, "x" * 100, start_time=float(i))
            for i, speaker in enumerate(speakers)
        ]

        inferencer = _make_inferencer(
            samples_per_speaker=1,
            max_chars_per_speaker=100,
            max_total_sample_chars=250,
            context_dialogs=0,
        )
        samples = inferencer._extract_sample_dialogs(dialogs, speakers)

        # Budget fits exactly 2 full speakers (100 + 100 <= 250, +1 more would
        # exceed it), so the earliest-appearing speakers survive and the rest
        # are excluded via the existing "no samples" fallback path.
        self.assertIn("Speaker0", samples)
        self.assertIn("Speaker1", samples)
        self.assertNotIn("Speaker9", samples)

    def test_late_appearing_speaker_still_sampled_when_budget_not_exhausted(self):
        """Regression guard for the P1b fix: as long as the global budget has
        room, a late-appearing speaker must still get its own samples."""
        inferencer = _make_inferencer(samples_per_speaker=3, context_dialogs=2)

        dialogs = []
        for i in range(15):
            dialogs.append(_make_dialog("Speaker1", f"Speaker1 line {i}", start_time=float(i)))
        dialogs.append(_make_dialog("Speaker2", "大家好我是王老师", start_time=15.0))

        samples = inferencer._extract_sample_dialogs(dialogs, ["Speaker1", "Speaker2"])

        self.assertIn("Speaker2", samples)
        self.assertTrue(samples["Speaker2"]["own_samples"])

    def test_context_before_counts_toward_global_budget(self):
        """ci-gate review: the global budget only summed own_samples, but
        _format_sample_dialogs also writes each speaker's context_before
        (the preceding turns used to help the LLM infer a name) into the
        prompt. A speaker whose own_samples are tiny but whose context is
        large must still be counted correctly, and the true (own + context)
        total must never exceed max_total_sample_chars."""
        num_speakers = 20
        speakers = [f"Speaker{i}" for i in range(num_speakers)]
        dialogs = []
        t = 0.0
        for speaker in speakers:
            # 2 long context lines from a "host" immediately before this
            # speaker's own (short) first line -- context_dialogs=2 pulls
            # both into context_before.
            dialogs.append(_make_dialog("Host", "x" * 150, start_time=t))
            t += 1.0
            dialogs.append(_make_dialog("Host", "x" * 150, start_time=t))
            t += 1.0
            # Own sample is deliberately tiny so pre-fix (own-only) budget
            # accounting would never trigger, while the true total
            # (own + context, ~300 chars/speaker) blows past a small cap.
            dialogs.append(_make_dialog(speaker, "hi", start_time=t))
            t += 1.0

        inferencer = _make_inferencer(
            samples_per_speaker=3,
            max_chars_per_speaker=400,
            context_dialogs=2,
            max_total_sample_chars=1500,
        )
        samples = inferencer._extract_sample_dialogs(dialogs, ["Host"] + speakers)

        true_total = sum(
            len(s) for info in samples.values() for s in info.get("own_samples") or []
        ) + sum(
            len(text)
            for info in samples.values()
            for _, text in info.get("context_before") or []
        )
        self.assertLessEqual(true_total, 1500)
        # 20 speakers x ~300 chars (2x150 context + "hi") would total ~6000
        # raw -- well past the 1500 cap, so some speakers must have been
        # dropped for this assertion to be meaningful (not a vacuous pass).
        self.assertLess(len(samples), num_speakers)


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

    def test_cache_hit_reapplies_current_confidence_threshold(self):
        """ci-gate review: cached mapping/applied was gated at write time
        using whatever confidence_threshold was active then. Changing the
        threshold afterward must retroactively affect already-cached
        results too -- not just future inferences -- since meta already
        stores the raw confidence per speaker."""
        cache_manager = MagicMock()
        cache_manager.get_speaker_mapping.return_value = {
            "mapping": {"Speaker1": "张三", "Speaker2": "李四"},
            "meta": {
                # Written when confidence_threshold was low enough that 0.7
                # passed; Speaker1 is comfortably confident either way.
                "Speaker1": {"name": "张三", "confidence": 0.95, "applied": True},
                "Speaker2": {"name": "李四", "confidence": 0.7, "applied": True},
            },
            "low_confidence": [],
        }
        # Threshold has since been raised past Speaker2's cached confidence.
        inferencer = _make_inferencer(cache_manager=cache_manager, confidence_threshold=0.8)

        result = inferencer.infer(
            speakers=["Speaker1", "Speaker2"],
            dialogs=[_make_dialog("Speaker1", "hi", 0.0)],
            title="t",
            platform="p",
            media_id="m",
        )

        inferencer.llm_client.call.assert_not_called()
        self.assertEqual(result["mapping"]["Speaker1"], "张三")
        # Speaker2's cached 0.7 confidence no longer clears the raised 0.8
        # threshold -- must be downgraded now, not stuck on the stale verdict.
        self.assertNotEqual(result["mapping"]["Speaker2"], "李四")
        self.assertIn("Speaker2", result["low_confidence"])
        self.assertFalse(result["meta"]["Speaker2"]["applied"])

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

    def test_legacy_flat_cache_is_ignored_and_triggers_reinference(self):
        """ci-gate review: legacy flat-dict cache (written before this PR
        introduced confidence gating) carries no real confidence signal.
        Treating it as confidence=1.0 was a fabricated guarantee that let
        every already-cached video permanently bypass the new low-confidence
        downgrade. It must instead be treated as unusable, forcing a fresh
        (confidence-aware) inference -- the one-time re-inference cost is
        acceptable; the resulting cache upgrades to the new format."""
        cache_manager = MagicMock()
        cache_manager.get_speaker_mapping.return_value = {
            "Speaker1": "张三",
            "Speaker2": "李四",
        }
        inferencer = _make_inferencer(cache_manager=cache_manager)
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm_response(
                speaker_mapping={"Speaker1": "张三", "Speaker2": "王五"},
                confidence={"Speaker1": 0.95, "Speaker2": 0.3},
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

        # Legacy cache must not short-circuit inference, even though it
        # nominally "covers" both current speakers.
        inferencer.llm_client.call.assert_called_once()
        # Fresh, confidence-aware result -- Speaker2's low confidence (0.3)
        # correctly triggers the downgrade the legacy cache was hiding.
        self.assertEqual(result["mapping"]["Speaker1"], "张三")
        self.assertNotEqual(result["mapping"]["Speaker2"], "王五")
        self.assertIn("Speaker2", result["low_confidence"])
        # Re-inferred result is persisted back in the new format, upgrading
        # the cache so future hits carry real confidence data.
        cache_manager.save_speaker_mapping.assert_called_once()
        saved_payload = cache_manager.save_speaker_mapping.call_args[0][2]
        self.assertIn("meta", saved_payload)


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
