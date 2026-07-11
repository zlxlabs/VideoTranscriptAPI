"""
llm_ops helper function unit tests.

Covers:
- _generate_title_if_needed
- _prepare_llm_content
- _build_result_dict
- _build_calibration_warning
- _sanitize_title

All console output must be in English only (no emoji, no Chinese).
"""

import pytest
from unittest.mock import MagicMock

from video_transcript_api.api.services.llm_ops import (
    _prepare_llm_content,
    _build_result_dict,
    _build_calibration_warning,
    _should_backfill_summary,
    _save_llm_results,
)
from video_transcript_api.utils.llm_status import CalibrationStatus, SummaryStatus


class TestPrepareLLMContent:
    """Test _prepare_llm_content."""

    def test_plain_text_without_speaker(self):
        """No speaker recognition should return transcript as-is."""
        result = _prepare_llm_content(
            llm_task={"transcription_data": None},
            transcript="hello world",
            use_speaker_recognition=False,
        )
        assert result == "hello world"

    def test_speaker_with_dict_segments(self):
        """Dict transcription_data should extract segments."""
        result = _prepare_llm_content(
            llm_task={"transcription_data": {"segments": [{"text": "hi"}]}},
            transcript="fallback",
            use_speaker_recognition=True,
        )
        assert result == [{"text": "hi"}]

    def test_speaker_with_list_data(self):
        """List transcription_data should be used directly."""
        data = [{"text": "a"}, {"text": "b"}]
        result = _prepare_llm_content(
            llm_task={"transcription_data": data},
            transcript="fallback",
            use_speaker_recognition=True,
        )
        assert result == data

    def test_speaker_with_unexpected_type_falls_back(self):
        """Unexpected type should fall back to transcript text."""
        result = _prepare_llm_content(
            llm_task={"transcription_data": "unexpected string"},
            transcript="fallback text",
            use_speaker_recognition=True,
        )
        assert result == "fallback text"

    def test_speaker_without_data(self):
        """No transcription_data should return transcript."""
        result = _prepare_llm_content(
            llm_task={},
            transcript="text only",
            use_speaker_recognition=True,
        )
        assert result == "text only"


class TestBuildResultDict:
    """Test _build_result_dict."""

    def test_basic_result(self):
        """Should build result dict from coordinator result."""
        coordinator_result = {
            "calibrated_text": "calibrated",
            "summary_text": "summary",
            "stats": {"original_length": 100},
            "models_used": {"calibrate_model": "test"},
        }
        result = _build_result_dict(coordinator_result)
        assert result["calibrate_success"] is True
        assert result["summary_success"] is True
        assert result["skip_summary"] is False

    def test_no_summary(self):
        """None summary should set skip_summary=True."""
        coordinator_result = {
            "calibrated_text": "cal",
            "summary_text": None,
            "stats": {},
            "models_used": {},
        }
        result = _build_result_dict(coordinator_result)
        assert result["skip_summary"] is True
        assert result["summary_success"] is False

    def test_structured_data_included(self):
        """structured_data should be passed through."""
        coordinator_result = {
            "calibrated_text": "cal",
            "summary_text": "sum",
            "stats": {},
            "models_used": {},
            "structured_data": {"key": "value"},
        }
        result = _build_result_dict(coordinator_result)
        assert result["structured_data"] == {"key": "value"}

    def test_calibration_status_none_marks_calibrate_failure(self):
        """calibrate_success is now derived from calibration_status: when the
        whole calibration degraded to raw fallback (NONE), it must be False
        instead of the old hardcoded True."""
        coordinator_result = {
            "calibrated_text": "raw fallback text",
            "summary_text": "summary",
            "stats": {"calibration_status": CalibrationStatus.NONE},
            "models_used": {},
        }
        result = _build_result_dict(coordinator_result)
        assert result["calibrate_success"] is False

    def test_calibration_status_partial_still_counts_as_success(self):
        """PARTIAL still means some real calibration happened -> calibrate_success stays True."""
        coordinator_result = {
            "calibrated_text": "partially calibrated text",
            "summary_text": "summary",
            "stats": {"calibration_status": CalibrationStatus.PARTIAL},
            "models_used": {},
        }
        result = _build_result_dict(coordinator_result)
        assert result["calibrate_success"] is True

    def test_summary_status_passed_through(self):
        """summary_status from coordinator stats must be surfaced on the result dict."""
        coordinator_result = {
            "calibrated_text": "cal",
            "summary_text": None,
            "stats": {"summary_status": SummaryStatus.FAILED},
            "models_used": {},
        }
        result = _build_result_dict(coordinator_result)
        assert result["summary_status"] == SummaryStatus.FAILED


