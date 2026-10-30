"""第三人提及旁证（呼叫-回应）的验收与回归测试（gate 修复轮第 5 轮①②④）。

生产形态（卡面续修指令）：主持人从不自称、称呼全部出现在他人段落且呼叫后
紧邻本人应答——旧实现下这类称呼无法成为 dialog_address 证据（本人片段分区
核对不到，跨说话人借用被禁止），主持人只能显示「说话人1」。旁证机制上线后：

- 采样阶段收集「他人呼叫 + 紧邻本人应答」的短句（每说话人 ≤2 条、单条
  ≤80 字符、全局 ≤24 条），渲染为独立旁证区；
- dialog_address 的 quote 允许来自本说话人的旁证条目（quote 含候选名 +
  收集时已程序核实呼叫-回应），meta 如实标注来源说话人与段号；
- 错误目标拒绝（呼叫后非本人应答不成立）、仅提及无指向不 applied。

fixture：tests/fixtures/call_response_host/（人工合成，见该目录 README.md）。
"""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from video_transcript_api.llm.core.speaker_inferencer import SpeakerInferencer

FIXTURE_PATH = (
    Path(__file__).parent.parent / "fixtures" / "call_response_host" / "transcript_funasr.json"
)

# 与 fixture README 元数据逐字一致。
FIXTURE_TITLE = "远航夜话之江远专场：小团队出海避坑实录"
FIXTURE_AUTHOR = "江远"
FIXTURE_DESCRIPTION = "直播 participated by 沈舟, 阿北, 老白, 阿慧, 木木, 阿岛"

CALL_QUOTE_1 = "江哥，开场白你先来，我们洗耳恭听。"  # 段1（Speaker3 说）→ 段2 Speaker1 应答 ✔
CALL_QUOTE_2 = "江哥，时间差不多了吧？"  # 段13（Speaker3 说）→ 段14 Speaker1 应答 ✔
CALL_QUOTE_3 = "江哥，我第一个举手。"  # 段15（Speaker4 说）→ 段16 Speaker1 应答 ✔（超每人 2 条配额）
WRONG_TARGET_QUOTE = "江哥，开放提问环节大概什么时候开始？"  # 段9 → 段10 Speaker3 应答（非 Speaker1）
MENTION_QUOTE = "知遥姐这个开场就很抓人，我都想直接听结论了。"  # 段5 → 段6 Speaker4 应答（非 Speaker2）

ALL_SPEAKERS = ["Speaker1", "Speaker2", "Speaker3", "Speaker4"]


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


def _make_inferencer():
    return SpeakerInferencer(
        llm_client=MagicMock(),
        cache_manager=None,
        model="test-model",
    )


def _mock_llm(structured_output):
    response = MagicMock()
    response.structured_output = structured_output
    return response


def _run_infer(candidates, confidence, speakers=None):
    """以 mock LLM 跑完整 infer 链路（合成 fixture + 旁证裁决）。"""
    inferencer = _make_inferencer()
    mapping = {speaker: speaker for speaker in (speakers or ALL_SPEAKERS)}
    inferencer.llm_client.call = MagicMock(
        return_value=_mock_llm(
            {
                "speaker_mapping": mapping,
                "confidence": confidence,
                "candidates": candidates,
                "reasoning": "mock",
            }
        )
    )
    result = inferencer.infer(
        speakers=speakers or ALL_SPEAKERS,
        dialogs=_load_fixture_dialogs(),
        title=FIXTURE_TITLE,
        author=FIXTURE_AUTHOR,
        description=FIXTURE_DESCRIPTION,
        platform="twitter_space",
    )
    return inferencer, result


