"""Unit tests for SpeakerInferencer: per-speaker sampling, confidence gating,
cache coverage validation, and prompt group rendering.

Covers the "per-speaker sampling + confidence downgrade" refactor:
- Late-appearing speakers still get sampled with first-appearance context.
- Per-speaker sample count / char caps are enforced.
- Low-confidence mappings are downgraded to a placeholder label ("SpeakerN"),
  not applied as a real name.
- Missing/malformed confidence defaults to 0.0 (low confidence, downgraded).
- Cache is only reused when it covers the current speaker set AND carries
  real confidence data; legacy (flat dict, pre-confidence-gating) cache
  format is treated as unusable and forces re-inference (ci-gate review).
- Prompt sample groups are ordered by first-appearance time.
- The prompt's raw speaker-label list ("original_speakers") is cropped to
  the speakers that survive the global sample budget, so it can't blow up
  the prompt on its own when diarization produces thousands of labels.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

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

        context_dialogs=0 isolates this test to own_samples + per-speaker
        rendering overhead accounting only (each dialog line here is from a
        different speaker, so with the default context_dialogs=2 every
        speaker's context_before would pull in the preceding speaker's line
        too -- exercised separately and deliberately in
        test_context_before_counts_toward_global_budget).

        The budget that fits exactly 2 speakers is derived from the real
        renderer (_render_speaker_segment) instead of a hand-computed magic
        number: pre-fix, "100 raw chars x 2 <= 250" was a valid mental model
        because the budget only summed raw text; post-fix the budget also
        counts each speaker's header/timestamp/section-label/prefix
        overhead, so a hardcoded 250 would silently fit only 1 speaker
        (not 2) and would need re-tuning by hand every time the prompt
        template changes -- exactly the fragility this fix eliminates.
        """
        speakers = [f"Speaker{i}" for i in range(10)]
        dialogs = [
            _make_dialog(speaker, "x" * 100, start_time=float(i))
            for i, speaker in enumerate(speakers)
        ]

        # Probe with an effectively unlimited budget to get each speaker's
        # unclipped sample info, then measure the real rendered length of
        # the first 2 speakers' segments (the same renderer that produces
        # the final prompt) to compute the exact budget that fits both.
        probe = _make_inferencer(
            samples_per_speaker=1,
            max_chars_per_speaker=100,
            max_total_sample_chars=10**9,
            context_dialogs=0,
        )
        unclipped = probe._extract_sample_dialogs(dialogs, speakers)
        two_speaker_budget = (
            len(probe._render_speaker_segment("Speaker0", unclipped["Speaker0"]))
            + 2  # "\n\n" segment separator used by _format_sample_dialogs
            + len(probe._render_speaker_segment("Speaker1", unclipped["Speaker1"]))
        )

        inferencer = _make_inferencer(
            samples_per_speaker=1,
            max_chars_per_speaker=100,
            max_total_sample_chars=two_speaker_budget,
            context_dialogs=0,
        )
        samples = inferencer._extract_sample_dialogs(dialogs, speakers)

        # Budget fits exactly 2 full speakers; 1 more would exceed it, so
        # the earliest-appearing speakers survive and the rest are excluded
        # via the existing "no samples" fallback path.
        self.assertIn("Speaker0", samples)
        self.assertIn("Speaker1", samples)
        self.assertNotIn("Speaker2", samples)
        self.assertNotIn("Speaker9", samples)

        # Shrinking the budget by just 1 char below the 2-speaker threshold
        # must drop Speaker1 sooner -- proving the cap genuinely reacts to
        # the configured limit, not a fixed constant.
        smaller = _make_inferencer(
            samples_per_speaker=1,
            max_chars_per_speaker=100,
            max_total_sample_chars=two_speaker_budget - 1,
            context_dialogs=0,
        )
        smaller_samples = smaller._extract_sample_dialogs(dialogs, speakers)
        self.assertIn("Speaker0", smaller_samples)
        self.assertNotIn("Speaker1", smaller_samples)

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

    def test_many_tiny_speakers_actual_rendered_prompt_stays_within_budget(self):
        """ci-gate review (4th round): the global budget summed own_samples +
        context_before RAW TEXT, but the actual prompt is produced by
        _format_sample_dialogs, which adds per-speaker structural text on top
        of that raw text -- a group header ("[SpeakerN]"), a "first seen at"
        timestamp annotation, a "context" section label, a "[speaker]: "
        prefix per context line, a "own dialog samples" section label, and a
        "- " bullet prefix per own line. When diarization goes badly wrong
        and produces hundreds of spurious speaker labels with only a
        character or two of text each, this fixed per-speaker rendering
        overhead dominates: the raw-text sum stays tiny (looks safe to the
        old budget check) while the real rendered prompt blows way past
        max_total_sample_chars.

        This test builds exactly that pathological scenario (400 fake
        speakers, 1-2 CJK characters of dialog each, plus a timestamp and
        one context line per speaker so every structural element the
        renderer adds is exercised) and asserts against the ACTUAL rendered
        string that will be written into the prompt -- not against a
        recomputed approximation of it. Pre-fix, this fails (rendered length
        >> budget) because the cap never even triggers; post-fix, the budget
        computation reuses the same per-speaker renderer that produces the
        final prompt, so it cannot drift from what actually gets written.

        Margin: the fixed implementation accumulates each kept speaker's
        segment length plus its "\\n\\n" join separator, so the running
        total is an exact (not approximate) prefix of the final joined
        string's length before the trailing .rstrip(); rstrip only ever
        removes trailing whitespace, so the real rendered length can only be
        <= that running total <= max_total_sample_chars. No slack is needed
        -- the margin is 0.
        """
        num_speakers = 400
        speakers = [f"Speaker{i}" for i in range(num_speakers)]
        dialogs = []
        t = 0.0
        for speaker in speakers:
            # One short line from the *previous* fake speaker so
            # context_dialogs=1 gives every speaker a (tiny) context line,
            # exercising the context-section rendering overhead too.
            dialogs.append(_make_dialog("Host", "喂", start_time=t))
            t += 1.0
            dialogs.append(_make_dialog(speaker, "嗯", start_time=t))
            t += 1.0

        inferencer = _make_inferencer(
            samples_per_speaker=1,
            max_chars_per_speaker=400,
            context_dialogs=1,
            max_total_sample_chars=8000,
        )
        samples = inferencer._extract_sample_dialogs(dialogs, ["Host"] + speakers)
        rendered = inferencer._format_sample_dialogs(samples)

        # The bug this test guards against: raw own+context text is minuscule
        # (a handful of chars per speaker) and would never trip a budget
        # computed from raw text alone, even though the real rendered prompt
        # is many times larger once structural overhead is included.
        raw_total = sum(
            len(s) for info in samples.values() for s in info.get("own_samples") or []
        ) + sum(
            len(text)
            for info in samples.values()
            for _, text in info.get("context_before") or []
        )
        self.assertLess(
            raw_total,
            8000,
            "sanity check: raw text alone must stay far under budget so this "
            "scenario actually exercises the rendering-overhead gap, not a "
            "second raw-text overflow",
        )

        # The real assertion: what actually gets written into the prompt
        # must not exceed the configured budget. Zero margin -- see
        # docstring for why the fixed implementation guarantees this exactly.
        self.assertLessEqual(len(rendered), 8000)

        # With 400 speakers and a non-trivial per-speaker rendering overhead,
        # the budget must have actually dropped some speakers for this test
        # to be meaningful (not a vacuous pass).
        self.assertLess(len(samples), num_speakers + 1)

    def test_extract_sample_dialogs_stays_fast_with_many_speakers(self):
        """The per-speaker budget check must stay O(n) in the number of
        speakers -- re-rendering every already-kept speaker's segment on
        each new addition (an O(n^2) approach) would turn into a real
        latency cliff once diarization produces a thousand-plus spurious
        speaker labels. This is a coarse regression guard, not a strict
        micro-benchmark: it just needs to catch an accidental return to
        quadratic behavior."""
        num_speakers = 1200
        speakers = [f"Speaker{i}" for i in range(num_speakers)]
        dialogs = [
            _make_dialog(speaker, "hi", start_time=float(i))
            for i, speaker in enumerate(speakers)
        ]

        inferencer = _make_inferencer(
            samples_per_speaker=1,
            max_chars_per_speaker=400,
            context_dialogs=2,
            max_total_sample_chars=8000,
        )

        started_at = time.monotonic()
        samples = inferencer._extract_sample_dialogs(dialogs, speakers)
        inferencer._format_sample_dialogs(samples)
        elapsed = time.monotonic() - started_at

        self.assertLess(
            elapsed,
            2.0,
            f"extraction + rendering took {elapsed:.3f}s for {num_speakers} "
            f"speakers, which suggests non-linear (e.g. O(n^2)) behavior",
        )


