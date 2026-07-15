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

import json
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

    def test_calibration_none_then_full_flow_requeues_calibration_again(
        self, monkeypatch, patch_runtime
    ):
        """codex-review R4 #2: cache has a fallback-formatted-original
        llm_calibrated.txt from a PRIOR calibration attempt that fully
        degraded (calibration_status=none -- llm_ops._save_llm_results now
        persists that fallback artifact instead of dropping it). A
        subsequent full-flow request must still treat calibration as
        MISSING and re-queue a real attempt -- "an artifact exists" must not
        be conflated with "already satisfied", exactly like the existing
        disabled-placeholder case above, otherwise one failed attempt would
        permanently lock the media into the failed fallback text."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "fallback formatted original text (calibration fully failed)",
            "llm_status": {"calibration_status": CalibrationStatus.NONE},
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True},
        )

        assert result["status"] == "success"
        assert len(queued) == 1
        task = queued[0]
        assert task["processing_options"] == {"calibrate": True, "summarize": True}
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

    def test_calibrate_only_speaker_cache_propagates_cached_speaker_count(
        self, monkeypatch, patch_runtime
    ):
        """codex-review R5 #3: same "只补总结" decision as the test above,
        but for a speaker-recognition cache. transcription_data is still
        forced to None (no re-diarization), but the real speaker count from
        the cached llm_processed.json structured data must be read and
        threaded onto the queued task as cached_speaker_count, so llm_ops/
        coordinator can override the (otherwise wrong, plain-text-implied)
        single-speaker auto-inference for the summary step."""
        cache_data = {
            **BASE_CACHE_DATA,
            "transcript_type": "funasr",
            "transcript_data": {
                "segments": [
                    {"speaker": "S0", "text": "hello", "start_time": 0, "end_time": 1}
                ]
            },
            "use_speaker_recognition": True,
            "llm_calibrated": "REAL calibrated text from a genuine speaker-aware pass",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
            "llm_processed": {
                "dialogs": [{"speaker": "Alice", "text": "hello"}],
                "speaker_mapping": {"S0": "Alice", "S1": "Bob", "S2": "Carol"},
            },
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True},
        )

        assert result["status"] == "success"
        assert len(queued) == 1
        task = queued[0]
        assert task["processing_options"] == {"calibrate": False, "summarize": True}
        assert task["transcription_data"] is None
        assert task["use_speaker_recognition"] is True
        assert task["cached_speaker_count"] == 3

    def test_non_speaker_cache_leaves_cached_speaker_count_none(
        self, monkeypatch, patch_runtime
    ):
        """Non-speaker caches must not fabricate a speaker count."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "REAL calibrated text from a genuine LLM pass",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True},
        )

        assert len(queued) == 1
        assert queued[0]["cached_speaker_count"] is None

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
                "dialogs": [
                    {"speaker": "Alice", "text": "hello"},
                    {"speaker": "Bob", "text": "hi there"},
                ],
                "speaker_mapping": {"S0": "Alice", "S1": "Bob"},
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
                # codex-review R5 #3: transcription.py reads this from the
                # cached llm_processed.json's speaker_mapping (see
                # TestLayeredCacheMatrix.
                # test_calibrate_only_speaker_cache_propagates_cached_speaker_count)
                # and threads it through so the coordinator doesn't
                # misjudge this as single-speaker just because
                # transcription_data was forced to None above.
                "cached_speaker_count": len(existing_structured["speaker_mapping"]),
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

            # codex-review R5 #3: the real (>1) speaker count must reach the
            # coordinator despite content being plain text (transcription_data
            # forced None above) -- this is what lets SummaryProcessor pick
            # the multi-speaker prompt instead of silently defaulting to
            # single-speaker.
            coordinator.process.assert_called_once()
            assert coordinator.process.call_args.kwargs["speaker_count_hint"] == 2

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


