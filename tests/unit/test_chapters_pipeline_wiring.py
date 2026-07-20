"""Unit tests for T6 chapters pipeline wiring.

Covers:
- chapters normalize / title gate (R7)
- need_chapters layer satisfaction matrix (R3)
- _save_llm_results chapters merge / suppress / GENERATED file write
- recalibrate force-recompute when prior GENERATED (R6)

Console output must be English only (no emoji, no Chinese).
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from video_transcript_api.api.processing_options import normalize_processing_options
from video_transcript_api.api.services import llm_ops
from video_transcript_api.utils.llm_status import ChaptersStatus, SummaryStatus


# ---------------------------------------------------------------------------
# R7: chapters alone must not require title LLM
# ---------------------------------------------------------------------------

class TestRequiresLlmTitleChapters:
    def test_chapters_only_does_not_require_title(self):
        assert (
            llm_ops._requires_llm_title(
                {
                    "calibrate": False,
                    "summarize": False,
                    "infer_speaker_names": False,
                    "chapters": True,
                },
                use_speaker_recognition=False,
            )
            is False
        )

    def test_summarize_still_requires_title(self):
        assert (
            llm_ops._requires_llm_title(
                {
                    "calibrate": False,
                    "summarize": True,
                    "infer_speaker_names": False,
                    "chapters": False,
                },
                use_speaker_recognition=False,
            )
            is True
        )


# ---------------------------------------------------------------------------
# R3: need_chapters satisfaction matrix (pure helper logic mirror)
# ---------------------------------------------------------------------------

def _chapters_layer_satisfied(cached_status, has_file: bool) -> bool:
    """Mirror of transcription.py need_chapters satisfaction table."""
    if cached_status == ChaptersStatus.GENERATED and has_file:
        return True
    if cached_status in (
        ChaptersStatus.SKIPPED_SHORT,
        ChaptersStatus.SKIPPED_NO_TIMELINE,
    ):
        return True
    return False


class TestNeedChaptersMatrix:
    @pytest.mark.parametrize(
        "status,has_file,expected",
        [
            (ChaptersStatus.GENERATED, True, True),
            (ChaptersStatus.GENERATED, False, False),  # status/file mismatch
            (ChaptersStatus.SKIPPED_SHORT, False, True),
            (ChaptersStatus.SKIPPED_NO_TIMELINE, False, True),
            (ChaptersStatus.FAILED, False, False),
            (ChaptersStatus.DISABLED, False, False),
            (None, False, False),
            (None, True, False),  # file without status
            (ChaptersStatus.FAILED, True, False),
        ],
    )
    def test_layer_satisfaction(self, status, has_file, expected):
        assert _chapters_layer_satisfied(status, has_file) is expected

    def test_need_chapters_false_when_satisfied_and_requested(self):
        requested = True
        satisfied = _chapters_layer_satisfied(ChaptersStatus.GENERATED, True)
        assert (requested and not satisfied) is False

    def test_need_chapters_true_when_failed_and_requested(self):
        requested = True
        satisfied = _chapters_layer_satisfied(ChaptersStatus.FAILED, False)
        assert (requested and not satisfied) is True

    def test_need_chapters_false_when_not_requested(self):
        requested = False
        satisfied = _chapters_layer_satisfied(ChaptersStatus.FAILED, False)
        assert (requested and not satisfied) is False


# ---------------------------------------------------------------------------
# _save_llm_results chapters branches
# ---------------------------------------------------------------------------

class TestSaveLlmResultsChapters:
    def _patch_cm(self, monkeypatch, *, snapshot=None, old_status=None):
        mock_cm = MagicMock()
        mock_cm.media_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_cm.media_lock.return_value.__exit__ = MagicMock(return_value=False)
        mock_cm.get_cache.return_value = snapshot or {
            "file_path": "/tmp/cache/x",
        }
        mock_cm.invalidate_llm_status.return_value = old_status or {}
        mock_cm.save_llm_result.return_value = True
        monkeypatch.setattr(llm_ops, "cache_manager", mock_cm)
        return mock_cm

    def _base_result(self, **overrides):
        d = {
            "校对文本": "calibrated body",
            "内容总结": "summary body",
            "skip_summary": False,
            "summary_status": SummaryStatus.GENERATED,
            "stats": {"calibration_status": "full"},
            "models_used": {},
            "calibrate_success": True,
            "summary_success": True,
        }
        d.update(overrides)
        return d

    def test_generated_writes_chapters_file_and_status(self, monkeypatch):
        mock_cm = self._patch_cm(monkeypatch)
        chapters = [
            {
                "index": 0,
                "title": "Intro",
                "gist": "Hello",
                "start_seg": 0,
                "end_seg": 2,
                "start_time": 0.0,
                "end_time": 10.0,
            }
        ]
        result = llm_ops._save_llm_results(
            task_id="t1",
            platform="youtube",
            media_id="m1",
            use_speaker_recognition=False,
            result_dict=self._base_result(
                chapters_status=ChaptersStatus.GENERATED,
                chapters=chapters,
                chapters_fingerprint="fp1",
                chapters_segment_count=3,
                chapters_source_kind="segments",
            ),
            calibrate_only=False,
            processing_options={
                "calibrate": True,
                "summarize": True,
                "infer_speaker_names": True,
                "chapters": True,
            },
        )
        chapters_calls = [
            c
            for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "chapters"
        ]
        assert len(chapters_calls) == 1
        payload = chapters_calls[0].kwargs["content"]
        assert payload["format_version"] == "v1"
        assert payload["chapters"][0]["start_seg"] == 0
        assert payload["source"]["fingerprint"] == "fp1"
        assert result["chapters_status"] == ChaptersStatus.GENERATED
        assert (
            mock_cm.save_llm_status.call_args.kwargs["chapters_status"]
            == ChaptersStatus.GENERATED
        )

    def test_skipped_does_not_write_chapters_file(self, monkeypatch):
        mock_cm = self._patch_cm(monkeypatch)
        llm_ops._save_llm_results(
            task_id="t2",
            platform="youtube",
            media_id="m1",
            use_speaker_recognition=False,
            result_dict=self._base_result(
                chapters_status=ChaptersStatus.SKIPPED_SHORT,
                chapters=[],
            ),
            calibrate_only=False,
            processing_options=normalize_processing_options(
                {"chapters": True, "summarize": True}
            ),
        )
        chapters_calls = [
            c
            for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "chapters"
        ]
        assert chapters_calls == []
        assert (
            mock_cm.save_llm_status.call_args.kwargs["chapters_status"]
            == ChaptersStatus.SKIPPED_SHORT
        )

    def test_suppress_does_not_overwrite_generated(self, monkeypatch):
        """Existing GENERATED + chapters=false must not rewrite file/status."""
        old = {"chapters_status": ChaptersStatus.GENERATED, "summary_status": "generated"}
        snapshot = {
            "file_path": "/tmp/cache/x",
            "llm_calibrated": "old",
            "llm_summary": "old sum",
            "llm_chapters": {"chapters": [{"title": "Old"}]},
            "llm_status": old,
        }
        mock_cm = self._patch_cm(monkeypatch, snapshot=snapshot, old_status=old)
        result = llm_ops._save_llm_results(
            task_id="t3",
            platform="youtube",
            media_id="m1",
            use_speaker_recognition=False,
            result_dict=self._base_result(
                chapters_status=None,
                chapters=[],
            ),
            calibrate_only=False,
            processing_options={
                "calibrate": True,
                "summarize": True,
                "infer_speaker_names": True,
                "chapters": False,
            },
        )
        chapters_calls = [
            c
            for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "chapters"
        ]
        assert chapters_calls == []
        # effective None => preserve; final passed to save_llm_status is old GENERATED
        assert result["chapters_status"] is None
        assert (
            mock_cm.save_llm_status.call_args.kwargs["chapters_status"]
            == ChaptersStatus.GENERATED
        )
        # calibration/summary still written (not wiped)
        assert mock_cm.save_llm_status.call_args.kwargs["summary_status"] is not None

    def test_disabled_when_no_prior_and_not_requested(self, monkeypatch):
        mock_cm = self._patch_cm(monkeypatch, old_status={})
        result = llm_ops._save_llm_results(
            task_id="t4",
            platform="youtube",
            media_id="m1",
            use_speaker_recognition=False,
            result_dict=self._base_result(chapters_status=None, chapters=[]),
            calibrate_only=False,
            processing_options={
                "calibrate": True,
                "summarize": True,
                "infer_speaker_names": True,
                "chapters": False,
            },
        )
        assert result["chapters_status"] == ChaptersStatus.DISABLED
        assert (
            mock_cm.save_llm_status.call_args.kwargs["chapters_status"]
            == ChaptersStatus.DISABLED
        )

    def test_save_does_not_wipe_calibration_summary_when_writing_chapters(
        self, monkeypatch
    ):
        mock_cm = self._patch_cm(
            monkeypatch,
            old_status={
                "calibration_status": "full",
                "summary_status": "generated",
            },
        )
        llm_ops._save_llm_results(
            task_id="t5",
            platform="youtube",
            media_id="m1",
            use_speaker_recognition=False,
            result_dict=self._base_result(
                chapters_status=ChaptersStatus.GENERATED,
                chapters=[{"index": 0, "title": "A", "gist": "g", "start_seg": 0, "end_seg": 1, "start_time": 0, "end_time": 1}],
                chapters_fingerprint="x",
                chapters_segment_count=2,
                chapters_source_kind="dialogs",
            ),
            calibrate_only=False,
            processing_options=normalize_processing_options(None),
        )
        kwargs = mock_cm.save_llm_status.call_args.kwargs
        assert kwargs["calibration_status"] == "full"
        assert kwargs["summary_status"] == SummaryStatus.GENERATED
        assert kwargs["chapters_status"] == ChaptersStatus.GENERATED


class TestRecalibrateForceChapters:
    def test_prior_generated_forces_skip_chapters_false(self, monkeypatch):
        """Simulate the flag logic used in _handle_llm_task for R6."""
        # This unit-tests the decision table without spinning the full queue.
        old_status = ChaptersStatus.GENERATED
        chapters_requested = True
        force = old_status == ChaptersStatus.GENERATED
        skip = not chapters_requested and not force
        if force:
            skip = False
        assert skip is False

    def test_prior_skipped_does_not_force(self):
        old_status = ChaptersStatus.SKIPPED_SHORT
        chapters_requested = False
        force = old_status == ChaptersStatus.GENERATED
        skip = not chapters_requested and not force
        if force:
            skip = False
        assert skip is True


# ---------------------------------------------------------------------------
# Recalibrate chapters gate through the real _handle_llm_task path
# ---------------------------------------------------------------------------

class TestRecalibrateChaptersSkipGate:
    """Assert the skip_chapters flag the coordinator actually receives.

    The recalibrate route (tasks.py) builds llm_task with calibrate_only=True
    and processing_options=normalize_processing_options(None), so
    chapters_requested is always True on that path and must not decide the
    chapters gate. Only a prior GENERATED status forces recompute; any other
    prior state (SKIPPED_*/FAILED/DISABLED/no status) must reach the
    coordinator with skip_chapters=True to avoid unbudgeted LLM spend.
    """

    def _run_handle_llm_task(self, monkeypatch, *, old_status=None,
                             task_overrides=None):
        captured = {}

        mock_cm = MagicMock()
        mock_cm.media_lock.return_value.__enter__ = MagicMock(return_value=None)
        mock_cm.media_lock.return_value.__exit__ = MagicMock(return_value=False)
        llm_status = {}
        if old_status is not None:
            llm_status["chapters_status"] = old_status
        mock_cm.get_cache.return_value = {
            # no file_path -> _should_backfill_summary deterministically False
            "llm_status": llm_status,
            "llm_processed": {
                "dialogs": [
                    {"speaker": "A", "start_time": 0.0, "end_time": 1.0,
                     "text": "x"},
                ],
            },
        }
        mock_cm.invalidate_llm_status.return_value = {}
        mock_cm.save_llm_result.return_value = True
        mock_cm.save_llm_status.return_value = True
        mock_cm.get_task_by_id.return_value = {"view_token": "tok"}
        mock_cm.update_task_status.return_value = True

        def fake_process(**kwargs):
            captured.update(kwargs)
            return {
                "calibrated_text": "calibrated",
                "summary_text": "summary",
                "stats": {
                    "calibration_status": "full",
                    "summary_status": SummaryStatus.GENERATED,
                },
                "models_used": {},
            }

        mock_coordinator = MagicMock()
        mock_coordinator.process = fake_process

        class _FakeQueue:
            def task_done(self):
                pass

        class _FakeRouter:
            def send_text(self, content, **kwargs):
                return {"fake": True}

            def send_long_text(self, **kwargs):
                return {"fake": True}

            def notify_task_status(self, **kwargs):
                return {"fake": True}

        @contextmanager
        def noop_lock(task_id):
            yield

        monkeypatch.setattr(llm_ops, "llm_task_queue", _FakeQueue())
        monkeypatch.setattr(llm_ops, "cache_manager", mock_cm)
        monkeypatch.setattr(llm_ops, "llm_coordinator", mock_coordinator)
        monkeypatch.setattr(llm_ops, "task_lock", noop_lock)
        monkeypatch.setattr(
            llm_ops, "get_notification_router", lambda: _FakeRouter()
        )
        monkeypatch.setattr(llm_ops, "get_base_url", lambda: "https://fake-base")

        llm_task = {
            # Mirrors the recalibrate route payload (tasks.py): calibrate_only
            # + normalized default processing_options (chapters_requested is
            # therefore always True on this path).
            "task_id": "task-recal",
            "url": "https://example.com/v/1",
            "platform": "youtube",
            "media_id": "vid-1",
            "video_title": "Video 1",
            "author": "author",
            "description": "desc",
            "transcript": "transcript",
            "use_speaker_recognition": False,
            "transcription_data": None,
            "wechat_webhook": None,
            "notification_webhooks": {},
            "is_generic": False,
            "calibrate_only": True,
            "processing_options": normalize_processing_options(None),
        }
        if task_overrides:
            llm_task.update(task_overrides)

        llm_ops._handle_llm_task(llm_task)
        return captured

    @pytest.mark.parametrize(
        "old_status",
        [
            ChaptersStatus.SKIPPED_SHORT,
            ChaptersStatus.SKIPPED_NO_TIMELINE,
            ChaptersStatus.FAILED,
            ChaptersStatus.DISABLED,
            None,
        ],
    )
    def test_recalibrate_skips_chapters_unless_prior_generated(
        self, monkeypatch, old_status
    ):
        captured = self._run_handle_llm_task(monkeypatch, old_status=old_status)
        assert captured["skip_chapters"] is True
        assert captured["timeline_segments"] is None

    def test_recalibrate_prior_generated_forces_recompute(self, monkeypatch):
        captured = self._run_handle_llm_task(
            monkeypatch, old_status=ChaptersStatus.GENERATED
        )
        assert captured["skip_chapters"] is False
        # chapters actually runs: timeline seed resolved from cached dialogs
        assert captured["timeline_segments"] == [
            {"speaker": "A", "start_time": 0.0, "end_time": 1.0, "text": "x"},
        ]

    def test_non_recalibrate_chapters_requested_still_runs(self, monkeypatch):
        captured = self._run_handle_llm_task(
            monkeypatch, task_overrides={"calibrate_only": False}
        )
        assert captured["skip_chapters"] is False

    def test_non_recalibrate_chapters_not_requested_still_skips(
        self, monkeypatch
    ):
        captured = self._run_handle_llm_task(
            monkeypatch,
            task_overrides={
                "calibrate_only": False,
                "processing_options": normalize_processing_options(
                    {"chapters": False}
                ),
            },
        )
        assert captured["skip_chapters"] is True


class TestBuildResultDictChapters:
    def test_chapters_fields_pass_through(self):
        result = llm_ops._build_result_dict(
            {
                "calibrated_text": "c",
                "summary_text": "s",
                "stats": {
                    "chapters_status": ChaptersStatus.GENERATED,
                    "chapters": [{"index": 0, "title": "T", "gist": "g", "start_seg": 0, "end_seg": 1, "start_time": 0.0, "end_time": 1.0}],
                    "chapters_fingerprint": "fp",
                    "chapters_segment_count": 2,
                    "chapters_source_kind": "dialogs",
                },
                "models_used": {},
            }
        )
        assert result["chapters_status"] == ChaptersStatus.GENERATED
        assert result["chapters"][0]["title"] == "T"
        assert result["chapters_fingerprint"] == "fp"


class TestRealCacheManagerChaptersRoundtrip:
    """End-to-end file write via real CacheManager + _save_llm_results."""

    def test_generated_persists_llm_chapters_json(self, tmp_path, monkeypatch):
        from video_transcript_api.cache.cache_manager import CacheManager

        cm = CacheManager(cache_dir=str(tmp_path / "cache"))
        monkeypatch.setattr(llm_ops, "cache_manager", cm)
        cm.save_cache(
            platform="youtube",
            url="https://example.com/v",
            media_id="vid-chapters",
            use_speaker_recognition=False,
            transcript_data="hello world transcript",
            transcript_type="capswriter",
            title="T",
            author="A",
            description="",
        )
        chapters = [
            {
                "index": 0,
                "title": "Opening",
                "gist": "Starts here.",
                "start_seg": 0,
                "end_seg": 3,
                "start_time": 0.0,
                "end_time": 42.0,
            }
        ]
        effective = llm_ops._save_llm_results(
            task_id="task-ch",
            platform="youtube",
            media_id="vid-chapters",
            use_speaker_recognition=False,
            result_dict={
                "校对文本": "calibrated",
                "内容总结": "summary",
                "skip_summary": False,
                "summary_status": SummaryStatus.GENERATED,
                "chapters_status": ChaptersStatus.GENERATED,
                "chapters": chapters,
                "chapters_fingerprint": "fp-real",
                "chapters_segment_count": 4,
                "chapters_source_kind": "segments",
                "stats": {"calibration_status": "full"},
                "models_used": {},
                "calibrate_success": True,
                "summary_success": True,
            },
            calibrate_only=False,
            processing_options=normalize_processing_options(None),
        )
        assert effective["chapters_status"] == ChaptersStatus.GENERATED
        cached = cm.get_cache(
            platform="youtube", media_id="vid-chapters", use_speaker_recognition=False
        )
        assert "llm_chapters" in cached
        assert cached["llm_chapters"]["format_version"] == "v1"
        assert cached["llm_chapters"]["chapters"][0]["start_seg"] == 0
        assert cached["llm_status"]["chapters_status"] == ChaptersStatus.GENERATED
        on_disk = Path(cached["file_path"]) / "llm_chapters.json"
        assert on_disk.exists()
        cm.close()


class TestResolveChaptersTimeline:
    def test_prefers_task_timeline_segments(self, monkeypatch):
        mock_cm = MagicMock()
        monkeypatch.setattr(llm_ops, "cache_manager", mock_cm)
        segs, kind = llm_ops._resolve_chapters_timeline_segments(
            llm_task={"timeline_segments": [{"text": "a", "start_time": 0}]},
            platform="youtube",
            media_id="m",
            use_speaker_recognition=False,
        )
        assert kind == "segments"
        assert segs[0]["text"] == "a"
        mock_cm.get_cache.assert_not_called()

    def test_uses_cached_dialogs(self, monkeypatch):
        mock_cm = MagicMock()
        mock_cm.get_cache.return_value = {
            "llm_processed": {
                "dialogs": [{"text": "d0", "start_time": "00:00:01"}],
            },
            "file_path": "/tmp/x",
        }
        monkeypatch.setattr(llm_ops, "cache_manager", mock_cm)
        segs, kind = llm_ops._resolve_chapters_timeline_segments(
            llm_task={},
            platform="youtube",
            media_id="m",
            use_speaker_recognition=True,
        )
        assert kind == "cached_dialogs"
        assert segs[0]["text"] == "d0"
