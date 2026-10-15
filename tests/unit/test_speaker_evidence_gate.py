"""说话人显示名证据裁决的验收与回归测试（card-vta-261015-speaker-evidence）。

覆盖：
- 真实生产 fixture（X Space 1wxWjlzQVwDJQ，383 段 FunASR 产物）上的四条
  验收形态（mock LLM，不打真网）：作者名主持 applied、dialog_address+
  title_credit 双证据复合格式、伪造引用作废降级、低 confidence 不再拦截。
- input_fingerprint 扩展（title/author/description/rules_version）。
- 裁决规则的确定性分支（description_roster 不独立生效、无佐证标题名拒绝、
  未知证据类型作废）。
- v1（confidence 门控）旧产物缓存重放走旧降级语义；v2 产物缓存重放直接采用。
"""

import json
import inspect
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from video_transcript_api.llm.core.speaker_inferencer import (
    SPEAKER_EVIDENCE_RULES_VERSION,
    SpeakerInferencer,
)

FIXTURE_PATH = (
    Path(__file__).parent.parent
    / "fixtures"
    / "twitter_space_1wxWjlzQVwDJQ"
    / "transcript_funasr.json"
)

# 与卡面固定的生产元数据逐字一致（fixture 的契约的一部分）。
FIXTURE_TITLE = "噪音之后之魂蓝专场：AI+情趣用品出海，七天变现六位数"
FIXTURE_AUTHOR = "Adrian Punk"
FIXTURE_DESCRIPTION = (
    "Twitter Space participated by ChastityACER, 黄金家族—马克（Mark）| X收益实测, "
    "Ad, 杰瑞, 云潇（Nobody）, 大熊, 聂二AI落地内参, 泓珩, 有芝士就嘻嘻, "
    "yone ginger, dodoliu, Baoxing, MARKIM"
)


def _load_fixture_dialogs():
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return [
        {
            "speaker": seg.get("speaker") or "",
            "text": seg.get("text") or "",
            "start_time": seg.get("start_time"),
        }
        for seg in data["segments"]
    ]


def _candidate(name, evidence_type, quote):
    return {"name": name, "evidence_type": evidence_type, "evidence_quote": quote}


def _make_inferencer(cached_mapping=None, confidence_threshold=0.6):
    llm_client = MagicMock()
    cache_manager = MagicMock()
    cache_manager.get_speaker_mapping.return_value = cached_mapping
    return SpeakerInferencer(
        llm_client=llm_client,
        cache_manager=cache_manager,
        model="test-model",
        confidence_threshold=confidence_threshold,
    )


def _mock_llm(structured_output):
    response = MagicMock()
    response.structured_output = structured_output
    return response


