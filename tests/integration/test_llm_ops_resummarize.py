"""Integration test: /api/resummarize's summary-only llm_task must regenerate
ONLY the summary layer -- the already-persisted calibration/chapters layers
(files AND llm_status.json values) must stay untouched.

The llm_task built here mirrors exactly what routes/tasks.py::resummarize
puts on the LLM queue (fixed processing_options={"calibrate": False,
"summarize": True, "infer_speaker_names": False, "chapters": False},
transcript taken from the cached llm_calibrated text, transcription_data=
None, no calibrate_only flag). _handle_llm_task/_save_llm_results need no
changes for this: suppress_calibration = calibrated_exists_before and not
calibrate_requested protects the calibrated layer, and llm_status.json's
merge-on-write semantics preserve the untouched layers' old values.

All console output must be in English only (no emoji, no Chinese).
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.task_status import TaskStatus
from src.video_transcript_api.utils.llm_status import (
    CalibrationStatus,
    ChaptersStatus,
    SummaryStatus,
)
from src.video_transcript_api.api.services import llm_ops


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _seed_prior_run_with_failed_summary(cm):
    """Simulate a prior full-flow run: real calibrated layer + chapters both
    persisted with honest statuses, but the summary layer FAILED (the exact
    production incident /api/resummarize exists to repair)."""
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
        summary_status=SummaryStatus.FAILED,
        chapters_status=ChaptersStatus.GENERATED,
    )


def _resummarize_task(task_id):
    """Mirrors the llm_task built by routes/tasks.py::resummarize:
    summary-only processing_options, transcript from the cached
    llm_calibrated text, transcription_data=None, no calibrate_only."""
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
        "cached_speaker_count": None,
        "is_generic": False,
        "wechat_webhook": None,
        "notification_channel": None,
        "notification_webhooks": {},
        "processing_options": {
            "calibrate": False,
            "summarize": True,
            "infer_speaker_names": False,
            "chapters": False,
        },
    }


def _patches(cm, coordinator):
    """Patch only the true external I/O boundaries (LLM coordinator, queue,
    notifications) -- cache_manager and _save_llm_results stay REAL so the
    layered-cache suppression + llm_status.json merge semantics actually run,
    which is exactly what this test asserts."""
    return [
        patch.object(llm_ops, "cache_manager", cm),
        patch.object(llm_ops, "llm_coordinator", coordinator),
        patch.object(llm_ops, "llm_task_queue", MagicMock()),
        patch.object(llm_ops, "_send_notification", MagicMock()),
        patch.object(llm_ops, "get_notification_router", lambda: MagicMock()),
        patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
        patch.object(llm_ops, "_prepare_llm_content", lambda t, tr, spk: tr),
    ]


class TestResummarizeOnlyTouchesSummaryLayer:
    def test_summary_regenerated_calibrated_layer_untouched(self, cm):
        _seed_prior_run_with_failed_summary(cm)
        task_id = cm.create_task(
            url="https://example.com/v1", platform="youtube", media_id="vid1",
        )["task_id"]
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)

        cache_data = cm.get_cache("youtube", "vid1", use_speaker_recognition=False)
        calibrated_path = os.path.join(cache_data["file_path"], "llm_calibrated.txt")
        with open(calibrated_path, "r", encoding="utf-8") as f:
            calibrated_before = f.read()
        calibrated_mtime_before = os.path.getmtime(calibrated_path)

        coordinator = MagicMock()
        # Real coordinator.process(skip_calibration=True) behavior: calibration
        # comes back DISABLED (this round didn't touch it), a real summary is
        # produced. Chapters were not requested this round.
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
            llm_ops._handle_llm_task(_resummarize_task(task_id))
        finally:
            for c in ctxs:
                c.stop()

        row = cm.get_task_by_id(task_id)
        assert row["status"] == "success"

        # Summary layer: regenerated on disk and in llm_status.json.
        cache_data = cm.get_cache("youtube", "vid1", use_speaker_recognition=False)
        summary_path = os.path.join(cache_data["file_path"], "llm_summary.txt")
        with open(summary_path, "r", encoding="utf-8") as f:
            assert f.read() == "a real fresh summary"
        assert cache_data["llm_status"]["summary_status"] == SummaryStatus.GENERATED

        # Calibrated layer: file content AND mtime untouched -- the
        # placeholder text from this skip-calibration round must never be
        # written over the real calibrated text.
        with open(calibrated_path, "r", encoding="utf-8") as f:
            assert f.read() == calibrated_before
        assert os.path.getmtime(calibrated_path) == calibrated_mtime_before

        # llm_status.json merge semantics: the untouched layers keep their
        # old values, only summary_status moves failed -> generated.
        assert cache_data["llm_status"]["calibration_status"] == CalibrationStatus.FULL
        assert cache_data["llm_status"]["chapters_status"] == ChaptersStatus.GENERATED
