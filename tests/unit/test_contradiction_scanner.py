"""Increment 2 semantic contradiction scanner tests."""

import logging
import re
from contextlib import nullcontext
from unittest.mock import MagicMock, patch
import json
from pathlib import Path

import pytest

from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.core.contradiction_scanner import (
    ContradictionScanner,
    SCAN_WINDOW_OVERLAP,
    SCAN_WINDOW_SEGMENTS,
)
from video_transcript_api.llm.processors.speaker_aware_processor import SpeakerAwareProcessor
from video_transcript_api.llm.core.key_info_extractor import KeyInfo
from video_transcript_api.llm.prompts.schemas.contradiction_scan import (
    CONTRADICTION_REASONS,
    CONTRADICTION_SCAN_SCHEMA,
)
from video_transcript_api.api.services import llm_ops


def _config(**overrides):
    values = dict(
        api_key="k",
        base_url="http://test",
        calibrate_model="calibrate-model",
        summary_model="summary-model",
    )
    values.update(overrides)
    return LLMConfig(**values)


def _response(payload):
    response = MagicMock()
    response.structured_output = payload
    return response


def test_config_switch_defaults_on_and_accepts_nested_override():
    base = {
        "llm": {
            "api_key": "k",
            "base_url": "http://test",
            "calibrate_model": "calibrate-model",
            "summary_model": "summary-model",
        }
    }
    assert LLMConfig.from_dict(base).contradiction_scan_enabled is True
    base["llm"]["speaker_inference"] = {"contradiction_scan_enabled": False}
    assert LLMConfig.from_dict(base).contradiction_scan_enabled is False


def _dialogs(count=4):
    return [
        {
            "segment_id": f"seg_{i}",
            "speaker": "大卫" if i % 2 else "小丹尼",
            "speaker_id": f"Speaker{i % 2 + 1}",
            "text": f"text {i}",
        }
        for i in range(count)
    ]


def test_scanner_marks_valid_contradiction_and_passes_compact_input():
    client = MagicMock()
    client.call.return_value = _response(
        {
            "contradictions": [
                {
                    "segment_id": "seg_1",
                    "reason": "direct_address_conflict",
                    "evidence_segment_ids": ["seg_0"],
                }
            ]
        }
    )
    scanner = ContradictionScanner(client, model="scan-model")

    result = scanner.scan_contradictions(
        _dialogs(40),
        {"Speaker1": "小丹尼", "Speaker2": "大卫"},
        {"Speaker1": {"confidence": 0.8}},
        title="Title",
    )

    assert result == {
        "status": "completed",
        "segment_overrides": {
            "seg_1": {
                "status": "suspect",
                "assignment_source": "semantic_evidence",
                "reason": "direct_address_conflict",
                "evidence_segment_ids": ["seg_0"],
            }
        },
        "speaker_risk_flags": [],
    }
    call = client.call.call_args.kwargs
    assert call["model"] == "scan-model"
    assert "seg_0" in call["user_prompt"]
    assert "text 0" in call["user_prompt"]
    assert "speaker_mapping" in call["user_prompt"]


def test_scanner_prompt_caps_each_text_at_first_100_characters():
    client = MagicMock()
    client.call.return_value = _response({"contradictions": []})
    long_text = "a" * 101
    scanner = ContradictionScanner(client, model="scan-model")
    scanner.scan_contradictions(
        [{"segment_id": "seg_1", "speaker": "Alice", "speaker_id": "S1", "text": long_text}],
        {},
        {},
    )
    prompt = client.call.call_args.kwargs["user_prompt"]
    assert '"text": "' + ("a" * 100) + '"' in prompt
    assert '"text": "' + ("a" * 101) + '"' not in prompt


def test_scanner_windows_501_segments_instead_of_skipping():
    client = MagicMock()
    client.call.return_value = _response({"contradictions": []})
    result = ContradictionScanner(client, model="scan-model").scan_contradictions(
        _dialogs(501), {}, {}
    )
    assert client.call.call_count == 2
    assert result["status"] == "completed"