class TestPromptSpeakerListRespectsBudget(unittest.TestCase):
    """The speaker-label list embedded in the prompt ("original_speakers") must
    be cropped to the budget-surviving speaker set, not the full raw
    `speakers` list -- ci-gate review (5th round). _apply_global_sample_budget
    already caps the sample-snippet section, but if infer() still hands the
    prompt builder the UNCROPPED speakers list, that label list alone
    (joined with ", ") can add tens of thousands of characters when
    diarization produces thousands of spurious labels, defeating the budget
    entirely even though the snippet section stayed capped."""

    def test_original_speakers_passed_to_prompt_builder_is_budget_cropped(self):
        num_speakers = 2000
        speakers = [f"Speaker{i}" for i in range(num_speakers)]
        dialogs = [
            _make_dialog(speaker, "hi", start_time=float(i))
            for i, speaker in enumerate(speakers)
        ]

        inferencer = _make_inferencer(
            samples_per_speaker=1,
            max_chars_per_speaker=400,
            context_dialogs=0,
            max_total_sample_chars=8000,
        )
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm_response(
                speaker_mapping={s: s for s in speakers}, confidence=1.0
            )
        )

        captured = {}

        def fake_prompt_builder(**kwargs):
            captured.update(kwargs)
            return "fake prompt"

        with patch(
            "video_transcript_api.llm.core.speaker_inferencer."
            "build_speaker_inference_user_prompt",
            side_effect=fake_prompt_builder,
        ):
            inferencer.infer(speakers=speakers, dialogs=dialogs, title="t")

        self.assertIn("original_speakers", captured)
        passed_speakers = captured["original_speakers"]

        # Pre-fix behaviour passes the FULL, uncropped speakers list
        # (num_speakers entries) here; the fix must narrow it to only the
        # speakers that survived the global sample budget.
        self.assertLess(len(passed_speakers), num_speakers)
        self.assertGreater(len(passed_speakers), 0)

        # The label list itself, rendered exactly as the real prompt builder
        # does (", ".join(...)), must not reintroduce an unbounded prompt.
        label_list_chars = len(", ".join(passed_speakers))
        self.assertLess(label_list_chars, 8000)

        # Order must still follow first-appearance order (same as the
        # sample-snippet section), not be scrambled by an unordered set/dict.
        self.assertEqual(passed_speakers, sorted(passed_speakers, key=speakers.index))

    def test_small_speaker_set_prompt_speaker_list_matches_full_set(self):
        """Sanity check: when the budget never triggers, the prompt's speaker
        list must still equal the full original speaker set (no accidental
        narrowing in the normal, unclipped case)."""
        speakers = ["Speaker1", "Speaker2"]
        dialogs = [
            _make_dialog("Speaker1", "hello there", start_time=0.0),
            _make_dialog("Speaker2", "hi back", start_time=1.0),
        ]

        inferencer = _make_inferencer()
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm_response(
                speaker_mapping={"Speaker1": "张三", "Speaker2": "李四"}, confidence=1.0
            )
        )

        captured = {}

        def fake_prompt_builder(**kwargs):
            captured.update(kwargs)
            return "fake prompt"

        with patch(
            "video_transcript_api.llm.core.speaker_inferencer."
            "build_speaker_inference_user_prompt",
            side_effect=fake_prompt_builder,
        ):
            inferencer.infer(speakers=speakers, dialogs=dialogs, title="t")

        self.assertEqual(captured["original_speakers"], ["Speaker1", "Speaker2"])


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

    def test_missing_confidence_field_defaults_to_low_and_downgrades(self):
        """A response missing the `confidence` field entirely must NOT be
        treated as maximally confident -- this feature exists specifically
        to avoid applying uncertain inferences, and a missing confidence
        signal is the most uncertain case of all. It must default to a LOW
        confidence so the speaker gets downgraded to a placeholder."""
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

        self.assertEqual(result["meta"]["Speaker1"]["confidence"], 0.0)
        self.assertFalse(result["meta"]["Speaker1"]["applied"])
        self.assertEqual(result["mapping"]["Speaker1"], "说话人1")
        self.assertIn("Speaker1", result["low_confidence"])

    def test_per_speaker_confidence_missing_key_defaults_to_low_and_downgrades(self):
        """A per-speaker confidence dict that simply omits one speaker's key
        (as opposed to containing an unparsable value) must also default to
        low confidence for that speaker, not 1.0. The other speaker, whose
        key IS present with a valid high value, must be unaffected."""
        result = self._run_infer(
            speaker_mapping={"Speaker1": "张三", "Speaker2": "李四"},
            confidence={"Speaker2": 0.9},  # Speaker1 key missing entirely
        )

        self.assertEqual(result["meta"]["Speaker1"]["confidence"], 0.0)
        self.assertFalse(result["meta"]["Speaker1"]["applied"])
        self.assertEqual(result["mapping"]["Speaker1"], "说话人1")
        self.assertIn("Speaker1", result["low_confidence"])

        self.assertEqual(result["meta"]["Speaker2"]["confidence"], 0.9)
        self.assertTrue(result["meta"]["Speaker2"]["applied"])

    def test_malformed_confidence_value_defaults_to_low_and_downgrades(self):
        """A confidence value that cannot be parsed into a float (e.g. a
        non-numeric string) must default to low confidence, not 1.0. A
        sibling speaker with a valid, high confidence value in the same
        response must be unaffected."""
        result = self._run_infer(
            speaker_mapping={"Speaker1": "张三", "Speaker2": "李四"},
            confidence={"Speaker1": "not-a-number", "Speaker2": 0.9},
        )

        self.assertEqual(result["meta"]["Speaker1"]["confidence"], 0.0)
        self.assertFalse(result["meta"]["Speaker1"]["applied"])
        self.assertEqual(result["mapping"]["Speaker1"], "说话人1")
        self.assertIn("Speaker1", result["low_confidence"])

        self.assertEqual(result["meta"]["Speaker2"]["confidence"], 0.9)
        self.assertTrue(result["meta"]["Speaker2"]["applied"])

    def test_scalar_confidence_applies_to_all_speakers(self):
        result = self._run_infer(
            speaker_mapping={"Speaker1": "张三", "Speaker2": "李四"},
            confidence=0.9,
        )

        self.assertEqual(result["meta"]["Speaker1"]["confidence"], 0.9)
        self.assertEqual(result["meta"]["Speaker2"]["confidence"], 0.9)
        self.assertTrue(result["meta"]["Speaker1"]["applied"])
        self.assertTrue(result["meta"]["Speaker2"]["applied"])


