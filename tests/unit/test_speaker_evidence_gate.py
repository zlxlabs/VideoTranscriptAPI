"""说话人证据闸门回归测试：核对、缓存、复合显示名与下游语义。"""

import json
import inspect
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from video_transcript_api.llm.core.speaker_inferencer import (
    SPEAKER_EVIDENCE_RULES_VERSION,
    SpeakerInferencer,
)
from tests.unit.test_speaker_self_intro_verifier import install_speaker_llm

FIXTURE_PATH = (
    Path(__file__).parent.parent
    / "fixtures"
    / "twitter_space_1wxWjlzQVwDJQ"
    / "transcript_funasr.json"
)

# 与合成 fixture 逐字一致（fixture 契约的一部分；合成对应关系见该目录 README.md）。
FIXTURE_TITLE = "夜航电台之青柠专场：独立开发者的播客实验，三十天拉新一万"
FIXTURE_AUTHOR = "林晚风"
FIXTURE_DESCRIPTION = (
    "Twitter Space participated by 星野, 长风万里, 阿澈, 小满, 铁蛋, "
    "数码君日常, 阿凯, 夜行者, 望舒, 青山不改, 路人甲, 拾光者"
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


def _verify_in_context(candidates, sections):
    verified, rejected, _pending = SpeakerInferencer._verify_candidates(
        candidates,
        sections,
        dialog_sources={
            "context_before": sections.get("dialog_address", ""),
            "own_samples": "",
        },
    )
    return verified, rejected


def _run_infer_with(candidates, confidence, dialogs=None, speakers=None, title=FIXTURE_TITLE):
    """以 mock LLM 跑完整推断链路，返回 inferencer 与结果。"""
    dialogs = dialogs if dialogs is not None else _load_fixture_dialogs()
    speakers = speakers if speakers is not None else ["Speaker1", "Speaker2"]
    inferencer = _make_inferencer()
    install_speaker_llm(
        inferencer,
        _mock_llm(
            {
                "speaker_mapping": {
                    "Speaker1": "林晚风",
                    "Speaker2": "柠姐",
                },
                "confidence": confidence,
                "candidates": candidates,
                "reasoning": "mock",
            }
        ),
    )
    result = inferencer.infer(
        speakers=speakers,
        dialogs=dialogs,
        title=title,
        author=FIXTURE_AUTHOR,
        description=FIXTURE_DESCRIPTION,
    )
    return inferencer, result


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
    response.structured_output = {"roles": {}, **structured_output}
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
        return _run_infer_with(candidates, confidence, self.dialogs, self.speakers)[1]

    def test_host_author_name_applied_not_placeholder(self):
        """author_host 与本人自我介绍可共同生成推测标注名。"""
        # dialog_address 的 quote 取 Speaker1 真实采样文本的首条（必然逐字
        # 在 prompt 中）；name 是 LLM 对话称呼的声称值，程序只核对 quote 存在性。
        speaker1_sample = self.sample_groups["Speaker1"]["own_samples"][0]
        candidates = {
            "Speaker1": [
                _candidate("林晚风", "author_host", FIXTURE_AUTHOR),
                _candidate("风哥", "dialog_address", speaker1_sample),
            ],
        }
        result = self._run_infer(candidates, confidence={"Speaker1": 0.4, "Speaker2": 0.4})

        prompt_text = self._prompt_text()
        self.assertIn(FIXTURE_AUTHOR, prompt_text)
        self.assertIn(speaker1_sample, prompt_text)
        self.assertNotEqual(result["mapping"]["Speaker1"], "说话人1")
        self.assertTrue(result["meta"]["Speaker1"]["applied"])
        # 双证据 → 带标注复合格式：说话人编号 + 作者关联推测 + 两条来源名
        # （gate 修复轮第 3 轮①：作者名与对话称呼是否同一人属模型推测）。
        self.assertEqual(
            result["mapping"]["Speaker1"],
            "说话人1｜作者关联推测:林晚风｜对话称:风哥",
        )

    def test_soul_lan_composite_display_name(self):
        """Speaker2 的柠姐对话证据与青柠标题证据生成复合名。"""
        prompt_text = self._prompt_text()
        # 「柠姐」必须真实被采样覆盖（top2 加额后在本 fixture 上成立），
        # 否则此测试的前提即失效——称呼片段进不了输入正是本卡要修的根因。
        self.assertIn("柠姐", prompt_text)
        candidates = {
            "Speaker2": [
                _candidate("柠姐", "dialog_address", "柠姐"),
                _candidate("青柠", "title_credit", "青柠专场"),
            ],
        }
        result = self._run_infer(candidates, confidence={"Speaker1": 0.0, "Speaker2": 0.0})

        self.assertEqual(
            result["mapping"]["Speaker2"],
            "说话人2｜标题关联推测:青柠｜对话称:柠姐",
        )
        self.assertTrue(result["meta"]["Speaker2"]["applied"])

    def test_static_quote_must_bind_candidate_name(self):
        candidates = {
            "Speaker2": [
                _candidate("Invented Person", "title_credit", "OpenAI"),
                _candidate("Invented Person", "description_roster", "小满"),
                _candidate("柠姐", "dialog_address", "大家好，我是柠姐，麦能听到了吗？"),
            ],
        }
        result = _run_infer_with(
            candidates, {"Speaker1": 0.0, "Speaker2": 0.0}, title="OpenAI"
        )[1]

        verified = result["meta"]["Speaker2"]["verified_candidates"]
        self.assertEqual([candidate["name"] for candidate in verified], ["柠姐"])
        self.assertEqual(result["mapping"]["Speaker2"], "柠姐")

    def test_forged_quote_rejected_to_placeholder(self):
        """不在输入中的引用作废并降级。"""
        candidates = {
            "Speaker2": [
                _candidate("某大V", "dialog_address", "这句引用根本不在输入文本中"),
                _candidate("青柠", "title_credit", "不存在的标题片段XYZ"),
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
        """低 confidence 不改变通过证据的裁决。"""
        candidates = {
            "Speaker2": [
                _candidate("柠姐", "dialog_address", "柠姐"),
            ],
        }
        result = self._run_infer(candidates, confidence={"Speaker1": 0.0, "Speaker2": 0.1})

        self.assertEqual(result["mapping"]["Speaker2"], "柠姐")
        self.assertTrue(result["meta"]["Speaker2"]["applied"])
        self.assertEqual(result["meta"]["Speaker2"]["confidence"], 0.1)
        # Speaker1 无候选 → 降级；其 confidence 高低不改变结果。
        self.assertEqual(result["mapping"]["Speaker1"], "说话人1")

    def test_whitespace_or_punctuation_quote_rejected(self):
        """真实采样区中的纯标点引用必须作废。"""
        quote = "，"
        prompt_text = self._prompt_text()
        # 前提自证：该 quote 确实逐字存在于本轮输入（排除假红）。
        self.assertIn(quote, prompt_text)
        candidates = {
            "Speaker2": [
                _candidate("某大V", "dialog_address", quote),
            ],
        }
        result = self._run_infer(candidates, confidence={"Speaker1": 0.9, "Speaker2": 0.9})

        self.assertEqual(result["mapping"]["Speaker2"], "说话人2")
        self.assertFalse(result["meta"]["Speaker2"]["applied"])
        self.assertEqual(result["meta"]["Speaker2"]["rejection_reason"], "quote_missing")

    def test_cross_section_quote_rejected(self):
        """描述名单中的名字不能充当对话称呼证据。"""
        # 望舒只出现在描述参会名单，从不出现在任何发言采样里（前提已探针）。
        prompt_text = self._prompt_text()
        self.assertIn("望舒", prompt_text)
        candidates = {
            "Speaker2": [
                _candidate("望舒", "dialog_address", "望舒"),
            ],
        }
        result = self._run_infer(candidates, confidence={"Speaker1": 0.9, "Speaker2": 0.9})

        self.assertEqual(result["mapping"]["Speaker2"], "说话人2")
        self.assertFalse(result["meta"]["Speaker2"]["applied"])
        self.assertEqual(result["meta"]["Speaker2"]["rejection_reason"], "quote_missing")

    def test_v1_cached_artifact_replays_with_legacy_confidence_semantics(self):
        """v1 缓存仍走 confidence 重判。"""
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
        """v2 缓存命中直接复用已裁决结果。"""
        cached_v2 = {
            "mapping": {
                "Speaker1": "说话人1｜作者关联推测:林晚风｜对话称:风哥",
                "Speaker2": "说话人2｜标题关联推测:青柠｜对话称:柠姐",
            },
            "meta": {
                "Speaker1": {
                    "name": "林晚风",
                    "confidence": 0.42,
                    "applied": True,
                    "sampled": True,
                    "candidates": [],
                    "verified_candidates": [
                        {"name": "林晚风", "evidence_type": "author_host"},
                    ],
                },
                "Speaker2": {
                    "name": "柠姐",
                    "confidence": 0.1,
                    "applied": True,
                    "sampled": True,
                    "candidates": [],
                    "verified_candidates": [
                        {"name": "柠姐", "evidence_type": "dialog_address"},
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
        self.assertEqual(
            result["mapping"]["Speaker2"],
            "说话人2｜标题关联推测:青柠｜对话称:柠姐",
        )
        self.assertTrue(result["meta"]["Speaker2"]["applied"])

    def test_dialog_quote_without_name_rejected(self):
        """本人片段引用不含候选名时作废。"""
        quote = "先说结论：三十天拉新一万这件事，没有捷径，全是重复劳动。"
        # 前提自证：quote 真实存在于 Speaker2 自身片段（排除假红——若采样没
        # 覆盖该句，候选会挂在分区核对 quote_missing 而非本判据）。
        inferencer = _make_inferencer()
        sample_groups = inferencer._extract_sample_dialogs(self.dialogs, self.speakers)
        seg2 = inferencer._render_speaker_segment("Speaker2", sample_groups["Speaker2"])
        self.assertIn(quote, seg2)
        self.assertNotIn("某主播", quote)

        candidates = {
            "Speaker2": [_candidate("某主播", "dialog_address", quote)],
        }
        result = self._run_infer(candidates, confidence={"Speaker1": 0.9, "Speaker2": 0.9})

        self.assertEqual(result["mapping"]["Speaker2"], "说话人2")
        self.assertFalse(result["meta"]["Speaker2"]["applied"])
        self.assertEqual(
            result["meta"]["Speaker2"]["rejection_reason"], "name_not_in_quote"
        )

    def test_dialog_quote_containing_name_still_applies(self):
        """本人自我介绍仍然通过核对。"""
        quote = "大家好，我是柠姐，麦能听到了吗？"
        candidates = {
            "Speaker2": [_candidate("柠姐", "dialog_address", quote)],
        }
        result = self._run_infer(candidates, confidence={"Speaker1": 0.9, "Speaker2": 0.9})

        self.assertEqual(result["mapping"]["Speaker2"], "柠姐")
        self.assertTrue(result["meta"]["Speaker2"]["applied"])


class TestGateRound2QuoteScope(unittest.TestCase):
    """说话人对话证据的来源范围回归。"""

    def setUp(self):
        self.dialogs = _load_fixture_dialogs()
        self.speakers = ["Speaker1", "Speaker2"]
        inferencer = _make_inferencer()
        self.sample_groups = inferencer._extract_sample_dialogs(self.dialogs, self.speakers)
        self.seg1 = inferencer._render_speaker_segment("Speaker1", self.sample_groups["Speaker1"])
        self.seg2 = inferencer._render_speaker_segment("Speaker2", self.sample_groups["Speaker2"])

    def test_dialog_quote_borrowed_from_other_speaker_rejected(self):
        """不允许借用别的说话人的上下文称呼。"""
        quote = "柠姐，我邀请你做共同主持人了"
        self.assertIn(quote, self.seg2)
        self.assertNotIn(quote, self.seg1)

        candidates = {
            "Speaker1": [_candidate("柠姐", "dialog_address", quote)],
            "Speaker2": [_candidate("柠姐", "dialog_address", quote)],
        }
        _, result = _run_infer_with(candidates, {"Speaker1": 0.9, "Speaker2": 0.9})

        # 跨说话人借用：Speaker1 的候选作废、降级「说话人1」，meta 记 quote_missing。
        self.assertEqual(result["mapping"]["Speaker1"], "说话人1")
        self.assertFalse(result["meta"]["Speaker1"]["applied"])
        self.assertEqual(result["meta"]["Speaker1"]["rejection_reason"], "quote_missing")
        # 正例对照：同一 quote 对归属说话人（出场上下文所在者）核对通过 applied。
        self.assertEqual(result["mapping"]["Speaker2"], "柠姐")
        self.assertTrue(result["meta"]["Speaker2"]["applied"])

    def test_static_evidence_only_never_applies(self):
        """三类静态证据通过核对也不能单独命名。"""
        candidates = {
            "Speaker2": [
                _candidate("青柠", "title_credit", "青柠专场"),
                _candidate("林晚风", "author_host", FIXTURE_AUTHOR),
                _candidate("小满", "description_roster", "小满"),
            ],
        }
        _, result = _run_infer_with(candidates, {"Speaker1": 0.9, "Speaker2": 0.9})

        self.assertEqual(result["mapping"]["Speaker2"], "说话人2")
        self.assertFalse(result["meta"]["Speaker2"]["applied"])
        # 三条静态行 quote 均真实 → 核对全部通过（收窄不波及静态行），
        # 但裁决拒绝：拒绝原因是标题名缺对话佐证。
        self.assertEqual(len(result["meta"]["Speaker2"]["verified_candidates"]), 3)
        self.assertEqual(
            result["meta"]["Speaker2"]["rejection_reason"],
            "title_credit_without_dialog_support",
        )


class _FingerprintAwareCache:
    """模拟按真实 input_fingerprint 命中缓存。"""

    def __init__(self):
        self.store = {}
        self.saved_fingerprints = []

    def get_speaker_mapping(self, platform, media_id, input_fingerprint="", speakers=None):
        return self.store.get((platform, media_id, input_fingerprint))

    def save_speaker_mapping(self, platform, media_id, result, input_fingerprint="", speakers=None, source=""):
        self.saved_fingerprints.append(input_fingerprint)
        self.store[(platform, media_id, input_fingerprint)] = {
            "mapping": dict(result["mapping"]),
            "meta": json.loads(json.dumps(result["meta"])),
            "low_confidence": list(result["low_confidence"]),
        }


class TestMetadataChangeMissesCache(unittest.TestCase):
    """元数据变化必须改变缓存指纹并重算。"""

    def _make(self):
        inferencer = _make_inferencer()
        cache = _FingerprintAwareCache()
        inferencer.cache_manager = cache
        calls = {"n": 0}

        def fake_llm_call(**kwargs):
            calls["n"] += 1
            return _mock_llm(
                {
                    "speaker_mapping": {"Speaker1": "林晚风", "Speaker2": "柠姐"},
                    "confidence": {"Speaker1": 0.9, "Speaker2": 0.9},
                    "candidates": {},
                    "reasoning": "mock",
                }
            )

        inferencer.llm_client.call = MagicMock(side_effect=fake_llm_call)
        return inferencer, cache, calls

    def test_metadata_change_misses_cache_and_reinfers(self):
        inferencer, cache, calls = self._make()
        base = dict(
            speakers=["Speaker1", "Speaker2"],
            dialogs=_load_fixture_dialogs(),
            platform="twitter_space",
            media_id="1wxWjlzQVwDJQ",
        )

        r1 = inferencer.infer(title="T1", author="A1", description="D1", **base)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(r1["source"], "llm")

        # 同元数据再次推断：指纹相同 → 缓存命中，不再调 LLM（对照，排除
        # 「永远 miss」的恒真假绿）。
        r2 = inferencer.infer(title="T1", author="A1", description="D1", **base)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(r2["source"], "cache_hit")

        # title 变 → miss 重算
        r3 = inferencer.infer(title="T2", author="A1", description="D1", **base)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(r3["source"], "llm")
        # author 变 → miss 重算
        inferencer.infer(title="T2", author="A2", description="D1", **base)
        self.assertEqual(calls["n"], 3)
        # description 变 → miss 重算
        inferencer.infer(title="T2", author="A2", description="D2", **base)
        self.assertEqual(calls["n"], 4)

        # 四次真实推断的写侧指纹互不相同（元数据组合两两有差异）。
        self.assertEqual(len(set(cache.saved_fingerprints)), 4)


class TestEvidenceGateRules(unittest.TestCase):
    """裁决规则的确定性分支（直接驱动 _verify_candidates/_decide_display_name）。"""

    def test_description_roster_alone_never_applies(self):
        """参会名单核对通过也不得单独 applied（名单≠发言人）。"""
        verified, rejected = _verify_in_context(
            [_candidate("小满", "description_roster", "小满")],
            {"description_roster": "描述：…小满…"},
        )
        self.assertEqual(len(verified), 1)
        name, reason = SpeakerInferencer._decide_display_name(
            verified, "Speaker2", ["Speaker2"]
        )
        self.assertIsNone(name)
        self.assertEqual(reason, "description_roster_not_standalone")

    def test_static_name_binding_keeps_matching_author_without_support(self):
        for evidence_type, name, quote, section, label in (
            ("title_credit", "青柠", "青柠专场", "title_credit", "标题"),
            ("author_host", "John Collison", "John Collison", "author_host", "作者"),
            ("author_host", " John Collison ", "JOHN COLLISON", "author_host", "作者"),
        ):
            verified, rejected = _verify_in_context(
                [_candidate(name, evidence_type, quote)],
                {section: f"- {label}：{quote}"},
            )
            self.assertEqual(rejected, [])
            self.assertEqual([candidate["name"] for candidate in verified], [name.strip()])
            display, reason = SpeakerInferencer._decide_display_name(
                verified, "Speaker2", ["Speaker2"]
            )
            self.assertIsNone(display)
            self.assertEqual(reason, "title_credit_without_dialog_support")

    def test_context_before_address_remains_valid(self):
        quote = "今天是青柠姐的专场"
        inferencer = _make_inferencer()
        dialogs = _load_fixture_dialogs()
        speakers = ["Speaker1", "Speaker2"]
        samples = inferencer._extract_sample_dialogs(dialogs, speakers)
        context = "\n".join(text for _, text in samples["Speaker2"]["context_before"])
        own = "\n".join(samples["Speaker2"]["own_samples"])
        self.assertIn(quote, context)
        self.assertNotIn(quote, own)
        result = _run_infer_with(
            {"Speaker2": [_candidate("青柠姐", "dialog_address", quote)]},
            {"Speaker1": 0.0, "Speaker2": 0.0},
        )[1]
        self.assertEqual(result["mapping"]["Speaker2"], "青柠姐")

    def test_composite_format_is_exact(self):
        """title_credit + dialog_address → 带标注复合名（gate 修复轮第 3 轮①：
        三要素缺一不可——说话人编号、「推测」字样、两条来源名）。"""
        verified, _ = _verify_in_context(
            [
                _candidate("青柠", "title_credit", "青柠专场"),
                _candidate("柠姐", "dialog_address", "柠姐"),
            ],
            {"title_credit": "标题：…青柠专场…", "dialog_address": "对话…柠姐…"},
        )
        name, reason = SpeakerInferencer._decide_display_name(
            verified, "Speaker2", ["Speaker2"]
        )
        self.assertIsNone(reason)
        self.assertEqual(name, "说话人2｜标题关联推测:青柠｜对话称:柠姐")

    def test_unknown_evidence_type_rejected(self):
        verified, rejected = _verify_in_context(
            [_candidate("张三", "vibes", "张三")],
            {"dialog_address": "张三"},
        )
        self.assertEqual(verified, [])
        self.assertEqual(rejected, ["unknown_evidence_type"])

    def test_partial_quote_substring_is_enough(self):
        """quote 是输入的逐字子串即可（不要求整行/整段）。"""
        verified, _ = _verify_in_context(
            [_candidate("柠姐", "dialog_address", "柠姐，我邀请你")],
            {"dialog_address": "开场：柠姐，我邀请你做共同主持人了……"},
        )
        self.assertEqual(len(verified), 1)

    def test_whitespace_and_punctuation_only_quotes_all_rejected(self):
        """修复①边界值：纯空白/纯标点（含长度>=2 的多标点串）一律
        quote_missing；带实质字符的才放行。"""
        for quote in ("\u3000\u3000", "\u201c\u201d", "，", "，。", " 。 "):
            verified, rejected = _verify_in_context(
                [_candidate("张三", "dialog_address", quote)],
                {"dialog_address": f"对话{quote}后文"},
            )
            self.assertEqual(verified, [], f"quote={quote!r} should be rejected")
            self.assertEqual(rejected, ["quote_missing"])
        # 对照：去首尾空白后有实质内容的 quote 放行（逐字存在性仍用原文核对）。
        verified, _ = _verify_in_context(
            [_candidate("张三", "dialog_address", " 张三 ")],
            {"dialog_address": "对话 张三 后文"},
        )
        self.assertEqual(len(verified), 1)

    def test_dialog_name_binding_case_insensitive_for_english(self):
        """修复④（第 4 轮）边界：dialog_address 的 quote 必须包含候选名本身，
        英文名忽略大小写（ADRIAN PUNK 支持 Adrian Punk）；不含名的真实
        quote 对该名字无证据力 → name_not_in_quote。"""
        verified, rejected = _verify_in_context(
            [_candidate("Adrian Punk", "dialog_address", "welcome, ADRIAN PUNK speaking")],
            {"dialog_address": "welcome, ADRIAN PUNK speaking"},
        )
        self.assertEqual(len(verified), 1)
        self.assertEqual(rejected, [])

        verified, rejected = _verify_in_context(
            [_candidate("张三", "dialog_address", "今天天气不错")],
            {"dialog_address": "今天天气不错"},
        )
        self.assertEqual(verified, [])
        self.assertEqual(rejected, ["name_not_in_quote"])

    def test_quote_in_wrong_section_rejected(self):
        """修复②边界：quote 只存在于另一分区/分区缺失时，逐字存在也不放行。"""
        verified, rejected = _verify_in_context(
            [_candidate("望舒", "dialog_address", "望舒")],
            {"description_roster": "- 描述：…望舒…"},
        )
        self.assertEqual(verified, [])
        self.assertEqual(rejected, ["quote_missing"])
        # 分区缺失（如空描述不进 prompt）→ fail-closed。
        verified, rejected = _verify_in_context(
            [_candidate("青柠", "title_credit", "青柠专场")],
            {},
        )
        self.assertEqual(verified, [])
        self.assertEqual(rejected, ["quote_missing"])


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


class TestCompositeNameSemantics(unittest.TestCase):
    """复合名必须包含说话人、推测标记和两条来源。"""

    def _verified(self, credit_type="title_credit"):
        credit_name = "青柠" if credit_type == "title_credit" else "林晚风"
        credit_quote = "青柠专场" if credit_type == "title_credit" else "林晚风"
        verified, rejected = _verify_in_context(
            [
                _candidate(credit_name, credit_type, credit_quote),
                _candidate("柠姐", "dialog_address", "柠姐"),
            ],
            {
                "title_credit": "标题：…青柠专场…原文引用…",
                "author_host": "作者：…林晚风…原文引用…",
                "dialog_address": "对话…柠姐…",
            },
        )
        self.assertEqual(rejected, [])
        self.assertEqual(len(verified), 2)
        return verified

    def test_title_composite_has_speaker_number_and_marker_and_both_names(self):
        name, reason = SpeakerInferencer._decide_display_name(
            self._verified("title_credit"), "Speaker2", ["Speaker1", "Speaker2"]
        )
        self.assertIsNone(reason)
        for element in ("说话人2", "推测", "青柠", "柠姐"):
            self.assertIn(element, name)
        self.assertEqual(name, "说话人2｜标题关联推测:青柠｜对话称:柠姐")

    def test_author_composite_marker(self):
        name, reason = SpeakerInferencer._decide_display_name(
            self._verified("author_host"), "Speaker1", ["Speaker1", "Speaker2"]
        )
        self.assertIsNone(reason)
        for element in ("说话人1", "推测", "林晚风", "柠姐"):
            self.assertIn(element, name)
        self.assertEqual(name, "说话人1｜作者关联推测:林晚风｜对话称:柠姐")

    def test_composite_never_bare_credit_name(self):
        """反例锁定：双证据时绝不允许返回裸标题名/作者名（无标注形态）。"""
        for credit_type in ("title_credit", "author_host"):
            name, _ = SpeakerInferencer._decide_display_name(
                self._verified(credit_type), "Speaker2", ["Speaker2"]
            )
            self.assertIn("推测", name)
            self.assertNotEqual(name, "青柠")
            self.assertNotEqual(name, "林晚风")
            self.assertNotEqual(name, "柠姐")

    def test_non_numeric_label_uses_position_number(self):
        """非数字说话人标签按列表位次编号。"""
        verified = self._verified("title_credit")
        name, _ = SpeakerInferencer._decide_display_name(
            verified, "说话人B", ["说话人A", "说话人B"]
        )
        self.assertTrue(name.startswith("说话人2｜"), name)

    def test_wrong_credit_association_still_annotated_end_to_end(self):
        """真实但错误关联的两条证据仍须显示推测标注。"""
        dialogs = [
            {"speaker": "Speaker1", "text": "欢迎小张来到今晚的直播", "start_time": 0.0},
            {"speaker": "Speaker2", "text": "大家好我是小张，今天来串门", "start_time": 1.0},
            {"speaker": "Speaker1", "text": "小张请坐", "start_time": 2.0},
        ]
        inferencer = _make_inferencer()
        inferencer.llm_client.call = MagicMock(
            return_value=_mock_llm(
                {
                    "speaker_mapping": {"Speaker1": "小张", "Speaker2": "青柠"},
                    "confidence": {"Speaker1": 0.9, "Speaker2": 0.9},
                    "candidates": {
                        "Speaker2": [
                            _candidate("小张", "dialog_address", "欢迎小张"),
                            # 语义错误关联：小张与标题专场毫无关系，但 quote 逐字真实。
                            _candidate("青柠", "title_credit", "青柠专场"),
                        ],
                    },
                    "reasoning": "mock",
                }
            )
        )
        result = inferencer.infer(
            speakers=["Speaker1", "Speaker2"],
            dialogs=dialogs,
            title="青柠专场：夜话电台",
        )

        display = result["mapping"]["Speaker2"]
        # 不允许裸名（无论裸标题名还是裸对话称呼）：推测必须就地可见。
        self.assertNotEqual(display, "小张")
        self.assertNotEqual(display, "青柠")
        for element in ("说话人2", "推测", "青柠", "小张"):
            self.assertIn(element, display)
        self.assertTrue(result["meta"]["Speaker2"]["applied"])


class TestCompositeNameDownstreamSurfaces(unittest.TestCase):
    """复合名在网页与归一化结果中保持不变。"""

    COMPOSITE = "说话人2｜标题关联推测:青柠｜对话称:柠姐"

    def test_web_html_contains_all_composite_elements(self):
        """网页 HTML 保留复合名的四个组成部分。"""
        import tempfile

        from video_transcript_api.utils.rendering.dialog_renderer import DialogRenderer

        payload = {
            "dialogs": [
                {
                    "segment_id": "seg-1",
                    "speaker": self.COMPOSITE,
                    "speaker_id": "Speaker2",
                    "text": "大家好",
                    "start_time": "00:00:01",
                }
            ],
            "speaker_mapping": {"Speaker2": self.COMPOSITE},
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "render"
            cache_dir.mkdir()
            (cache_dir / "llm_processed.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            html_out = DialogRenderer()._render_from_structured_data(str(cache_dir))

        for element in ("说话人2", "推测", "青柠", "柠姐"):
            self.assertIn(element, html_out)
        # 复合名自带推测标注，不再触发「待确认」badge（全串锚定匹配不命中）。
        self.assertNotIn('data-speaker-status="pending"', html_out)

    def test_normalized_dialogs_carry_composite_verbatim(self):
        """归一化对话保留复合名与原始 speaker_id。"""
        from video_transcript_api.llm.processors.speaker_aware_processor import (
            SpeakerAwareProcessor,
        )

        processor = SpeakerAwareProcessor.__new__(SpeakerAwareProcessor)
        dialogs = [
            {
                "speaker": "Speaker2",
                "text": "大家好",
                "start_time": "00:00:01",
                "end_time": "00:00:02",
            }
        ]
        normalized = processor._normalize_and_merge_dialogs(
            dialogs, {"Speaker2": self.COMPOSITE}, has_speaker=True
        )
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["speaker"], self.COMPOSITE)
        self.assertEqual(normalized[0]["speaker_id"], "Speaker2")


class TestPlatformSpeakerPriors(unittest.TestCase):
    """平台背景只进入提示词，不参与证据引用核对。"""

    def _build(self, platform=""):
        from video_transcript_api.llm.prompts import (
            PLATFORM_SPEAKER_PRIORS,
            build_speaker_inference_user_prompt,
        )

        return build_speaker_inference_user_prompt(
            context_snippets="[Speaker1]：你好",
            original_speakers=["Speaker1"],
            video_title="标题",
            author="作者",
            description="描述",
            platform=platform,
        ), PLATFORM_SPEAKER_PRIORS

    def test_prior_injected_for_known_platform(self):
        prompt, priors = self._build(platform="twitter_space")
        self.assertIn(priors["twitter_space"], prompt)
        # 先验段位置：位于元数据区内（描述行之后、转录片段区之前）。
        self.assertIn(priors["twitter_space"], prompt.split("**转录内容片段**")[0])

    def test_unknown_or_missing_platform_output_unchanged(self):
        """未配置先验的平台保持旧 prompt 文本。"""
        from video_transcript_api.llm.prompts import build_speaker_inference_user_prompt

        legacy = build_speaker_inference_user_prompt(
            context_snippets="[Speaker1]：你好",
            original_speakers=["Speaker1"],
            video_title="标题",
            author="作者",
            description="描述",
        )
        for platform in ("", "youtube", "bilibili"):
            self.assertEqual(
                self._build(platform=platform)[0],
                legacy,
                f"platform={platform!r} 的 prompt 与旧输出不一致",
            )

    def test_prior_never_enters_quote_sections(self):
        """平台先验不进入任何证据引用分区。"""
        from video_transcript_api.llm.prompts import (
            PLATFORM_SPEAKER_PRIORS,
            build_speaker_quote_sections,
        )

        sections = build_speaker_quote_sections(
            video_title="标题",
            author="作者",
            description="描述",
            context_snippets="[Speaker1]：你好",
        )
        prior = PLATFORM_SPEAKER_PRIORS["twitter_space"]
        for evidence_type, text in sections.items():
            self.assertNotIn(prior, text)
            self.assertNotIn("平台背景", text)



if __name__ == "__main__":
    unittest.main()