class TestBuildCalibrationWarning:
    """Test _build_calibration_warning."""

    def test_no_stats(self):
        """No calibration_stats should return empty string."""
        assert _build_calibration_warning({}) == ""

    def test_all_success(self):
        """All success should return empty string."""
        stats = {
            "calibration_stats": {
                "total_chunks": 5,
                "success_count": 5,
                "failed_count": 0,
                "fallback_count": 0,
            }
        }
        assert _build_calibration_warning(stats) == ""

    def test_total_failure(self):
        """All chunks failed should return total failure warning."""
        stats = {
            "calibration_stats": {
                "total_chunks": 3,
                "success_count": 0,
                "failed_count": 3,
                "fallback_count": 0,
            }
        }
        warning = _build_calibration_warning(stats)
        assert "completely failed" in warning.lower() or "完全失败" in warning

    def test_partial_failure(self):
        """Some failures should return partial warning."""
        stats = {
            "calibration_stats": {
                "total_chunks": 5,
                "success_count": 3,
                "failed_count": 1,
                "fallback_count": 1,
            }
        }
        warning = _build_calibration_warning(stats)
        assert "3/5" in warning
        assert "1" in warning  # fallback count

    def test_plain_text_shape_all_success(self):
        """Plain-text (segment-shaped) stats: all calibrated -> no warning."""
        stats = {
            "calibration_stats": {
                "total_segments": 4,
                "calibrated_segments": 4,
                "fallback_segments": 0,
                "low_quality_segments": 0,
            }
        }
        assert _build_calibration_warning(stats) == ""

    def test_plain_text_shape_total_failure(self):
        """Plain-text shape: every segment fell back to raw original -> total failure warning."""
        stats = {
            "calibration_stats": {
                "total_segments": 3,
                "calibrated_segments": 0,
                "fallback_segments": 3,
                "low_quality_segments": 0,
            }
        }
        warning = _build_calibration_warning(stats)
        assert "完全失败" in warning

    def test_plain_text_shape_partial_with_low_quality(self):
        """Plain-text shape: a low_quality segment must still surface a warning
        (this is exactly the visibility gap the honest status model fixes --
        the plain-text path used to have NO calibration_stats at all)."""
        stats = {
            "calibration_stats": {
                "total_segments": 4,
                "calibrated_segments": 4,
                "fallback_segments": 0,
                "low_quality_segments": 1,
            }
        }
        warning = _build_calibration_warning(stats)
        assert warning != ""
        assert "4/4" in warning


class TestShouldBackfillSummary:
    """Test _should_backfill_summary helper.

    Decides whether /api/recalibrate should also re-run summary generation
    when the cache is missing a usable llm_summary.txt.
    """

    def test_not_calibrate_only_returns_false(self, tmp_path):
        """Non calibrate-only path should never trigger backfill."""
        summary_file = tmp_path / "llm_summary.txt"
        summary_file.write_text("stale", encoding="utf-8")
        cache_data = {"file_path": str(tmp_path)}
        assert _should_backfill_summary(cache_data, calibrate_only=False) is False

    def test_summary_exists_non_empty_returns_false(self, tmp_path):
        """Existing non-empty summary should not trigger backfill."""
        (tmp_path / "llm_summary.txt").write_text("real summary", encoding="utf-8")
        cache_data = {"file_path": str(tmp_path)}
        assert _should_backfill_summary(cache_data, calibrate_only=True) is False

    def test_summary_missing_returns_true(self, tmp_path):
        """Missing summary file should trigger backfill."""
        cache_data = {"file_path": str(tmp_path)}
        assert _should_backfill_summary(cache_data, calibrate_only=True) is True

    def test_summary_empty_file_returns_true(self, tmp_path):
        """Zero-byte summary placeholder should trigger backfill."""
        (tmp_path / "llm_summary.txt").write_text("", encoding="utf-8")
        cache_data = {"file_path": str(tmp_path)}
        assert _should_backfill_summary(cache_data, calibrate_only=True) is True

    def test_no_file_path_returns_false(self):
        """Cache data without file_path cannot be inspected; be conservative."""
        assert _should_backfill_summary({}, calibrate_only=True) is False
        assert _should_backfill_summary({"file_path": None}, calibrate_only=True) is False