def test_scanner_windows_use_400_segments_and_ten_segment_overlap():
    assert SCAN_WINDOW_SEGMENTS == 400
    assert SCAN_WINDOW_OVERLAP == 10
    client = MagicMock()
    client.call.return_value = _response({"contradictions": []})
    dialogs = _dialogs(900)
    ContradictionScanner(client, model="scan-model").scan_contradictions(dialogs, {}, {})
    assert client.call.call_count == 3
    prompts = [call.kwargs["user_prompt"] for call in client.call.call_args_list]
    window_ids = []
    for prompt in prompts:
        dialogs_text = prompt.split("<dialogs>\n", 1)[1].split(
            "\n</dialogs>", 1
        )[0]
        window_ids.append(re.findall(r'"segment_id": "([^"]+)"', dialogs_text))
    assert window_ids == [
        [f"seg_{i}" for i in range(400)],
        [f"seg_{i}" for i in range(390, 790)],
        [f"seg_{i}" for i in range(780, 900)],
    ]


def test_scanner_requires_target_in_window_but_allows_evidence_from_other_window():
    client = MagicMock()
    client.call.side_effect = [
        _response(
            {
                "contradictions": [
                    {
                        "segment_id": "seg_1",
                        "reason": "direct_address_conflict",
                        "evidence_segment_ids": ["seg_400"],
                    }
                ]
            }
        ),
        _response(
            {
                "contradictions": [
                    {
                        "segment_id": "seg_400",
                        "reason": "self_reference_conflict",
                        "evidence_segment_ids": ["seg_1"],
                    },
                    {
                        "segment_id": "seg_1",
                        "reason": "third_person_conflict",
                        "evidence_segment_ids": ["seg_400"],
                    },
                ]
            }
        ),
    ]
    # 401 dialogs produce two windows. The second response's seg_1 is outside
    # its target window and must be discarded; cross-window evidence is valid.
    result = ContradictionScanner(client).scan_contradictions(_dialogs(401), {}, {})
    assert set(result["segment_overrides"]) == {"seg_1", "seg_400"}
    assert result["segment_overrides"]["seg_1"]["reason"] == "direct_address_conflict"


def test_scanner_merges_overlapping_targets_first_seen_wins():
    client = MagicMock()
    client.call.side_effect = [
        _response(
            {
                "contradictions": [
                    {
                        "segment_id": "seg_395",
                        "reason": "direct_address_conflict",
                        "evidence_segment_ids": ["seg_1"],
                    }
                ]
            }
        ),
        _response(
            {
                "contradictions": [
                    {
                        "segment_id": "seg_395",
                        "reason": "self_reference_conflict",
                        "evidence_segment_ids": ["seg_2"],
                    }
                ]
            }
        ),
    ]
    result = ContradictionScanner(client).scan_contradictions(_dialogs(401), {}, {})
    assert result["segment_overrides"]["seg_395"]["reason"] == "direct_address_conflict"


def test_scanner_middle_window_failure_rolls_back_all_overrides(caplog):
    client = MagicMock()
    client.call.side_effect = [
        _response(
            {
                "contradictions": [
                    {
                        "segment_id": "seg_1",
                        "reason": "direct_address_conflict",
                        "evidence_segment_ids": ["seg_1"],
                    }
                ]
            }
        ),
        TimeoutError("window timeout"),
        _response({"contradictions": []}),
    ]
    with patch(
        "video_transcript_api.llm.core.contradiction_scanner.logger",
        logging.getLogger("contradiction_scanner_window_failure_test"),
    ), caplog.at_level(logging.WARNING):
        result = ContradictionScanner(client).scan_contradictions(_dialogs(900), {}, {})
    assert result == {
        "status": "failed",
        "segment_overrides": {},
        "speaker_risk_flags": [],
    }
    assert "Contradiction scan failed for window 2" in caplog.text