class TestCallResponseEndToEnd(unittest.TestCase):
    """生产形态端到端：不自称主持人的称呼经旁证区可达。"""

    def test_host_address_via_corroboration_applied(self):
        """核心可达性：Speaker1 的「江哥」称呼句在他人段落（旁证），配合
        author_host 双证据 → 复合显示名 applied。旧实现下该 quote 不在本人
        片段分区、核对作废，Speaker1 只能降级「说话人1」。"""
        _, result = _run_infer(
            candidates={
                "Speaker1": [
                    _candidate("江哥", "dialog_address", CALL_QUOTE_1),
                    _candidate("江远", "author_host", "- 作者：江远"),
                ],
                "Speaker2": [],
                "Speaker3": [],
                "Speaker4": [],
            },
            confidence={"Speaker1": 0.9, "Speaker2": 0.5, "Speaker3": 0.5, "Speaker4": 0.5},
        )
        self.assertNotEqual(result["mapping"]["Speaker1"], "说话人1")
        self.assertIn("江哥", result["mapping"]["Speaker1"])
        self.assertIn("江远", result["mapping"]["Speaker1"])
        self.assertIn("推测", result["mapping"]["Speaker1"])
        self.assertIn("说话人1", result["mapping"]["Speaker1"])
        self.assertTrue(result["meta"]["Speaker1"]["applied"])

    def test_self_introduced_speaker_unchanged(self):
        """Speaker2 型本人直呼案例：显示名保持自称名（旁证机制不改变既有
        路径的裁决结果）。"""
        _, result = _run_infer(
            candidates={
                "Speaker1": [],
                "Speaker2": [
                    _candidate(
                        "林知遥",
                        "dialog_address",
                        "大家好，我是林知遥，今天给大家分享我的出海经历。",
                    )
                ],
                "Speaker3": [],
                "Speaker4": [],
            },
            confidence={"Speaker1": 0.5, "Speaker2": 0.9, "Speaker3": 0.5, "Speaker4": 0.5},
        )
        self.assertEqual(result["mapping"]["Speaker2"], "林知遥")
        self.assertTrue(result["meta"]["Speaker2"]["applied"])

    def test_wrong_target_rejected(self):
        """错误目标拒绝：段9 呼叫「江哥」但紧邻应答者是 Speaker3——对
        Speaker1 不构成旁证（非本人应答不成立），Speaker2 引用同样作废。"""
        _, result = _run_infer(
            candidates={
                "Speaker1": [_candidate("江哥", "dialog_address", WRONG_TARGET_QUOTE)],
                "Speaker2": [_candidate("江哥", "dialog_address", WRONG_TARGET_QUOTE)],
                "Speaker3": [],
                "Speaker4": [],
            },
            confidence={"Speaker1": 0.9, "Speaker2": 0.9, "Speaker3": 0.5, "Speaker4": 0.5},
        )
        for speaker in ("Speaker1", "Speaker2"):
            self.assertEqual(
                result["mapping"][speaker],
                f"说话人{speaker[-1]}",
                f"{speaker} 不得通过非本人应答的呼叫句获得显示名",
            )
            self.assertEqual(result["meta"][speaker]["rejection_reason"], "quote_missing")

    def test_mention_without_response_rejected(self):
        """仅提及无指向不 applied：段5 提「知遥姐」但紧邻应答者是 Speaker4，
        Speaker2 引用它必须作废。"""
        _, result = _run_infer(
            candidates={
                "Speaker1": [],
                "Speaker2": [_candidate("知遥姐", "dialog_address", MENTION_QUOTE)],
                "Speaker3": [],
                "Speaker4": [],
            },
            confidence={"Speaker1": 0.5, "Speaker2": 0.9, "Speaker3": 0.5, "Speaker4": 0.5},
        )
        self.assertEqual(result["mapping"]["Speaker2"], "说话人2")
        self.assertEqual(result["meta"]["Speaker2"]["rejection_reason"], "quote_missing")

    def test_quota_boundary_third_call_rejected(self):
        """旁证预算边界：每说话人至多 2 条，第 3 条呼叫句（段15）不入旁证区，
        单独引用它必须作废。"""
        _, result = _run_infer(
            candidates={
                "Speaker1": [_candidate("江哥", "dialog_address", CALL_QUOTE_3)],
                "Speaker2": [],
                "Speaker3": [],
                "Speaker4": [],
            },
            confidence={"Speaker1": 0.9, "Speaker2": 0.5, "Speaker3": 0.5, "Speaker4": 0.5},
        )
        self.assertEqual(result["mapping"]["Speaker1"], "说话人1")
        self.assertEqual(result["meta"]["Speaker1"]["rejection_reason"], "quote_missing")

    def test_low_confidence_still_applied(self):
        """confidence 已退出裁决：自报 0.1 但旁证引用核对通过 → applied。"""
        _, result = _run_infer(
            candidates={
                "Speaker1": [
                    _candidate("江哥", "dialog_address", CALL_QUOTE_1),
                    _candidate("江远", "author_host", "- 作者：江远"),
                ],
                "Speaker2": [],
                "Speaker3": [],
                "Speaker4": [],
            },
            confidence={"Speaker1": 0.1, "Speaker2": 0.1, "Speaker3": 0.1, "Speaker4": 0.1},
        )
        self.assertTrue(result["meta"]["Speaker1"]["applied"])
        self.assertEqual(result["meta"]["Speaker1"]["confidence"], 0.1)

    def test_meta_records_corroboration_source(self):
        """evidence meta 如实标注旁证 quote 的来源说话人与段号。"""
        _, result = _run_infer(
            candidates={
                "Speaker1": [
                    _candidate("江哥", "dialog_address", CALL_QUOTE_1),
                    _candidate("江远", "author_host", "- 作者：江远"),
                ],
                "Speaker2": [],
                "Speaker3": [],
                "Speaker4": [],
            },
            confidence={"Speaker1": 0.9, "Speaker2": 0.5, "Speaker3": 0.5, "Speaker4": 0.5},
        )
        verified = result["meta"]["Speaker1"]["verified_candidates"]
        corroboration_entries = [
            entry["corroboration"]
            for entry in verified
            if "corroboration" in entry
        ]
        self.assertEqual(len(corroboration_entries), 1)
        self.assertEqual(corroboration_entries[0]["source_speaker"], "Speaker3")
        self.assertEqual(corroboration_entries[0]["segment_index"], 1)

    def test_corroboration_section_renders_into_prompt(self):
        """旁证区进入模型输入：prompt 含独立旁证区、实际发言者与段号标注，
        且不混入本人分组区。"""
        inferencer, _ = _run_infer(
            candidates={
                "Speaker1": [],
                "Speaker2": [],
                "Speaker3": [],
                "Speaker4": [],
            },
            confidence={speaker: 0.5 for speaker in ALL_SPEAKERS},
        )
        call = inferencer.llm_client.call
        self.assertEqual(call.call_count, 1)
        user_prompt = call.call_args.kwargs["user_prompt"]
        self.assertIn("**第三人提及旁证**", user_prompt)
        self.assertIn(f"[Speaker3]: {CALL_QUOTE_1}", user_prompt)
        self.assertIn("呼叫对象：Speaker1", user_prompt)
        # 独立区：旁证行以「- 段」开头且带呼叫对象标注；同一句出现在发言者
        # 本人分组（作为其发言样本）是预期——旁证区只是额外把它暴露给被
        # 呼叫说话人的证据通道，不改变发言者分组。
        corroboration_lines = [
            line for line in user_prompt.splitlines() if line.startswith("- 段")
        ]
        self.assertTrue(
            any(CALL_QUOTE_1 in line and "呼叫对象：Speaker1" in line for line in corroboration_lines),
            f"旁证区未渲染呼叫句：{corroboration_lines}",
        )


