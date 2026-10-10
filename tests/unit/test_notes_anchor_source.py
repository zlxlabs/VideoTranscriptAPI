"""Chapter anchor source truth: writer label must reproduce the reader's load.

Production shape reproduced here (#227): a cache directory holding BOTH the
structured ``llm_processed.json`` dialogs and the raw ``transcript_funasr.json``
segments, where a chapters-only re-generation anchors on the cached dialogs.

The invariant locked by these tests: the anchor fingerprint stored in a
``chapters_status=GENERATED`` product equals the fingerprint the notes reader
recomputes from that same cache directory under the repaired resolution rule.
A label that only records "what the input looked like" (queue seed shape) makes
the reader load the other source, which is exactly the production failure
(product 518 anchors / reader 556 segments -> fingerprint mismatch).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from video_transcript_api.api.services import llm_ops
from video_transcript_api.llm.coordinator import LLMCoordinator
from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.processors.chapters_processor import ChaptersProcessor
from video_transcript_api.llm.processors.notes_processor import (
    NotesProcessor,
    compute_notes_anchor_fingerprint,
    load_notes_source_segments,
)
from video_transcript_api.transcriber.segments import load_segments
from video_transcript_api.utils.llm_status import NotesStatus

# Dialog text must clear ChaptersProcessor.min_chapters_threshold (set low in
# _config below); the two sources deliberately differ in length and text so a
# mislabeled anchor cannot reproduce the other source's fingerprint.
def _hhmmss(total_seconds: int) -> str:
    return "%02d:%02d:%02d" % (
        total_seconds // 3600,
        (total_seconds % 3600) // 60,
        total_seconds % 60,
    )


DIALOGS: List[Dict[str, Any]] = [
    {
        "speaker": "Speaker1",
        "start_time": _hhmmss(index * 60),
        "end_time": _hhmmss(index * 60 + 45),
        "text": f"cached dialog line {index} " + "dialog padding words. " * 12,
    }
    for index in range(12)
]

SEGMENTS: List[Dict[str, Any]] = [
    {
        "start_time": float(index * 8),
        "end_time": float(index * 8 + 7),
        "text": f"raw funasr segment {index} " + "raw padding words. " * 9,
        "speaker": f"spk{index % 2}",
    }
    for index in range(17)
]


class FakeNotesClient:
    """Return scripted chapter notes without any network access."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeChaptersClient:
    """Answer the chapters LLM call with a fixed, valid chapter outline."""

    def __init__(self, chapters):
        self.chapters = chapters
        self.calls: List[Dict[str, Any]] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        response = MagicMock()
        response.structured_output = {"chapters": self.chapters}
        return response


def _notes_config() -> LLMConfig:
    return LLMConfig(
        api_key="test-key",
        base_url="http://localhost",
        calibrate_model="calibrate-model",
        summary_model="summary-model",
        notes_model="notes-model",
        notes_concurrency=1,
    )


def _coordinator_config() -> Dict[str, Any]:
    return {
        "llm": {
            "api_key": "test-key",
            "base_url": "http://test.invalid",
            "calibrate_model": "calibrate-model",
            "summary_model": "summary-model",
            "min_summary_threshold": 1,
            "min_chapters_threshold": 1,
            "structured_calibration": {"min_chunk_length": 1},
        }
    }


def _write_production_cache(cache_dir: Path) -> None:
    """Write the two coexisting anchor sources exactly as production does."""
    (cache_dir / "llm_processed.json").write_text(
        json.dumps({"dialogs": DIALOGS}, ensure_ascii=False), encoding="utf-8"
    )
    (cache_dir / "transcript_funasr.json").write_text(
        json.dumps({"segments": SEGMENTS}, ensure_ascii=False), encoding="utf-8"
    )