def test_scanner_structured_parse_failure_rolls_back_with_window_number(caplog):
    client = MagicMock()
    client.call.side_effect = [
        _response({"contradictions": []}),
        _response({"unexpected": []}),
    ]
    with patch(
        "video_transcript_api.llm.core.contradiction_scanner.logger",
        logging.getLogger("contradiction_scanner_parse_failure_test"),
    ), caplog.at_level(logging.WARNING):
        result = ContradictionScanner(client).scan_contradictions(_dialogs(401), {}, {})
    assert result["status"] == "failed"
    assert result["segment_overrides"] == {}
    assert result["speaker_risk_flags"] == []
    assert "Contradiction scan failed for window 2" in caplog.text


@pytest.mark.parametrize(
    ("extra_field", "extra_value"), [("name", "Alice"), ("reasoning", "free text")]
)
def test_scanner_discards_contradiction_with_hallucinated_extra_field(
    extra_field, extra_value
):
    client = MagicMock()
    entry = {
        "segment_id": "seg_1",
        "reason": "self_reference_conflict",
        "evidence_segment_ids": ["seg_0"],
        extra_field: extra_value,
    }
    client.call.return_value = _response({"contradictions": [entry]})

    result = ContradictionScanner(client).scan_contradictions(_dialogs(), {}, {})

    assert result["segment_overrides"] == {}


def test_scanner_logs_hallucinated_target_id_with_segment_count(caplog):
    client = MagicMock()
    client.call.return_value = _response(
        {
            "contradictions": [
                {
                    "segment_id": "seg_missing",
                    "reason": "self_reference_conflict",
                    "evidence_segment_ids": ["seg_0"],
                }
            ]
        }
    )
    scanner = ContradictionScanner(client)

    with patch(
        "video_transcript_api.llm.core.contradiction_scanner.logger",
        logging.getLogger("contradiction_scanner_test"),
    ), caplog.at_level(logging.WARNING):
        result = scanner.scan_contradictions(_dialogs(), {}, {})

    assert result["segment_overrides"] == {}
    assert "Contradiction scan dropped entry" in caplog.text
    assert "seg_missing" in caplog.text
    assert "target id hallucination" in caplog.text


def test_scanner_warns_when_dialogs_without_segment_id_are_skipped(caplog):
    client = MagicMock()
    client.call.return_value = _response({"contradictions": []})
    scanner = ContradictionScanner(client)
    dialogs = [{"speaker": "Alice", "text": "missing id"}] + _dialogs(2)

    with patch(
        "video_transcript_api.llm.core.contradiction_scanner.logger",
        logging.getLogger("contradiction_scanner_missing_id_test"),
    ), caplog.at_level(logging.WARNING):
        scanner.scan_contradictions(dialogs, {}, {})

    assert "Contradiction scan skipped 1 dialogs without segment_id" in caplog.text


def test_scanner_drops_entry_with_hallucinated_evidence_id():
    client = MagicMock()
    client.call.return_value = _response(
        {
            "contradictions": [
                {
                    "segment_id": "seg_1",
                    "reason": "self_reference_conflict",
                    "evidence_segment_ids": ["seg_0", "seg_missing"],
                }
            ]
        }
    )
    with patch(
        "video_transcript_api.llm.core.contradiction_scanner.logger"
    ) as mock_logger:
        result = ContradictionScanner(client).scan_contradictions(_dialogs(), {}, {})
    mock_logger.warning.assert_any_call(
        "Contradiction scan dropped entry for segment_id 'seg_1': "
        "evidence segment id hallucination"
    )
    assert result["status"] == "completed"
    assert result["segment_overrides"] == {}


def test_scanner_discards_all_overrides_when_more_than_twenty_percent_suspect():
    client = MagicMock()
    client.call.return_value = _response(
        {
            "contradictions": [
                {
                    "segment_id": f"seg_{i}",
                    "reason": "third_person_conflict",
                    "evidence_segment_ids": ["seg_0"],
                }
                for i in range(3)
            ]
        }
    )
    result = ContradictionScanner(client).scan_contradictions(_dialogs(10), {}, {})
    assert result["segment_overrides"] == {}
    assert result["speaker_risk_flags"] == ["semantic_scan_unreliable"]


