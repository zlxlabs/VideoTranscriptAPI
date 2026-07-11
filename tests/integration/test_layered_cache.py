"""Integration tests for the layered cache hit/miss decision in
process_transcription() (transcription.py), covering the cache-hit matrix
described in the per-task processing-depth feature:

  1. full flow first, then transcript-only request  -> full hit, no re-queue
  2. transcript-only first, then full flow request   -> re-queue BOTH layers
     (calibrated+summary), transcript itself is never re-downloaded/re-run
  3. calibrate+no-summary first, then full flow       -> re-queue summary ONLY,
     and the queued task reuses the EXISTING calibrated text as input rather
     than the raw transcript (so llm_calibrated.txt is never touched again --
     the actual no-overwrite guarantee is unit-tested at the
     llm_ops._save_llm_results layer; here we assert the transcription.py
     decision that feeds it)
  4. resubmitting identical options twice             -> idempotent, no re-queue

Mirrors the DummyCacheManager/DummyQueue pattern already used in
tests/features/test_transcription_flow_regression.py.

All console output must be in English only (no emoji, no Chinese).
"""

import pytest

import video_transcript_api.api.services.transcription as transcription
from video_transcript_api.utils.llm_status import CalibrationStatus


class DummyQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class DummyNotifier:
    def __init__(self, webhook=None):
        self.webhook = webhook
        self.messages = []

    def notify_task_status(self, *args, **kwargs):
        self.messages.append(("notify", args, kwargs))

    def send_text(self, text, **kwargs):
        self.messages.append(("send_text", text, kwargs))

    def _clean_url(self, url):
        return url


class DummyCacheManager:
    """Minimal cache_manager stand-in exposing exactly what
    process_transcription's cache-hit branch touches."""

    def __init__(self, cache_data=None):
        self.cache_data = cache_data
        self.saved = []
        self.status_updates = []
        self.tasks = {}

    def get_cache(self, platform, media_id, use_speaker_recognition):
        return self.cache_data

    def save_cache(self, **kwargs):
        self.saved.append(kwargs)
        return True

    def update_task_status(self, task_id, status, **kwargs):
        self.status_updates.append((task_id, status, kwargs))

    def get_task_by_id(self, task_id):
        return self.tasks.get(task_id)


BASE_CACHE_DATA = {
    "platform": "youtube",
    "media_id": "abc123",
    "title": "cached title",
    "author": "cached author",
    "description": "cached desc",
    "transcript_type": "capswriter",
    "transcript_data": "RAW uncalibrated transcript",
    "use_speaker_recognition": False,
}


@pytest.fixture
def patch_runtime(monkeypatch):
    queue = DummyQueue()
    monkeypatch.setattr(transcription, "llm_task_queue", queue)
    monkeypatch.setattr(transcription, "WechatNotifier", DummyNotifier)
    monkeypatch.setattr(transcription, "send_long_text_wechat", lambda *a, **k: None)
    monkeypatch.setattr(transcription, "get_base_url", lambda: "http://test")

    def fail_create_downloader(url):
        raise AssertionError("create_downloader should not be called on cache hit")

    monkeypatch.setattr(transcription, "create_downloader", fail_create_downloader)
    return queue


def _run(monkeypatch, patch_runtime, cache_data, processing_options, task_id="t"):
    cache_manager = DummyCacheManager(cache_data=cache_data)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    result = transcription.process_transcription(
        task_id=task_id,
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
        processing_options=processing_options,
    )
    return result, patch_runtime.items


class TestLayeredCacheMatrix:
    def test_full_flow_then_transcript_only_is_full_hit(self, monkeypatch, patch_runtime):
        """(1) Cache already has both layers (a prior full-flow run). A
        transcript-only request (calibrate=False, summarize=False) must be a
        full hit -- extra layers are returned as-is, nothing re-queued."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "real calibrated text",
            "llm_summary": "real summary",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": False, "summarize": False},
        )

        assert result["status"] == "success"
        assert result["data"]["cached"] is True
        assert queued == []

    def test_transcript_only_then_full_flow_requeues_both_layers(
        self, monkeypatch, patch_runtime
    ):
        """(2) Cache only has a disabled-calibration placeholder (a prior
        transcript-only run) and no summary. A full-flow request must
        re-queue BOTH calibrate and summarize, and the transcript itself
        (raw, not the disabled placeholder) must be reused, not re-downloaded."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "disabled placeholder text",
            "llm_status": {"calibration_status": CalibrationStatus.DISABLED},
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True},
        )

        assert result["status"] == "success"
        assert len(queued) == 1
        task = queued[0]
        assert task["processing_options"] == {"calibrate": True, "summarize": True}
        # Real calibration is needed -> feed the raw transcript, not the
        # disabled placeholder, and no download/transcription was re-run
        # (no save_cache call for a fresh transcript).
        assert task["transcript"] == "RAW uncalibrated transcript"

    def test_calibrate_only_then_full_flow_requeues_summary_only(
        self, monkeypatch, patch_runtime
    ):
        """(3) Cache has a REAL calibrated layer (prior calibrate=True,
        summarize=False run) and no summary. A full-flow request must only
        request summarize, and must feed the EXISTING calibrated text as the
        summary input (not the raw transcript) -- this is what lets
        llm_ops._save_llm_results leave llm_calibrated.txt untouched."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "REAL calibrated text from a genuine LLM pass",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True},
        )

        assert result["status"] == "success"
        assert len(queued) == 1
        task = queued[0]
        assert task["processing_options"] == {"calibrate": False, "summarize": True}
        assert task["transcript"] == "REAL calibrated text from a genuine LLM pass"
        # Force plain-text routing downstream (no re-diarization LLM call).
        assert task["transcription_data"] is None

    def test_repeated_identical_full_options_is_idempotent(
        self, monkeypatch, patch_runtime
    ):
        """(4) Resubmitting the exact same (already-satisfied) options twice
        must be a full hit both times -- no re-queue on either call."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "real calibrated text",
            "llm_summary": "real summary",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }

        result1, queued1 = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True}, task_id="t1",
        )
        result2, queued2 = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True}, task_id="t2",
        )

        assert result1["status"] == "success"
        assert result2["status"] == "success"
        assert queued1 == []
        assert queued2 == []

    def test_missing_processing_options_defaults_to_full_flow_legacy_gate(
        self, monkeypatch, patch_runtime
    ):
        """Backward compatibility: process_transcription(processing_options=None)
        must reproduce the pre-feature gate exactly (has_llm_calibrated and
        has_llm_summary)."""
        cache_data = {**BASE_CACHE_DATA, "llm_calibrated": "x"}  # summary missing

        result, queued = _run(monkeypatch, patch_runtime, cache_data, None)

        assert result["status"] == "success"
        assert len(queued) == 1
        # Legacy default (all True): calibrated layer already real (no
        # llm_status -> not disabled) so only summary is missing.
        assert queued[0]["processing_options"] == {"calibrate": False, "summarize": True}
