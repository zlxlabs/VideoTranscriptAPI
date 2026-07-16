"""Integration test: S1 (PR3 review hardening) -- _save_llm_results must
write-ahead revoke llm_status.json before starting to rewrite any product
file.

Root cause: _save_llm_results (llm_ops.py) rewrites calibrated/summary/
structured product files sequentially and only updates llm_status.json once
ALL writes for this round succeeded (any save_llm_result() call returning
False makes the function raise immediately, skipping the final
save_llm_status() call entirely). Before the fix, a failure AFTER an earlier
write in the same call already succeeded left the OLD llm_status.json sitting
on disk untouched -- silently vouching for a "new calibrated text + old
summary/structured" combination that never actually existed as a coherent,
fully-processed result. The next request's layered-cache hit judgment
(transcription.py) trusted that stale marker and returned the torn
combination directly, skipping retry.

Fix: cache_manager.invalidate_llm_status() deletes the status file (returning
its prior content) before any product file is rewritten. Any failure between
that point and the final save_llm_status() call now leaves the cache in a
"no status marker" state, which the (already-fixed) read-side judgment
treats as unconfirmed/incomplete -- triggering a real retry instead of
serving the mixed product. On the success path, the prior content returned
by invalidate_llm_status() is used to explicitly restore any layer this
round did not touch, so the merge-preserve semantics survive the delete.

All console output must be in English only (no emoji, no Chinese).
"""
from unittest.mock import MagicMock, patch

import pytest

from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.task_status import TaskStatus
from src.video_transcript_api.utils.llm_status import CalibrationStatus, SummaryStatus
from src.video_transcript_api.api.services import llm_ops


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _seed_prior_full_result(cm):
    """Simulate a prior fully-completed run: real calibrated + summary text,
    both mirrored in llm_status.json (calibration_status=full,
    summary_status=generated)."""
    cm.save_cache(
        platform="youtube",
        url="https://example.com/v1",
        media_id="vid1",
        use_speaker_recognition=False,
        transcript_data="raw transcript text",
        transcript_type="capswriter",
        title="Demo",
        author="Alice",
    )
    cm.save_llm_result(
        platform="youtube", media_id="vid1", use_speaker_recognition=False,
        llm_type="calibrated", content="OLD calibrated text",
    )
    cm.save_llm_result(
        platform="youtube", media_id="vid1", use_speaker_recognition=False,
        llm_type="summary", content="OLD summary text",
    )
    cm.save_llm_status(
        platform="youtube", media_id="vid1", use_speaker_recognition=False,
        calibration_status=CalibrationStatus.FULL,
        calibration_stats={"total_segments": 3},
        summary_status=SummaryStatus.GENERATED,
    )


def _full_reprocess_task(task_id):
    return {
        "task_id": task_id,
        "url": "https://example.com/v1",
        "display_url": "https://example.com/v1",
        "platform": "youtube",
        "media_id": "vid1",
        "video_title": "Demo",
        "author": "Alice",
        "description": "",
        "transcript": "OLD calibrated text",
        "use_speaker_recognition": False,
        "transcription_data": None,
        "is_generic": False,
        "wechat_webhook": None,
        "notification_channel": None,
        "notification_webhooks": {},
        "processing_options": {"calibrate": True, "summarize": True},
    }


def _patches(cm, coordinator):
    """Patch only the true external I/O boundaries (LLM coordinator, queue,
    notifications) -- cache_manager and _save_llm_results stay REAL so the
    write-ahead invalidation + llm_status.json merge semantics actually run,
    which is exactly what this bug/fix lives in."""
    return [
        patch.object(llm_ops, "cache_manager", cm),
        patch.object(llm_ops, "llm_coordinator", coordinator),
        patch.object(llm_ops, "llm_task_queue", MagicMock()),
        patch.object(llm_ops, "_send_notification", MagicMock()),
        patch.object(llm_ops, "get_notification_router", lambda: MagicMock()),
        patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
        patch.object(llm_ops, "_prepare_llm_content", lambda t, tr, spk: tr),
    ]


