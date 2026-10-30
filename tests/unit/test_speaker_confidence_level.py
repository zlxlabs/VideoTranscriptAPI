"""说话人推测名证据档位（high/medium/low/none）回归测试。

轴表来自卡 vta-261030-speaker-confidence-tiers「不变式轴表」，端到端用例
从 SpeakerInferencer.infer() 入口出发、只打桩 llm_client.call，真实夹具为
twitter 任务 2115809881399922849（tests/fixtures/twitter_collison_2115809881399922849/）。
"""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from video_transcript_api.llm.core.speaker_confidence_level import (
    CONFIDENCE_LEVELS,
    assign_confidence_levels,
)
from video_transcript_api.llm.core.speaker_inferencer import SpeakerInferencer

FIXTURE_DIR = (
    Path(__file__).parent.parent / "fixtures" / "twitter_collison_2115809881399922849"
)


def _load_fixture():
    transcript = json.loads(
        (FIXTURE_DIR / "transcript_funasr.json").read_text(encoding="utf-8")
    )
    metadata = json.loads((FIXTURE_DIR / "metadata.json").read_text(encoding="utf-8"))
    dialogs = [
        {
            "speaker": segment.get("speaker") or "",
            "text": segment.get("text") or "",
            "start_time": segment.get("start_time"),
        }
        for segment in transcript["segments"]
    ]
    return dialogs, metadata


def _make_inferencer():
    llm_client = MagicMock()
    cache_manager = MagicMock()
    cache_manager.get_speaker_mapping.return_value = None
    return SpeakerInferencer(
        llm_client=llm_client,
        cache_manager=cache_manager,
        model="test-model",
        reasoning_effort="low",
    )


def _install_llm(inferencer, structured_output):
    response = MagicMock()
    response.structured_output = structured_output
    inferencer.llm_client.call = MagicMock(return_value=response)


def _candidate(name, evidence_type, quote):
    return {"name": name, "evidence_type": evidence_type, "evidence_quote": quote}


def _structured_output(candidates, roles=None, mapping=None, confidence=None):
    return {
        "speaker_mapping": mapping or {speaker: speaker for speaker in candidates},
        "confidence": confidence or {speaker: 0.3 for speaker in candidates},
        "candidates": candidates,
        "roles": {} if roles is None else roles,
        "reasoning": "mock",
    }


def _entry(**overrides):
    """构造一个 evidence gate 拒绝分支的 meta 条目（low/none 判定的主场景）。"""
    entry = {
        "name": "Speaker1",
        "confidence": 0.3,
        "applied": False,
        "sampled": True,
        "candidates": [],
        "verified_candidates": [],
        "rejection_reason": "no_verified_evidence",
    }
    entry.update(overrides)
    return entry


def _level_fields(result, speaker):
    entry = result["meta"][speaker]
    return (
        entry.get("confidence_level"),
        entry.get("guess_name"),
        entry.get("basis"),
    )


