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

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import video_transcript_api.api.services.transcription as transcription
from video_transcript_api.api.services import llm_ops
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.task_status import TaskStatus
from video_transcript_api.utils.llm_status import CalibrationStatus, SummaryStatus


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

    def test_transcript_only_repeated_is_full_hit_without_keyerror(
        self, monkeypatch, patch_runtime
    ):
        """(5) Regression for codex-review R1 item 2: a transcript-only cache
        (calibrate=False, summarize=False on the FIRST request) has a disabled
        calibration placeholder but NO llm_summary key at all (see the
        skip_summary=False/DISABLED path in llm_ops._save_llm_results, which
        never writes llm_summary.txt for a disabled layer). Resubmitting the
        SAME transcript-only options must still be a full hit -- not a
        KeyError from unconditionally indexing cache_data["llm_summary"]."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "disabled placeholder text",
            "llm_status": {"calibration_status": CalibrationStatus.DISABLED},
            # deliberately no "llm_summary" key -- summarize was never requested
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": False, "summarize": False},
        )

        assert result["status"] == "success"
        assert result["data"]["cached"] is True
        assert queued == []

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


class TestFullHitMirrorsCacheStatusOnTaskRow:
    """Regression for codex-review R2 item 2: a full cache hit takes no
    further LLM action and calls update_task_status(..., SUCCESS) directly --
    but the task_status row backing that call is BRAND NEW (created earlier
    by the endpoint handler via create_task, columns start out NULL). Without
    mirroring the media's real llm_status.json into that call,
    calibration_status/summary_status stay NULL on the row forever, so
    /api/audit/history reports empty status for a task whose underlying cache
    is actually fully processed.

    Unlike the rest of this file (which uses DummyCacheManager to isolate the
    hit/miss decision), this test uses a REAL CacheManager against a tmp_path
    SQLite DB + cache directory -- it seeds the cache the way a genuine prior
    full-flow run (with LLM calls mocked out) would leave it on disk, then
    drives the actual second-request full-hit code path end to end and reads
    back the real task_status columns, comparing them against the real
    llm_status.json file on disk.
    """

    def test_full_hit_task_row_mirrors_llm_status_json(
        self, monkeypatch, patch_runtime, tmp_path
    ):
        real_cm = CacheManager(cache_dir=str(tmp_path / "cache"))
        try:
            # ---- Simulate a prior full-flow run (LLM calls mocked out at
            # their own layer -- see test_llm_ops_status_backfill.py for that
            # coverage). What matters here is the ON-DISK end state such a
            # run leaves behind: transcript + both LLM layers + a real
            # llm_status.json with non-trivial (non-"full", non-default)
            # values, so a naive "hardcode full/generated" fix would not
            # accidentally pass this assertion.
            real_cm.save_cache(
                platform="youtube",
                url="https://www.youtube.com/watch?v=abc123",
                media_id="abc123",
                use_speaker_recognition=False,
                transcript_data="RAW uncalibrated transcript",
                transcript_type="capswriter",
                title="cached title",
                author="cached author",
                description="cached desc",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="abc123", use_speaker_recognition=False,
                llm_type="calibrated", content="real calibrated text",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="abc123", use_speaker_recognition=False,
                llm_type="summary", content="real summary",
            )
            real_cm.save_llm_status(
                platform="youtube", media_id="abc123", use_speaker_recognition=False,
                calibration_status=CalibrationStatus.PARTIAL,
                summary_status=SummaryStatus.GENERATED,
            )

            # ---- Second request for the SAME URL: the endpoint handler
            # would create_task() before enqueueing; replicate that here.
            task_id = real_cm.create_task(
                url="https://www.youtube.com/watch?v=abc123",
                use_speaker_recognition=False,
                platform="youtube",
                media_id="abc123",
            )["task_id"]

            monkeypatch.setattr(transcription, "cache_manager", real_cm)

            result = transcription.process_transcription(
                task_id=task_id,
                url="https://www.youtube.com/watch?v=abc123",
                use_speaker_recognition=False,
                wechat_webhook=None,
                download_url=None,
                metadata_override=None,
                processing_options={"calibrate": True, "summarize": True},
            )

            assert result["status"] == "success"
            assert result["data"]["cached"] is True

            row = real_cm.get_task_by_id(task_id)
            assert row is not None
            assert row["status"] == "success"

            cache_data = real_cm.get_cache(
                "youtube", "abc123", use_speaker_recognition=False
            )
            llm_status = cache_data["llm_status"]

            # The bug this fixes: these two used to be NULL on a full-hit row.
            assert row["calibration_status"] is not None
            assert row["summary_status"] is not None
            assert row["calibration_status"] == llm_status["calibration_status"]
            assert row["summary_status"] == llm_status["summary_status"]
            assert row["calibration_status"] == CalibrationStatus.PARTIAL
            assert row["summary_status"] == SummaryStatus.GENERATED
        finally:
            real_cm.close()


class TestSpeakerCacheSummaryOnlyBackfillEndToEnd:
    """Regression coverage for codex-review R4 item 1: a speaker-recognition
    (funasr) cache that already has a REAL calibrated layer + structured
    data (llm_processed.json) but no summary. A subsequent full-flow request
    for the same URL must only backfill the summary layer, reusing the
    existing calibrated text via the forced plain-text route
    (transcription_data=None) while use_speaker_recognition stays True on
    the queued task -- exactly the shape that once risked
    `structured_data["calibration_stats"] = ...` crashing on None.

    Unlike TestLayeredCacheMatrix (which only asserts the transcription.py
    queuing DECISION), this drives the queued task all the way through
    llm_ops._handle_llm_task/_save_llm_results against a REAL CacheManager,
    so it also asserts the actual on-disk outcome: task success, summary
    persisted, and the pre-existing llm_processed.json left untouched.

    Note: as of the suppress_calibration guard added for codex-review R3,
    this exact call path (processing_options={"calibrate": False, ...} with
    the calibrated layer already present) was already crash-safe -- this
    test documents/locks that invariant for the speaker-recognition case
    (previously only covered with use_speaker_recognition=False). The
    TypeError itself is reproduced and locked down at the unit level in
    tests/unit/test_llm_ops_helpers.py::
    TestSaveLLMResultsLayeredCacheSuppression::
    test_structured_data_none_does_not_crash_when_not_suppressed, which
    exercises the other real call path (calibrate_only=True recalibrate,
    where suppression is unconditionally bypassed).
    """

    def test_summary_only_backfill_preserves_existing_structured_data(self, tmp_path):
        real_cm = CacheManager(cache_dir=str(tmp_path / "cache"))
        try:
            # ---- Seed a prior full speaker-recognition run: real
            # calibration + structured data, summary missing.
            real_cm.save_cache(
                platform="youtube",
                url="https://www.youtube.com/watch?v=abc123",
                media_id="abc123",
                use_speaker_recognition=True,
                transcript_data={
                    "segments": [
                        {"speaker": "S0", "text": "hello", "start_time": 0, "end_time": 1}
                    ]
                },
                transcript_type="funasr",
                title="cached title",
                author="cached author",
                description="cached desc",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="abc123", use_speaker_recognition=True,
                llm_type="calibrated",
                content="REAL calibrated text from a genuine speaker-aware pass",
            )
            existing_structured = {
                "dialogs": [{"speaker": "Alice", "text": "hello"}],
                "speaker_mapping": {"S0": "Alice"},
            }
            real_cm.save_llm_result(
                platform="youtube", media_id="abc123", use_speaker_recognition=True,
                llm_type="structured", content=existing_structured,
            )
            real_cm.save_llm_status(
                platform="youtube", media_id="abc123", use_speaker_recognition=True,
                calibration_status=CalibrationStatus.FULL,
                calibration_stats={
                    "total_chunks": 1, "success_count": 1,
                    "fallback_count": 0, "failed_count": 0,
                },
                summary_status=None,
            )

            task_id = real_cm.create_task(
                url="https://www.youtube.com/watch?v=abc123",
                use_speaker_recognition=True,
                platform="youtube",
                media_id="abc123",
            )["task_id"]
            real_cm.update_task_status(task_id, TaskStatus.CALIBRATING)

            # ---- Mirrors transcription.py's "校对层已满足，只缺总结" queuing
            # decision (see TestLayeredCacheMatrix.
            # test_calibrate_only_then_full_flow_requeues_summary_only): the
            # calibrated text is reused as input, transcription_data is
            # forced None (plain-text routing), use_speaker_recognition
            # stays True.
            llm_task = {
                "task_id": task_id,
                "url": "https://www.youtube.com/watch?v=abc123",
                "display_url": "https://www.youtube.com/watch?v=abc123",
                "platform": "youtube",
                "media_id": "abc123",
                "video_title": "cached title",
                "author": "cached author",
                "description": "cached desc",
                "transcript": "REAL calibrated text from a genuine speaker-aware pass",
                "use_speaker_recognition": True,
                "transcription_data": None,
                "is_generic": False,
                "wechat_webhook": None,
                "notification_channel": None,
                "notification_webhooks": {},
                "processing_options": {"calibrate": False, "summarize": True},
            }

            coordinator = MagicMock()
            # Real coordinator.process(skip_calibration=True) behavior for the
            # plain-text route: structured_data is None (only the
            # speaker-aware dialog-list route ever produces it).
            coordinator.process.return_value = {
                "calibrated_text": "REAL calibrated text from a genuine speaker-aware pass",
                "summary_text": "a real fresh summary",
                "stats": {
                    "calibration_status": CalibrationStatus.DISABLED,
                    "calibration_stats": {
                        "total_segments": 0, "calibrated_segments": 0,
                        "fallback_segments": 0, "low_quality_segments": 0,
                    },
                    "summary_status": SummaryStatus.GENERATED,
                },
                "models_used": {},
                "structured_data": None,
            }

            ctxs = [
                patch.object(llm_ops, "cache_manager", real_cm),
                patch.object(llm_ops, "llm_coordinator", coordinator),
                patch.object(llm_ops, "llm_task_queue", MagicMock()),
                patch.object(llm_ops, "_send_notification", MagicMock()),
                patch.object(llm_ops, "get_notification_router", lambda: MagicMock()),
                patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
            ]
            for c in ctxs:
                c.start()
            try:
                llm_ops._handle_llm_task(llm_task)
            finally:
                for c in ctxs:
                    c.stop()

            row = real_cm.get_task_by_id(task_id)
            assert row["status"] == "success"
            assert row["error_message"] is None

            cache_data = real_cm.get_cache(
                "youtube", "abc123", use_speaker_recognition=True
            )
            assert cache_data["llm_summary"] == "a real fresh summary"

            # The pre-existing structured data (llm_processed.json) must
            # survive untouched on disk -- this round produced no new
            # structured data (plain-text route), so it must not be
            # overwritten/wiped. get_cache() doesn't surface this file's
            # content directly, so read it back from the cache dir.
            import json

            structured_file = Path(cache_data["file_path"]) / "llm_processed.json"
            assert structured_file.exists()
            with open(structured_file, "r", encoding="utf-8") as f:
                persisted_structured = json.load(f)
            assert persisted_structured["dialogs"] == existing_structured["dialogs"]
            assert persisted_structured["speaker_mapping"] == existing_structured["speaker_mapping"]
        finally:
            real_cm.close()