class TestRealFixtureEvidenceGate(unittest.TestCase):
    """真实生产 fixture + mock LLM 的四条验收形态（卡面完成条件·行为验收）。"""

    def setUp(self):
        self.dialogs = _load_fixture_dialogs()
        # 生产案例只涉及其中的主对话双方。
        self.speakers = ["Speaker1", "Speaker2"]
        inferencer = _make_inferencer()
        self.sample_groups = inferencer._extract_sample_dialogs(self.dialogs, self.speakers)

    def _prompt_text(self):
        """与 infer() 相同方式组装核对文本（元数据区 + 转录片段区）。"""
        snippets = _make_inferencer()._format_sample_dialogs(self.sample_groups)
        return (
            f"- 标题：{FIXTURE_TITLE}\n"
            f"- 作者：{FIXTURE_AUTHOR}\n"
            f"- 描述：{FIXTURE_DESCRIPTION}\n" + snippets
        )

    def _run_infer(self, candidates, confidence):
        inferencer = _make_inferencer()
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm(
                {
                    "speaker_mapping": {
                        "Speaker1": "Adrian Punk",
                        "Speaker2": "兰姐",
                    },
                    "confidence": confidence,
                    "candidates": candidates,
                    "reasoning": "mock",
                }
            )
        )
        return inferencer.infer(
            speakers=self.speakers,
            dialogs=self.dialogs,
            title=FIXTURE_TITLE,
            author=FIXTURE_AUTHOR,
            description=FIXTURE_DESCRIPTION,
        )

    def test_host_author_name_applied_not_placeholder(self):
        """验收①：「Adrian Punk」案例形态——author_host + dialog_address 引用
        均取自输入真实文本 → applied，mapping["Speaker1"] != 说话人1。"""
        # dialog_address 的 quote 取 Speaker1 真实采样文本的首条（必然逐字
        # 在 prompt 中）；name 是 LLM 对话称呼的声称值，程序只核对 quote 存在性。
        speaker1_sample = self.sample_groups["Speaker1"]["own_samples"][0]
        candidates = {
            "Speaker1": [
                _candidate("Adrian Punk", "author_host", FIXTURE_AUTHOR),
                _candidate("胖哥", "dialog_address", speaker1_sample),
            ],
        }
        result = self._run_infer(candidates, confidence={"Speaker1": 0.4, "Speaker2": 0.4})

        prompt_text = self._prompt_text()
        self.assertIn(FIXTURE_AUTHOR, prompt_text)
        self.assertIn(speaker1_sample, prompt_text)
        self.assertNotEqual(result["mapping"]["Speaker1"], "说话人1")
        self.assertTrue(result["meta"]["Speaker1"]["applied"])
        # 双证据 → 复合格式：作者名为主名、对话称为别名。
        self.assertEqual(
            result["mapping"]["Speaker1"], f"Adrian Punk（对话称:胖哥）"
        )

    def test_soul_lan_composite_display_name(self):
        """验收②：「兰姐/魂蓝」案例形态——Speaker2 的 dialog_address（兰姐，
        quote 真实在采样覆盖的对话文本中）+ title_credit（魂蓝，quote 取自
        标题）双候选 → 「魂蓝（对话称:兰姐）」。"""
        prompt_text = self._prompt_text()
        # 「兰姐」必须真实被采样覆盖（top2 加额后在本 fixture 上成立），
        # 否则此测试的前提即失效——称呼片段进不了输入正是本卡要修的根因。
        self.assertIn("兰姐", prompt_text)
        candidates = {
            "Speaker2": [
                _candidate("兰姐", "dialog_address", "兰姐"),
                _candidate("魂蓝", "title_credit", "魂蓝专场"),
            ],
        }
        result = self._run_infer(candidates, confidence={"Speaker1": 0.0, "Speaker2": 0.0})

        self.assertEqual(result["mapping"]["Speaker2"], "魂蓝（对话称:兰姐）")
        self.assertTrue(result["meta"]["Speaker2"]["applied"])

    def test_forged_quote_rejected_to_placeholder(self):
        """验收③：引用伪造（quote 不在输入中）→ 候选作废、显示「说话人N」，
        meta 保留作废原因 quote_missing。"""
        candidates = {
            "Speaker2": [
                _candidate("某大V", "dialog_address", "这句引用根本不在输入文本中"),
                _candidate("魂蓝", "title_credit", "不存在的标题片段XYZ"),
            ],
        }
        result = self._run_infer(candidates, confidence={"Speaker1": 0.9, "Speaker2": 0.9})

        self.assertEqual(result["mapping"]["Speaker2"], "说话人2")
        self.assertFalse(result["meta"]["Speaker2"]["applied"])
        self.assertEqual(result["meta"]["Speaker2"]["rejection_reason"], "quote_missing")
        # LLM 原始候选保留在 meta 供审计。
        self.assertEqual(len(result["meta"]["Speaker2"]["candidates"]), 2)
        self.assertEqual(result["meta"]["Speaker2"]["verified_candidates"], [])

    def test_low_confidence_does_not_gate_application(self):
        """验收④：confidence=0.1 但证据核对通过 → 仍 applied（confidence 已
        退出裁决，只入 meta）。"""
        candidates = {
            "Speaker2": [
                _candidate("兰姐", "dialog_address", "兰姐"),
            ],
        }
        result = self._run_infer(candidates, confidence={"Speaker1": 0.0, "Speaker2": 0.1})

        self.assertEqual(result["mapping"]["Speaker2"], "兰姐")
        self.assertTrue(result["meta"]["Speaker2"]["applied"])
        self.assertEqual(result["meta"]["Speaker2"]["confidence"], 0.1)
        # Speaker1 无候选 → 降级；其 confidence 高低不改变结果。
        self.assertEqual(result["mapping"]["Speaker1"], "说话人1")

    def test_v1_cached_artifact_replays_with_legacy_confidence_semantics(self):
        """验收⑤：v1 旧产物（仅 confidence 无 candidates）读侧兼容不崩，
        走旧降级语义（conf 重判），报告选定「走旧降级语义」选项。"""
        cached_v1 = {
            "mapping": {"Speaker1": "张主持", "Speaker2": "李主讲"},
            "meta": {
                "Speaker1": {"name": "张主持", "confidence": 0.9, "applied": True, "sampled": True},
                "Speaker2": {"name": "李主讲", "confidence": 0.4, "applied": False, "sampled": True},
            },
            "low_confidence": ["Speaker2"],
        }
        inferencer = _make_inferencer(cached_mapping=cached_v1)
        inferencer.llm_client.call = MagicMock(side_effect=AssertionError("must not call LLM"))

        result = inferencer.infer(
            speakers=self.speakers,
            dialogs=self.dialogs,
            title=FIXTURE_TITLE,
            author=FIXTURE_AUTHOR,
            description=FIXTURE_DESCRIPTION,
            platform="p",
            media_id="m",
        )

        inferencer.llm_client.call.assert_not_called()
        self.assertEqual(result["source"], "cache_hit")
        # conf=0.9 >= 0.6 → applied；conf=0.4 < 0.6 → 旧降级语义「说话人N」。
        self.assertEqual(result["mapping"]["Speaker1"], "张主持")
        self.assertEqual(result["mapping"]["Speaker2"], "说话人2")
        self.assertIn("Speaker2", result["low_confidence"])

    def test_v2_cached_artifact_replays_verdict_directly(self):
        """v2 产物缓存命中：证据裁决结论无法离线重核（输入全文不在缓存），
        直接采用缓存 mapping/applied，不发起 LLM 调用。"""
        cached_v2 = {
            "mapping": {"Speaker1": "Adrian Punk（对话称:胖哥）", "Speaker2": "魂蓝（对话称:兰姐）"},
            "meta": {
                "Speaker1": {
                    "name": "Adrian Punk",
                    "confidence": 0.42,
                    "applied": True,
                    "sampled": True,
                    "candidates": [],
                    "verified_candidates": [
                        {"name": "Adrian Punk", "evidence_type": "author_host"},
                    ],
                },
                "Speaker2": {
                    "name": "兰姐",
                    "confidence": 0.1,
                    "applied": True,
                    "sampled": True,
                    "candidates": [],
                    "verified_candidates": [
                        {"name": "兰姐", "evidence_type": "dialog_address"},
                    ],
                },
            },
            "low_confidence": [],
        }
        inferencer = _make_inferencer(cached_mapping=cached_v2)
        inferencer.llm_client.call = MagicMock(side_effect=AssertionError("must not call LLM"))

        result = inferencer.infer(
            speakers=self.speakers,
            dialogs=self.dialogs,
            title=FIXTURE_TITLE,
            author=FIXTURE_AUTHOR,
            description=FIXTURE_DESCRIPTION,
            platform="p",
            media_id="m",
        )

        inferencer.llm_client.call.assert_not_called()
        self.assertEqual(result["source"], "cache_hit")
        self.assertEqual(result["mapping"]["Speaker2"], "魂蓝（对话称:兰姐）")
        self.assertTrue(result["meta"]["Speaker2"]["applied"])