@pytest.mark.parametrize(
    ("total", "suspect_count", "expected_overrides", "expected_flags"),
    [
        (5, 1, 1, ["semantic_contradiction_detected"]),
        (5, 2, 0, ["semantic_scan_unreliable"]),
    ],
)
def test_scanner_twenty_percent_boundary_is_not_unreliable_until_exceeded(
    total, suspect_count, expected_overrides, expected_flags
):
    client = MagicMock()
    client.call.return_value = _response(
        {
            "contradictions": [
                {
                    "segment_id": f"seg_{i}",
                    "reason": "third_person_conflict",
                    "evidence_segment_ids": ["seg_0"],
                }
                for i in range(suspect_count)
            ]
        }
    )
    result = ContradictionScanner(client).scan_contradictions(_dialogs(total), {}, {})
    assert len(result["segment_overrides"]) == expected_overrides
    assert result["speaker_risk_flags"] == expected_flags


@pytest.mark.parametrize("count", [3, 1])
def test_scanner_flag_boundaries(count):
    client = MagicMock()
    client.call.return_value = _response(
        {
            "contradictions": [
                {
                    "segment_id": f"seg_{i}",
                    "reason": "qa_adjacency_conflict",
                    "evidence_segment_ids": ["seg_0"],
                }
                for i in range(count)
            ]
        }
    )
    result = ContradictionScanner(client).scan_contradictions(_dialogs(100), {}, {})
    expected = ["semantic_contradiction_detected"] if count == 3 else []
    assert result["speaker_risk_flags"] == expected


def test_scanner_failure_is_reported_without_raising():
    client = MagicMock()
    client.call.side_effect = TimeoutError("boom")
    result = ContradictionScanner(client).scan_contradictions(_dialogs(), {}, {})
    assert result == {
        "status": "failed",
        "segment_overrides": {},
        "speaker_risk_flags": [],
    }


@pytest.mark.parametrize(
    ("total", "expected_flags"),
    [(34, []), (33, ["semantic_contradiction_detected"])],
)
def test_scanner_ratio_boundary_detects_one_of_thirty_three_only(total, expected_flags):
    client = MagicMock()
    client.call.return_value = _response(
        {
            "contradictions": [
                {
                    "segment_id": "seg_0",
                    "reason": "qa_adjacency_conflict",
                    "evidence_segment_ids": ["seg_0"],
                }
            ]
        }
    )
    result = ContradictionScanner(client).scan_contradictions(_dialogs(total), {}, {})
    assert result["speaker_risk_flags"] == expected_flags


def test_contradiction_schema_locks_reason_enum_and_unknown_fields():
    item_schema = CONTRADICTION_SCAN_SCHEMA["properties"]["contradictions"]["items"]
    assert tuple(item_schema["properties"]["reason"]["enum"]) == CONTRADICTION_REASONS
    assert ContradictionScanner.REASONS == frozenset(CONTRADICTION_REASONS)
    assert item_schema["additionalProperties"] is False
    assert CONTRADICTION_SCAN_SCHEMA["additionalProperties"] is False


def test_processor_sanitizer_derives_reasons_from_scanner(monkeypatch):
    monkeypatch.setattr(ContradictionScanner, "REASONS", frozenset({"dynamic_reason"}))
    override = {
        "status": "suspect",
        "assignment_source": "semantic_evidence",
        "reason": "dynamic_reason",
        "evidence_segment_ids": ["seg_0"],
    }

    sanitized = SpeakerAwareProcessor._sanitize_contradiction_overrides(
        {"seg_0": override}, {"seg_0"}
    )

    assert sanitized == {"seg_0": override}