class TestSaveLLMResultsSummaryStatus:
    """Test _save_llm_results branching on the new summary_status three-state model.

    Regression coverage for the root-cause bug: the old code derived
    skip_summary and summary_success as exact complements of each other
    (both from `summary_text is None`), so the "text too short -> save
    calibrated text as a stand-in summary" branch was dead code in every real
    coordinator-driven call (skip_summary=True always implied
    summary_success=False, so the `elif summary_success:` gate never let
    execution reach it). That's the actual mechanism behind the permanent
    "总结处理中..." placeholder: llm_summary.txt was simply never written for
    short texts.
    """

    def _patch_cache_manager(self, monkeypatch):
        from video_transcript_api.api.services import llm_ops

        mock_cm = MagicMock()
        monkeypatch.setattr(llm_ops, "cache_manager", mock_cm)
        return mock_cm

    def _summary_calls(self, mock_cm):
        return [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "summary"
        ]

    def test_legacy_skip_summary_flag_saves_calibrated_text_as_summary(self, monkeypatch):
        """Regression test for the dead-code bug: a legacy caller (no
        summary_status key) with skip_summary=True/summary_success=False must
        still write the calibrated text as a stand-in summary."""
        mock_cm = self._patch_cache_manager(monkeypatch)

        _save_llm_results(
            task_id="legacy1",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict={
                "校对文本": "calibrated body",
                "内容总结": None,
                "skip_summary": True,
                "stats": {},
                "models_used": {},
                "calibrate_success": True,
                "summary_success": False,
            },
            calibrate_only=False,
            summary_backfill=False,
        )

        summary_calls = self._summary_calls(mock_cm)
        assert len(summary_calls) == 1
        assert summary_calls[0].kwargs["content"] == "calibrated body"

    def test_summary_status_failed_does_not_copy_calibrated_text(self, monkeypatch):
        """status=failed must NOT fabricate a summary file from the calibrated
        text -- that's exactly the honesty violation being fixed."""
        mock_cm = self._patch_cache_manager(monkeypatch)

        _save_llm_results(
            task_id="fail1",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict={
                "校对文本": "calibrated body",
                "内容总结": None,
                "skip_summary": True,
                "summary_status": SummaryStatus.FAILED,
                "stats": {},
                "models_used": {},
                "calibrate_success": True,
                "summary_success": False,
            },
            calibrate_only=False,
            summary_backfill=False,
        )

        assert self._summary_calls(mock_cm) == []

    def test_summary_status_skipped_short_saves_calibrated_text(self, monkeypatch):
        """status=skipped_short keeps the existing behavior: calibrated text
        stands in for the summary."""
        mock_cm = self._patch_cache_manager(monkeypatch)

        _save_llm_results(
            task_id="skip1",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict={
                "校对文本": "calibrated body",
                "内容总结": None,
                "skip_summary": True,
                "summary_status": SummaryStatus.SKIPPED_SHORT,
                "stats": {},
                "models_used": {},
                "calibrate_success": True,
                "summary_success": False,
            },
            calibrate_only=False,
            summary_backfill=False,
        )

        summary_calls = self._summary_calls(mock_cm)
        assert len(summary_calls) == 1
        assert summary_calls[0].kwargs["content"] == "calibrated body"

    def test_summary_status_generated_saves_real_summary(self, monkeypatch):
        mock_cm = self._patch_cache_manager(monkeypatch)

        _save_llm_results(
            task_id="gen1",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict={
                "校对文本": "calibrated body",
                "内容总结": "a real summary",
                "skip_summary": False,
                "summary_status": SummaryStatus.GENERATED,
                "stats": {},
                "models_used": {},
                "calibrate_success": True,
                "summary_success": True,
            },
            calibrate_only=False,
            summary_backfill=False,
        )

        summary_calls = self._summary_calls(mock_cm)
        assert len(summary_calls) == 1
        assert summary_calls[0].kwargs["content"] == "a real summary"

    def test_writes_llm_status_json_via_save_llm_status(self, monkeypatch):
        """_save_llm_results must call cache_manager.save_llm_status with the
        calibration/summary status extracted from stats, on every save (both
        processing paths)."""
        mock_cm = self._patch_cache_manager(monkeypatch)

        _save_llm_results(
            task_id="status1",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict={
                "校对文本": "calibrated body",
                "内容总结": "a real summary",
                "skip_summary": False,
                "summary_status": SummaryStatus.GENERATED,
                "stats": {
                    "calibration_status": CalibrationStatus.FULL,
                    "calibration_stats": {"total_segments": 2},
                },
                "models_used": {},
                "calibrate_success": True,
                "summary_success": True,
            },
            calibrate_only=False,
            summary_backfill=False,
        )

        mock_cm.save_llm_status.assert_called_once()
        call_kwargs = mock_cm.save_llm_status.call_args.kwargs
        assert call_kwargs["calibration_status"] == CalibrationStatus.FULL
        assert call_kwargs["calibration_stats"] == {"total_segments": 2}
        assert call_kwargs["summary_status"] == SummaryStatus.GENERATED

    def test_calibrate_only_no_backfill_passes_none_summary_status_to_preserve(self, monkeypatch):
        """When calibrate_only=True and no backfill, the coordinator's
        summary_status is None ("not attempted this round"). That None must
        reach save_llm_status unchanged so its merge semantics preserve the
        prior summary_status instead of erasing it."""
        mock_cm = self._patch_cache_manager(monkeypatch)

        _save_llm_results(
            task_id="preserve1",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict={
                "校对文本": "recalibrated body",
                "内容总结": None,
                "skip_summary": True,
                "summary_status": None,  # explicit: coordinator did not attempt summary
                "stats": {"calibration_status": CalibrationStatus.FULL},
                "models_used": {},
                "calibrate_success": True,
                "summary_success": False,
            },
            calibrate_only=True,
            summary_backfill=False,
        )

        # Calibrate-only, no backfill: summary file itself is untouched...
        assert self._summary_calls(mock_cm) == []
        # ...and the status file write must pass summary_status=None (preserve, not erase).
        mock_cm.save_llm_status.assert_called_once()
        assert mock_cm.save_llm_status.call_args.kwargs["summary_status"] is None