class TestCallResponseCollection(unittest.TestCase):
    """_collect_call_response_evidence 的收集判据（单元级）。"""

    def setUp(self):
        self.inferencer = _make_inferencer()
        self.speakers = ["Speaker1", "Speaker2"]
        self.sample_groups = self.inferencer._extract_sample_dialogs(
            self._dialogs(), self.speakers
        )

    def _dialogs(self):
        return _load_fixture_dialogs()

    def test_call_with_immediate_response_collected(self):
        corroboration = self.inferencer._collect_call_response_evidence(
            self._dialogs(), self.sample_groups
        )
        entries = corroboration.get("Speaker1", [])
        texts = [entry["text"] for entry in entries]
        self.assertIn(CALL_QUOTE_1, texts)
        first = next(entry for entry in entries if entry["text"] == CALL_QUOTE_1)
        self.assertEqual(first["speaker"], "Speaker3")
        self.assertEqual(first["index"], 1)
        self.assertEqual(first["time"], "00:00:04")

    def test_response_by_other_speaker_not_collected(self):
        """错误目标：段9 的紧邻应答者是 Speaker3 → 不构成 Speaker1/Speaker2
        任何人的旁证条目。"""
        corroboration = self.inferencer._collect_call_response_evidence(
            self._dialogs(), self.sample_groups
        )
        for speaker in ("Speaker1", "Speaker2"):
            for entry in corroboration.get(speaker, []):
                self.assertNotEqual(
                    entry["text"],
                    WRONG_TARGET_QUOTE,
                    f"{speaker} 不得从非本人应答的呼叫句获得旁证",
                )

    def test_mention_responded_by_third_party_not_target(self):
        """仅提及：段5 提「知遥姐」但紧邻应答者是 Speaker4 → 不构成
        Speaker2 的旁证条目。"""
        corroboration = self.inferencer._collect_call_response_evidence(
            self._dialogs(), self.sample_groups
        )
        for entry in corroboration.get("Speaker2", []):
            self.assertNotEqual(entry["text"], MENTION_QUOTE)

    def test_per_speaker_quota_two(self):
        """每说话人至多 2 条：Speaker1 的 3 条合格呼叫只收前 2 条（段1、
        段13），段15 超配额不入。"""
        corroboration = self.inferencer._collect_call_response_evidence(
            self._dialogs(), self.sample_groups
        )
        entries = corroboration["Speaker1"]
        self.assertEqual(len(entries), 2)
        texts = [entry["text"] for entry in entries]
        self.assertIn(CALL_QUOTE_1, texts)
        self.assertIn(CALL_QUOTE_2, texts)
        self.assertNotIn(CALL_QUOTE_3, texts)

    def test_single_entry_truncated(self):
        """单条旁证短句截断到 80 字符。"""
        inferencer = _make_inferencer()
        dialogs = [
            {"speaker": "Speaker1", "text": "开场", "start_time": 0.0},
            {
                "speaker": "Speaker2",
                "text": "老师" + "讲得非常非常仔细认真耐心负责" * 12 + "请继续。",
                "start_time": 4.0,
            },
            {"speaker": "Speaker1", "text": "好的，我继续讲。", "start_time": 8.0},
        ]
        sample_groups = inferencer._extract_sample_dialogs(dialogs, ["Speaker1"])
        corroboration = inferencer._collect_call_response_evidence(dialogs, sample_groups)
        entries = corroboration["Speaker1"]
        self.assertEqual(len(entries), 1)
        self.assertLessEqual(len(entries[0]["text"]), 80)

    def test_unsampled_speaker_never_receives(self):
        """未被采样（预算裁剪）的说话人不收集旁证：responder 不在
        sample_groups 的 key 里。"""
        inferencer = _make_inferencer()
        dialogs = [
            {"speaker": "Speaker1", "text": "开场白", "start_time": 0.0},
            {"speaker": "Speaker9", "text": "老师请继续。", "start_time": 4.0},
            {"speaker": "Speaker9", "text": "好的。", "start_time": 8.0},
        ]
        sample_groups = inferencer._extract_sample_dialogs(dialogs, ["Speaker1"])
        corroboration = inferencer._collect_call_response_evidence(dialogs, sample_groups)
        # Speaker9 未被采样：呼叫句不构成它的旁证；responder 不是被采样
        # 说话人 → 不收进任何人。
        self.assertEqual(corroboration, {})

    def test_global_cap_24_entries(self):
        """全局旁证条数上限 24：13 个被采样说话人 × 每人 2 条合格呼叫 = 26
        条欲收，全局 cap 24 截停（按呼叫发生时间先到先得）。"""
        inferencer = _make_inferencer()
        responders = [f"Speaker{i}" for i in range(1, 14)]  # 13 个被采样说话人
        callers = [f"Guest{i}" for i in range(1, 27)]  # 26 个呼叫者
        dialogs = []
        t = 0.0
        for i, caller in enumerate(callers):
            dialogs.append({"speaker": caller, "text": "老师请讲。", "start_time": t})
            t += 4
            dialogs.append(
                {"speaker": responders[i % len(responders)], "text": "好的。", "start_time": t}
            )
            t += 4
        sample_groups = inferencer._extract_sample_dialogs(dialogs, responders)
        corroboration = inferencer._collect_call_response_evidence(dialogs, sample_groups)
        total = sum(len(entries) for entries in corroboration.values())
        self.assertEqual(total, 24)


