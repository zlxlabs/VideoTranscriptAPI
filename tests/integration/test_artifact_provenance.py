"""Check coordinator provenance through cached bytes and the reader CLI."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from video_transcript_api.api.services import llm_ops
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.llm.coordinator import LLMCoordinator
from video_transcript_api.llm.core.speaker_inferencer import SpeakerInferencer
from video_transcript_api.utils.llm_status import CalibrationStatus, SummaryStatus


REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM = "bilibili"
MEDIA_ID = "provenance-video"
SEGMENTS = [
    {"speaker": "SPEAKER_00", "text": "Opening words", "start": 0, "end": 1},
    {"speaker": "SPEAKER_01", "text": "Reply words", "start": 1, "end": 2},
]
TRANSCRIPTION_DATA = {"speakers": ["SPEAKER_00", "SPEAKER_01"], "segments": SEGMENTS}
REAL_NAMES = {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}


def _config():
    return {"llm": {
        "api_key": "test-key", "base_url": "http://test.invalid",
        "calibrate_model": "requested-calibration-model",
        "summary_model": "requested-summary-model", "min_summary_threshold": 1,
        "structured_calibration": {"min_chunk_length": 1},
    }}


def _coordinator(tmp_path):
    with patch("video_transcript_api.llm.coordinator.LLMClient"), patch(
        "video_transcript_api.llm.coordinator.KeyInfoExtractor"
    ), patch("video_transcript_api.llm.coordinator.SpeakerInferencer"), patch(
        "video_transcript_api.llm.coordinator.UnifiedQualityValidator"
    ), patch("video_transcript_api.llm.coordinator.PlainTextProcessor"), patch(
        "video_transcript_api.llm.coordinator.SpeakerAwareProcessor"
    ), patch("video_transcript_api.llm.coordinator.SummaryProcessor"), patch(
        "video_transcript_api.llm.coordinator.ChaptersProcessor"
    ):
        coordinator = LLMCoordinator(_config(), str(tmp_path))
    coordinator.speaker_aware_processor.process = Mock()
    coordinator.plain_text_processor.process = Mock()
    coordinator.summary_processor.process = Mock(
        return_value=SimpleNamespace(text="Summary from calibrated dialogue", status=SummaryStatus.GENERATED)
    )
    return coordinator


@pytest.fixture
def artifact_store(tmp_path, monkeypatch):
    cache = CacheManager(cache_dir=str(tmp_path / "cache"))
    coordinator = _coordinator(tmp_path)
    monkeypatch.setattr(llm_ops, "cache_manager", cache)
    yield cache, coordinator
    cache.close()


def _calibration_result(text, status=CalibrationStatus.FULL):
    dialogs = [
        {"speaker": item["speaker"], "speaker_id": item["speaker"], "text": item["text"]}
        for item in SEGMENTS
    ]
    return {
        "calibrated_text": text, "key_info": {},
        "stats": {"calibration_status": status, "speaker_inference_source": "identity_fallback"},
        "structured_data": {
            "dialogs": dialogs,
            "speaker_mapping": {speaker: speaker for speaker in REAL_NAMES},
        },
    }


def _seed_identity_mapping(cache):
    cache.save_cache(
        platform=PLATFORM, url=f"https://example.invalid/{MEDIA_ID}", media_id=MEDIA_ID,
        use_speaker_recognition=True, transcript_data=TRANSCRIPTION_DATA,
        transcript_type="funasr", title="Demo video", author="Channel",
    )
    cache.save_llm_result(
        platform=PLATFORM, media_id=MEDIA_ID, use_speaker_recognition=True,
        llm_type="structured", content={"dialogs": [], "speaker_mapping": REAL_NAMES},
    )
    speakers = SpeakerInferencer.extract_speaker_labels(SEGMENTS)
    cache.save_speaker_mapping(
        PLATFORM, MEDIA_ID,
        {"mapping": REAL_NAMES, "meta": {s: {"name": n, "confidence": 0.9}
                                          for s, n in REAL_NAMES.items()}},
        input_fingerprint=SpeakerInferencer.input_fingerprint(speakers, SEGMENTS),
        speakers=speakers,
    )


def _produce_and_save(
    cache, coordinator, *, text, summarize=True, speaker_recognition=True,
    title="Demo video", calibration_status=CalibrationStatus.FULL,
):
    if speaker_recognition:
        coordinator.speaker_aware_processor.process.return_value = _calibration_result(
            text, calibration_status
        )
        content = SEGMENTS
    else:
        coordinator.plain_text_processor.process.return_value = {
            "calibrated_text": text, "key_info": {},
            "stats": {"calibration_status": calibration_status},
        }
        content = "Raw transcript for a plain-text video"
    produced = coordinator.process(
        content=content, title=title, author="Channel", description="Description",
        platform=PLATFORM, media_id=MEDIA_ID, skip_summary=not summarize,
        skip_chapters=True, infer_speaker_names=True, contradiction_scan=False,
    )
    assert "artifact_sources" in produced
    result = llm_ops._build_result_dict(produced)
    sources = produced["artifact_sources"]
    # Avoid task-database setup; the captured sources still come from the real producer.
    result["models_used"] = {}
    llm_ops._save_llm_results(
        task_id="artifact-provenance-test", platform=PLATFORM, media_id=MEDIA_ID,
        use_speaker_recognition=speaker_recognition, result_dict=result,
        calibrate_only=False,
        processing_options={"calibrate": True, "summarize": summarize, "chapters": False},
    )
    return sources


def _report(cache_dir):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/artifact_provenance_report.py"),
         "--cache-dir", str(cache_dir)],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize(
    "has_source,file_exists",
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_only_a_valid_source_requires_readable_saved_bytes(
    artifact_store, monkeypatch, has_source, file_exists,
):
    cache, _coordinator = artifact_store
    cache.save_cache(
        platform=PLATFORM, url=f"https://example.invalid/{MEDIA_ID}", media_id=MEDIA_ID,
        use_speaker_recognition=False, transcript_data="raw", transcript_type="capswriter",
    )
    leaf = Path(cache.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=False)["file_path"])
    proxy = Mock(wraps=cache)
    proxy.get_cache.return_value = cache.get_cache(
        PLATFORM, MEDIA_ID, use_speaker_recognition=False,
    ) if has_source else None
    proxy.save_llm_result.side_effect = (
        cache.save_llm_result if file_exists else lambda **kwargs: True
    )
    monkeypatch.setattr(llm_ops, "cache_manager", proxy)
    sources = (
        {"calibration": {
            "recipe_version": "calibration-v1", "source_fingerprint": "a" * 64,
            "generation_kind": "llm",
        }}
        if has_source else {}
    )
    save_args = {
        "task_id": "source-boundary-test", "platform": PLATFORM, "media_id": MEDIA_ID,
        "use_speaker_recognition": False,
        "result_dict": {
            "校对文本": "calibrated", "内容总结": None, "skip_summary": True,
            "summary_status": SummaryStatus.DISABLED,
            "stats": {"calibration_status": CalibrationStatus.FULL},
            "models_used": {}, "artifact_sources": sources,
        },
        "calibrate_only": False,
    }
    if has_source and not file_exists:
        with pytest.raises(FileNotFoundError):
            llm_ops._save_llm_results(**save_args)
    else:
        llm_ops._save_llm_results(**save_args)
    assert proxy.get_cache.call_count == int(has_source)
    assert proxy.save_llm_status.called is (not (has_source and not file_exists))
    if not has_source:
        status = cache.get_cache(
            PLATFORM, MEDIA_ID, use_speaker_recognition=False,
        )["llm_status"]
        assert status["calibration_status"] == CalibrationStatus.FULL
        assert "artifact_provenance" not in status
    elif file_exists:
        record = cache.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=False)[
            "llm_status"]["artifact_provenance"]["layers"]["calibration"]
        assert record["output_sha256"] == hashlib.sha256(
            (leaf / "llm_calibrated.txt").read_bytes()
        ).hexdigest()


def test_real_coordinator_files_and_cli_verify_final_restored_bytes_and_layer_updates(
    artifact_store,
):
    cache, coordinator = artifact_store
    _seed_identity_mapping(cache)
    original_placeholder = "SPEAKER_00：Opening words\n\nSPEAKER_01：Reply words"
    sources = _produce_and_save(cache, coordinator, text=original_placeholder)

    saved = cache.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=True)
    leaf = Path(saved["file_path"])
    calibration_bytes = (leaf / "llm_calibrated.txt").read_bytes()
    records = saved["llm_status"]["artifact_provenance"]["layers"]
    assert calibration_bytes.decode() == "Alice：Opening words\n\nBob：Reply words"
    assert records["calibration"]["source_fingerprint"] == sources["calibration"]["source_fingerprint"]
    assert records["summary"]["source_fingerprint"] == sources["summary"]["source_fingerprint"]
    assert records["calibration"]["output_sha256"] == hashlib.sha256(calibration_bytes).hexdigest()
    summary_bytes = (leaf / "llm_summary.txt").read_bytes()
    assert records["summary"]["output_sha256"] == hashlib.sha256(summary_bytes).hexdigest()
    assert records["calibration"]["requested_model"] == "requested-calibration-model"
    assert records["summary"]["requested_model"] == "requested-summary-model"
    assert records["calibration"]["actual_model"] == records["calibration"]["prompt_version"] == "unknown"

    report = _report(leaf)
    assert report.returncode == 0, report.stderr
    assert {k: v["status"] for k, v in json.loads(report.stdout)["layers"].items()} == {
        "calibration": "verified_output", "summary": "verified_output",
    }
    summary_path = leaf / "llm_summary.txt"
    summary_path.write_bytes(summary_bytes + b"tampered")
    report = _report(leaf)
    assert report.returncode == 1
    assert json.loads(report.stdout)["layers"]["summary"]["status"] == "mismatch"
    summary_path.write_bytes(summary_bytes)
    summary_path.unlink()
    assert json.loads(_report(leaf).stdout)["layers"]["summary"]["status"] == "mismatch"
    summary_path.write_bytes(summary_bytes)
    summary_record = records["summary"].copy()
    changed_sources = _produce_and_save(
        cache, coordinator, text="SPEAKER_00：Changed opening\n\nSPEAKER_01：Changed reply",
        summarize=False, title="Changed title",
    )
    updated = cache.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=True)
    updated_layers = updated["llm_status"]["artifact_provenance"]["layers"]
    assert updated_layers["calibration"]["source_fingerprint"] == changed_sources["calibration"]["source_fingerprint"]
    assert updated_layers["calibration"]["source_fingerprint"] != records["calibration"]["source_fingerprint"]
    assert updated_layers["summary"] == summary_record

    legacy_result = {
        "校对文本": "legacy caller rewrote calibration", "内容总结": None,
        "skip_summary": True, "stats": {"calibration_status": CalibrationStatus.FULL},
        "models_used": {},
    }
    llm_ops._save_llm_results(
        task_id="legacy-caller", platform=PLATFORM, media_id=MEDIA_ID,
        use_speaker_recognition=True, result_dict=legacy_result, calibrate_only=False,
        processing_options={"calibrate": True, "summarize": False, "chapters": False},
    )
    final = cache.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=True)
    assert set(final["llm_status"]["artifact_provenance"]["layers"]) == {"summary"}
    assert _report(Path(final["file_path"])).returncode == 0


def test_writer_failure_does_not_leave_a_verifiable_new_record(artifact_store):
    cache, coordinator = artifact_store
    cache.save_cache(
        platform=PLATFORM, url=f"https://example.invalid/{MEDIA_ID}", media_id=MEDIA_ID,
        use_speaker_recognition=False, transcript_data="Raw transcript",
        transcript_type="capswriter", title="Demo video", author="Channel",
    )
    coordinator.plain_text_processor.process.return_value = {
        "calibrated_text": "calibrated", "key_info": {},
        "stats": {"calibration_status": CalibrationStatus.FULL},
    }
    coordinator.summary_processor.process.return_value = SimpleNamespace(
        text="summary", status=SummaryStatus.GENERATED,
    )
    original_save = cache.save_llm_result

    def fail_summary(*args, **kwargs):
        if kwargs.get("llm_type") == "summary":
            return False
        return original_save(*args, **kwargs)

    cache.save_llm_result = fail_summary
    with pytest.raises(OSError, match="failed to persist summary artifact"):
        _produce_and_save(cache, coordinator, text="calibrated", speaker_recognition=False)
    saved = cache.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=False)
    report = _report(Path(saved["file_path"]))
    assert report.returncode == 0
    assert {x["status"] for x in json.loads(report.stdout)["layers"].values()} == {"unknown"}


def test_fallback_is_recorded_as_fallback_and_legacy_or_bad_json_is_read_honestly(
    artifact_store,
):
    cache, coordinator = artifact_store
    cache.save_cache(
        platform=PLATFORM, url=f"https://example.invalid/{MEDIA_ID}", media_id=MEDIA_ID,
        use_speaker_recognition=False, transcript_data="tiny raw text",
        transcript_type="capswriter", title="Demo video", author="Channel",
    )
    coordinator.config.min_summary_threshold = 500
    _produce_and_save(
        cache, coordinator, text="tiny fallback", speaker_recognition=False,
        calibration_status=CalibrationStatus.NONE,
    )
    saved = cache.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=False)
    layers = saved["llm_status"]["artifact_provenance"]["layers"]
    assert layers["calibration"]["generation_kind"] == "formatted_fallback"
    assert layers["summary"]["generation_kind"] == "copied_calibration"
    assert saved["llm_calibrated"] == saved["llm_summary"] == "tiny fallback"

    legacy = Path(saved["file_path"]) / "legacy"
    legacy.mkdir()
    status_path = legacy / "llm_status.json"
    status_path.write_text("{}", encoding="utf-8")
    report = _report(legacy)
    assert report.returncode == 0
    assert {x["status"] for x in json.loads(report.stdout)["layers"].values()} == {"unknown"}
    status_path.write_text("{broken", encoding="utf-8")
    report = _report(legacy)
    assert report.returncode == 2 and report.stdout == ""
