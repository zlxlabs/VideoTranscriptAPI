"""Concurrency regression test for _save_llm_results' layered-cache suppress
check (codex-review R3 finding #1).

Background: _save_llm_results decides whether to suppress writing a layer
(e.g. calibration) by snapshotting cache_manager.get_cache() *before* it
writes anything. When two tasks for the SAME (platform, media_id) run
concurrently with different processing_options, this "check -> write ->
merge status" sequence must be atomic per media, otherwise:

  Task A (processing_options={"calibrate": False, "summarize": True}) takes
  a snapshot showing no calibration layer exists yet, then -- before it
  writes -- Task B (processing_options={"calibrate": True, "summarize":
  True}) runs completely and writes a REAL calibrated result. Task A then
  resumes using its now-stale snapshot and, believing the layer still does
  not exist, overwrites B's real calibration with its own disabled
  placeholder text, and downgrades the persisted calibration_status to
  DISABLED -- destroying the layered cache's "artifacts only grow, never
  shrink" invariant.

The test uses a real CacheManager (tmp_path-backed) so the suppress check
exercises real file I/O, and monkeypatches get_cache to deterministically
delay task A right after it takes its snapshot -- giving task B a
controlled window to run its full (otherwise near-instant) write before A
resumes. This turns an inherently racy bug into a reliably reproducible
one without relying on raw OS thread scheduling luck.

All console output must be in English only (no emoji, no Chinese).
"""
import json
import threading
import time
from pathlib import Path

import pytest

from video_transcript_api.api.services import llm_ops as llm_ops_module
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.llm_status import CalibrationStatus, SummaryStatus


@pytest.fixture
def real_cm(tmp_path, monkeypatch):
    """A real CacheManager wired into llm_ops as the module-level cache_manager,
    with a single pre-existing cache entry (transcript only, no LLM layers yet)
    for platform=youtube / media_id=vidRace.
    """
    cm = CacheManager(cache_dir=str(tmp_path / "cache"))
    cm.save_cache(
        platform="youtube",
        url="https://example.com/vidRace",
        media_id="vidRace",
        use_speaker_recognition=False,
        transcript_data="raw transcript text",
        transcript_type="capswriter",
        title="t",
        author="a",
        description="d",
    )
    monkeypatch.setattr(llm_ops_module, "cache_manager", cm)
    yield cm
    cm.close()


def _result_dict(calibrated_text, summary_text, calibration_status):
    return {
        "校对文本": calibrated_text,
        "内容总结": summary_text,
        "skip_summary": False,
        "summary_status": SummaryStatus.GENERATED,
        "stats": {"calibration_status": calibration_status},
        "models_used": {},
        "calibrate_success": True,
        "summary_success": True,
    }


class TestSaveLLMResultsConcurrentSuppressCheck:
    def test_stale_suppress_snapshot_must_not_clobber_concurrent_real_write(
        self, real_cm, monkeypatch
    ):
        a_thread_id = {}
        a_took_snapshot = threading.Event()
        b_started = threading.Event()

        orig_get_cache = real_cm.get_cache

        def patched_get_cache(*args, **kwargs):
            # Only the FIRST call made by task A's thread is the layered-cache
            # "does this layer already exist" snapshot check; capture its
            # result before injecting the delay so it reflects genuinely
            # pre-B-write state (the real race condition), not a stalled read.
            result = orig_get_cache(*args, **kwargs)
            if threading.get_ident() == a_thread_id.get("id") and not a_took_snapshot.is_set():
                a_took_snapshot.set()
                # Give task B a deterministic window to start; then a fixed
                # grace period for B's (fast, local-file-only) write to fully
                # complete before task A is allowed to resume.
                assert b_started.wait(timeout=2), "task B never started"
                time.sleep(0.3)
            return result

        monkeypatch.setattr(real_cm, "get_cache", patched_get_cache)

        def run_task_a():
            a_thread_id["id"] = threading.get_ident()
            llm_ops_module._save_llm_results(
                task_id="taskA",
                platform="youtube",
                media_id="vidRace",
                use_speaker_recognition=False,
                result_dict=_result_dict(
                    "DISABLED PLACEHOLDER (formatted raw text)",
                    "summary from A",
                    CalibrationStatus.DISABLED,
                ),
                calibrate_only=False,
                summary_backfill=False,
                processing_options={"calibrate": False, "summarize": True},
            )

        def run_task_b():
            assert a_took_snapshot.wait(timeout=2), "task A never took its snapshot"
            b_started.set()
            llm_ops_module._save_llm_results(
                task_id="taskB",
                platform="youtube",
                media_id="vidRace",
                use_speaker_recognition=False,
                result_dict=_result_dict(
                    "REAL CALIBRATED TEXT FROM B",
                    "summary from B",
                    CalibrationStatus.FULL,
                ),
                calibrate_only=False,
                summary_backfill=False,
                processing_options={"calibrate": True, "summarize": True},
            )

        thread_a = threading.Thread(target=run_task_a)
        thread_b = threading.Thread(target=run_task_b)
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)
        assert not thread_a.is_alive(), "task A worker did not finish"
        assert not thread_b.is_alive(), "task B worker did not finish"

        cache_data = real_cm.get_cache(
            platform="youtube", media_id="vidRace", use_speaker_recognition=False
        )
        assert cache_data["llm_calibrated"] == "REAL CALIBRATED TEXT FROM B", (
            "task A's stale-snapshot write must not clobber task B's real "
            "calibrated result"
        )

        status_file = Path(cache_data["file_path"]) / "llm_status.json"
        status = json.loads(status_file.read_text(encoding="utf-8"))
        assert status["calibration_status"] != CalibrationStatus.DISABLED, (
            "persisted calibration_status must not be downgraded to disabled "
            "by the stale-snapshot task"
        )
        assert status["calibration_status"] == CalibrationStatus.FULL