class TestEvidenceGateRules(unittest.TestCase):
    """裁决规则的确定性分支（直接驱动 _verify_candidates/_decide_display_name）。"""

    def test_description_roster_alone_never_applies(self):
        """参会名单核对通过也不得单独 applied（名单≠发言人）。"""
        verified, rejected = SpeakerInferencer._verify_candidates(
            [_candidate("杰瑞", "description_roster", "杰瑞")],
            "描述：…杰瑞…",
        )
        self.assertEqual(len(verified), 1)
        name, reason = SpeakerInferencer._decide_display_name(verified)
        self.assertIsNone(name)
        self.assertEqual(reason, "description_roster_not_standalone")

    def test_title_credit_without_dialog_support_rejected(self):
        verified, _ = SpeakerInferencer._verify_candidates(
            [_candidate("魂蓝", "title_credit", "魂蓝专场")],
            "标题：…魂蓝专场…",
        )
        name, reason = SpeakerInferencer._decide_display_name(verified)
        self.assertIsNone(name)
        self.assertEqual(reason, "title_credit_without_dialog_support")

    def test_dialog_address_alone_applies_directly(self):
        verified, _ = SpeakerInferencer._verify_candidates(
            [_candidate("兰姐", "dialog_address", "今天是兰姐的专场")],
            "…今天是兰姐的专场…",
        )
        name, reason = SpeakerInferencer._decide_display_name(verified)
        self.assertEqual(name, "兰姐")
        self.assertIsNone(reason)

    def test_composite_format_is_exact(self):
        """title_credit + dialog_address → 「主名（对话称:别名）」。"""
        verified, _ = SpeakerInferencer._verify_candidates(
            [
                _candidate("魂蓝", "title_credit", "魂蓝专场"),
                _candidate("兰姐", "dialog_address", "兰姐"),
            ],
            "标题：…魂蓝专场…对话…兰姐…",
        )
        name, reason = SpeakerInferencer._decide_display_name(verified)
        self.assertIsNone(reason)
        self.assertEqual(name, "魂蓝（对话称:兰姐）")

    def test_unknown_evidence_type_rejected(self):
        verified, rejected = SpeakerInferencer._verify_candidates(
            [_candidate("张三", "vibes", "张三")],
            "张三",
        )
        self.assertEqual(verified, [])
        self.assertEqual(rejected, ["unknown_evidence_type"])

    def test_partial_quote_substring_is_enough(self):
        """quote 是输入的逐字子串即可（不要求整行/整段）。"""
        verified, _ = SpeakerInferencer._verify_candidates(
            [_candidate("兰姐", "dialog_address", "兰姐，我邀请你")],
            "开场：兰姐，我邀请你做共同主持人了……",
        )
        self.assertEqual(len(verified), 1)


