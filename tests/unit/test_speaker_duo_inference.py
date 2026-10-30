"""两人访谈「元数据 + 角色」推测命名通路回归测试。

轴表来自卡面「不变式轴表」，真实夹具来自 twitter 任务 2115809881399922849
（见 tests/fixtures/twitter_collison_2115809881399922849/README.md）。
本文件是本卡新增测试的唯一落点，超限老文件只做机械改动。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from video_transcript_api.llm.core.speaker_duo_inference import (
    DUO_EVIDENCE_TIER,
    select_primary_speakers,
)
from video_transcript_api.llm.core.speaker_inferencer import SpeakerInferencer
from video_transcript_api.llm.prompts.schemas.speaker_mapping import (
    SPEAKER_MAPPING_SCHEMA,
    SPEAKER_ROLE_TYPES,
)

FIXTURE_DIR = (
    Path(__file__).parent.parent
    / "fixtures"
    / "twitter_collison_2115809881399922849"
)

# 轴表第一行的期望显示名（真实夹具）。
EXPECTED_HOST_NAME = "说话人1｜作者关联推测:John Collison"
EXPECTED_GUEST_NAME = "说话人2｜标题关联推测:sama"


def _load_fixture():
    """真实 FunASR 转录 + 任务元数据（标题/作者/描述）。"""
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
    """只打桩 llm_client.call（首轮说话人推断是唯一被调用的任务）。"""
    response = MagicMock()
    response.structured_output = structured_output
    inferencer.llm_client.call = MagicMock(return_value=response)


def _structured_output(candidates, roles=None, mapping=None, confidence=None):
    """构造符合新 SPEAKER_MAPPING_SCHEMA 必填字段形状的首轮输出。"""
    payload = {
        "speaker_mapping": mapping or {speaker: speaker for speaker in candidates},
        "confidence": confidence or {speaker: 0.3 for speaker in candidates},
        "candidates": candidates,
        "roles": {} if roles is None else roles,
        "reasoning": "mock",
    }
    assert set(payload) == set(SPEAKER_MAPPING_SCHEMA["required"])
    return payload


def _candidate(name, evidence_type, quote):
    return {"name": name, "evidence_type": evidence_type, "evidence_quote": quote}


def _dialogs(counts):
    """按每人条数造对话。"""
    dialogs = []
    for speaker, count in counts.items():
        for index in range(count):
            dialogs.append(
                {
                    "speaker": speaker,
                    "text": f"{speaker} says line {index}",
                    "start_time": float(index),
                }
            )
    return dialogs


def _addressed_dialogs(address_line, count=10):
    """两人对话；Speaker1 首次出场前的最后一句是别人对它的称呼。

    称呼句紧贴 Speaker1 的首次出场（context_before 只取出场前最近 2 条），
    这样 dialog_address 证据能逐字核对通过，无需触发自我介绍复核调用。
    """
    dialogs = [
        {
            "speaker": "Speaker2",
            "text": f"Speaker2 says line {index}",
            "start_time": float(index),
        }
        for index in range(count)
    ]
    dialogs[-1]["text"] = address_line
    dialogs += [
        {
            "speaker": "Speaker1",
            "text": f"Speaker1 says line {index}",
            "start_time": float(count + index),
        }
        for index in range(count)
    ]
    return dialogs


class TestDuoPrimarySelection(unittest.TestCase):
    """主要说话人判定：按 base dialogs 的发言条数算占比。"""

    def test_primary_selection_cases(self):
        cases = [
            # (说明, 每人条数, 期望的主要说话人)
            ("两人各半", {"Speaker1": 10, "Speaker2": 10}, ["Speaker1", "Speaker2"]),
            ("三人分摊", {"S1": 10, "S2": 10, "S3": 10}, []),
            ("两个主要 + 3% 小簇", {"S1": 48, "S2": 47, "S3": 3}, ["S1", "S2"]),
            ("两人合计不足 80%", {"S1": 20, "S2": 20, "S3": 60}, []),
        ]
        for label, counts, expected in cases:
            with self.subTest(label):
                speakers = list(counts)
                self.assertEqual(
                    select_primary_speakers(_dialogs(counts), speakers), expected
                )


class TestDuoAxes(unittest.TestCase):
    """轴表各格：结构前提由程序核对，LLM 只给角色与候选。"""

    def _run(self, dialogs, title, author, candidates, roles, speakers=None):
        speakers = speakers or ["Speaker1", "Speaker2"]
        inferencer = _make_inferencer()
        _install_llm(inferencer, _structured_output(candidates, roles=roles))
        result = inferencer.infer(
            speakers=speakers, dialogs=dialogs, title=title, author=author
        )
        return inferencer, result

    def _collison_candidates(self):
        return {
            "Speaker1": [_candidate("John Collison", "author_host", "John Collison")],
            "Speaker2": [_candidate("sama", "title_credit", "@sama")],
        }

    def test_closed_pathway_cases(self):
        """通路必须关闭的各格：两位说话人都保持占位符与原拒绝原因。"""
        dialogs, metadata = _load_fixture()
        collison = self._collison_candidates()
        cases = [
            (
                "角色 unknown",
                dialogs,
                metadata["title"],
                metadata["author"],
                collison,
                {"Speaker1": "host", "Speaker2": "unknown"},
            ),
            (
                "角色重复",
                dialogs,
                metadata["title"],
                metadata["author"],
                collison,
                {"Speaker1": "host", "Speaker2": "host"},
            ),
            (
                "标题两个 @ 提及",
                dialogs,
                "OpenAI’s @sama and @karpathy joined me at the pub",
                metadata["author"],
                collison,
                {"Speaker1": "host", "Speaker2": "guest"},
            ),
            (
                "两人静态候选同名",
                _dialogs({"Speaker1": 10, "Speaker2": 10}),
                "Alex and alex talk",
                "Alex",
                {
                    "Speaker1": [_candidate("Alex", "author_host", "Alex")],
                    "Speaker2": [_candidate("alex", "title_credit", "Alex")],
                },
                {"Speaker1": "host", "Speaker2": "guest"},
            ),
            (
                "一人挂两个不同静态名",
                _dialogs({"Speaker1": 10, "Speaker2": 10}),
                "Johnny and sama joined me at the pub",
                "John",
                {
                    "Speaker1": [
                        _candidate("John", "author_host", "John"),
                        _candidate("Johnny", "title_credit", "Johnny"),
                    ],
                    "Speaker2": [_candidate("sama", "title_credit", "sama")],
                },
                {"Speaker1": "host", "Speaker2": "guest"},
            ),
        ]
        for label, case_dialogs, title, author, candidates, roles in cases:
            with self.subTest(label):
                _, result = self._run(
                    case_dialogs, title, author, candidates, roles
                )
                self.assertEqual(result["mapping"]["Speaker1"], "说话人1")
                self.assertEqual(result["mapping"]["Speaker2"], "说话人2")
                for speaker in ("Speaker1", "Speaker2"):
                    self.assertFalse(result["meta"][speaker]["applied"])
                    self.assertNotIn(DUO_EVIDENCE_TIER, result["meta"][speaker])

    def test_missing_roles_field_closes_pathway(self):
        """轴表第九行：LLM 没给 roles 字段时通路关闭，不报错。"""
        dialogs, metadata = _load_fixture()
        inferencer = _make_inferencer()
        payload = _structured_output(self._collison_candidates())
        del payload["roles"]
        _install_llm(inferencer, payload)
        result = inferencer.infer(
            speakers=["Speaker1", "Speaker2"],
            dialogs=dialogs,
            title=metadata["title"],
            author=metadata["author"],
        )
        self.assertEqual(result["mapping"]["Speaker1"], "说话人1")
        self.assertEqual(result["mapping"]["Speaker2"], "说话人2")
        self.assertNotIn(DUO_EVIDENCE_TIER, result["meta"]["Speaker1"])

    def test_small_cluster_keeps_placeholder(self):
        """轴表第五行：小簇保持占位符，两个主要说话人照样采用。"""
        dialogs = _dialogs({"Speaker1": 48, "Speaker2": 47, "Speaker3": 3})
        candidates = {
            "Speaker1": [_candidate("Ann", "author_host", "Ann")],
            "Speaker2": [_candidate("Bob", "title_credit", "Bob")],
            "Speaker3": [],
        }
        _, result = self._run(
            dialogs,
            "Ann and Bob talk",
            "Ann",
            candidates,
            {"Speaker1": "host", "Speaker2": "guest"},
            speakers=["Speaker1", "Speaker2", "Speaker3"],
        )
        self.assertEqual(result["mapping"]["Speaker1"], "说话人1｜作者关联推测:Ann")
        self.assertEqual(result["mapping"]["Speaker2"], "说话人2｜标题关联推测:Bob")
        self.assertEqual(result["mapping"]["Speaker3"], "说话人3")
        self.assertFalse(result["meta"]["Speaker3"]["applied"])

    def test_adopted_dialog_name_keeps_other_speaker_unnamed(self):
        """轴表第八行：一人已被对话称呼采用，另一人不得采用标题名。

        关闭的机制是「两位主要说话人各至少 1 条核对通过的静态候选」：
        Speaker1 的名字来自对话称呼（dialog_address），它没有静态候选，
        整条通路随之关闭。

        口径说明：卡面锁定决策里的撞名判据是 casefold **完全相等**
        （「Sam」与「sama」casefold 后并不相等），本卡按该口径实现。
        """
        dialogs = _addressed_dialogs("Sam welcome to the show")
        candidates = {
            "Speaker1": [
                _candidate("Sam", "dialog_address", "Sam welcome to the show")
            ],
            "Speaker2": [_candidate("sama", "title_credit", "sama")],
        }
        _, result = self._run(
            dialogs,
            "sama joined me at the pub",
            "John",
            candidates,
            {"Speaker1": "host", "Speaker2": "guest"},
        )
        # 现行规则已采用的名字保持不变（dialog_address 单证据）。
        self.assertEqual(result["mapping"]["Speaker1"], "Sam")
        self.assertEqual(result["mapping"]["Speaker2"], "说话人2")
        self.assertFalse(result["meta"]["Speaker2"]["applied"])

    def test_identical_adopted_name_conflict_closes_pathway(self):
        """已采用的名字与本通路定出的名字 casefold 相等时关闭通路。"""
        dialogs = _addressed_dialogs("Sama welcome to the show")
        candidates = {
            "Speaker1": [
                _candidate("Sama", "dialog_address", "Sama welcome to the show"),
                _candidate("John", "author_host", "John"),
            ],
            "Speaker2": [_candidate("sama", "title_credit", "sama")],
        }
        _, result = self._run(
            dialogs,
            "sama joined me at the pub",
            "John",
            candidates,
            {"Speaker1": "host", "Speaker2": "guest"},
        )
        self.assertTrue(result["meta"]["Speaker1"]["applied"])
        self.assertEqual(result["mapping"]["Speaker2"], "说话人2")
        self.assertFalse(result["meta"]["Speaker2"]["applied"])

    def test_role_only_applies_to_rejected_speaker(self):
        """现行规则已定名的人保持现行结果，本通路不插手。"""
        dialogs = _addressed_dialogs("Ann welcome to the show")
        candidates = {
            "Speaker1": [
                _candidate("Ann", "dialog_address", "Ann welcome to the show"),
                _candidate("John", "author_host", "John"),
            ],
            "Speaker2": [_candidate("Bob", "title_credit", "Bob")],
        }
        _, result = self._run(
            dialogs,
            "Bob joined me at the pub",
            "John",
            candidates,
            {"Speaker1": "host", "Speaker2": "guest"},
        )
        self.assertEqual(
            result["mapping"]["Speaker1"], "说话人1｜作者关联推测:John｜对话称:Ann"
        )
        self.assertEqual(result["mapping"]["Speaker2"], "说话人2｜标题关联推测:Bob")
        self.assertTrue(result["meta"]["Speaker2"]["applied"])
        self.assertEqual(result["meta"]["Speaker2"]["evidence_tier"], DUO_EVIDENCE_TIER)
        self.assertEqual(result["meta"]["Speaker2"]["role"], "guest")
        # 已被现行规则采用的人不带 duo tier。
        self.assertNotIn("evidence_tier", result["meta"]["Speaker1"])


class TestCollisonFixtureEndToEnd(unittest.TestCase):
    """真实夹具 + 只打桩 llm_client.call：从 infer() 入口到渲染产物。"""

    def setUp(self):
        self.dialogs, self.metadata = _load_fixture()
        self.candidates = {
            "Speaker1": [_candidate("John Collison", "author_host", "John Collison")],
            "Speaker2": [_candidate("sama", "title_credit", "@sama")],
        }
        self.inferencer = _make_inferencer()
        _install_llm(
            self.inferencer,
            _structured_output(
                self.candidates,
                roles={"Speaker1": "host", "Speaker2": "guest"},
                mapping={"Speaker1": "John Collison", "Speaker2": "sama"},
            ),
        )
        self.result = self.inferencer.infer(
            speakers=["Speaker1", "Speaker2"],
            dialogs=self.dialogs,
            title=self.metadata["title"],
            author=self.metadata["author"],
            description=self.metadata["description"],
        )

    def test_mapping_equals_axis_row(self):
        """轴表第一行：两位主要说话人都拿到带推测标注的名字。"""
        self.assertEqual(
            self.result["mapping"],
            {"Speaker1": EXPECTED_HOST_NAME, "Speaker2": EXPECTED_GUEST_NAME},
        )
        self.assertEqual(self.result["low_confidence"], [])

    def test_meta_records_tier_and_role(self):
        for speaker, role in (("Speaker1", "host"), ("Speaker2", "guest")):
            entry = self.result["meta"][speaker]
            self.assertTrue(entry["applied"])
            self.assertEqual(entry["evidence_tier"], DUO_EVIDENCE_TIER)
            self.assertEqual(entry["role"], role)
            self.assertNotIn("rejection_reason", entry)
            self.assertTrue(entry["verified_candidates"])

    def test_only_one_llm_call_and_schema_shape(self):
        """LLM 输出是跨进程边界：mock 形状必须符合新 schema。"""
        self.assertEqual(self.inferencer.llm_client.call.call_count, 1)
        call = self.inferencer.llm_client.call.call_args
        self.assertEqual(call.kwargs["task_type"], "speaker_inference")
        self.assertEqual(call.kwargs["response_schema"], SPEAKER_MAPPING_SCHEMA)
        schema = call.kwargs["response_schema"]
        self.assertIn("roles", schema["required"])
        self.assertEqual(
            schema["properties"]["roles"]["additionalProperties"]["enum"],
            list(SPEAKER_ROLE_TYPES),
        )

    def test_roles_instruction_reaches_the_llm(self):
        """角色说明必须真的进入本轮 system prompt（roles 与 candidates 同属一次调用）。"""
        system_prompt = self.inferencer.llm_client.call.call_args.kwargs["system_prompt"]
        for fragment in ("roles", "host", "guest", "unknown", "提问", "作者"):
            self.assertIn(fragment, system_prompt)

    def test_rendered_html_has_no_pending_badge(self):
        """渲染后两位说话人都不带「待确认」徽章。"""
        from video_transcript_api.utils.rendering.dialog_renderer import DialogRenderer

        payload = {
            "dialogs": [
                {
                    "segment_id": f"seg-{index}",
                    "speaker": self.result["mapping"][dialog["speaker"]],
                    "speaker_id": dialog["speaker"],
                    "text": dialog["text"],
                    "start_time": "00:00:01",
                }
                for index, dialog in enumerate(self.dialogs[:6])
            ],
            "speaker_mapping": self.result["mapping"],
            "speaker_inference_meta": self.result["meta"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "render"
            cache_dir.mkdir()
            (cache_dir / "llm_processed.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            html_out = DialogRenderer()._render_from_structured_data(str(cache_dir))

        self.assertIn("作者关联推测:John Collison", html_out)
        self.assertIn("标题关联推测:sama", html_out)
        self.assertNotIn('data-speaker-status="pending"', html_out)


class TestDuoSchemaContract(unittest.TestCase):
    """roles 字段的 schema 契约。"""

    def test_roles_is_required(self):
        self.assertIn("roles", SPEAKER_MAPPING_SCHEMA["required"])
        self.assertEqual(
            SPEAKER_MAPPING_SCHEMA["properties"]["roles"]["type"], "object"
        )

    def test_role_domain_is_closed(self):
        enum_values = SPEAKER_MAPPING_SCHEMA["properties"]["roles"][
            "additionalProperties"
        ]["enum"]
        self.assertEqual(tuple(enum_values), SPEAKER_ROLE_TYPES)
        self.assertEqual(set(SPEAKER_ROLE_TYPES), {"host", "guest", "unknown"})