class TestTranscriptOnlyCacheBothSwitchesOffIsNotFullHit:
    """codex-review R5 #2: a cache that has ONLY the transcript layer (no
    llm_calibrated/llm_summary/llm_status at all -- e.g. an old pre-LLM
    cache, or simply the very first request for this media) combined with
    calibrate=False AND summarize=False must NOT be misjudged as "cache
    already has LLM results".

    Before the fix, need_calibrated/need_summary were computed False purely
    because the REQUEST didn't want those layers -- not because they
    already existed -- so the code took the "cache has full LLM results"
    display branch, read the nonexistent llm_calibrated as an empty string,
    sent an essentially blank calibration notification, and never marked
    the task row/llm_status.json as calibration-disabled (so a later
    calibrate=True request could not tell "already disabled" from
    "never attempted").

    The fix routes this case through the SAME enqueue-to-llm_task_queue
    path already used for genuine partial hits, so the existing
    skip_calibration/skip_summary machinery in llm_ops/coordinator
    produces exactly the outcome a brand-new calibrate=False&summarize=False
    request would -- no bespoke inline handling in transcription.py.
    """

    def test_missing_llm_layers_with_both_switches_off_is_queued_not_displayed(
        self, monkeypatch, patch_runtime
    ):
        """Decision-level check (mirrors TestLayeredCacheMatrix): a
        transcript-only cache must be queued for real (disabled) LLM
        processing, not treated as an already-complete full hit."""
        cache_data = {**BASE_CACHE_DATA}  # transcript layer ONLY

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": False, "summarize": False},
        )

        assert result["status"] == "success"
        assert len(queued) == 1
        task = queued[0]
        assert task["processing_options"] == {"calibrate": False, "summarize": False}
        # Real (raw) transcript reused as input -- no re-download/re-transcribe.
        assert task["transcript"] == "RAW uncalibrated transcript"

    def test_queued_task_end_to_end_produces_disabled_status_and_real_notification(
        self, tmp_path
    ):
        """End-to-end: drive the queued task all the way through
        llm_ops._handle_llm_task/_save_llm_results against a REAL
        CacheManager and a captured notification router, and assert on the
        actual observable outcomes the review flagged as broken:
        - the push notification is non-empty and carries the real
          (locally-formatted) calibrated text, with the "disabled" wording
          the codebase already uses for a genuinely-off layer (not silently
          treated as "not yet generated")
        - the task row is marked calibration_status=disabled (not left NULL)
        - llm_calibrated.txt actually gets written to disk, so the view
          page's ?raw=calibrated can render real content instead of hitting
          the "file does not exist" branch forever
        """
        real_cm = CacheManager(cache_dir=str(tmp_path / "cache"))
        try:
            # ---- Seed a transcript-only cache: LLM has never run for this
            # media at all (no llm_calibrated/llm_summary/llm_status).
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

            task_id = real_cm.create_task(
                url="https://www.youtube.com/watch?v=abc123",
                use_speaker_recognition=False,
                platform="youtube",
                media_id="abc123",
            )["task_id"]
            real_cm.update_task_status(task_id, TaskStatus.CALIBRATING)

            # ---- Mirrors transcription.py's fixed queuing decision for this
            # scenario (see TestTranscriptOnlyCacheBothSwitchesOffIsNotFullHit
            # above): calibrate=False & summarize=False, both genuinely
            # missing -> queued for real (disabled) processing.
            llm_task = {
                "task_id": task_id,
                "url": "https://www.youtube.com/watch?v=abc123",
                "display_url": "https://www.youtube.com/watch?v=abc123",
                "platform": "youtube",
                "media_id": "abc123",
                "video_title": "cached title",
                "author": "cached author",
                "description": "cached desc",
                "transcript": "RAW uncalibrated transcript",
                "use_speaker_recognition": False,
                "transcription_data": None,
                "is_generic": False,
                "wechat_webhook": None,
                "notification_channel": None,
                "notification_webhooks": {},
                "processing_options": {"calibrate": False, "summarize": False},
            }

            # coordinator.process(skip_calibration=True, skip_summary=True):
            # calibration_status is DISABLED (local formatting, no LLM call),
            # summary_text/summary_status are None ("not attempted this
            # round" -- _save_llm_results is the one that turns the missing
            # summary layer into an explicit DISABLED status, exercised for
            # real below, not mocked).
            coordinator = MagicMock()
            coordinator.process.return_value = {
                "calibrated_text": "RAW uncalibrated transcript (locally formatted)",
                "summary_text": None,
                "stats": {
                    "calibration_status": CalibrationStatus.DISABLED,
                    "calibration_stats": {
                        "total_segments": 0, "calibrated_segments": 0,
                        "fallback_segments": 0, "low_quality_segments": 0,
                    },
                    "summary_status": None,
                },
                "models_used": {},
                "structured_data": None,
            }

            notification_router = MagicMock()
            notification_router.send_long_text = MagicMock()
            notification_router.send_text = MagicMock()

            ctxs = [
                patch.object(llm_ops, "cache_manager", real_cm),
                patch.object(llm_ops, "llm_coordinator", coordinator),
                patch.object(llm_ops, "llm_task_queue", MagicMock()),
                patch.object(llm_ops, "get_notification_router", lambda: notification_router),
                patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
            ]
            for c in ctxs:
                c.start()
            try:
                llm_ops._handle_llm_task(llm_task)
            finally:
                for c in ctxs:
                    c.stop()

            # ---- Task row: success, and calibration explicitly marked
            # disabled (not left NULL as the pre-fix full-hit branch did).
            row = real_cm.get_task_by_id(task_id)
            assert row["status"] == "success"
            assert row["calibration_status"] == CalibrationStatus.DISABLED
            assert row["summary_status"] == SummaryStatus.DISABLED

            # ---- llm_status.json on disk mirrors the same disabled states,
            # and llm_calibrated.txt is real -- the view page can render it.
            cache_data = real_cm.get_cache(
                "youtube", "abc123", use_speaker_recognition=False
            )
            assert cache_data["llm_status"]["calibration_status"] == CalibrationStatus.DISABLED
            assert cache_data["llm_status"]["summary_status"] == SummaryStatus.DISABLED
            assert cache_data["llm_calibrated"] == "RAW uncalibrated transcript (locally formatted)"

            calibrated_file = Path(cache_data["file_path"]) / "llm_calibrated.txt"
            assert calibrated_file.exists()
            assert calibrated_file.read_text(encoding="utf-8").strip() != ""

            # ---- Notification: non-empty, carries the real calibrated
            # text (not a blank string), and uses the codebase's existing
            # "disabled" wording rather than silently implying "not yet
            # generated".
            assert notification_router.send_long_text.called
            sent_text = notification_router.send_long_text.call_args.kwargs["text"]
            assert sent_text.strip() != ""
            assert "RAW uncalibrated transcript (locally formatted)" in sent_text
            assert "未启用" in sent_text
        finally:
            real_cm.close()