class TestUnsampledSpeakerIdentityFallback(unittest.TestCase):
    """A speaker excluded from sampling entirely (global budget, or simply no
    valid dialog) must keep its ORIGINAL label -- not get downgraded to a
    "SpeakerN" placeholder. Regression guard: fixing "missing confidence
    defaults to low" (so a speaker the LLM was asked about but didn't answer
    for gets downgraded) must not also sweep in speakers the LLM was never
    asked about at all -- those have zero inference signal and belong to the
    pre-existing identity-fallback path, not the low-confidence-downgrade
    path. ci-gate review: "应将未采样标签与 LLM 返回的低置信度猜测区分开，
    前者保留原标签"."""

    def test_budget_excluded_speaker_keeps_original_label_not_placeholder(self):
        speakers = ["alpha", "beta", "gamma"]
        dialogs = [
            _make_dialog(speaker, "x" * 100, start_time=float(i))
            for i, speaker in enumerate(speakers)
        ]

        # Derive a budget that fits exactly "alpha" and "beta" (same probe
        # technique as test_budget_is_configurable_and_triggers_earlier_when_smaller),
        # so "gamma" is deterministically excluded by the global sample budget.
        probe = _make_inferencer(
            samples_per_speaker=1,
            max_chars_per_speaker=100,
            max_total_sample_chars=10**9,
            context_dialogs=0,
        )
        unclipped = probe._extract_sample_dialogs(dialogs, speakers)
        two_speaker_budget = (
            len(probe._render_speaker_segment("alpha", unclipped["alpha"]))
            + 2
            + len(probe._render_speaker_segment("beta", unclipped["beta"]))
        )

        inferencer = _make_inferencer(
            samples_per_speaker=1,
            max_chars_per_speaker=100,
            max_total_sample_chars=two_speaker_budget,
            context_dialogs=0,
            confidence_threshold=0.6,
        )
        # The LLM is only ever prompted with "alpha"/"beta" context (gamma
        # was excluded before the prompt was built), so a realistic mock
        # response has no opinion on "gamma" at all.
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm_response(
                speaker_mapping={"alpha": "张三", "beta": "李四"},
                confidence={"alpha": 0.9, "beta": 0.9},
            )
        )

        result = inferencer.infer(speakers=speakers, dialogs=dialogs, title="t")

        self.assertEqual(result["mapping"]["alpha"], "张三")
        self.assertEqual(result["mapping"]["beta"], "李四")
        # The bug: gamma used to come out as "说话人3" (fabricated
        # placeholder) instead of keeping its own original label.
        self.assertEqual(result["mapping"]["gamma"], "gamma")
        self.assertFalse(result["meta"]["gamma"]["applied"])
        self.assertFalse(result["meta"]["gamma"]["sampled"])
        self.assertIn("gamma", result["low_confidence"])

    def test_speaker_with_no_valid_dialog_keeps_original_label_alongside_sampled_peers(self):
        """Exclusion from sampling isn't only caused by the global budget --
        a speaker with no valid (non-empty) dialog text at all is excluded
        from sample_groups the same way, even while OTHER speakers in the
        same call get sampled and sent to the LLM normally. That speaker
        must also keep its original label, not a fabricated placeholder."""
        inferencer = _make_inferencer(confidence_threshold=0.6)
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm_response(
                speaker_mapping={"alpha": "张三"},
                confidence={"alpha": 0.9},
            )
        )
        dialogs = [
            _make_dialog("alpha", "hello there", start_time=0.0),
            _make_dialog("beta", "", start_time=1.0),  # empty text -> no sample at all
        ]

        result = inferencer.infer(speakers=["alpha", "beta"], dialogs=dialogs, title="t")

        self.assertEqual(result["mapping"]["alpha"], "张三")
        self.assertTrue(result["meta"]["alpha"]["sampled"])
        self.assertEqual(result["mapping"]["beta"], "beta")
        self.assertFalse(result["meta"]["beta"]["sampled"])
        self.assertFalse(result["meta"]["beta"]["applied"])
        self.assertIn("beta", result["low_confidence"])

    def test_fresh_inference_persists_sampled_flag_for_excluded_speaker(self):
        """The sampled=False marker must be written into the cache payload
        at fresh-inference time, not just correctly replayed when it's
        already present in a hand-crafted cache fixture (covered by
        test_cache_replay_preserves_unsampled_identity_across_threshold_changes).
        Otherwise a real first-time inference would cache a payload without
        the flag, and the very next cache-hit would default gamma back to
        sampled=True and misapply the confidence gate to it."""
        cache_manager = MagicMock()
        cache_manager.get_speaker_mapping.return_value = None
        inferencer = _make_inferencer(cache_manager=cache_manager, confidence_threshold=0.6)
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm_response(
                speaker_mapping={"alpha": "张三"},
                confidence={"alpha": 0.9},
            )
        )
        dialogs = [
            _make_dialog("alpha", "hello there", start_time=0.0),
            _make_dialog("beta", "", start_time=1.0),
        ]

        inferencer.infer(
            speakers=["alpha", "beta"],
            dialogs=dialogs,
            title="t",
            platform="p",
            media_id="m",
        )

        cache_manager.save_speaker_mapping.assert_called_once()
        saved_payload = cache_manager.save_speaker_mapping.call_args[0][2]
        self.assertFalse(saved_payload["meta"]["beta"]["sampled"])
        self.assertTrue(saved_payload["meta"]["alpha"]["sampled"])

    def test_cache_replay_preserves_unsampled_identity_across_threshold_changes(self):
        """A cached meta entry marked sampled=False (never sent to the LLM in
        the original inference) must stay identity-mapped on cache replay
        regardless of how the confidence_threshold is reconfigured afterward
        -- it has no confidence signal to re-gate, unlike a genuinely sampled
        speaker's cached confidence value."""
        cache_manager = MagicMock()
        cache_manager.get_speaker_mapping.return_value = {
            "mapping": {"alpha": "张三", "gamma": "gamma"},
            "meta": {
                "alpha": {"name": "张三", "confidence": 0.9, "applied": True, "sampled": True},
                "gamma": {
                    "name": "gamma",
                    "confidence": 0.0,
                    "applied": False,
                    "sampled": False,
                },
            },
            "low_confidence": ["gamma"],
        }
        # Even a permissive (near-zero) threshold must not resurrect a
        # fabricated "applied" name for gamma -- it was never inferred.
        inferencer = _make_inferencer(cache_manager=cache_manager, confidence_threshold=0.01)

        result = inferencer.infer(
            speakers=["alpha", "gamma"],
            dialogs=[_make_dialog("alpha", "hi", 0.0)],
            title="t",
            platform="p",
            media_id="m",
        )

        inferencer.llm_client.call.assert_not_called()
        self.assertEqual(result["mapping"]["gamma"], "gamma")
        self.assertFalse(result["meta"]["gamma"]["applied"])
        self.assertIn("gamma", result["low_confidence"])


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
        self.assertEqual(
            result,
            {
                "mapping": {}, "meta": {}, "low_confidence": [],
                "source": "identity_fallback",
            },
        )

    def test_no_valid_samples_falls_back_to_identity(self):
        inferencer = _make_inferencer()
        result = inferencer.infer(
            speakers=["Speaker1"],
            dialogs=[{"speaker": "Speaker1", "text": ""}],  # empty text -> no sample
            title="t",
        )
        self.assertEqual(result["mapping"], {"Speaker1": "Speaker1"})
        self.assertFalse(result["meta"]["Speaker1"]["applied"])
        self.assertEqual(result["source"], "identity_fallback")

    def test_llm_call_exception_falls_back_to_identity(self):
        """Local codex review round 6, G4: a transient LLM failure (network
        blip, rate limit, timeout...) must be tagged "identity_fallback",
        not silently indistinguishable from a genuine successful inference --
        llm_ops._refresh_speaker_names_in_existing_structured_artifact relies
        on this tag to refuse overwriting an already-displayed good name with
        this fallback's raw "Speaker1" placeholder."""
        inferencer = _make_inferencer()
        inferencer.llm_client.call = MagicMock(side_effect=Exception("boom"))

        result = inferencer.infer(
            speakers=["Speaker1"],
            dialogs=[_make_dialog("Speaker1", "hello", 0.0)],
            title="t",
        )

        self.assertEqual(result["mapping"], {"Speaker1": "Speaker1"})
        self.assertFalse(result["meta"]["Speaker1"]["applied"])
        self.assertEqual(result["source"], "identity_fallback")

    def test_allow_llm_false_returns_identity_fallback_source(self):
        inferencer = _make_inferencer()
        result = inferencer.infer(
            speakers=["Speaker1"],
            dialogs=[_make_dialog("Speaker1", "hello", 0.0)],
            title="t",
            allow_llm=False,
        )
        self.assertEqual(result["source"], "identity_fallback")

    def test_real_llm_success_tags_source_llm(self):
        inferencer = _make_inferencer()
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm_response(
                speaker_mapping={"Speaker1": "张三"},
                confidence={"Speaker1": 0.9},
            )
        )
        result = inferencer.infer(
            speakers=["Speaker1"],
            dialogs=[_make_dialog("Speaker1", "hello", 0.0)],
            title="t",
        )
        self.assertEqual(result["source"], "llm")

    def test_save_rejection_falls_back_to_identity_not_a_task_failure(self):
        """R5 (PR3 review hardening): CacheManager.save_speaker_mapping now
        validates the payload shape before persisting (shared with
        get_speaker_mapping's read-side validation) and raises ValueError
        for a malformed result (e.g. an LLM response with a non-str name or
        a bool confidence value gets carried straight through
        _apply_confidence_gate's meta["name"]/["confidence"] fields into the
        save call). infer()'s existing broad `except Exception` (the same
        one that already handles a raw LLM-call exception, see
        test_llm_call_exception_falls_back_to_identity above) must catch
        this too and degrade to identity_fallback -- not propagate the
        exception and fail the whole task."""
        cache_manager = MagicMock()
        cache_manager.get_speaker_mapping.return_value = None  # no cache hit
        cache_manager.save_speaker_mapping.side_effect = ValueError(
            "speaker mapping result failed shape validation"
        )
        inferencer = _make_inferencer(cache_manager=cache_manager)
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm_response(
                speaker_mapping={"Speaker1": "张三"},
                confidence={"Speaker1": 0.9},
            )
        )

        result = inferencer.infer(
            speakers=["Speaker1"],
            dialogs=[_make_dialog("Speaker1", "hello", 0.0)],
            title="t",
            platform="p",
            media_id="m",
        )

        cache_manager.save_speaker_mapping.assert_called_once()
        self.assertEqual(result["mapping"], {"Speaker1": "Speaker1"})
        self.assertFalse(result["meta"]["Speaker1"]["applied"])
        self.assertEqual(result["source"], "identity_fallback")

    def test_cache_hit_tags_source_cache_hit_not_llm(self):
        cache_manager = MagicMock()
        cache_manager.get_speaker_mapping.return_value = {
            "mapping": {"Speaker1": "张三"},
            "meta": {"Speaker1": {"name": "张三", "confidence": 0.95, "applied": True}},
            "low_confidence": [],
        }
        inferencer = _make_inferencer(cache_manager=cache_manager)

        result = inferencer.infer(
            speakers=["Speaker1"],
            dialogs=[_make_dialog("Speaker1", "hi", 0.0)],
            title="t",
            platform="p",
            media_id="m",
        )

        inferencer.llm_client.call.assert_not_called()
        self.assertEqual(result["source"], "cache_hit")


if __name__ == "__main__":
    unittest.main()