class _FileBackedCacheManager:
    """Minimal get_cache that reads the real cache files, as production does."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir

    def get_cache(self, platform, media_id, use_speaker_recognition=False):
        processed = json.loads(
            (self.cache_dir / "llm_processed.json").read_text(encoding="utf-8")
        )
        return {"llm_processed": processed}


CHAPTER_OUTLINE = [
    {"title": "Opening", "gist": "intro", "start_seg": 0},
    {"title": "Middle", "gist": "core", "start_seg": 5},
    {"title": "Closing", "gist": "outro", "start_seg": 10},
]


def _run_chapters_round(
    coordinator: LLMCoordinator,
    *,
    seed_segments,
    seed_kind: str,
) -> Dict[str, Any]:
    """Run the real coordinator chapters stage with calibration suppressed.

    Calibration suppression is the production trigger: the re-generation ran
    chapters only, so ``structured_data`` is absent and the anchor is whatever
    seed llm_ops resolved from cache.
    """
    coordinator._route_to_calibration_processor = MagicMock(
        return_value={
            "calibrated_text": "cached calibrated text",
            "key_info": {},
            "stats": {"calibration_status": "disabled"},
            "structured_data": None,
        }
    )
    return coordinator.process(
        content="",
        title="Anchor source video",
        skip_summary=True,
        skip_calibration=True,
        timeline_segments=seed_segments,
        timeline_segments_kind=seed_kind,
    )


def _write_chapters_product(cache_dir: Path, result: Dict[str, Any]) -> Dict[str, Any]:
    """Persist llm_chapters.json with the same field mapping llm_ops uses."""
    stats = result["stats"]
    payload = {
        "format_version": "v1",
        "source": {
            "kind": stats["chapters_source_kind"],
            "segment_count": stats["chapters_segment_count"],
            "fingerprint": stats["chapters_fingerprint"],
            "generated_at": "2026-10-10T15:12:30+00:00",
        },
        "chapters": stats["chapters"],
    }
    (cache_dir / "llm_chapters.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return payload


class TestCachedDialogsSeedAnchorsNotesReader:
    """Production scenario: cached dialogs seed, both sources coexist."""

    def test_seed_resolution_prefers_cached_dialogs(self, monkeypatch, tmp_path):
        _write_production_cache(tmp_path)
        monkeypatch.setattr(llm_ops, "cache_manager", _FileBackedCacheManager(tmp_path))

        seed, seed_kind = llm_ops._resolve_chapters_timeline_segments(
            llm_task={},
            platform="bilibili",
            media_id="BV1fcHD6cEw3",
            use_speaker_recognition=False,
        )

        assert seed_kind == "cached_dialogs"
        assert seed == DIALOGS

    def test_generated_product_reloads_the_same_anchor_in_notes_reader(
        self, monkeypatch, tmp_path
    ):
        _write_production_cache(tmp_path)
        monkeypatch.setattr(llm_ops, "cache_manager", _FileBackedCacheManager(tmp_path))
        seed, seed_kind = llm_ops._resolve_chapters_timeline_segments(
            llm_task={},
            platform="bilibili",
            media_id="BV1fcHD6cEw3",
            use_speaker_recognition=False,
        )

        coordinator = LLMCoordinator(_coordinator_config(), str(tmp_path))
        coordinator.chapters_processor = ChaptersProcessor(
            llm_client=FakeChaptersClient(CHAPTER_OUTLINE),
            config=LLMConfig.from_dict(_coordinator_config()),
        )

        result = _run_chapters_round(
            coordinator, seed_segments=seed, seed_kind=seed_kind
        )
        payload = _write_chapters_product(tmp_path, result)

        # The writer must name the source it actually anchored on, and the two
        # available sources must be distinguishable (otherwise this test would
        # pass no matter which one the reader picked).
        assert payload["source"]["kind"] == "cached_dialogs"
        assert payload["source"]["segment_count"] == len(DIALOGS)
        assert len(load_segments(tmp_path)) != len(DIALOGS)
        assert (
            compute_notes_anchor_fingerprint(load_segments(tmp_path))
            != payload["source"]["fingerprint"]
        )

        anchor, anchor_kind = load_notes_source_segments(
            tmp_path, payload["source"]["kind"]
        )
        assert anchor_kind == "dialogs"
        assert anchor == DIALOGS
        assert (
            compute_notes_anchor_fingerprint(anchor) == payload["source"]["fingerprint"]
        )

    def test_notes_generation_succeeds_against_cached_dialogs_anchor(
        self, monkeypatch, tmp_path
    ):
        _write_production_cache(tmp_path)
        monkeypatch.setattr(llm_ops, "cache_manager", _FileBackedCacheManager(tmp_path))
        seed, seed_kind = llm_ops._resolve_chapters_timeline_segments(
            llm_task={},
            platform="bilibili",
            media_id="BV1fcHD6cEw3",
            use_speaker_recognition=False,
        )

        coordinator = LLMCoordinator(_coordinator_config(), str(tmp_path))
        coordinator.chapters_processor = ChaptersProcessor(
            llm_client=FakeChaptersClient(CHAPTER_OUTLINE),
            config=LLMConfig.from_dict(_coordinator_config()),
        )
        payload = _write_chapters_product(
            tmp_path,
            _run_chapters_round(coordinator, seed_segments=seed, seed_kind=seed_kind),
        )

        client = FakeNotesClient(["notes one", "notes two", "notes three"])
        notes_result = NotesProcessor(client, _notes_config()).process(
            cache_dir=tmp_path
        )

        assert notes_result.status is NotesStatus.GENERATED, notes_result.error
        assert notes_result.text
        assert notes_result.fingerprint == payload["source"]["fingerprint"]
        assert len(client.calls) == 3

    def test_handle_llm_task_passes_the_resolved_seed_kind(self, monkeypatch):
        """The seed kind resolved by llm_ops must reach the coordinator."""
        captured: Dict[str, Any] = {}

        mock_cache_manager = MagicMock()
        mock_cache_manager.get_cache.return_value = {
            "llm_processed": {"dialogs": DIALOGS},
        }
        mock_cache_manager.media_lock.return_value.__enter__ = MagicMock(
            return_value=None
        )
        mock_cache_manager.media_lock.return_value.__exit__ = MagicMock(
            return_value=False
        )
        mock_cache_manager.invalidate_llm_status.return_value = {}
        mock_cache_manager.save_llm_result.return_value = True
        mock_cache_manager.save_llm_status.return_value = True
        mock_cache_manager.get_task_by_id.return_value = {"view_token": "tok"}
        mock_cache_manager.update_task_status.return_value = True

        mock_coordinator = MagicMock()
        mock_coordinator.process = lambda **kwargs: captured.update(kwargs) or {
            "calibrated_text": "calibrated",
            "summary_text": "summary",
            "stats": {},
            "models_used": {},
        }

        queue = MagicMock()
        router = MagicMock()
        monkeypatch.setattr(llm_ops, "llm_task_queue", queue)
        monkeypatch.setattr(llm_ops, "cache_manager", mock_cache_manager)
        monkeypatch.setattr(llm_ops, "llm_coordinator", mock_coordinator)
        monkeypatch.setattr(llm_ops, "get_notification_router", lambda: router)
        monkeypatch.setattr(llm_ops, "normalize_processing_options", lambda _options: {
            "calibrate": True,
            "summarize": True,
            "chapters": True,
            "infer_speaker_names": False,
        })

        llm_ops._handle_llm_task(
            {
                "task_id": "task-anchor",
                "url": "https://example.com/v/1",
                "platform": "bilibili",
                "media_id": "BV1fcHD6cEw3",
                "video_title": "Anchor source video",
                "author": "author",
                "description": "desc",
                "transcript": "transcript",
                "use_speaker_recognition": False,
                "transcription_data": None,
                "wechat_webhook": None,
                "notification_webhooks": {},
                "is_generic": False,
                "calibrate_only": False,
                "processing_options": {
                    "calibrate": False,
                    "summarize": False,
                    "chapters": True,
                    "infer_speaker_names": False,
                },
            }
        )

        assert captured["timeline_segments"] == DIALOGS
        assert captured["timeline_segments_kind"] == "cached_dialogs"


class TestSegmentsAnchorIsNotRegressed:
    """The #228 path (chapters anchored on raw segments) still resolves."""

    def test_segments_label_reads_the_transcript_anchor(self, tmp_path):
        (tmp_path / "llm_processed.json").write_text(
            json.dumps({"dialogs": DIALOGS}, ensure_ascii=False), encoding="utf-8"
        )
        (tmp_path / "transcript_funasr.json").write_text(
            json.dumps({"segments": SEGMENTS}, ensure_ascii=False), encoding="utf-8"
        )

        anchor, anchor_kind = load_notes_source_segments(tmp_path, "segments")

        assert anchor_kind == "segments"
        assert [entry["text"] for entry in anchor] == [
            entry["text"] for entry in SEGMENTS
        ]
        assert compute_notes_anchor_fingerprint(anchor) == (
            compute_notes_anchor_fingerprint(load_segments(tmp_path))
        )


