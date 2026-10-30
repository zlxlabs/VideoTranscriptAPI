"""Independent self-intro review: payload isolation, verdicts, and infer() axes."""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from video_transcript_api.llm.core.speaker_inferencer import SpeakerInferencer
from video_transcript_api.llm.core.speaker_self_intro_verifier import (
    SELF_INTRO_PASS,
    SELF_INTRO_UNCLEAR,
    SPEAKER_SELF_INTRO_TASK_TYPE,
    SpeakerSelfIntroVerifier,
    apply_self_intro_review,
)
from video_transcript_api.llm.prompts.schemas.speaker_self_intro import (
    SELF_INTRO_CLASSIFICATIONS,
    SPEAKER_SELF_INTRO_SCHEMA,
)
from video_transcript_api.llm.prompts.speaker_self_intro import (
    SPEAKER_SELF_INTRO_SYSTEM_PROMPT,
    build_speaker_self_intro_user_prompt,
)

FIXTURE_PATH = (
    Path(__file__).parent.parent
    / "fixtures"
    / "twitter_space_1wxWjlzQVwDJQ"
    / "transcript_funasr.json"
)
CALL_RESPONSE_PATH = (
    Path(__file__).parent.parent / "fixtures" / "call_response_host" / "transcript_funasr.json"
)
FIXTURE_TITLE = "夜航电台之青柠专场：独立开发者的播客实验，三十天拉新一万"
FIXTURE_AUTHOR = "林晚风"
FIXTURE_DESCRIPTION = (
    "Twitter Space participated by 星野, 长风万里, 阿澈, 小满, 铁蛋, "
    "数码君日常, 阿凯, 夜行者, 望舒, 青山不改, 路人甲, 拾光者"
)


def parse_self_intro_items(user_prompt: str):
    start = user_prompt.index("<items>") + len("<items>")
    end = user_prompt.index("</items>")
    return json.loads(user_prompt[start:end].strip())


def speaker_llm_side_effect(
    inference_response,
    classification=SELF_INTRO_PASS,
    by_name=None,
    raise_on_verify=None,
):
    """Dispatch first-round inference vs self-intro review by task_type."""

    def fake(*args, **kwargs):
        if kwargs.get("task_type") != SPEAKER_SELF_INTRO_TASK_TYPE:
            return inference_response
        if raise_on_verify is not None:
            raise raise_on_verify
        items = parse_self_intro_items(kwargs["user_prompt"])
        verdicts = []
        for item in items:
            chosen = classification
            if by_name is not None and item["name"] in by_name:
                chosen = by_name[item["name"]]
            if chosen is None:
                continue
            verdicts.append({"id": item["id"], "classification": chosen})
        response = MagicMock()
        response.structured_output = {"verdicts": verdicts}
        return response

    return fake


def install_speaker_llm(inferencer, inference_response, **kwargs):
    inferencer.llm_client.call = MagicMock(
        side_effect=speaker_llm_side_effect(inference_response, **kwargs)
    )


def assert_inference_and_review(call_mock):
    types = [c.kwargs.get("task_type") for c in call_mock.call_args_list]
    if types.count("speaker_inference") != 1 or types.count("speaker_self_intro") != 1:
        raise AssertionError("unexpected task_types=%r" % (types,))


def _mock_llm(structured_output):
    response = MagicMock()
    response.structured_output = structured_output
    return response


def _candidate(name, evidence_type, quote):
    return {"name": name, "evidence_type": evidence_type, "evidence_quote": quote}


def _make_inferencer(cached_mapping=None):
    cache_manager = MagicMock()
    cache_manager.get_speaker_mapping.return_value = cached_mapping
    return SpeakerInferencer(
        llm_client=MagicMock(),
        cache_manager=cache_manager,
        model="test-model",
        reasoning_effort="low",
    )


