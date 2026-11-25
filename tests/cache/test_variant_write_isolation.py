"""Byte-level regressions for cache-variant and generation write isolation."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.video_transcript_api.api.services import llm_ops
from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.cache.variant_isolation import CacheGenerationMismatch
from src.video_transcript_api.utils.llm_status import NotesStatus


@pytest.fixture
def cm(tmp_path, monkeypatch):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    monkeypatch.setattr(llm_ops, "cache_manager", manager)
    yield manager
    manager.close()


def _save_variant(cm, speaker, transcript):
    return cm.save_cache(
        platform="generic",
        url="https://example.com/same-generic-media",
        media_id="same-generic-media",
        use_speaker_recognition=speaker,
        transcript_data=transcript,
        transcript_type="capswriter",
    )


def _save_llm_round(cm, *, speaker, files_loc, suffix):
    return llm_ops._save_llm_results(
        task_id=f"task-{suffix}",
        platform="generic",
        media_id="same-generic-media",
        use_speaker_recognition=speaker,
        result_dict={
            "校对文本": f"calibrated-{suffix}",
            "内容总结": f"summary-{suffix}",
            "stats": {
                "calibration_status": "full",
                "calibration_stats": {"round": suffix},
                "summary_status": "generated",
            },
            "models_used": {},
            "share_card": {"title": f"card-{suffix}"},
            "artifact_sources": {
                "calibration": {
                    "source_fingerprint": "a" * 64,
                    "recipe_version": "calibration-v1",
                    "generation_kind": "test",
                },
                "summary": {
                    "source_fingerprint": "b" * 64,
                    "recipe_version": "summary-v1",
                    "generation_kind": "test",
                },
            },
            "expected_files_loc": files_loc,
        },
        calibrate_only=False,
        processing_options={"calibrate": True, "summarize": True},
    )


def _rows(cm):
    with cm._get_cursor() as cursor:
        cursor.execute(
            "SELECT use_speaker_recognition, files_loc FROM video_cache "
            "WHERE platform = ? AND media_id = ? ORDER BY use_speaker_recognition",
            ("generic", "same-generic-media"),
        )
        return {bool(row["use_speaker_recognition"]): row["files_loc"] for row in cursor.fetchall()}


def test_true_then_false_generic_runs_keep_independent_artifact_bytes(cm):
    """Assert actual SQLite publication and independent raw directory bytes."""
    true_cache = _save_variant(cm, True, "speaker transcript")
    assert true_cache is not None
    _save_llm_round(cm, speaker=True, files_loc=true_cache["files_loc"], suffix="true")
    true_dir = cm.cache_dir / true_cache["files_loc"]
    protected_names = (
        "llm_calibrated.txt",
        "llm_summary.txt",
        "llm_status.json",
        "llm_share_card.json",
    )
    true_bytes_before = {name: (true_dir / name).read_bytes() for name in protected_names}

    false_cache = _save_variant(cm, False, "plain transcript")
    assert false_cache is not None
    false_dir = cm.cache_dir / false_cache["files_loc"]
    assert false_cache["files_loc"] != true_cache["files_loc"]
    assert not any((false_dir / name).exists() for name in protected_names), (
        "the first save of a sibling variant must start without copied LLM products"
    )
    _save_llm_round(cm, speaker=False, files_loc=false_cache["files_loc"], suffix="false")

    rows = _rows(cm)
    assert set(rows) == {False, True}
    assert rows[True] == true_cache["files_loc"]
    assert rows[False] == false_cache["files_loc"]
    assert rows[False] != rows[True]
    assert {name: (true_dir / name).read_bytes() for name in protected_names} == true_bytes_before

    false_bytes = {name: (false_dir / name).read_bytes() for name in protected_names}
    assert false_bytes["llm_calibrated.txt"] == b"calibrated-false"
    assert false_bytes["llm_summary.txt"] == b"summary-false"
    assert json.loads(false_bytes["llm_status.json"])["summary_status"] == "generated"
    assert json.loads(false_bytes["llm_share_card.json"]) == {"title": "card-false"}
    status = json.loads(false_bytes["llm_status.json"])
    provenance = status["artifact_provenance"]["layers"]
    expected_hash = hashlib.sha256(false_bytes["llm_summary.txt"]).hexdigest()
    assert provenance["summary"]["output_sha256"] == expected_hash


def test_false_read_fallback_and_exact_integer_variant_writes(cm):
    saved = _save_variant(cm, True, "speaker transcript")
    assert saved is not None

    # Read-side False->True reuse remains a deliberate business behavior.
    assert cm.get_cache(
        "generic", "same-generic-media", use_speaker_recognition=False
    )["use_speaker_recognition"] == 1
    assert cm.get_cache(
        "generic", "same-generic-media", use_speaker_recognition=1
    )["use_speaker_recognition"] == 1

    assert cm.save_llm_result(
        "generic", "same-generic-media", 1, "summary", "integer true",
        expected_files_loc=saved["files_loc"],
    )
    status = cm.save_llm_status(
        "generic", "same-generic-media", 1, summary_status="generated",
        expected_files_loc=saved["files_loc"],
    )
    exact = cm.get_cache(
        "generic", "same-generic-media", use_speaker_recognition=1,
        exact_variant=True,
    )
    assert exact["files_loc"] == saved["files_loc"]
    assert exact["llm_summary"] == "integer true"
    assert status["summary_status"] == "generated"


def test_integer_one_does_not_fall_back_to_false_variant_write(cm):
    false_cache = cm.save_cache(
        platform="generic",
        url="https://example.com/false-only",
        media_id="false-only",
        use_speaker_recognition=False,
        transcript_data="plain transcript",
        transcript_type="capswriter",
    )
    assert false_cache is not None
    assert cm.get_cache(
        "generic", "false-only", use_speaker_recognition=1
    ) is None
    assert cm._resolve_variant_artifact_dir(
        "generic", "false-only", use_speaker_recognition=1
    ) is None
    assert not cm.save_llm_result(
        "generic", "false-only", 1, "summary", "must not land in false variant"
    )
    assert not (
        cm.cache_dir / false_cache["files_loc"] / "llm_summary.txt"
    ).exists()


def test_old_generation_handoff_is_rejected_after_same_variant_republish(cm):
    old = _save_variant(cm, False, "old transcript")
    assert old is not None
    new = _save_variant(cm, False, "new transcript")
    assert new is not None and new["files_loc"] != old["files_loc"]
    new_dir = cm.cache_dir / new["files_loc"]

    with pytest.raises(CacheGenerationMismatch, match="CACHE-GENERATION-MISMATCH"):
        cm.save_llm_result(
            "generic", "same-generic-media", False, "summary", "stale summary",
            expected_files_loc=old["files_loc"],
        )
    with pytest.raises(CacheGenerationMismatch, match="CACHE-GENERATION-MISMATCH"):
        cm.invalidate_llm_status(
            "generic", "same-generic-media", False,
            expected_files_loc=old["files_loc"],
        )
    assert not (new_dir / "llm_summary.txt").exists()
    assert (new_dir / "transcript_capswriter.txt").read_text(encoding="utf-8") == "new transcript"


def test_notes_fallback_pins_cache_generation_before_concurrent_republish(
    cm, monkeypatch
):
    old = _save_variant(cm, False, "old transcript")
    assert old is not None
    result = SimpleNamespace(
        status=NotesStatus.GENERATED,
        text="notes generated from the old cache generation",
        error=None,
        fingerprint="old-generation-anchor",
        chapter_count=0,
    )

    def republish_before_notes_write(**_kwargs):
        new = _save_variant(cm, False, "new transcript")
        assert new is not None and new["files_loc"] != old["files_loc"]
        return result

    processor = SimpleNamespace(process=republish_before_notes_write)
    monkeypatch.setattr(llm_ops, "NotesProcessor", lambda **_kwargs: processor)
    monkeypatch.setattr(
        llm_ops,
        "llm_coordinator",
        SimpleNamespace(llm_client=MagicMock(), config=SimpleNamespace(get_models=lambda: {})),
    )
    monkeypatch.setattr(
        llm_ops, "_validate_notes_commit_fingerprint", lambda **_kwargs: None
    )

    with pytest.raises(CacheGenerationMismatch, match="CACHE-GENERATION-MISMATCH"):
        llm_ops._handle_notes_generation(
            llm_task={
                "task_id": "notes-fallback-generation",
                "platform": "generic",
                "media_id": "same-generic-media",
                "use_speaker_recognition": False,
            },
            tracker=MagicMock(),
            processing_options={"notes": True},
            notes_outcome={},
        )

    new_snapshot = cm.get_cache(
        "generic", "same-generic-media", use_speaker_recognition=False, exact_variant=True
    )
    new_dir = cm.cache_dir / new_snapshot["files_loc"]
    assert new_snapshot["files_loc"] != old["files_loc"]
    assert not (new_dir / "llm_notes.txt").exists()


def test_notes_supplied_old_cache_dir_is_pinned_after_republish(cm, monkeypatch):
    old = _save_variant(cm, False, "old transcript")
    assert old is not None
    old_cache_dir = cm.cache_dir / old["files_loc"]
    new = _save_variant(cm, False, "new transcript")
    assert new is not None and new["files_loc"] != old["files_loc"]
    new_dir = cm.cache_dir / new["files_loc"]
    result = SimpleNamespace(
        status=NotesStatus.GENERATED,
        text="notes generated from the old cache generation",
        error=None,
        fingerprint="old-generation-anchor",
        chapter_count=0,
    )
    monkeypatch.setattr(
        llm_ops, "NotesProcessor",
        lambda **_kwargs: SimpleNamespace(process=lambda **_args: result),
    )
    monkeypatch.setattr(
        llm_ops,
        "llm_coordinator",
        SimpleNamespace(llm_client=MagicMock(), config=SimpleNamespace(get_models=lambda: {})),
    )
    monkeypatch.setattr(
        llm_ops, "_validate_notes_commit_fingerprint", lambda **_kwargs: None
    )
    monkeypatch.setattr(llm_ops, "finalize_terminal_status_and_notify", lambda *_a, **_k: True)
    monkeypatch.setattr(llm_ops, "get_notification_router", MagicMock())

    with pytest.raises(CacheGenerationMismatch, match="CACHE-GENERATION-MISMATCH"):
        llm_ops._handle_notes_generation(
            llm_task={
                "task_id": "notes-supplied-old-generation",
                "platform": "generic",
                "media_id": "same-generic-media",
                "use_speaker_recognition": False,
                "cache_dir": old_cache_dir,
            },
            tracker=MagicMock(),
            processing_options={"notes": True},
            notes_outcome={},
        )

    current = cm.get_cache(
        "generic", "same-generic-media", use_speaker_recognition=False,
        exact_variant=True,
    )
    assert current["files_loc"] == new["files_loc"]
    assert not (new_dir / "llm_notes.txt").exists()


def test_notes_rejects_cache_dir_outside_cache_root_without_writing(
    cm, tmp_path, monkeypatch
):
    saved = _save_variant(cm, False, "current transcript")
    assert saved is not None
    current_dir = cm.cache_dir / saved["files_loc"]
    outside_cache_dir = tmp_path / "external-cache"
    outside_cache_dir.mkdir()
    result = SimpleNamespace(
        status=NotesStatus.GENERATED,
        text="notes from an untrusted cache directory",
        error=None,
        fingerprint="external-cache-anchor",
        chapter_count=0,
    )
    monkeypatch.setattr(
        llm_ops, "NotesProcessor",
        lambda **_kwargs: SimpleNamespace(process=lambda **_args: result),
    )
    monkeypatch.setattr(
        llm_ops,
        "llm_coordinator",
        SimpleNamespace(llm_client=MagicMock(), config=SimpleNamespace(get_models=lambda: {})),
    )
    monkeypatch.setattr(
        llm_ops, "_validate_notes_commit_fingerprint", lambda **_kwargs: None
    )
    monkeypatch.setattr(llm_ops, "finalize_terminal_status_and_notify", lambda *_a, **_k: True)
    monkeypatch.setattr(llm_ops, "get_notification_router", MagicMock())

    with pytest.raises(
        CacheGenerationMismatch,
        match="CACHE-GENERATION-MISMATCH: cache_dir outside cache root",
    ):
        llm_ops._handle_notes_generation(
            llm_task={
                "task_id": "notes-outside-cache-root",
                "platform": "generic",
                "media_id": "same-generic-media",
                "use_speaker_recognition": False,
                "cache_dir": outside_cache_dir,
            },
            tracker=MagicMock(),
            processing_options={"notes": True},
            notes_outcome={},
        )

    current = cm.get_cache(
        "generic", "same-generic-media", use_speaker_recognition=False,
        exact_variant=True,
    )
    assert current["files_loc"] == saved["files_loc"]
    assert not (current_dir / "llm_notes.txt").exists()
    assert not (outside_cache_dir / "llm_notes.txt").exists()


def test_save_llm_status_rejects_stale_expected_generation(cm):
    old = _save_variant(cm, False, "old transcript")
    assert old is not None
    new = _save_variant(cm, False, "new transcript")
    assert new is not None
    with pytest.raises(CacheGenerationMismatch, match="CACHE-GENERATION-MISMATCH"):
        cm.save_llm_status(
            "generic", "same-generic-media", False,
            summary_status="generated", expected_files_loc=old["files_loc"],
        )
    assert not (cm.cache_dir / new["files_loc"] / "llm_status.json").exists()