class TestDeclaredKindFailsSafe:
    """A declared kind must never be silently substituted (fail-safe only)."""

    def test_dialogs_label_without_dialogs_file_returns_nothing(self, tmp_path):
        (tmp_path / "transcript_funasr.json").write_text(
            json.dumps({"segments": SEGMENTS}, ensure_ascii=False), encoding="utf-8"
        )

        assert load_notes_source_segments(tmp_path, "dialogs") == (None, None)

    def test_segments_label_without_transcript_file_returns_nothing(self, tmp_path):
        (tmp_path / "llm_processed.json").write_text(
            json.dumps({"dialogs": DIALOGS}, ensure_ascii=False), encoding="utf-8"
        )

        assert load_notes_source_segments(tmp_path, "segments") == (None, None)

    def test_notes_fails_loudly_when_declared_anchor_is_gone(self, tmp_path):
        (tmp_path / "transcript_funasr.json").write_text(
            json.dumps({"segments": SEGMENTS}, ensure_ascii=False), encoding="utf-8"
        )
        anchor_fingerprint = compute_notes_anchor_fingerprint(SEGMENTS)
        (tmp_path / "llm_chapters.json").write_text(
            json.dumps(
                {
                    "format_version": "v1",
                    "source": {
                        "kind": "dialogs",
                        "segment_count": len(DIALOGS),
                        "fingerprint": anchor_fingerprint,
                    },
                    "chapters": [
                        {
                            "title": "Only",
                            "gist": "gone",
                            "start_seg": 0,
                            "end_seg": 0,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        client = FakeNotesClient(["must not be called"])
        result = NotesProcessor(client, _notes_config()).process(cache_dir=tmp_path)

        assert result.status is NotesStatus.FAILED
        assert client.calls == []