class TestWriteAheadInvalidationOnMidRewriteFailure:
    def test_status_marker_revoked_after_second_file_write_fails(self, cm):
        """calibrated write succeeds (new content lands on disk), summary
        write then fails -- llm_status.json must not survive as a stale
        marker vouching for this torn combination.

        RED on unfixed code: llm_status.json is never touched by the failing
        call, so it keeps describing the OLD (now stale) combination as
        "full/generated" even though the calibrated text on disk has already
        moved on to this round's content.
        """
        _seed_prior_full_result(cm)
        task_id = cm.create_task(
            url="https://example.com/v1", platform="youtube", media_id="vid1",
        )["task_id"]
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)

        coordinator = MagicMock()
        coordinator.process.return_value = {
            "calibrated_text": "NEW calibrated text from this round",
            "summary_text": "NEW summary text from this round",
            "stats": {
                "calibration_status": CalibrationStatus.FULL,
                "summary_status": SummaryStatus.GENERATED,
            },
            "models_used": {},
        }

        # Simulate a mid-rewrite failure: the calibrated write is allowed to
        # go through for real (so the "new text already on disk" precondition
        # for the bug genuinely exists), but the summary write fails.
        real_save_llm_result = cm.save_llm_result

        def flaky_save_llm_result(*args, **kwargs):
            if kwargs.get("llm_type") == "summary":
                return False
            return real_save_llm_result(*args, **kwargs)

        cm.save_llm_result = flaky_save_llm_result

        ctxs = _patches(cm, coordinator)
        for c in ctxs:
            c.start()
        try:
            llm_ops._handle_llm_task(_full_reprocess_task(task_id))
        finally:
            for c in ctxs:
                c.stop()

        # The task must end up failed -- _save_llm_results raised when the
        # summary write returned False.
        row = cm.get_task_by_id(task_id)
        assert row["status"] == "failed"

        cache_data = cm.get_cache("youtube", "vid1", use_speaker_recognition=False)
        # Precondition for the bug: the calibrated write DID land, so a naive
        # "status file left untouched on failure" implementation would keep a
        # status marker describing a combination ("new calibrated text" +
        # "old summary text") that was never actually produced as a coherent
        # result.
        assert cache_data["llm_calibrated"] == "NEW calibrated text from this round"
        assert cache_data["llm_summary"] == "OLD summary text"

        # The fix: llm_status.json must have been write-ahead revoked before
        # the calibrated write started, and since the summary write failed
        # before the function could reach its final save_llm_status() call,
        # no new status was ever written back either. transcription.py's
        # layered-cache hit judgment computes
        # `cached_calibration_status = cached_llm_status.get("calibration_status")`
        # and requires it to be not-None before treating the calibrated layer
        # as satisfied -- a missing "llm_status" key alone is therefore
        # sufficient to guarantee the next request treats this media as
        # unconfirmed / not cache-complete and retries, rather than silently
        # returning the torn combination above.
        assert "llm_status" not in cache_data


class TestWriteAheadInvalidationHappyPath:
    def test_full_rewrite_success_leaves_fresh_consistent_status(self, cm):
        """No injected failure: both files rewrite successfully, and the
        write-ahead delete + restore round trip must not corrupt the final
        merged llm_status.json."""
        _seed_prior_full_result(cm)
        task_id = cm.create_task(
            url="https://example.com/v1", platform="youtube", media_id="vid1",
        )["task_id"]
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)

        coordinator = MagicMock()
        coordinator.process.return_value = {
            "calibrated_text": "NEW calibrated text from this round",
            "summary_text": "NEW summary text from this round",
            "stats": {
                "calibration_status": CalibrationStatus.FULL,
                "summary_status": SummaryStatus.GENERATED,
            },
            "models_used": {},
        }

        ctxs = _patches(cm, coordinator)
        for c in ctxs:
            c.start()
        try:
            llm_ops._handle_llm_task(_full_reprocess_task(task_id))
        finally:
            for c in ctxs:
                c.stop()

        row = cm.get_task_by_id(task_id)
        assert row["status"] == "success"

        cache_data = cm.get_cache("youtube", "vid1", use_speaker_recognition=False)
        assert cache_data["llm_calibrated"] == "NEW calibrated text from this round"
        assert cache_data["llm_summary"] == "NEW summary text from this round"
        assert cache_data["llm_status"]["calibration_status"] == CalibrationStatus.FULL
        assert cache_data["llm_status"]["summary_status"] == SummaryStatus.GENERATED


class TestRecalibrateOnlyPreservesSummaryAcrossInvalidation:
    def test_calibrate_only_no_backfill_preserves_summary_status_on_disk(self, cm):
        """calibrate_only=True with an existing non-empty summary (so no
        backfill is triggered) is the one path where _save_llm_results never
        fetches existing_snapshot (by design, to avoid the R3 stale-snapshot
        bug documented at the top of that function) -- so the write-ahead
        invalidation's "restore the untouched summary layer" step must fall
        back to invalidate_llm_status()'s own returned prior content, not to
        existing_snapshot (which stays None on this path). Assert the real
        merged llm_status.json still shows the old summary status after a
        full successful recalibrate-only round trip.
        """
        _seed_prior_full_result(cm)
        task_id = cm.create_task(
            url="https://example.com/v1", platform="youtube", media_id="vid1",
        )["task_id"]
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)

        coordinator = MagicMock()
        coordinator.process.return_value = {
            "calibrated_text": "NEW recalibrated text",
            "summary_text": None,
            "stats": {
                "calibration_status": CalibrationStatus.FULL,
                # coordinator was told skip_summary=True this round (calibrate_only,
                # no backfill needed) -- summary_status stays None, meaning
                # "not attempted this round, preserve whatever is on disk".
                "summary_status": None,
            },
            "models_used": {},
        }

        task = _full_reprocess_task(task_id)
        task["calibrate_only"] = True
        # Mirrors tasks.py's real /api/recalibrate call site: it always
        # normalizes to the explicit default {"calibrate": True, "summarize": True},
        # never omits processing_options.
        task["processing_options"] = {"calibrate": True, "summarize": True}

        ctxs = _patches(cm, coordinator)
        for c in ctxs:
            c.start()
        try:
            llm_ops._handle_llm_task(task)
        finally:
            for c in ctxs:
                c.stop()

        row = cm.get_task_by_id(task_id)
        assert row["status"] == "success"

        cache_data = cm.get_cache("youtube", "vid1", use_speaker_recognition=False)
        assert cache_data["llm_calibrated"] == "NEW recalibrated text"
        # Summary was never touched this round -- must still show the OLD
        # summary text/status, not be wiped out by the write-ahead
        # invalidation's delete.
        assert cache_data["llm_summary"] == "OLD summary text"
        assert cache_data["llm_status"]["calibration_status"] == CalibrationStatus.FULL
        assert cache_data["llm_status"]["summary_status"] == SummaryStatus.GENERATED