@pytest.mark.parametrize(
    ("extra_field", "extra_value"), [("name", "Alice"), ("reasoning", "free text")]
)
def test_processor_sanitizer_discards_contradiction_with_hallucinated_extra_field(
    extra_field, extra_value, caplog
):
    override = {
        "status": "suspect",
        "assignment_source": "semantic_evidence",
        "reason": "self_reference_conflict",
        "evidence_segment_ids": ["seg_0"],
        extra_field: extra_value,
    }

    with patch(
        "video_transcript_api.llm.processors.speaker_aware_processor.logger",
        logging.getLogger("speaker_aware_processor_field_set_test"),
    ), caplog.at_level(logging.WARNING):
        sanitized = SpeakerAwareProcessor._sanitize_contradiction_overrides(
            {"seg_0": override}, {"seg_0"}
        )

    assert sanitized == {}
    assert "Contradiction override dropped for segment_id 'seg_0'" in caplog.text
    assert "field set mismatch" in caplog.text


def test_processor_sanitizer_discards_contradiction_with_hallucinated_evidence_id(
    caplog,
):
    override = {
        "status": "suspect",
        "assignment_source": "semantic_evidence",
        "reason": "self_reference_conflict",
        "evidence_segment_ids": ["seg_missing"],
    }

    with patch(
        "video_transcript_api.llm.processors.speaker_aware_processor.logger",
        logging.getLogger("speaker_aware_processor_evidence_test"),
    ), caplog.at_level(logging.WARNING):
        sanitized = SpeakerAwareProcessor._sanitize_contradiction_overrides(
            {"seg_0": override}, {"seg_0"}
        )

    assert sanitized == {}
    assert "Contradiction override dropped for segment_id 'seg_0'" in caplog.text
    assert "evidence segment id hallucination" in caplog.text


def test_processor_normalizes_scanner_skipped_status_to_failed():
    client = MagicMock()
    key_info = MagicMock()
    key_info.extract.return_value = KeyInfo([], [], [], [], [], [], [])
    inferencer = MagicMock()
    inferencer.infer.return_value = {
        "mapping": {"Speaker1": "Alice"},
        "meta": {},
        "source": "llm",
    }
    scanner = MagicMock()
    scanner.scan_contradictions.return_value = {
        "status": "skipped",
        "segment_overrides": {},
        "speaker_risk_flags": [],
    }
    processor = SpeakerAwareProcessor(
        _config(), client, key_info, inferencer, MagicMock(), contradiction_scanner=scanner
    )

    result = processor.process(
        [{"speaker": "Speaker1", "text": "hello", "start_time": 0, "end_time": 1}],
        title="slice",
        skip_calibration=True,
    )

    assert result["stats"]["contradiction_scan_status"] == "failed"
    assert result["structured_data"]["segment_overrides"] == {}
    assert result["structured_data"]["speaker_risk_flags"] == []


@pytest.mark.parametrize(
    "scanner_result",
    [
        {
            "status": "disabled",
            "segment_overrides": {"seg_00000000_speaker1": {"status": "suspect"}},
            "speaker_risk_flags": ["semantic_contradiction_detected"],
        },
        {"status": "unexpected", "segment_overrides": {}, "speaker_risk_flags": []},
        None,
        ["not-an-object"],
    ],
    ids=["disabled", "unknown", "none", "non-dict"],
)
def test_processor_normalizes_non_completed_scanner_results_to_failed(scanner_result):
    client = MagicMock()
    key_info = MagicMock()
    key_info.extract.return_value = KeyInfo([], [], [], [], [], [], [])
    inferencer = MagicMock()
    inferencer.infer.return_value = {
        "mapping": {"Speaker1": "Alice"},
        "meta": {},
        "source": "llm",
    }
    scanner = MagicMock()
    scanner.scan_contradictions.return_value = scanner_result
    processor = SpeakerAwareProcessor(
        _config(), client, key_info, inferencer, MagicMock(), contradiction_scanner=scanner
    )

    result = processor.process(
        [{"speaker": "Speaker1", "text": "x", "start_time": 0, "end_time": 1}],
        title="t",
        skip_calibration=True,
    )

    scanner.scan_contradictions.assert_called_once()
    assert result["stats"]["contradiction_scan_status"] == "failed"
    assert result["structured_data"]["segment_overrides"] == {}
    assert result["structured_data"]["speaker_risk_flags"] == []