class TestInputFingerprintExtension(unittest.TestCase):
    """锁定决策 5：指纹纳入 title/author/description/规则版本，唯此一处实现。"""

    DIALOGS = [{"speaker": "Speaker1", "text": "hello", "start_time": 0.0}]
    SPEAKERS = ["Speaker1"]

    def test_signature_has_metadata_keyword_params(self):
        """Accept-Check：签名参数 >= 3（keyword 参数扩展）。"""
        sig = inspect.signature(SpeakerInferencer.input_fingerprint)
        self.assertGreaterEqual(len(sig.parameters), 3)
        for param in ("title", "author", "description", "rules_version"):
            self.assertIn(param, sig.parameters)
            self.assertTrue(sig.parameters[param].kind in (inspect.Parameter.KEYWORD_ONLY,))

    def test_metadata_change_changes_fingerprint(self):
        base = SpeakerInferencer.input_fingerprint(self.SPEAKERS, self.DIALOGS, title="t1")
        self.assertNotEqual(
            base,
            SpeakerInferencer.input_fingerprint(self.SPEAKERS, self.DIALOGS, title="t2"),
        )
        self.assertNotEqual(
            base,
            SpeakerInferencer.input_fingerprint(
                self.SPEAKERS, self.DIALOGS, title="t1", author="someone"
            ),
        )
        self.assertNotEqual(
            base,
            SpeakerInferencer.input_fingerprint(
                self.SPEAKERS, self.DIALOGS, title="t1", description="new desc"
            ),
        )

    def test_rules_version_change_changes_fingerprint(self):
        base = SpeakerInferencer.input_fingerprint(
            self.SPEAKERS, self.DIALOGS, rules_version=SPEAKER_EVIDENCE_RULES_VERSION
        )
        self.assertNotEqual(
            base,
            SpeakerInferencer.input_fingerprint(
                self.SPEAKERS, self.DIALOGS, rules_version=SPEAKER_EVIDENCE_RULES_VERSION + 1
            ),
        )

    def test_fingerprint_is_stable_and_default_compatible(self):
        """同输入指纹稳定；不带 keyword 的旧调用形态等价于显式空元数据。"""
        a = SpeakerInferencer.input_fingerprint(self.SPEAKERS, self.DIALOGS, title="t")
        b = SpeakerInferencer.input_fingerprint(self.SPEAKERS, self.DIALOGS, title="t")
        self.assertEqual(a, b)
        self.assertEqual(
            SpeakerInferencer.input_fingerprint(self.SPEAKERS, self.DIALOGS),
            SpeakerInferencer.input_fingerprint(
                self.SPEAKERS, self.DIALOGS, title="", author="", description=""
            ),
        )


if __name__ == "__main__":
    unittest.main()