class TestCalibrateOnlyBackfillPreservesExistingSummaryNotification:
    """codex-review R8 #1: a cache that already has a REAL summary but a
    disabled/missing calibration layer (e.g. a prior calibrate=False &
    summarize=True request). A subsequent request that only needs to
    backfill calibration (processing_options={"calibrate": True,
    "summarize": False}, mirroring transcription.py's need_summary=False
    decision when llm_summary.txt already exists) must not lose the
    existing summary in the completion notification.

    Before the fix, _build_result_dict() derived skip_summary purely from
    THIS round's coordinator output (summary_text=None because the
    coordinator was told to skip summary) -- even though
    _save_llm_results()/save_llm_status() correctly preserved the real
    generated summary on disk via merge semantics. _send_notification()
    then consumed the stale in-memory result_dict and reported "总结未生成"
    (summary not generated), discarding a summary that genuinely exists in
    the cache.
    """

    def test_calibrate_backfill_with_existing_summary_notifies_real_summary(
        self, tmp_path
    ):
        real_cm = CacheManager(cache_dir=str(tmp_path / "cache"))
        try:
            # ---- Seed a prior calibrate=False & summarize=True run: a real
            # summary already exists, calibration is only a locally-formatted
            # disabled placeholder.
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
                llm_type="calibrated",
                content="RAW uncalibrated transcript (locally formatted)",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="abc123", use_speaker_recognition=False,
                llm_type="summary",
                content="EXISTING real summary text from a prior generation",
            )
            real_cm.save_llm_status(
                platform="youtube", media_id="abc123", use_speaker_recognition=False,
                calibration_status=CalibrationStatus.DISABLED,
                summary_status=SummaryStatus.GENERATED,
            )

            task_id = real_cm.create_task(
                url="https://www.youtube.com/watch?v=abc123",
                use_speaker_recognition=False,
                platform="youtube",
                media_id="abc123",
            )["task_id"]
            real_cm.update_task_status(task_id, TaskStatus.CALIBRATING)

            # ---- Mirrors transcription.py's "校对层缺失/未启用，总结层已满足"
            # queuing decision: calibrate=True (real calibration requested),
            # summarize=False (llm_summary.txt already exists).
            llm_task = {
                "task_id": task_id,
                "url": "https://www.youtube.com/watch?v=abc123",
                "display_url": "https://www.youtube.com/watch?v=abc123",
                "platform": "youtube",
                "media_id": "abc123",
                "video_title": "cached title",
                "author": "cached author",
                "description": "cached desc",
                "transcript": "RAW uncalibrated transcript",
                "use_speaker_recognition": False,
                "transcription_data": None,
                "is_generic": False,
                "wechat_webhook": None,
                "notification_channel": None,
                "notification_webhooks": {},
                "processing_options": {"calibrate": True, "summarize": False},
            }

            # coordinator.process(skip_summary=True): this round performs a
            # real calibration pass but never touches summary -- summary_text
            # and summary_status are both None ("not attempted this round"),
            # exactly the signal _save_llm_results()/llm_ops relies on to
            # preserve the cached summary untouched.
            coordinator = MagicMock()
            coordinator.process.return_value = {
                "calibrated_text": "REAL calibrated text from this round",
                "summary_text": None,
                "stats": {
                    "calibration_status": CalibrationStatus.FULL,
                    "calibration_stats": {
                        "total_segments": 1, "calibrated_segments": 1,
                        "fallback_segments": 0, "low_quality_segments": 0,
                    },
                    "summary_status": None,
                },
                "models_used": {},
                "structured_data": None,
            }

            notification_router = MagicMock()
            notification_router.send_long_text = MagicMock()
            notification_router.send_text = MagicMock()

            ctxs = [
                patch.object(llm_ops, "cache_manager", real_cm),
                patch.object(llm_ops, "llm_coordinator", coordinator),
                patch.object(llm_ops, "llm_task_queue", MagicMock()),
                patch.object(llm_ops, "get_notification_router", lambda: notification_router),
                patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
            ]
            for c in ctxs:
                c.start()
            try:
                llm_ops._handle_llm_task(llm_task)
            finally:
                for c in ctxs:
                    c.stop()

            # ---- Task row: real calibration result, and the merged
            # (preserved) summary status -- not lost/reset to NULL.
            row = real_cm.get_task_by_id(task_id)
            assert row["status"] == "success"
            assert row["calibration_status"] == CalibrationStatus.FULL
            assert row["summary_status"] == SummaryStatus.GENERATED

            # ---- llm_status.json / cache content: the real summary text
            # survives untouched on disk, calibration is upgraded from the
            # disabled placeholder to the real text.
            cache_data = real_cm.get_cache(
                "youtube", "abc123", use_speaker_recognition=False
            )
            assert cache_data["llm_calibrated"] == "REAL calibrated text from this round"
            assert cache_data["llm_summary"] == "EXISTING real summary text from a prior generation"
            assert cache_data["llm_status"]["calibration_status"] == CalibrationStatus.FULL
            assert cache_data["llm_status"]["summary_status"] == SummaryStatus.GENERATED

            # ---- Notification: must carry the real cached summary text
            # and must NOT report "总结未生成" (summary not generated) --
            # this is the codex-review R8 #1 regression this test locks down.
            assert notification_router.send_long_text.called
            call_kwargs = notification_router.send_long_text.call_args.kwargs
            sent_text = call_kwargs["text"]
            assert "EXISTING real summary text from a prior generation" in sent_text
            assert "总结未生成" not in sent_text
            assert "未生成" not in sent_text
            assert call_kwargs["is_summary"] is True
        finally:
            real_cm.close()