class TestAssignLevelsUnit(unittest.TestCase):
    """单元级：直接构造 meta 条目判定档位。"""

    def _assign(self, meta, mapping=None):
        result = {"mapping": mapping or {}, "meta": meta, "low_confidence": []}
        return assign_confidence_levels(result)

    def test_high_single_dialog(self):
        meta = {
            "Speaker1": _entry(
                applied=True,
                name="兰姐",
                rejection_reason=None,
                verified_candidates=[
                    _candidate("兰姐", "dialog_address", "兰姐你来说说"),
                ],
            )
        }
        result = self._assign(meta, {"Speaker1": "兰姐"})
        level, guess, basis = _level_fields(result, "Speaker1")
        self.assertEqual(level, "high")
        self.assertEqual(guess, "兰姐")
        self.assertEqual(basis["evidence_type"], "dialog_address")
        self.assertEqual(basis["quote"], "兰姐你来说说")
        self.assertIsNone(basis["role"])
        self.assertIsNone(basis["rejection_reason"])

    def test_high_composite_name_guesses_dialog_name(self):
        """复合名（dialog+credit）：档位 high，guess_name 取 dialog 候选的名字。"""
        meta = {
            "Speaker2": _entry(
                applied=True,
                name="魂蓝",
                rejection_reason=None,
                verified_candidates=[
                    _candidate("魂蓝", "title_credit", "作者魂蓝的新书"),
                    _candidate("兰姐", "dialog_address", "兰姐你来说说"),
                ],
            )
        }
        result = self._assign(
            meta, {"Speaker2": "说话人2｜标题关联推测:魂蓝｜对话称:兰姐"}
        )
        level, guess, basis = _level_fields(result, "Speaker2")
        self.assertEqual(level, "high")
        self.assertEqual(guess, "兰姐")
        self.assertEqual(basis["evidence_type"], "dialog_address")
        self.assertIsNone(basis["role"])

    def test_medium_duo_tier_host_prefers_author(self):
        meta = {
            "Speaker1": _entry(
                applied=True,
                name="John Collison",
                rejection_reason=None,
                evidence_tier="duo_metadata_role",
                role="host",
                verified_candidates=[
                    _candidate("John Collison", "author_host", "John Collison"),
                ],
            )
        }
        result = self._assign(meta, {"Speaker1": "说话人1｜作者关联推测:John Collison"})
        level, guess, basis = _level_fields(result, "Speaker1")
        self.assertEqual(level, "medium")
        self.assertEqual(guess, "John Collison")
        self.assertEqual(basis["evidence_type"], "author_host")
        self.assertEqual(basis["quote"], "John Collison")
        self.assertEqual(basis["role"], "host")
        self.assertIsNone(basis["rejection_reason"])

    def test_medium_duo_tier_guest_prefers_title(self):
        meta = {
            "Speaker2": _entry(
                applied=True,
                name="sama",
                rejection_reason=None,
                evidence_tier="duo_metadata_role",
                role="guest",
                verified_candidates=[
                    _candidate("sama", "title_credit", "@sama"),
                    _candidate("sama", "author_host", "sama"),
                ],
            )
        }
        result = self._assign(meta, {"Speaker2": "说话人2｜标题关联推测:sama"})
        level, guess, basis = _level_fields(result, "Speaker2")
        self.assertEqual(level, "medium")
        self.assertEqual(guess, "sama")
        self.assertEqual(basis["evidence_type"], "title_credit")
        self.assertEqual(basis["role"], "guest")

    def test_low_rejected_with_model_name(self):
        meta = {
            "Speaker2": _entry(
                name="Sam Altman",
                candidates=[_candidate("Sam Altman", "title_credit", "@sama")],
                rejection_reason="name_not_in_quote",
            )
        }
        result = self._assign(meta, {"Speaker2": "说话人2"})
        level, guess, basis = _level_fields(result, "Speaker2")
        self.assertEqual(level, "low")
        self.assertEqual(guess, "Sam Altman")
        self.assertEqual(basis["evidence_type"], "title_credit")
        self.assertEqual(basis["quote"], "@sama")
        self.assertIsNone(basis["role"])
        self.assertEqual(basis["rejection_reason"], "name_not_in_quote")

    def test_low_without_candidates_keeps_rejection_reason(self):
        meta = {
            "Speaker1": _entry(
                name="刘钊泽",
                candidates=[],
                rejection_reason="quote_missing",
            )
        }
        result = self._assign(meta, {"Speaker1": "说话人1"})
        level, guess, basis = _level_fields(result, "Speaker1")
        self.assertEqual(level, "low")
        self.assertEqual(guess, "刘钊泽")
        self.assertIsNone(basis["evidence_type"])
        self.assertIsNone(basis["quote"])
        self.assertEqual(basis["rejection_reason"], "quote_missing")

    def test_low_quote_truncated_to_80_chars(self):
        long_quote = "x" * 200
        meta = {
            "Speaker1": _entry(
                name="Ann",
                candidates=[_candidate("Ann", "title_credit", long_quote)],
                rejection_reason="name_not_in_quote",
            )
        }
        result = self._assign(meta, {"Speaker1": "说话人1"})
        basis = result["meta"]["Speaker1"]["basis"]
        self.assertEqual(len(basis["quote"]), 80)

    def test_none_cases(self):
        """占位名（等于原始标签 / 说话人N / Speaker N）、空名、缺名都是 none。"""
        cases = [
            ("等于原始标签", "Speaker1", "Speaker1"),
            ("说话人N 占位", "Speaker1", "说话人2"),
            ("Speaker N 占位", "Speaker1", "Speaker 3"),
            ("前后空白", "Speaker1", "   "),
            ("缺名字段", "Speaker1", None),
        ]
        for label, speaker, name in cases:
            with self.subTest(label):
                meta = {speaker: _entry(name=name, candidates=[])}
                result = self._assign(meta, {speaker: speaker})
                level, guess, basis = _level_fields(result, speaker)
                self.assertEqual(level, "none")
                self.assertIsNone(guess)
                self.assertEqual(
                    basis,
                    {
                        "evidence_type": None,
                        "quote": None,
                        "role": None,
                        "rejection_reason": None,
                    },
                )

    def test_none_not_sampled_drops_rejection_reason(self):
        """契约：none 档 basis 四键全 null，not_sampled 原因只在原字段保留。"""
        meta = {
            "Speaker3": {
                "name": "Speaker3",
                "confidence": 0.0,
                "applied": False,
                "sampled": False,
                "candidates": [],
                "verified_candidates": [],
                "rejection_reason": "not_sampled",
            }
        }
        result = self._assign(meta, {"Speaker3": "Speaker3"})
        level, guess, basis = _level_fields(result, "Speaker3")
        self.assertEqual(level, "none")
        self.assertIsNone(guess)
        self.assertIsNone(basis["rejection_reason"])

    def test_levels_domain(self):
        self.assertEqual(CONFIDENCE_LEVELS, ("high", "medium", "low", "none"))


