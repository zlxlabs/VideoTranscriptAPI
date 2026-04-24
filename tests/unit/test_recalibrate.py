"""Unit tests for recalibrate feature.

Tests cover:
- UserManager.check_permission: permission granted / denied / legacy user
- _save_llm_results: summary_backfill behavior for missing summary recovery
"""

import json
import tempfile
from unittest.mock import MagicMock

import pytest


class TestCheckPermission:
    """Test UserManager.check_permission method."""

    def _make_manager(self):
        """Create a UserManager without loading real config."""
        from video_transcript_api.utils.accounts.user_manager import UserManager

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"users": {}}, f)
            config_path = f.name

        return UserManager(users_config_path=config_path)

    def test_legacy_user_has_all_permissions(self):
        """Legacy single-token user should have all permissions."""
        mgr = self._make_manager()
        user_info = {"user_id": "legacy_user", "is_legacy": True}
        assert mgr.check_permission(user_info, "recalibrate") is True
        assert mgr.check_permission(user_info, "anything_else") is True

    def test_user_with_permission(self):
        """Multi-user with recalibrate in permissions should pass."""
        mgr = self._make_manager()
        user_info = {"user_id": "admin", "permissions": ["recalibrate", "other"]}
        assert mgr.check_permission(user_info, "recalibrate") is True

    def test_user_without_permission(self):
        """Multi-user without recalibrate should fail."""
        mgr = self._make_manager()
        user_info = {"user_id": "reader", "permissions": ["read"]}
        assert mgr.check_permission(user_info, "recalibrate") is False

    def test_user_no_permissions_field(self):
        """Multi-user with no permissions field should fail."""
        mgr = self._make_manager()
        user_info = {"user_id": "basic_user"}
        assert mgr.check_permission(user_info, "recalibrate") is False


class TestSaveLLMResultsSummaryBackfill:
    """Test _save_llm_results summary_backfill flag.

    When /api/recalibrate runs against a task whose llm_summary.txt is missing,
    the worker sets summary_backfill=True so the save path actually writes a
    fresh summary instead of preserving the (non-existent) old one.
    """

    def _make_result_dict(self, summary_text, summary_success=True):
        return {
            "校对文本": "calibrated body",
            "内容总结": summary_text,
            "skip_summary": False,
            "stats": {},
            "models_used": {},
            "calibrate_success": True,
            "summary_success": summary_success,
        }

    def _patch_cache_manager(self, monkeypatch):
        from video_transcript_api.api.services import llm_ops

        mock_cm = MagicMock()
        monkeypatch.setattr(llm_ops, "cache_manager", mock_cm)
        return mock_cm

    def test_calibrate_only_without_backfill_preserves_summary(self, monkeypatch):
        """Original behavior: calibrate_only=True, no backfill -> summary not touched."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        mock_cm = self._patch_cache_manager(monkeypatch)

        _save_llm_results(
            task_id="t1",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict=self._make_result_dict("fresh summary"),
            calibrate_only=True,
            summary_backfill=False,
        )

        summary_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "summary"
        ]
        assert summary_calls == []

    def test_backfill_writes_new_summary(self, monkeypatch):
        """summary_backfill=True with a generated summary -> write llm_summary.txt."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        mock_cm = self._patch_cache_manager(monkeypatch)

        _save_llm_results(
            task_id="t2",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict=self._make_result_dict("fresh summary"),
            calibrate_only=True,
            summary_backfill=True,
        )

        summary_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "summary"
        ]
        assert len(summary_calls) == 1
        assert summary_calls[0].kwargs["content"] == "fresh summary"
        assert summary_calls[0].kwargs["platform"] == "youtube"
        assert summary_calls[0].kwargs["media_id"] == "abc"

    def test_backfill_with_none_summary_skips_write(self, monkeypatch):
        """summary_backfill=True but summary failed -> no summary file written."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        mock_cm = self._patch_cache_manager(monkeypatch)

        _save_llm_results(
            task_id="t3",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict=self._make_result_dict(None, summary_success=False),
            calibrate_only=True,
            summary_backfill=True,
        )

        summary_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "summary"
        ]
        assert summary_calls == []

    def test_backfill_still_saves_calibrated_text(self, monkeypatch):
        """Backfill must not regress calibrated text saving."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        mock_cm = self._patch_cache_manager(monkeypatch)

        _save_llm_results(
            task_id="t4",
            platform="youtube",
            media_id="abc",
            use_speaker_recognition=False,
            result_dict=self._make_result_dict("fresh summary"),
            calibrate_only=True,
            summary_backfill=True,
        )

        calibrated_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "calibrated"
        ]
        assert len(calibrated_calls) == 1
        assert calibrated_calls[0].kwargs["content"] == "calibrated body"