def test_processor_runs_scan_after_segment_dedup_and_keeps_skip_calibration():
    client = MagicMock()
    key_info = MagicMock()
    key_info.extract.return_value = KeyInfo([], [], [], [], [], [], [])
    inferencer = MagicMock()
    inferencer.infer.return_value = {
        "mapping": {"Speaker1": "大卫"},
        "meta": {},
        "source": "llm",
    }
    scanner = MagicMock()
    scanner.scan_contradictions.return_value = {
        "status": "completed",
        "segment_overrides": {
            "seg_00000000_speaker1": {
                "status": "suspect",
                "assignment_source": "semantic_evidence",
                "reason": "direct_address_conflict",
                "evidence_segment_ids": ["seg_00000000_speaker1"],
            }
        },
        "speaker_risk_flags": ["semantic_contradiction_detected"],
    }
    processor = SpeakerAwareProcessor(
        _config(), client, key_info, inferencer, MagicMock(), contradiction_scanner=scanner
    )

    result = processor.process(
        [{"speaker": "Speaker1", "text": "刚才大卫", "start_time": 0, "end_time": 1}],
        title="slice",
        skip_calibration=True,
    )

    scanner.scan_contradictions.assert_called_once()
    assert result["stats"]["contradiction_scan_status"] == "completed"
    assert result["structured_data"]["segment_overrides"]
    assert "semantic_contradiction_detected" in result["structured_data"]["speaker_risk_flags"]


def test_processor_scanner_gate_uses_speaker_and_own_switch_only():
    for has_speaker, infer_names, enabled, expected in [
        (False, True, True, "skipped"),
        (True, False, True, "completed"),
        (True, True, False, "disabled"),
    ]:
        client = MagicMock()
        key_info = MagicMock()
        key_info.extract.return_value = KeyInfo([], [], [], [], [], [], [])
        inferencer = MagicMock()
        inferencer.infer.return_value = {"mapping": {}, "meta": {}, "source": "llm"}
        scanner = MagicMock()
        if has_speaker and not infer_names:
            scanner.scan_contradictions.return_value = {
                "status": "completed",
                "segment_overrides": {
                    "seg_00000000_speaker1": {
                        "status": "suspect",
                        "assignment_source": "semantic_evidence",
                        "reason": "direct_address_conflict",
                        "evidence_segment_ids": ["seg_00000000_speaker1"],
                    }
                },
                "speaker_risk_flags": ["semantic_contradiction_detected"],
            }
        else:
            scanner.scan_contradictions.return_value = {
                "status": "completed",
                "segment_overrides": {},
                "speaker_risk_flags": [],
            }
        processor = SpeakerAwareProcessor(
            _config(contradiction_scan_enabled=enabled),
            client,
            key_info,
            inferencer,
            MagicMock(),
            contradiction_scanner=scanner,
        )
        result = processor.process(
            [{"speaker": "Speaker1", "text": "x", "start_time": 0, "end_time": 1}]
            if has_speaker
            else [{"text": "x", "start_time": 0, "end_time": 1}],
            title="t",
            skip_calibration=True,
            has_speaker=has_speaker,
            infer_speaker_names=infer_names,
        )
        assert result["stats"]["contradiction_scan_status"] == expected
        if expected == "completed":
            scanner.scan_contradictions.assert_called_once()
            assert result["structured_data"]["segment_overrides"] == {
                "seg_00000000_speaker1": {
                    "status": "suspect",
                    "assignment_source": "semantic_evidence",
                    "reason": "direct_address_conflict",
                    "evidence_segment_ids": ["seg_00000000_speaker1"],
                }
            }
            assert result["structured_data"]["speaker_risk_flags"] == [
                "semantic_contradiction_detected"
            ]
        else:
            scanner.scan_contradictions.assert_not_called()