class TestCollisonFixtureTiers(unittest.TestCase):
    """轴表端到端：真实夹具 + 只打桩 llm_client.call。"""

    def _run(self, candidates, mapping=None):
        dialogs, metadata = _load_fixture()
        inferencer = _make_inferencer()
        _install_llm(
            inferencer,
            _structured_output(
                candidates,
                roles={"Speaker1": "host", "Speaker2": "guest"},
                mapping=mapping,
            ),
        )
        return inferencer.infer(
            speakers=["Speaker1", "Speaker2"],
            dialogs=dialogs,
            title=metadata["title"],
            author=metadata["author"],
            description=metadata["description"],
        )

    def test_axis_row2_account_name_gives_both_medium(self):
        """轴表第二行：S2 照抄账号名 sama，两人都是 medium。"""
        result = self._run(
            {
                "Speaker1": [
                    _candidate("John Collison", "author_host", "John Collison")
                ],
                "Speaker2": [_candidate("sama", "title_credit", "@sama")],
            },
            mapping={"Speaker1": "John Collison", "Speaker2": "sama"},
        )
        self.assertEqual(
            result["mapping"]["Speaker1"], "说话人1｜作者关联推测:John Collison"
        )
        self.assertEqual(result["mapping"]["Speaker2"], "说话人2｜标题关联推测:sama")
        self.assertEqual(_level_fields(result, "Speaker1")[0], "medium")
        self.assertEqual(_level_fields(result, "Speaker2")[0], "medium")
        self.assertEqual(_level_fields(result, "Speaker2")[1], "sama")

    def test_llm_failure_fallback_still_has_levels(self):
        """轴表末行：LLM 调用失败走 identity fallback，每个条目都有 none 档，不报错。"""
        dialogs, metadata = _load_fixture()
        inferencer = _make_inferencer()
        inferencer.llm_client.call = MagicMock(side_effect=RuntimeError("boom"))
        result = inferencer.infer(
            speakers=["Speaker1", "Speaker2"],
            dialogs=dialogs,
            title=metadata["title"],
            author=metadata["author"],
        )
        for speaker in ("Speaker1", "Speaker2"):
            level, guess, basis = _level_fields(result, speaker)
            self.assertEqual(level, "none")
            self.assertIsNone(guess)
            self.assertEqual(
                basis,
                {
                    "evidence_type": None,
                    "quote": None,
                    "role": None,
                    "rejection_reason": None,
                },
            )


if __name__ == "__main__":
    unittest.main()