class TestCalibrateOnlyBackfillDoesNotMisreportSkippedShortAsSummary:
    """codex-review R9 P2: a cache whose llm_summary.txt is actually the
    SKIPPED_SHORT honest-state fallback (the full calibrated text saved
    verbatim as a stand-in, per the honest-state model -- see
    _save_llm_results' SKIPPED_SHORT branch) must NOT be mistaken for a real
    generated summary by _restore_cached_summary_for_notification() just
    because the file exists on disk.

    Same "只补校对" (calibrate=True, summarize=False) shape as the sibling
    R8 #1 test above, but the seeded cache carries summary_status=
    SKIPPED_SHORT instead of GENERATED. Before the R9 fix, the restore
    helper only checked file existence/non-emptiness, so it would copy the
    stale fallback text into result_dict["内容总结"] and flip
    skip_summary=False -- which both mislabels the notification as "总结"
    and skips the skip_summary branch's 5000-char NOTIFICATION_TEXT_THRESHOLD
    truncation (the summary branch has no length cap at all).
    """

    def test_calibrate_backfill_with_skipped_short_cache_keeps_not_generated_wording(
        self, tmp_path
    ):
        real_cm = CacheManager(cache_dir=str(tmp_path / "cache"))
        try:
            # ---- Seed a prior run whose text was too short to summarize:
            # llm_summary.txt holds the SKIPPED_SHORT fallback -- the
            # calibrated text saved verbatim as a stand-in, NOT a real
            # summary. summary_status is SKIPPED_SHORT, not GENERATED.
            real_cm.save_cache(
                platform="youtube",
                url="https://www.youtube.com/watch?v=short1",
                media_id="short1",
                use_speaker_recognition=False,
                transcript_data="RAW short transcript",
                transcript_type="capswriter",
                title="cached title",
                author="cached author",
                description="cached desc",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="short1", use_speaker_recognition=False,
                llm_type="calibrated",
                content="RAW short transcript (locally formatted)",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="short1", use_speaker_recognition=False,
                llm_type="summary",
                content="STALE calibrated-as-summary fallback text (should not leak as real summary)",
            )
            real_cm.save_llm_status(
                platform="youtube", media_id="short1", use_speaker_recognition=False,
                calibration_status=CalibrationStatus.DISABLED,
                summary_status=SummaryStatus.SKIPPED_SHORT,
            )

            task_id = real_cm.create_task(
                url="https://www.youtube.com/watch?v=short1",
                use_speaker_recognition=False,
                platform="youtube",
                media_id="short1",
            )["task_id"]
            real_cm.update_task_status(task_id, TaskStatus.CALIBRATING)

            # ---- "只补校对" request: calibrate=True (real calibration
            # requested), summarize=False (llm_summary.txt already exists,
            # even if it's only the SKIPPED_SHORT fallback).
            llm_task = {
                "task_id": task_id,
                "url": "https://www.youtube.com/watch?v=short1",
                "display_url": "https://www.youtube.com/watch?v=short1",
                "platform": "youtube",
                "media_id": "short1",
                "video_title": "cached title",
                "author": "cached author",
                "description": "cached desc",
                "transcript": "RAW short transcript",
                "use_speaker_recognition": False,
                "transcription_data": None,
                "is_generic": False,
                "wechat_webhook": None,
                "notification_channel": None,
                "notification_webhooks": {},
                "processing_options": {"calibrate": True, "summarize": False},
            }

            coordinator = MagicMock()
            coordinator.process.return_value = {
                "calibrated_text": "REAL calibrated text from this round",
                "summary_text": None,
                "stats": {
                    "calibration_status": CalibrationStatus.FULL,
                    "calibration_stats": {
                        "total_segments": 1, "calibrated_segments": 1,
                        "fallback_segments": 0, "low_quality_segments": 0,
                    },
                    "summary_status": None,
                },
                "models_used": {},
                "structured_data": None,
            }

            notification_router = MagicMock()
            notification_router.send_long_text = MagicMock()
            notification_router.send_text = MagicMock()

            ctxs = [
                patch.object(llm_ops, "cache_manager", real_cm),
                patch.object(llm_ops, "llm_coordinator", coordinator),
                patch.object(llm_ops, "llm_task_queue", MagicMock()),
                patch.object(llm_ops, "get_notification_router", lambda: notification_router),
                patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
            ]
            for c in ctxs:
                c.start()
            try:
                llm_ops._handle_llm_task(llm_task)
            finally:
                for c in ctxs:
                    c.stop()

            # ---- Task row and cache: SKIPPED_SHORT must be preserved as-is
            # (not silently promoted to GENERATED by the notification path).
            row = real_cm.get_task_by_id(task_id)
            assert row["status"] == "success"
            assert row["calibration_status"] == CalibrationStatus.FULL
            assert row["summary_status"] == SummaryStatus.SKIPPED_SHORT

            cache_data = real_cm.get_cache(
                "youtube", "short1", use_speaker_recognition=False
            )
            assert cache_data["llm_calibrated"] == "REAL calibrated text from this round"
            assert cache_data["llm_status"]["calibration_status"] == CalibrationStatus.FULL
            assert cache_data["llm_status"]["summary_status"] == SummaryStatus.SKIPPED_SHORT

            # ---- Notification: must take the skip_summary branch (fresh
            # calibrated text, "未生成" wording, 5000-char threshold logic
            # in play) and must NOT leak the stale SKIPPED_SHORT fallback
            # content as if it were a real "总结" -- this is the
            # codex-review R9 P2 regression this test locks down.
            assert notification_router.send_long_text.called
            call_kwargs = notification_router.send_long_text.call_args.kwargs
            sent_text = call_kwargs["text"]
            assert "## 校对文本" in sent_text
            assert "REAL calibrated text from this round" in sent_text
            assert "STALE calibrated-as-summary fallback text" not in sent_text
            assert "总结 未生成" in sent_text
            assert call_kwargs["is_summary"] is False
        finally:
            real_cm.close()
