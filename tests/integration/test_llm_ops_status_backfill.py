"""Integration test: codex-review R1 item 3 -- backfilling a single LLM layer
must not null out the OTHER layer's already-persisted status on the
task_status row.

Root cause: _save_llm_results() returns the "effective" status for THIS round
only -- a layer that was suppressed (already existed in the cache, not
requested this round) comes back as None ("untouched, preserve old value",
which is the correct semantics for llm_status.json's merge-on-write). But
llm_ops._handle_llm_task() blindly copies that None into
result_dict["stats"] and then straight into
cache_manager.update_task_status(). Every request creates a BRAND NEW task_id
row (task_status.calibration_status starts out NULL at INSERT time), so
passing None there is a no-op on an already-empty column -- the new task row
ends up with calibration_status=NULL even though the cache (llm_status.json)
already has "full" from an earlier run. /api/audit/history then reports
calibration_status: null for a task whose cache is actually fully calibrated.

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


def _seed_prior_full_calibration(cm):
    """Simulate a prior full-flow run: cache already has a REAL calibrated
    layer with calibration_status=full persisted, but no summary yet."""
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
        llm_type="calibrated", content="REAL calibrated text from a genuine LLM pass",
    )
    cm.save_llm_status(
        platform="youtube", media_id="vid1", use_speaker_recognition=False,
        calibration_status=CalibrationStatus.FULL,
        calibration_stats={"total_segments": 3},
        summary_status=None,
    )


def _summary_only_backfill_task(task_id):
    """Mirrors the queued_processing_options built by transcription.py's
    partial-cache-hit branch when only the summary layer is missing:
    calibrate=False (reuse existing calibrated text), summarize=True."""
    return {
        "task_id": task_id,
        "url": "https://example.com/v1",
        "display_url": "https://example.com/v1",
        "platform": "youtube",
        "media_id": "vid1",
        "video_title": "Demo",
        "author": "Alice",
        "description": "",
        "transcript": "REAL calibrated text from a genuine LLM pass",
        "use_speaker_recognition": False,
        "transcription_data": None,
        "is_generic": False,
        "wechat_webhook": None,
        "notification_channel": None,
        "notification_webhooks": {},
        "processing_options": {"calibrate": False, "summarize": True},
    }


def _patches(cm, coordinator):
    """Patch only the true external I/O boundaries (LLM coordinator, queue,
    notifications) -- cache_manager and _save_llm_results stay REAL so the
    layered-cache suppression + llm_status.json merge semantics actually run,
    which is exactly what this bug lives in."""
    return [
        patch.object(llm_ops, "cache_manager", cm),
        patch.object(llm_ops, "llm_coordinator", coordinator),
        patch.object(llm_ops, "llm_task_queue", MagicMock()),
        patch.object(llm_ops, "_send_notification", MagicMock()),
        patch.object(llm_ops, "get_notification_router", lambda: MagicMock()),
        patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
        patch.object(llm_ops, "_prepare_llm_content", lambda t, tr, spk: tr),
    ]


class TestSummaryBackfillPreservesCalibrationStatus:
    def test_task_status_row_keeps_full_calibration_after_summary_only_backfill(self, cm):
        _seed_prior_full_calibration(cm)
        task_id = cm.create_task(
            url="https://example.com/v1", platform="youtube", media_id="vid1",
        )["task_id"]
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)

        coordinator = MagicMock()
        # Real coordinator.process(skip_calibration=True) behavior: calibration_status
        # comes back DISABLED (this round didn't touch calibration), a real summary
        # is produced.
        coordinator.process.return_value = {
            "calibrated_text": "disabled placeholder text",
            "summary_text": "a real fresh summary",
            "stats": {
                "calibration_status": CalibrationStatus.DISABLED,
                "summary_status": SummaryStatus.GENERATED,
            },
            "models_used": {},
        }

        ctxs = _patches(cm, coordinator)
        for c in ctxs:
            c.start()
        try:
            llm_ops._handle_llm_task(_summary_only_backfill_task(task_id))
        finally:
            for c in ctxs:
                c.stop()

        row = cm.get_task_by_id(task_id)
        assert row["status"] == "success"
        # The bug: this used to come back NULL because the suppressed layer's
        # effective_status (None, "preserve old value") was copied verbatim
        # into a brand-new task row that started out NULL.
        assert row["calibration_status"] == CalibrationStatus.FULL
        assert row["summary_status"] == SummaryStatus.GENERATED

        # The cache-level llm_status.json itself was never the broken part
        # (save_llm_status's merge semantics already preserve it) -- assert
        # it too, as a sanity check that the fix reads from the right source.
        cache_data = cm.get_cache("youtube", "vid1", use_speaker_recognition=False)
        assert cache_data["llm_status"]["calibration_status"] == CalibrationStatus.FULL
        assert cache_data["llm_status"]["summary_status"] == SummaryStatus.GENERATED