def _inference_output(candidates, confidence, mapping=None, reasoning="first-round-reasoning"):
    mapping = mapping or {speaker: speaker for speaker in candidates}
    return _mock_llm(
        {
            "speaker_mapping": mapping,
            "confidence": confidence,
            "candidates": candidates,
            "reasoning": reasoning,
        }
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


def _load_call_response_dialogs():
    data = json.loads(CALL_RESPONSE_PATH.read_text(encoding="utf-8"))
    return [
        {
            "speaker": seg.get("speaker") or "",
            "text": seg.get("text") or "",
            "start_time": seg.get("start_time"),
        }
        for seg in data["segments"]
    ]


class TestSelfIntroPromptIsolation(unittest.TestCase):
    def test_user_prompt_contains_only_id_name_quote(self):
        prompt = build_speaker_self_intro_user_prompt(
            [{"id": "si-0", "name": "Bob", "quote": "Bob here"}]
        )
        self.assertEqual(
            parse_self_intro_items(prompt),
            [{"id": "si-0", "name": "Bob", "quote": "Bob here"}],
        )
        for forbidden in ("reasoning", "confidence", "evidence_type", "speaker_mapping"):
            self.assertNotIn(forbidden, prompt)

    def test_schema_classifications_are_closed(self):
        enum_values = SPEAKER_SELF_INTRO_SCHEMA["properties"]["verdicts"]["items"][
            "properties"
        ]["classification"]["enum"]
        self.assertEqual(tuple(enum_values), SELF_INTRO_CLASSIFICATIONS)


class TestApplySelfIntroReview(unittest.TestCase):
    def test_empty_pending_does_not_call_llm(self):
        llm = MagicMock()
        verified: list = []
        rejected: list = []
        apply_self_intro_review(
            llm,
            "test-model",
            None,
            [("Speaker1", 0.9, [], verified, rejected, [])],
        )
        llm.call.assert_not_called()
        self.assertEqual(verified, [])
        self.assertEqual(rejected, [])


class TestVerifierCallContract(unittest.TestCase):
    def test_verify_sends_schema_task_type_and_isolated_prompt(self):
        llm_client = MagicMock()
        llm_client.call.return_value = _mock_llm(
            {"verdicts": [{"id": "si-0", "classification": SELF_INTRO_PASS}]}
        )
        result = SpeakerSelfIntroVerifier(
            llm_client, model="test-model", reasoning_effort="low"
        ).verify([{"id": "si-0", "name": "Bob", "quote": "Bob here"}])
        self.assertEqual(result, {"si-0": SELF_INTRO_PASS})
        kwargs = llm_client.call.call_args.kwargs
        self.assertEqual(kwargs["task_type"], SPEAKER_SELF_INTRO_TASK_TYPE)
        self.assertEqual(kwargs["response_schema"], SPEAKER_SELF_INTRO_SCHEMA)
        self.assertEqual(kwargs["model"], "test-model")
        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertEqual(kwargs["system_prompt"], SPEAKER_SELF_INTRO_SYSTEM_PROMPT)
        items = parse_self_intro_items(kwargs["user_prompt"])
        self.assertEqual(items[0]["name"], "Bob")
        self.assertEqual(items[0]["quote"], "Bob here")
        self.assertNotIn("reasoning", kwargs["user_prompt"])

    def test_empty_items_do_not_call_llm(self):
        llm_client = MagicMock()
        self.assertEqual(SpeakerSelfIntroVerifier(llm_client, "test-model").verify([]), {})
        llm_client.call.assert_not_called()


class TestInferSelfIntroAxes(unittest.TestCase):
    """Axis table locked from SpeakerInferencer.infer(); only llm_client.call is stubbed."""

    def _run(self, candidates, dialogs, speakers, side_effect, **infer_kwargs):
        inferencer = _make_inferencer()
        inferencer.llm_client.call = MagicMock(side_effect=side_effect)
        result = inferencer.infer(
            speakers=speakers,
            dialogs=dialogs,
            title=infer_kwargs.get("title", "t"),
            author=infer_kwargs.get("author", ""),
            description=infer_kwargs.get("description", ""),
            platform=infer_kwargs.get("platform", "p"),
            media_id=infer_kwargs.get("media_id", "m"),
        )
        return inferencer, result

    def _own_sample_run(self, quote, name, classification=SELF_INTRO_PASS):
        candidates = {"Speaker1": [_candidate(name, "dialog_address", quote)]}
        return self._run(
            candidates,
            [{"speaker": "Speaker1", "text": quote}],
            ["Speaker1"],
            speaker_llm_side_effect(
                _inference_output(candidates, {"Speaker1": 0.9}, {"Speaker1": name}),
                classification=classification,
            ),
        )

    def test_own_sample_address_classifications(self):
        cases = [
            ("Bob here", "Bob", "self_introduction", True),
            ("柠姐在这儿", "柠姐", "self_introduction", True),
            ("John Collison wrote about this", "John Collison", "third_party_mention", False),
            ("欢迎张三发言", "张三", "addressed_by_other", False),
        ]
        for quote, name, classification, is_intro in cases:
            with self.subTest(quote=quote):
                inferencer, result = self._own_sample_run(
                    quote, name, classification=classification
                )
                expected = name if is_intro else "说话人1"
                self.assertEqual(result["mapping"]["Speaker1"], expected)
                self.assertEqual(result["meta"]["Speaker1"]["applied"], is_intro)
                self.assertEqual(
                    result["meta"]["Speaker1"]["candidates"][0]["self_intro_verdict"],
                    classification,
                )
                if not is_intro:
                    self.assertEqual(
                        result["meta"]["Speaker1"]["rejection_reason"],
                        "name_not_self_intro",
                    )
                if is_intro:
                    self.assertEqual(inferencer.llm_client.call.call_count, 2)

    def test_missing_or_illegal_classification_is_unclear(self):
        _, missing = self._own_sample_run("Bob here", "Bob", classification=None)
        self.assertEqual(missing["meta"]["Speaker1"]["rejection_reason"], "name_not_self_intro")
        self.assertEqual(
            missing["meta"]["Speaker1"]["candidates"][0]["self_intro_verdict"],
            SELF_INTRO_UNCLEAR,
        )
        _, illegal = self._own_sample_run("Bob here", "Bob", classification="not_a_class")
        self.assertEqual(illegal["mapping"]["Speaker1"], "说话人1")
        self.assertEqual(
            illegal["meta"]["Speaker1"]["candidates"][0]["self_intro_verdict"],
            SELF_INTRO_UNCLEAR,
        )

    def test_context_before_quote_skips_review(self):
        dialogs = [
            {"speaker": "Speaker1", "text": "欢迎柠姐", "start_time": 0.0},
            {"speaker": "Speaker2", "text": "大家好", "start_time": 1.0},
        ]
        candidates = {"Speaker2": [_candidate("柠姐", "dialog_address", "欢迎柠姐")]}
        inferencer, result = self._run(
            candidates,
            dialogs,
            ["Speaker1", "Speaker2"],
            speaker_llm_side_effect(
                _inference_output(
                    candidates,
                    {"Speaker1": 0.9, "Speaker2": 0.9},
                    {"Speaker1": "Speaker1", "Speaker2": "柠姐"},
                )
            ),
        )
        self.assertEqual(result["mapping"]["Speaker2"], "柠姐")
        self.assertEqual(inferencer.llm_client.call.call_count, 1)
        self.assertEqual(
            inferencer.llm_client.call.call_args.kwargs["task_type"], "speaker_inference"
        )

    def test_corroboration_quote_skips_review(self):
        quote = "江哥，开场白你先来，我们洗耳恭听。"
        candidates = {
            "Speaker1": [_candidate("江哥", "dialog_address", quote)],
            "Speaker2": [],
            "Speaker3": [],
            "Speaker4": [],
        }
        inferencer, result = self._run(
            candidates,
            _load_call_response_dialogs(),
            ["Speaker1", "Speaker2", "Speaker3", "Speaker4"],
            speaker_llm_side_effect(
                _inference_output(candidates, {s: 0.9 for s in candidates}, {"Speaker1": "江哥"})
            ),
            title="远航夜话之江远专场：小团队出海避坑实录",
            author="江远",
            description="直播 participated by 沈舟, 阿北, 老白, 阿慧, 木木, 阿岛",
        )
        self.assertEqual(result["mapping"]["Speaker1"], "江哥")
        self.assertEqual(inferencer.llm_client.call.call_count, 1)
        verified = result["meta"]["Speaker1"]["verified_candidates"]
        self.assertTrue(any("corroboration" in entry for entry in verified))

    def test_no_own_sample_candidate_calls_llm_once(self):
        candidates = {"Speaker1": [_candidate("林晚风", "author_host", FIXTURE_AUTHOR)]}
        inferencer, result = self._run(
            candidates,
            _load_fixture_dialogs(),
            ["Speaker1", "Speaker2"],
            speaker_llm_side_effect(
                _inference_output(
                    candidates,
                    {"Speaker1": 0.9, "Speaker2": 0.9},
                    {"Speaker1": "林晚风", "Speaker2": "Speaker2"},
                )
            ),
            title=FIXTURE_TITLE,
            author=FIXTURE_AUTHOR,
            description=FIXTURE_DESCRIPTION,
        )
        self.assertEqual(inferencer.llm_client.call.call_count, 1)
        self.assertEqual(
            result["meta"]["Speaker1"]["rejection_reason"],
            "title_credit_without_dialog_support",
        )

    def test_two_own_sample_candidates_share_one_review_call(self):
        dialogs = [
            {"speaker": "Speaker1", "text": "Bob here", "start_time": 0.0},
            {"speaker": "Speaker2", "text": "柠姐在这儿", "start_time": 1.0},
        ]
        candidates = {
            "Speaker1": [_candidate("Bob", "dialog_address", "Bob here")],
            "Speaker2": [_candidate("柠姐", "dialog_address", "柠姐在这儿")],
        }
        inferencer, result = self._run(
            candidates,
            dialogs,
            ["Speaker1", "Speaker2"],
            speaker_llm_side_effect(
                _inference_output(
                    candidates,
                    {"Speaker1": 0.9, "Speaker2": 0.9},
                    {"Speaker1": "Bob", "Speaker2": "柠姐"},
                )
            ),
        )
        self.assertEqual(result["mapping"], {"Speaker1": "Bob", "Speaker2": "柠姐"})
        self.assertEqual(inferencer.llm_client.call.call_count, 2)
        review = inferencer.llm_client.call.call_args_list[1]
        self.assertEqual(review.kwargs["task_type"], SPEAKER_SELF_INTRO_TASK_TYPE)
        items = parse_self_intro_items(review.kwargs["user_prompt"])
        self.assertEqual({item["name"] for item in items}, {"Bob", "柠姐"})

    def test_review_exception_uses_identity_fallback(self):
        _, result = self._run(
            {"Speaker1": [_candidate("Bob", "dialog_address", "Bob here")]},
            [{"speaker": "Speaker1", "text": "Bob here"}],
            ["Speaker1"],
            speaker_llm_side_effect(
                _inference_output(
                    {"Speaker1": [_candidate("Bob", "dialog_address", "Bob here")]},
                    {"Speaker1": 0.9},
                    {"Speaker1": "Bob"},
                ),
                raise_on_verify=RuntimeError("review boom"),
            ),
        )
        self.assertEqual(result["source"], "identity_fallback")
        self.assertEqual(result["mapping"]["Speaker1"], "Speaker1")

    def test_cache_hit_skips_review(self):
        cached = {
            "mapping": {"Speaker1": "Bob"},
            "meta": {
                "Speaker1": {
                    "name": "Bob",
                    "confidence": 0.9,
                    "applied": True,
                    "sampled": True,
                    "candidates": [
                        {
                            "name": "Bob",
                            "evidence_type": "dialog_address",
                            "evidence_quote": "Bob here",
                            "self_intro_verdict": SELF_INTRO_PASS,
                        }
                    ],
                    "verified_candidates": [
                        {
                            "name": "Bob",
                            "evidence_type": "dialog_address",
                            "evidence_quote": "Bob here",
                            "self_intro_verdict": SELF_INTRO_PASS,
                        }
                    ],
                }
            },
            "low_confidence": [],
        }
        inferencer = _make_inferencer(cached_mapping=cached)
        inferencer.llm_client.call = MagicMock(side_effect=AssertionError("must not call LLM"))
        result = inferencer.infer(
            speakers=["Speaker1"],
            dialogs=[{"speaker": "Speaker1", "text": "Bob here"}],
            title="t",
            platform="p",
            media_id="m",
        )
        inferencer.llm_client.call.assert_not_called()
        self.assertEqual(result["source"], "cache_hit")
        self.assertEqual(result["mapping"]["Speaker1"], "Bob")

    def test_e2e_review_prompt_has_quote_not_first_round_reasoning(self):
        quote = "大家好，我是柠姐，麦能听到了吗？"
        candidates = {"Speaker2": [_candidate("柠姐", "dialog_address", quote)]}
        inferencer, result = self._run(
            candidates,
            _load_fixture_dialogs(),
            ["Speaker1", "Speaker2"],
            speaker_llm_side_effect(
                _inference_output(
                    candidates,
                    {"Speaker1": 0.9, "Speaker2": 0.9},
                    {"Speaker1": "Speaker1", "Speaker2": "柠姐"},
                    reasoning="first-round-reasoning-secret",
                )
            ),
            title=FIXTURE_TITLE,
            author=FIXTURE_AUTHOR,
            description=FIXTURE_DESCRIPTION,
        )
        self.assertEqual(result["mapping"]["Speaker2"], "柠姐")
        first, review = inferencer.llm_client.call.call_args_list
        self.assertEqual(first.kwargs["task_type"], "speaker_inference")
        self.assertEqual(review.kwargs["task_type"], SPEAKER_SELF_INTRO_TASK_TYPE)
        self.assertEqual(review.kwargs["response_schema"], SPEAKER_SELF_INTRO_SCHEMA)
        user_prompt = review.kwargs["user_prompt"]
        items = parse_self_intro_items(user_prompt)
        self.assertEqual(items[0]["name"], "柠姐")
        self.assertEqual(items[0]["quote"], quote)
        self.assertNotIn("first-round-reasoning-secret", user_prompt)
        self.assertNotIn("reasoning", user_prompt)
        self.assertNotIn(FIXTURE_TITLE, user_prompt)
        self.assertNotIn(FIXTURE_AUTHOR, user_prompt)