@pytest.mark.parametrize(
    ("config_enabled", "task_enabled", "expected"),
    [(True, False, "disabled"), (False, True, "completed")],
)
def test_processor_task_gate_overrides_config(
    config_enabled, task_enabled, expected
):
    client = MagicMock()
    key_info = MagicMock()
    key_info.extract.return_value = KeyInfo([], [], [], [], [], [], [])
    inferencer = MagicMock()
    inferencer.infer.return_value = {
        "mapping": {"Speaker1": "Alice"},
        "meta": {},
        "source": "llm",
    }
    scanner = MagicMock()
    scanner.scan_contradictions.return_value = {
        "status": "completed",
        "segment_overrides": {},
        "speaker_risk_flags": [],
    }
    processor = SpeakerAwareProcessor(
        _config(contradiction_scan_enabled=config_enabled),
        client,
        key_info,
        inferencer,
        MagicMock(),
        contradiction_scanner=scanner,
    )
    result = processor.process(
        [{"speaker": "Speaker1", "text": "x", "start_time": 0, "end_time": 1}],
        title="t",
        skip_calibration=True,
        contradiction_scan=task_enabled,
    )
    assert result["stats"]["contradiction_scan_status"] == expected
    if expected == "completed":
        scanner.scan_contradictions.assert_called_once()
    else:
        scanner.scan_contradictions.assert_not_called()


def test_llm_ops_passes_explicit_contradiction_gate_to_coordinator(monkeypatch):
    coordinator = MagicMock()
    coordinator.process.return_value = {
        "calibrated_text": "text",
        "summary_text": None,
        "stats": {},
        "models_used": {},
    }
    monkeypatch.setattr(llm_ops, "llm_coordinator", coordinator)
    cache_manager = MagicMock()
    cache_manager.update_task_status.return_value = True
    monkeypatch.setattr(llm_ops, "cache_manager", cache_manager)
    monkeypatch.setattr(llm_ops, "_save_llm_results", MagicMock(return_value={}))
    monkeypatch.setattr(llm_ops, "_render_completion_body", MagicMock())
    monkeypatch.setattr(
        llm_ops,
        "get_notification_router",
        lambda: MagicMock(send_text=MagicMock(return_value=True)),
    )
    queue = MagicMock()
    monkeypatch.setattr(llm_ops, "llm_task_queue", queue)
    tracker = MagicMock()
    tracker.track.return_value = nullcontext()

    llm_ops._handle_llm_task(
        {
            "task_id": "task-contradiction-gate",
            "url": "https://example.test/video",
            "video_title": "Title",
            "transcript": "hello",
            "use_speaker_recognition": True,
            "processing_options": {
                "calibrate": False,
                "summarize": False,
                "infer_speaker_names": False,
                "contradiction_scan": True,
            },
            "perf_tracker": tracker,
        }
    )

    assert coordinator.process.call_args.kwargs["infer_speaker_names"] is False
    assert coordinator.process.call_args.kwargs["contradiction_scan"] is True


