"""Tests for input envelopes attached to saved LLM artifact provenance."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from video_transcript_api.llm import coordinator as coordinator_module
from video_transcript_api.llm.coordinator import LLMCoordinator


def _config():
    return {
        "llm": {
            "api_key": "test-key",
            "base_url": "http://test.invalid",
            "calibrate_model": "requested-calibration-model",
            "summary_model": "requested-summary-model",
            "min_summary_threshold": 1,
            "structured_calibration": {"min_chunk_length": 1},
        }
    }


def test_artifact_input_fingerprint_is_deterministic_without_normalizing_text():
    first = {
        "schema_version": "artifact-input-v1",
        "content": {"text": "Line 1\nLine 2 ", "meta": {"a": 1, "b": 2}},
    }
    reordered = {
        "content": {"meta": {"b": 2, "a": 1}, "text": "Line 1\nLine 2 "},
        "schema_version": "artifact-input-v1",
    }

    fingerprint = getattr(coordinator_module, "_artifact_input_fingerprint", None)
    assert callable(fingerprint), "coordinator must fingerprint a versioned input envelope"
    assert fingerprint(first) == fingerprint(reordered)
    assert fingerprint(first) != fingerprint(
        {**first, "content": {"text": "Line 1\nLine 2", "meta": {"a": 1, "b": 2}}}
    )


def test_coordinator_fingerprints_actual_calibration_and_summary_processor_inputs(
    tmp_path,
):
    with patch("video_transcript_api.llm.coordinator.PlainTextProcessor"), patch(
        "video_transcript_api.llm.coordinator.SpeakerAwareProcessor"
    ), patch("video_transcript_api.llm.coordinator.SummaryProcessor"), patch(
        "video_transcript_api.llm.coordinator.ChaptersProcessor"
    ):
        coordinator = LLMCoordinator(_config(), str(tmp_path))

    content = [
        {"speaker": "Speaker1", "text": "  exact input\n", "start_time": 0.0},
        {"speaker": "Speaker2", "text": "another line", "start_time": 2.0},
    ]
    calibrated_text = "Restored calibration output\n"
    coordinator.speaker_aware_processor.process = Mock(
        return_value={
            "calibrated_text": calibrated_text,
            "key_info": {},
            "stats": {"calibration_status": "full"},
            "structured_data": {
                "speaker_mapping": {"Speaker1": "Alice", "Speaker2": "Bob"}
            },
        }
    )
    coordinator.summary_processor.process = Mock(
        return_value=SimpleNamespace(text="A summary", status="generated")
    )

    result = coordinator.process(
        content=content,
        title="Video title",
        author="Author",
        description="Description",
        platform="youtube",
        media_id="video-1",
        skip_chapters=True,
        infer_speaker_names=False,
        contradiction_scan=False,
    )

    calibration_args = coordinator.speaker_aware_processor.process.call_args.kwargs
    assert calibration_args["dialogs"] == content
    assert calibration_args["title"] == "Video title"
    summary_args = coordinator.summary_processor.process.call_args.kwargs
    assert summary_args["text"] == calibrated_text
    assert summary_args["speaker_count"] == 2
    assert summary_args["transcription_data"] == {"segments": content}

    sources = result["artifact_sources"]
    expected_calibration = {
        "schema_version": "artifact-input-v1",
        "layer": "calibration",
        "content": content,
        "title": "Video title",
        "author": "Author",
        "description": "Description",
        "platform": "youtube",
        "media_id": "video-1",
        "options": {
            "skip_calibration": False,
            "infer_speaker_names": False,
            "contradiction_scan": False,
            "selected_models": result["models_used"],
        },
    }
    expected_summary = {
        "schema_version": "artifact-input-v1",
        "layer": "summary",
        "text": calibrated_text,
        "title": "Video title",
        "author": "Author",
        "description": "Description",
        "speaker_count": 2,
        "transcription_data": {"segments": content},
        "selected_models": result["models_used"],
    }
    fingerprint = coordinator_module._artifact_input_fingerprint
    assert sources["calibration"]["source_fingerprint"] == fingerprint(
        expected_calibration
    )
    assert sources["summary"]["source_fingerprint"] == fingerprint(
        expected_summary
    )
    assert sources["summary"]["source_fingerprint"] != fingerprint(
        {**expected_summary, "text": "  exact input\n"}
    )


def test_dict_segments_fingerprint_matches_the_list_sent_to_processor(tmp_path):
    with patch("video_transcript_api.llm.coordinator.PlainTextProcessor"), patch(
        "video_transcript_api.llm.coordinator.SpeakerAwareProcessor"
    ), patch("video_transcript_api.llm.coordinator.SummaryProcessor"), patch(
        "video_transcript_api.llm.coordinator.ChaptersProcessor"
    ):
        coordinator = LLMCoordinator(_config(), str(tmp_path))
    segments = [{"speaker": "S1", "text": "dialog"}]
    coordinator.speaker_aware_processor.process = Mock(
        return_value={
            "calibrated_text": "calibrated",
            "key_info": {},
            "stats": {"calibration_status": "disabled"},
        }
    )
    result = coordinator.process(
        content={"segments": segments, "unused": "not passed to processor"},
        title="Video",
        skip_summary=True,
        skip_chapters=True,
    )

    call = coordinator.speaker_aware_processor.process.call_args.kwargs
    assert call["dialogs"] == segments
    assert result["artifact_sources"]["calibration"]["generation_kind"] == "disabled_local_format"
    expected_envelope = {
        "schema_version": "artifact-input-v1",
        "layer": "calibration",
        "content": call["dialogs"],
        "title": call["title"],
        "author": call["author"],
        "description": call["description"],
        "platform": call["platform"],
        "media_id": call["media_id"],
        "options": {
            "skip_calibration": call["skip_calibration"],
            "infer_speaker_names": call["infer_speaker_names"],
            "contradiction_scan": call["contradiction_scan"],
            "selected_models": result["models_used"],
        },
    }
    assert result["artifact_sources"]["calibration"]["source_fingerprint"] == (
        coordinator_module._artifact_input_fingerprint(expected_envelope)
    )