class TestSaveLLMResultsLayeredCacheSuppression:
    """Test the "don't overwrite an already-satisfied layer" protection added
    for per-task processing depth (processing_options.calibrate/summarize).

    The guard only activates when processing_options explicitly requests
    calibrate=False or summarize=False AND the corresponding cache file
    already existed before this round started -- it must be a complete no-op
    (zero extra cache_manager.get_cache calls, identical behavior) for every
    caller that omits processing_options entirely (the default is
    calibrate=True/summarize=True), which is what all the older tests in this
    file exercise.
    """

    def _patch_cache_manager(self, monkeypatch):
        from video_transcript_api.api.services import llm_ops

        mock_cm = MagicMock()
        monkeypatch.setattr(llm_ops, "cache_manager", mock_cm)
        return mock_cm

    def _calibrated_calls(self, mock_cm):
        return [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "calibrated"
        ]

    def _summary_calls(self, mock_cm):
        return [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "summary"
        ]

    def test_default_processing_options_never_probes_cache(self, monkeypatch):
        """Omitting processing_options (all existing callers) must not trigger
        the new get_cache() snapshot lookup at all -- zero behavior change."""
        mock_cm = self._patch_cache_manager(monkeypatch)

        _save_llm_results(
            task_id="t1",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict={
                "校对文本": "calibrated body",
                "内容总结": "a summary",
                "skip_summary": False,
                "summary_status": SummaryStatus.GENERATED,
                "stats": {"calibration_status": CalibrationStatus.FULL},
                "models_used": {},
                "calibrate_success": True,
                "summary_success": True,
            },
            calibrate_only=False,
            summary_backfill=False,
        )

        mock_cm.get_cache.assert_not_called()
        assert len(self._calibrated_calls(mock_cm)) == 1
        assert len(self._summary_calls(mock_cm)) == 1

    def test_calibrate_false_with_existing_calibrated_file_suppresses_write(
        self, monkeypatch
    ):
        """calibrate=False this round + calibrated layer already exists ->
        must NOT overwrite llm_calibrated.txt, and must pass
        calibration_status=None to save_llm_status (preserve the real status,
        not the disabled placeholder this round's skip_calibration produced)."""
        mock_cm = self._patch_cache_manager(monkeypatch)
        mock_cm.get_cache.return_value = {
            "llm_calibrated": "existing REAL calibrated text",
            "llm_summary": "existing summary",
        }

        result = _save_llm_results(
            task_id="t2",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict={
                "校对文本": "disabled placeholder text",
                "内容总结": "fresh summary",
                "skip_summary": False,
                "summary_status": SummaryStatus.GENERATED,
                "stats": {"calibration_status": CalibrationStatus.DISABLED},
                "models_used": {},
                "calibrate_success": True,
                "summary_success": True,
            },
            calibrate_only=False,
            summary_backfill=False,
            processing_options={"calibrate": False, "summarize": True},
        )

        assert self._calibrated_calls(mock_cm) == []
        assert len(self._summary_calls(mock_cm)) == 1  # summary was requested, still written
        mock_cm.save_llm_status.assert_called_once()
        assert mock_cm.save_llm_status.call_args.kwargs["calibration_status"] is None
        assert mock_cm.save_llm_status.call_args.kwargs["calibration_stats"] is None
        assert result["calibration_status"] is None

    def test_calibrate_false_no_existing_file_writes_disabled_placeholder(
        self, monkeypatch
    ):
        """calibrate=False this round but NO prior calibrated layer -> this is
        a genuine first-time disable; the disabled/formatted text must still
        be written (it's the only "calibrated" artifact this task will ever
        produce) and calibration_status=DISABLED must be persisted."""
        mock_cm = self._patch_cache_manager(monkeypatch)
        mock_cm.get_cache.return_value = {}  # nothing exists yet

        result = _save_llm_results(
            task_id="t3",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict={
                "校对文本": "formatted passthrough text",
                "内容总结": None,
                "skip_summary": True,
                "summary_status": None,
                "stats": {"calibration_status": CalibrationStatus.DISABLED},
                "models_used": {},
                "calibrate_success": True,
                "summary_success": False,
            },
            calibrate_only=False,
            summary_backfill=False,
            processing_options={"calibrate": False, "summarize": False},
        )

        calibrated_calls = self._calibrated_calls(mock_cm)
        assert len(calibrated_calls) == 1
        assert calibrated_calls[0].kwargs["content"] == "formatted passthrough text"
        assert result["calibration_status"] == CalibrationStatus.DISABLED

    def test_summarize_false_no_existing_summary_marks_disabled_no_write(
        self, monkeypatch
    ):
        """summarize=False + no prior summary file -> DISABLED status, and
        (unlike skipped_short) the calibrated text must NOT be copied in as a
        stand-in summary file."""
        mock_cm = self._patch_cache_manager(monkeypatch)
        mock_cm.get_cache.return_value = {"llm_calibrated": "some calibrated text"}

        result = _save_llm_results(
            task_id="t4",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict={
                "校对文本": "calibrated body",
                "内容总结": None,
                "skip_summary": True,
                "summary_status": None,  # coordinator's raw skip_summary output
                "stats": {"calibration_status": CalibrationStatus.FULL},
                "models_used": {},
                "calibrate_success": True,
                "summary_success": False,
            },
            calibrate_only=False,
            summary_backfill=False,
            processing_options={"calibrate": True, "summarize": False},
        )

        assert self._summary_calls(mock_cm) == []
        assert result["summary_status"] == SummaryStatus.DISABLED
        mock_cm.save_llm_status.assert_called_once()
        assert mock_cm.save_llm_status.call_args.kwargs["summary_status"] == (
            SummaryStatus.DISABLED
        )

    def test_summarize_false_with_existing_summary_preserves_old_value(
        self, monkeypatch
    ):
        """summarize=False this round but a real summary already exists (from
        an earlier full run) -> must NOT overwrite, and must pass
        summary_status=None to preserve the existing GENERATED value rather
        than stamping DISABLED over it."""
        mock_cm = self._patch_cache_manager(monkeypatch)
        mock_cm.get_cache.return_value = {
            "llm_calibrated": "old calibrated text",
            "llm_summary": "old real summary",
        }

        result = _save_llm_results(
            task_id="t5",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict={
                "校对文本": "freshly recalibrated text",
                "内容总结": None,
                "skip_summary": True,
                "summary_status": None,
                "stats": {"calibration_status": CalibrationStatus.FULL},
                "models_used": {},
                "calibrate_success": True,
                "summary_success": False,
            },
            calibrate_only=False,
            summary_backfill=False,
            processing_options={"calibrate": True, "summarize": False},
        )

        assert self._summary_calls(mock_cm) == []
        assert result["summary_status"] is None
        mock_cm.save_llm_status.assert_called_once()
        assert mock_cm.save_llm_status.call_args.kwargs["summary_status"] is None

    def test_recalibrate_bypasses_suppression_even_if_layer_exists(self, monkeypatch):
        """calibrate_only=True (the /api/recalibrate endpoint) never sets
        processing_options, so it defaults to calibrate=True -- the
        suppression guard must never engage for it, preserving the existing
        "recalibrate always overwrites" contract."""
        mock_cm = self._patch_cache_manager(monkeypatch)

        _save_llm_results(
            task_id="t6",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict={
                "校对文本": "recalibrated body",
                "内容总结": "fresh summary",
                "skip_summary": False,
                "summary_status": SummaryStatus.GENERATED,
                "stats": {"calibration_status": CalibrationStatus.FULL},
                "models_used": {},
                "calibrate_success": True,
                "summary_success": True,
            },
            calibrate_only=True,
            summary_backfill=True,
            # processing_options intentionally omitted, matching the real
            # recalibrate call site in tasks.py.
        )

        mock_cm.get_cache.assert_not_called()
        assert len(self._calibrated_calls(mock_cm)) == 1

    def test_structured_data_none_does_not_crash_when_not_suppressed(self, monkeypatch):
        """codex-review R4 #1: structured_data can legitimately be None when
        this round routes through the plain-text path (_prepare_llm_content
        falls back to str whenever transcription_data is missing/malformed,
        e.g. calibrate_only=True recalibrate on a funasr task whose cached
        transcript_data isn't the expected dict/list shape) while
        use_speaker_recognition stays True and suppress_calibration is False
        (calibrate_only always defaults processing_options to calibrate=True,
        which unconditionally bypasses suppression -- see
        test_recalibrate_bypasses_suppression_even_if_layer_exists above).
        The old code did `structured_data["calibration_stats"] = ...`
        unconditionally whenever the "structured_data" key was present,
        crashing with TypeError on None and marking an otherwise-successful
        task as failed. Must instead skip the structured-data save (leaving
        whatever already exists in the cache untouched) without raising."""
        mock_cm = self._patch_cache_manager(monkeypatch)

        result = _save_llm_results(
            task_id="t7",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=True,
            result_dict={
                "校对文本": "calibrated body from the plain-text fallback route",
                "内容总结": "fresh summary",
                "skip_summary": False,
                "summary_status": SummaryStatus.GENERATED,
                "stats": {
                    "calibration_status": CalibrationStatus.DISABLED,
                    "calibration_stats": {"total_segments": 0},
                },
                "models_used": {},
                "calibrate_success": True,
                "summary_success": True,
                "structured_data": None,
            },
            calibrate_only=True,
            summary_backfill=True,
            # processing_options omitted -> defaults to calibrate=True,
            # matching the real recalibrate call site (suppression bypassed).
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert structured_calls == []
        assert result["calibration_status"] == CalibrationStatus.DISABLED