def test_cached_layer_handoff_preserves_explicit_contradiction_scan(monkeypatch):
    """A cache backfill queue payload keeps the request's explicit gate value."""
    import video_transcript_api.api.services.transcription as transcription
    from video_transcript_api.utils.llm_status import CalibrationStatus, ChaptersStatus
    from video_transcript_api.utils.url_parser import ParsedURL

    class Queue:
        def __init__(self):
            self.items = []

        def put(self, item):
            self.items.append(item)

    class TempManager:
        def mark_active(self, task_id):
            pass

        def set_current_task(self, task_id):
            pass

        def create_task_dir(self, task_id):
            pass

        def maybe_sweep(self):
            pass

        def clean_up_task(self, task_id):
            pass

        def clear_current_task(self):
            pass

        def mark_done(self, task_id):
            pass

    class Router:
        def notify_task_status(self, *args, **kwargs):
            pass

        def send_text(self, *args, **kwargs):
            pass

        def send_long_text(self, *args, **kwargs):
            pass

    class CacheManager:
        def __init__(self):
            self.status_updates = []

        def get_cache(self, **kwargs):
            return {
                "platform": "youtube",
                "media_id": "cached-id",
                "title": "cached title",
                "author": "cached author",
                "description": "cached description",
                "transcript_type": "capswriter",
                "transcript_data": "raw transcript",
                "use_speaker_recognition": False,
                "llm_calibrated": "calibrated transcript",
                "llm_status": {
                    "calibration_status": CalibrationStatus.FULL,
                    "chapters_status": ChaptersStatus.SKIPPED_NO_TIMELINE,
                },
            }

        def update_task_status(self, task_id, status, **kwargs):
            self.status_updates.append((task_id, status, kwargs))
            return True

        def get_task_by_id(self, task_id):
            return {"view_token": "view-token"}

    queue = Queue()
    monkeypatch.setattr(transcription, "llm_task_queue", queue)
    monkeypatch.setattr(transcription, "cache_manager", CacheManager())
    monkeypatch.setattr(transcription, "get_notification_router", lambda: Router())
    monkeypatch.setattr(transcription, "get_temp_manager", lambda: TempManager())
    monkeypatch.setattr(transcription, "_register_llm_handoff", lambda task_id: None)

    result = transcription.process_transcription(
        task_id="task-contradiction-cache",
        url="https://www.youtube.com/watch?v=cached-id",
        processing_options={"contradiction_scan": False},
        preparsed_url=ParsedURL(
            platform="youtube",
            video_id="cached-id",
            normalized_url="https://www.youtube.com/watch?v=cached-id",
            is_short_url=False,
            original_url="https://www.youtube.com/watch?v=cached-id",
        ),
    )

    assert result["status"] == "success"
    assert len(queue.items) == 1
    assert queue.items[0]["processing_options"]["contradiction_scan"] is False


def test_production_slice_s2_direct_address_override_and_flag():
    fixture_path = Path(__file__).parents[1] / "fixtures" / "speaker_observability" / "production_slice.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    client = MagicMock()
    key_info = MagicMock()
    key_info.extract.return_value = KeyInfo([], [], [], [], [], [], [])
    inferencer = MagicMock()
    inferencer.infer.return_value = {
        "mapping": fixture["mapping"],
        "meta": fixture["meta"],
        "source": fixture["source"],
    }
    scanner = MagicMock()

    def scan_result(**kwargs):
        s2_dialog = next(
            dialog for dialog in kwargs["dialogs"]
            if dialog.get("speaker_id") == "Speaker2" and "刚才大卫" in dialog.get("text", "")
        )
        return {
            "status": "completed",
            "segment_overrides": {
                s2_dialog["segment_id"]: {
                    "status": "suspect",
                    "assignment_source": "semantic_evidence",
                    "reason": "direct_address_conflict",
                    "evidence_segment_ids": [s2_dialog["segment_id"]],
                }
            },
            "speaker_risk_flags": ["semantic_contradiction_detected"],
        }

    scanner.scan_contradictions.side_effect = scan_result
    processor = SpeakerAwareProcessor(
        _config(), client, key_info, inferencer, MagicMock(), contradiction_scanner=scanner
    )
    result = processor.process(fixture["segments"], title="production slice", skip_calibration=True)
    assert result["stats"]["contradiction_scan_status"] == "completed"
    assert "semantic_contradiction_detected" in result["structured_data"]["speaker_risk_flags"]
    assert len(result["structured_data"]["segment_overrides"]) == 1
    override = next(iter(result["structured_data"]["segment_overrides"].values()))
    assert override["reason"] == "direct_address_conflict"
    assert "name" not in override