class TestCorroborationVerify(unittest.TestCase):
    """_verify_candidates 旁证引用判据（单元级）。"""

    def _entries(self):
        return [
            {"speaker": "Speaker3", "text": CALL_QUOTE_1, "index": 1, "time": "00:00:04"},
        ]

    def test_corroboration_quote_verified_with_meta(self):
        verified, rejected = SpeakerInferencer._verify_candidates(
            [_candidate("江哥", "dialog_address", CALL_QUOTE_1)],
            quote_sections={"dialog_address": "[Speaker1]（本人片段，不含旁证句）"},
            corroboration_entries=self._entries(),
            dialog_sources={"context_before": "", "own_samples": ""},
        )
        self.assertEqual(rejected, [])
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["corroboration"]["source_speaker"], "Speaker3")
        self.assertEqual(verified[0]["corroboration"]["segment_index"], 1)

    def test_other_speaker_corroboration_entry_rejected(self):
        verified, rejected = SpeakerInferencer._verify_candidates(
            [_candidate("江哥", "dialog_address", WRONG_TARGET_QUOTE)],
            quote_sections={"dialog_address": "[Speaker1]（本人片段，不含旁证句）"},
            corroboration_entries=self._entries(),
            dialog_sources={"context_before": "", "own_samples": ""},
        )
        self.assertEqual(verified, [])
        self.assertEqual(rejected, ["quote_missing"])

    def test_corroboration_name_not_in_quote_rejected(self):
        verified, rejected = SpeakerInferencer._verify_candidates(
            [_candidate("林知遥", "dialog_address", CALL_QUOTE_1)],
            quote_sections={"dialog_address": "[Speaker1]（本人片段，不含旁证句）"},
            corroboration_entries=self._entries(),
            dialog_sources={"context_before": "", "own_samples": ""},
        )
        self.assertEqual(verified, [])
        self.assertEqual(rejected, ["name_not_in_quote"])

    def test_own_section_quote_still_takes_priority(self):
        own_quote = "我是江哥"
        verified, rejected = SpeakerInferencer._verify_candidates(
            [_candidate("江哥", "dialog_address", own_quote)],
            quote_sections={"dialog_address": own_quote},
            corroboration_entries=[{"speaker": "Guest", "text": own_quote}],
            dialog_sources={"context_before": "", "own_samples": own_quote},
        )
        self.assertEqual(rejected, [])
        self.assertEqual(len(verified), 1)
        self.assertNotIn("corroboration", verified[0])

    def test_non_dialog_type_has_no_corroboration_channel(self):
        verified, rejected = SpeakerInferencer._verify_candidates(
            [_candidate("江哥", "title_credit", CALL_QUOTE_1)],
            quote_sections={"title_credit": "- 标题：某标题", "dialog_address": ""},
            corroboration_entries=self._entries(),
            dialog_sources={"context_before": "", "own_samples": ""},
        )
        self.assertEqual(verified, [])
        self.assertEqual(rejected, ["quote_missing"])


if __name__ == "__main__":
    unittest.main()
