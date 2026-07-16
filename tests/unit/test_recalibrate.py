"""Unit tests for recalibrate feature.

Tests cover:
- UserManager.check_permission: permission granted / denied / legacy user
- recalibrate's permission check resolves UserManager through the same
  runtime-bound DI path as verify_token, not a separate always-legacy import
- _save_llm_results: summary_backfill behavior for missing summary recovery
- POST /api/recalibrate: the freshly-inserted task_status row must carry
  submitted_by + processing_options (PR3 invariant), not leave them NULL
"""

import asyncio
import queue
import tempfile
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


class TestCheckPermission:
    """Test UserManager.check_permission method."""

    def _make_manager(self):
        """Create a UserManager without loading real config."""
        from video_transcript_api.utils.accounts.user_manager import UserManager

        # A missing file selects the supported legacy fallback. An existing
        # {"users": {}} file is intentionally invalid under the strict
        # identity contract and must not be used as a neutral test fixture.
        with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as file:
            config_path = file.name
        return UserManager(users_config_path=config_path, fallback_config={})

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


class TestRecalibratePermissionCheckUsesBoundRuntimeUserManager:
    """gate review (乙3): recalibrate's permission check must resolve
    UserManager through the same DI path verify_token/users.py/audit.py all
    use -- context.get_user_manager (the actively bound RuntimeContext's own
    instance first, the legacy global singleton only as a fallback when no
    runtime is active) -- not a separate, always-legacy import that reads a
    module-level singleton independent from whatever runtime is bound.

    Proven by making the legacy singleton ALLOW recalibrate while the bound
    runtime's own UserManager DENIES it: if recalibrate reads the legacy
    singleton (the pre-fix bug), the request proceeds past the permission
    check and fails later with 404 (unknown view_token); if it correctly
    reads the bound runtime's UserManager, it is rejected with 403 before
    ever touching the cache.
    """

    class _FakeRequest:
        """Minimal stand-in for fastapi.Request: only headers.get(...) and
        .client.host are touched before the permission check runs."""

        class _Headers(dict):
            def get(self, key, default=None):
                return dict.get(self, key, default)

        headers = _Headers()
        client = None

    def test_permission_check_follows_bound_runtime_not_legacy_singleton(
        self, monkeypatch,
    ):
        import video_transcript_api.api.context as context_module
        from video_transcript_api.api.routes import tasks as tasks_route
        from video_transcript_api.api.services.transcription import RecalibrateRequest
        from video_transcript_api.utils.accounts import user_manager as legacy_module

        # Poison the legacy singleton: if recalibrate still consulted it
        # (the pre-fix behavior), permission would incorrectly PASS.
        allow_all = MagicMock(name="legacy_singleton_allow_all")
        allow_all.check_permission.return_value = True
        monkeypatch.setattr(legacy_module, "_user_manager", allow_all)

        # The actively bound runtime's own UserManager denies recalibrate.
        deny_all = MagicMock(name="bound_runtime_deny_all")
        deny_all.check_permission.return_value = False

        class _FakeRuntime:
            user_manager = deny_all
            logger = MagicMock()

        monkeypatch.setattr(tasks_route, "audit_logger", MagicMock())
        user_info = {"user_id": "u1", "api_key": "k1"}

        async def scenario():
            token = context_module.bind_runtime(_FakeRuntime())
            try:
                with pytest.raises(HTTPException) as exc_info:
                    await tasks_route.recalibrate(
                        RecalibrateRequest(view_token="whatever"),
                        self._FakeRequest(),
                        user_info,
                    )
                return exc_info.value
            finally:
                context_module.unbind_runtime(token)

        exc = asyncio.run(scenario())

        assert exc.status_code == 403, (
            "recalibrate must be rejected by the bound runtime's UserManager "
            "(deny_all) -- a 404 here would mean it fell through past the "
            "permission check because it consulted the legacy singleton "
            "(allow_all) instead"
        )
        deny_all.check_permission.assert_called_once_with(user_info, "recalibrate")
        allow_all.check_permission.assert_not_called()


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


class TestStructuredArtifactIsRefreshable:
    """J2 修复（本地增量复核第 3 轮）矩阵测试：structured_artifact_is_
    refreshable 是 transcription.py 排队侧（schema 层，不传 mapping）与
    llm_ops._refresh_speaker_names_in_existing_structured_artifact（schema
    层 + mapping 层，传入本轮新映射）共用的唯一判定实现。这里穷举"存在但
    不可刷新"的几种边界形状（空 dict、缺字段、旧格式缺 speaker_id、混合
    schema、空 dialogs），逐一断言两层判定的结果，防止两处调用点的口径
    再次分裂——排队侧此前只用 isinstance(x, dict)，比这里宽松得多，会把
    这些"存在但不可刷新"的形状误判为可刷新，排队后真烧一次 LLM 推断，
    helper 侧再静默跳过，结果永远不可见。"""

    def _refreshable(self, structured, mapping=None):
        from video_transcript_api.api.services.llm_ops import (
            structured_artifact_is_refreshable,
        )
        return structured_artifact_is_refreshable(structured, mapping)

    def test_none_is_not_refreshable(self):
        assert self._refreshable(None) is False

    def test_non_dict_is_not_refreshable(self):
        assert self._refreshable(["not", "a", "dict"]) is False

    def test_empty_dict_is_not_refreshable(self):
        assert self._refreshable({}) is False

    def test_missing_speaker_mapping_field_is_not_refreshable(self):
        structured = {"dialogs": [{"speaker_id": "S1", "speaker": "Alice"}]}
        assert self._refreshable(structured) is False

    def test_empty_dialogs_is_not_refreshable(self):
        structured = {"speaker_mapping": {"S1": "Alice"}, "dialogs": []}
        assert self._refreshable(structured) is False

    def test_dialogs_not_a_list_is_not_refreshable(self):
        structured = {"speaker_mapping": {"S1": "Alice"}, "dialogs": "not-a-list"}
        assert self._refreshable(structured) is False

    def test_old_format_missing_all_speaker_id_is_not_refreshable(self):
        """schema 演进前的旧格式：dialog 完全没有 speaker_id 字段。"""
        structured = {
            "speaker_mapping": {"S1": "Alice"},
            "dialogs": [{"speaker": "Alice", "text": "hi"}],
        }
        assert self._refreshable(structured) is False

    def test_mixed_schema_partial_speaker_id_is_not_refreshable(self):
        """混合 schema：部分 dialog 带 speaker_id，部分不带。"""
        structured = {
            "speaker_mapping": {"S1": "Alice"},
            "dialogs": [
                {"speaker_id": "S1", "speaker": "Alice", "text": "hi"},
                {"speaker": "Bob", "text": "yo"},
            ],
        }
        assert self._refreshable(structured) is False

    def test_only_non_dict_dialog_entries_is_not_refreshable(self):
        structured = {"speaker_mapping": {"S1": "Alice"}, "dialogs": ["garbage"]}
        assert self._refreshable(structured) is False

    def test_dict_and_non_dict_mixed_dialog_entries_is_not_refreshable(self):
        """K3 (CI review round 3, minor): before this fix, non-dict entries
        were filtered out first and only the remaining dict entries were
        validated -- a mix of one well-formed dict dialog plus one
        malformed non-dict entry would therefore be misjudged as
        refreshable (the single valid dict passed every check on its own),
        even though the artifact's overall shape is untrustworthy. Any
        non-dict entry anywhere in dialogs must now fail the whole
        judgment as a unit, instead of being filtered out and judged
        separately from the rest."""
        structured = {
            "speaker_mapping": {"S1": "Alice"},
            "dialogs": [
                {"speaker_id": "S1", "speaker": "Alice", "text": "hi"},
                "garbage",
            ],
        }
        assert self._refreshable(structured) is False
        # The mapping layer is unreachable here: even a mapping that would
        # cover the one well-formed dialog does not matter -- the mixed
        # shape is already rejected at the schema layer.
        assert self._refreshable(structured, mapping={"S1": "New Name"}) is False

    def test_well_formed_is_refreshable_without_mapping(self):
        """schema 层通过、不传 mapping（排队侧用法）：只看结构形状，不
        要求映射已经解析出来。"""
        structured = {
            "speaker_mapping": {"S1": "Alice"},
            "dialogs": [{"speaker_id": "S1", "speaker": "Alice", "text": "hi"}],
        }
        assert self._refreshable(structured) is True

    def test_well_formed_but_speaker_id_not_covered_by_mapping_layer_fails(self):
        """schema 层通过，但传入的 mapping 层（helper 侧用法）不覆盖某个
        speaker_id 时，整体判定为不可刷新。"""
        structured = {
            "speaker_mapping": {"S1": "Alice"},
            "dialogs": [{"speaker_id": "S1", "speaker": "Alice", "text": "hi"}],
        }
        assert self._refreshable(structured, mapping={"S2": "Bob"}) is False

    def test_well_formed_and_mapping_layer_covers_all_speaker_id(self):
        structured = {
            "speaker_mapping": {"S1": "Alice"},
            "dialogs": [{"speaker_id": "S1", "speaker": "Alice", "text": "hi"}],
        }
        assert self._refreshable(structured, mapping={"S1": "New Name"}) is True


class TestSaveLLMResultsSpeakerNameRefresh:
    """gate review (丙1): when calibrate=False but infer_speaker_names=True
    re-infers better names for a media that already has a real calibrated
    llm_processed.json, _save_llm_results must not silently drop the
    refreshed names on the floor. suppress_calibration correctly protects
    the already-corrected dialog TEXT from being overwritten by this
    round's uncorrected placeholder text, but the SPEAKER LABELS baked into
    the existing dialogs must still track the freshly recomputed
    speaker_mapping -- otherwise /view keeps rendering names from the last
    real calibration run forever (dialog_renderer._render_from_structured_data
    reads dialog["speaker"] directly, it never re-applies speaker_mapping).
    """

    def _patch_cache_manager(self, monkeypatch, existing_snapshot):
        from video_transcript_api.api.services import llm_ops

        mock_cm = MagicMock()
        mock_cm.get_cache.return_value = existing_snapshot
        monkeypatch.setattr(llm_ops, "cache_manager", mock_cm)
        return mock_cm

    def _make_result_dict(self, dialogs, speaker_mapping):
        return {
            "校对文本": "unused placeholder text from this skip-calibration round",
            "内容总结": None,
            "skip_summary": True,
            "stats": {},
            "models_used": {},
            "calibrate_success": True,
            "summary_success": False,
            "structured_data": {
                "dialogs": dialogs,
                "speaker_mapping": speaker_mapping,
            },
        }

    def test_refreshes_dialog_speaker_labels_without_touching_calibrated_text(
        self, monkeypatch,
    ):
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "format_version": "v3",
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "Alice_old", "text": "corrected line one"},
                    {"speaker_id": "Speaker2", "speaker": "Bob_old", "text": "corrected line two"},
                ],
                "speaker_mapping": {"Speaker1": "Alice_old", "Speaker2": "Bob_old"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[
                {"speaker": "Alice_new", "text": "raw uncorrected line one"},
                {"speaker": "Bob_new", "text": "raw uncorrected line two"},
            ],
            speaker_mapping={"Speaker1": "Alice_new", "Speaker2": "Bob_new"},
        )

        _save_llm_results(
            task_id="t-speaker-refresh",
            platform="youtube",
            media_id="media-refresh",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert len(structured_calls) == 1, "must refresh the displayed structured artifact exactly once"
        saved = structured_calls[0].kwargs["content"]
        assert [d["speaker"] for d in saved["dialogs"]] == ["Alice_new", "Bob_new"], (
            "dialog speaker labels must follow the freshly inferred mapping"
        )
        assert [d["text"] for d in saved["dialogs"]] == [
            "corrected line one", "corrected line two",
        ], (
            "must keep the previously-calibrated TEXT untouched -- this round's "
            "uncorrected placeholder text must never overwrite it"
        )
        assert saved["speaker_mapping"] == {"Speaker1": "Alice_new", "Speaker2": "Bob_new"}

        calibrated_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "calibrated"
        ]
        assert calibrated_calls == [], (
            "suppress_calibration must still block overwriting llm_calibrated.txt "
            "with this round's uncorrected placeholder text"
        )

    def test_no_op_when_mapping_is_unchanged(self, monkeypatch):
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        mapping = {"Speaker1": "Alice", "Speaker2": "Bob"}
        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [{"speaker_id": "Speaker1", "speaker": "Alice", "text": "line"}],
                "speaker_mapping": mapping,
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[{"speaker": "Alice", "text": "raw"}],
            speaker_mapping=dict(mapping),
        )

        _save_llm_results(
            task_id="t-speaker-refresh-nop",
            platform="youtube",
            media_id="media-refresh-nop",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert structured_calls == [], "unchanged mapping must not trigger a rewrite"

    def test_refresh_disambiguates_duplicate_old_display_names(self, monkeypatch):
        """T5 (local Codex review round 4): the old implementation reverse-
        looked-up "which raw label produced this dialog's current display
        name" by building {display_name: raw_label} from the OLD mapping --
        lossy whenever two different raw speakers were both displayed under
        the same old name (e.g. both unrecognized, degraded to the same
        generic placeholder, or two guests who happen to share a name).
        Every dialog under that shared old display name would then be
        rewritten using only the LAST raw label's new name -- misattributing
        the other speaker's lines to someone else entirely.

        Fixed by keying directly off each dialog's own speaker_id (the raw,
        pre-mapping label -- now preserved on every dialog produced by
        SpeakerAwareProcessor._normalize_dialog), never reverse-looking-up
        through the (possibly colliding) display name at all."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "S1", "speaker": "Guest", "text": "line by S1"},
                    {"speaker_id": "S2", "speaker": "Guest", "text": "line by S2"},
                ],
                "speaker_mapping": {"S1": "Guest", "S2": "Guest"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[
                {"speaker_id": "S1", "speaker": "Alice", "text": "raw"},
                {"speaker_id": "S2", "speaker": "Bob", "text": "raw"},
            ],
            speaker_mapping={"S1": "Alice", "S2": "Bob"},
        )

        _save_llm_results(
            task_id="t-speaker-collision",
            platform="youtube",
            media_id="media-collision",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert len(structured_calls) == 1
        saved_dialogs = structured_calls[0].kwargs["content"]["dialogs"]
        by_raw = {d["speaker_id"]: d["speaker"] for d in saved_dialogs}
        assert by_raw == {"S1": "Alice", "S2": "Bob"}, (
            "each raw speaker must be renamed to ITS OWN new name, not "
            "collapsed onto whichever raw label happened to be last in the "
            "old display-name reverse lookup"
        )

    def test_old_format_dialogs_without_speaker_id_skip_refresh_but_keep_mapping(
        self, monkeypatch,
    ):
        """Pre-migration structured artifacts (produced before dialogs
        carried speaker_id) cannot be precisely refreshed -- there is no raw
        label to key off, and falling back to the lossy display-name reverse
        lookup is exactly the bug this round fixes. Rather than guess, the
        refresh must skip these artifacts outright (the display stays stale
        until a full recalibration regenerates the artifact under the new
        schema), while still leaving the already-persisted new
        speaker_mapping in place -- retrying forever would never fix an
        artifact that structurally lacks the field, so there is nothing to
        gain by rolling it back, only wasted LLM tokens on every request."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker": "Alice_old", "text": "line one"},
                    {"speaker": "Bob_old", "text": "line two"},
                ],
                "speaker_mapping": {"Speaker1": "Alice_old", "Speaker2": "Bob_old"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[
                {"speaker": "Alice_new", "text": "raw"},
                {"speaker": "Bob_new", "text": "raw"},
            ],
            speaker_mapping={"Speaker1": "Alice_new", "Speaker2": "Bob_new"},
        )

        _save_llm_results(
            task_id="t-speaker-old-format",
            platform="youtube",
            media_id="media-old-format",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert structured_calls == [], "old-format artifact must not be guessed at"
        mock_cm.invalidate_speaker_mapping.assert_not_called()

    def test_real_calibration_output_retains_speaker_id_for_refresh(self, monkeypatch):
        """Full-chain regression, local codex review round 5 (F4). Every
        other test in this class hand-crafts existing_snapshot["llm_processed"]
        ["dialogs"] with speaker_id already baked in -- none of them actually
        exercise the code that produces dialogs from a real calibration run.
        This test does: it runs SpeakerAwareProcessor._calibrate_chunks
        (which internally calls _apply_corrections_by_id, the exact
        function this round fixes) to build the "existing_snapshot" this
        test starts from, proving the whole build -> calibrate/merge ->
        save -> refresh chain keeps speaker_id intact end to end.

        Before the fix: _apply_corrections_by_id rebuilt each merged dialog
        dict from scratch, copying only start_time/end_time/duration/
        speaker/text/original_text -- silently dropping speaker_id. A
        freshly-produced REAL calibration artifact came out indistinguishable
        from the pre-schema-migration legacy format, so
        _refresh_speaker_names_in_existing_structured_artifact's
        has_raw_labels check (any(... "speaker_id" in dialog ...)) found
        none, skipped the refresh, and logged a stale "legacy format"
        warning about output that had, in fact, just been produced under
        the current schema -- the name-inference feature silently stopped
        working for every real calibration run, not just old pre-migration
        data.
        """
        from video_transcript_api.llm.core.config import LLMConfig
        from video_transcript_api.llm.core.key_info_extractor import KeyInfo
        from video_transcript_api.llm.processors.speaker_aware_processor import (
            SpeakerAwareProcessor,
        )
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        config = MagicMock(spec=LLMConfig)
        config.calibrate_model = "test-model"
        config.calibrate_reasoning_effort = None
        config.max_calibration_retries = 0
        config.structured_fallback_strategy = "original"
        config.structured_validation_enabled = False
        config.calibration_concurrent_limit = 1
        config.min_calibrate_ratio = 0.8
        config.chunk_time_budget = 300
        config.min_correction_coverage = 0.5

        processor = SpeakerAwareProcessor(
            config=config,
            llm_client=MagicMock(),
            key_info_extractor=MagicMock(),
            speaker_inferencer=MagicMock(),
            quality_validator=MagicMock(),
        )

        # Mirrors real SpeakerAwareProcessor._normalize_dialog output: every
        # dialog carries its raw, pre-mapping speaker_id alongside the
        # display "speaker" name.
        chunk = [
            {"speaker": "Alice_old", "speaker_id": "S1", "text": "raw line one",
             "start_time": "00:00:00", "end_time": "00:00:02", "duration": 2.0},
            {"speaker": "Bob_old", "speaker_id": "S2", "text": "raw line two",
             "start_time": "00:00:02", "end_time": "00:00:04", "duration": 2.0},
        ]

        llm_result = MagicMock()
        llm_result.structured_output = {
            "corrections": [
                {"id": 0, "text": "corrected line one"},
                {"id": 1, "text": "corrected line two"},
            ]
        }
        processor.llm_client.call = MagicMock(return_value=llm_result)

        key_info = KeyInfo([], [], [], [], [], [], [])
        calibrated_chunks, _stats = processor._calibrate_chunks(
            chunks=[chunk], original_chunks=[chunk], key_info=key_info,
            speaker_mapping={}, title="Title", description="",
            selected_models={
                "calibrate_model": "test-model", "calibrate_reasoning_effort": None,
            },
            language="zh",
        )
        real_calibrated_dialogs = calibrated_chunks[0]

        # Sanity check: this is genuinely exercising the merge path, not a
        # trivial pass-through -- the mocked LLM corrections must have
        # actually landed on the produced dialogs.
        assert [d["text"] for d in real_calibrated_dialogs] == [
            "corrected line one", "corrected line two",
        ]
        # The exact assertion F4 fixes: real calibration output must still
        # carry speaker_id on every dialog.
        assert all("speaker_id" in d for d in real_calibrated_dialogs), (
            "every dialog produced by a real calibration run must retain "
            "speaker_id -- if this fails, someone reintroduced the bug where "
            "_apply_corrections_by_id (or any other dialog-dict rebuild "
            "site) drops it while reconstructing the merged dialog"
        )

        existing_snapshot = {
            "llm_calibrated": "corrected line one corrected line two",
            "llm_processed": {
                "dialogs": real_calibrated_dialogs,
                "speaker_mapping": {"S1": "Alice_old", "S2": "Bob_old"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[
                {"speaker": "Alice_new", "text": "raw uncorrected line one"},
                {"speaker": "Bob_new", "text": "raw uncorrected line two"},
            ],
            speaker_mapping={"S1": "Alice_new", "S2": "Bob_new"},
        )

        _save_llm_results(
            task_id="t-full-chain-refresh",
            platform="youtube",
            media_id="media-full-chain",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert len(structured_calls) == 1, (
            "refresh must actually run against this real-calibration "
            "artifact, not be skipped as 'legacy format' -- that's exactly "
            "what happens if speaker_id gets dropped somewhere upstream"
        )
        saved_dialogs = structured_calls[0].kwargs["content"]["dialogs"]
        assert [d["speaker"] for d in saved_dialogs] == ["Alice_new", "Bob_new"]
        assert [d["text"] for d in saved_dialogs] == [
            "corrected line one", "corrected line two",
        ], "the previously-calibrated TEXT must survive the refresh untouched"
        # Regression tripwire: every dialog in the FINAL SAVED artifact must
        # still carry speaker_id, so any future dialog-dict rebuild that
        # drops the field (here or anywhere else in the chain) fails this
        # test.
        assert all("speaker_id" in d for d in saved_dialogs)

    def test_refresh_save_failure_rolls_back_mapping_and_raises(self, monkeypatch):
        """A genuine write failure while refreshing the display artifact
        must not leave 'mapping already persisted, display never refreshed'
        as a silent, permanent inconsistency (defect b). The already-
        persisted new speaker_mapping.json (written eagerly by
        SpeakerInferencer.infer(), before this refresh ever runs) must be
        rolled back so the next request's input_fingerprint lookup misses
        and genuinely retries the whole inference -- and the task itself
        must surface the failure (raise), matching the sibling 'structured'
        full-save branch a few lines above, instead of only logging a
        warning while the overall task still reports success."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "S1", "speaker": "Alice_old", "text": "line one"},
                ],
                "speaker_mapping": {"S1": "Alice_old"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)
        mock_cm.save_llm_result.return_value = False

        result_dict = self._make_result_dict(
            dialogs=[{"speaker_id": "S1", "speaker": "Alice_new", "text": "raw"}],
            speaker_mapping={"S1": "Alice_new"},
        )

        with pytest.raises(Exception):
            _save_llm_results(
                task_id="t-speaker-refresh-fail",
                platform="youtube",
                media_id="media-refresh-fail",
                use_speaker_recognition=True,
                result_dict=result_dict,
                calibrate_only=False,
                processing_options={
                    "calibrate": False, "summarize": False, "infer_speaker_names": True,
                },
            )

        mock_cm.invalidate_speaker_mapping.assert_called_once_with(
            "youtube", "media-refresh-fail",
        )

    def test_identity_fallback_source_skips_refresh_and_preserves_existing_names(
        self, monkeypatch,
    ):
        """G4 (local codex review round 6): a transient LLM failure this
        round degrades SpeakerInferencer.infer()'s result to an identity
        fallback ({label: label}, tagged source="identity_fallback") -- which
        almost always differs from the existing, previously-inferred good
        mapping. The old "refresh whenever mapping changed" check would
        misread that difference as a legitimate update and overwrite the
        already-displayed real names with raw placeholder labels, while the
        task still reports success. Tagging the source and gating on it here
        must prevent that: no structured-artifact write, no mapping
        rollback (there is nothing to roll back -- infer() never persists an
        identity-fallback mapping in the first place)."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "Alice_real", "text": "line one"},
                ],
                "speaker_mapping": {"Speaker1": "Alice_real"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[{"speaker_id": "Speaker1", "speaker": "Speaker1", "text": "raw"}],
            speaker_mapping={"Speaker1": "Speaker1"},  # identity fallback shape
        )
        result_dict["stats"] = {"speaker_inference_source": "identity_fallback"}

        _save_llm_results(
            task_id="t-identity-fallback-skip",
            platform="youtube",
            media_id="media-identity-fallback",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert structured_calls == [], (
            "an identity-fallback mapping (produced by a transient LLM "
            "failure) must never overwrite the existing real display name"
        )
        mock_cm.invalidate_speaker_mapping.assert_not_called()

    def test_no_existing_structured_artifact_skip_calibration_round_does_not_backfill(
        self, monkeypatch,
    ):
        """H2 (增量复核，2026-07): supersedes the W4/G2 backfill behavior
        this test used to lock in. W4 (PR3 review hardening 二轮) added a
        branch that, when existing_snapshot has no llm_processed at all,
        treated this round's freshly produced structured_data as the
        media's first-ever structured artifact and persisted it directly.
        G2 (CI review round 2, major) bolted on a guard requiring the
        existing calibration status to be confirmed FULL before allowing
        that backfill -- but the guard checked the wrong thing: FULL only
        proves the *old* llm_calibrated text is complete, not that *this
        round's* structured_data (produced with skip_calibration=True, raw
        uncalibrated ASR dialogs whenever suppress_calibration routes here)
        is calibrated. Persisting it as the first structured artifact still
        let DialogRenderer (which prefers llm_processed.json unconditionally
        over the plain-text fallback) render raw dialogs in place of the
        real, complete calibrated text -- even in the "confirmed FULL"
        case this test exercises.

        Fix (H2): remove the backfill branch entirely. Two things make this
        safe: (1) the infinite-requeue problem the backfill originally
        existed to solve is now handled at the source by transcription.py's
        X1 fix (a structured artifact that's merely missing no longer
        triggers need_speaker_names once calibration is already genuinely
        satisfied); (2) DialogRenderer already falls back to rendering
        llm_calibrated.txt as plain text when llm_processed.json is absent,
        so no calibrated content is ever lost -- this round's speaker-name
        inference simply isn't persisted as a structured artifact.

        Red on the pre-H2 code: with existing calibration confirmed FULL and
        raw dialogs (deliberately different text than the old calibrated
        text) supplied this round, the old code persisted exactly one
        "structured" save call; this test now asserts zero.

        Follow-up (单项修复, PR3 review hardening): H2 made this call a safe
        no-op, but transcription.py's need_speaker_names decision still
        queued a calibrate=False name-only round for exactly this cache
        shape (mapping missing, calibration confirmed FULL, structured
        artifact missing) -- so SpeakerInferencer still burned a real LLM
        call every time, and _save_llm_results (this test) just quietly
        discarded the result afterwards. That's fixed at the source in
        transcription.py (see the condition table above need_speaker_names's
        assignment): this exact cache shape is no longer queued at all, so
        the call this test drives directly never actually happens in
        production anymore. The red/green regression lock for that fix lives
        in tests/integration/test_layered_cache.py::TestLayeredCacheMatrix::
        test_missing_mapping_with_full_calibration_and_missing_structured_is_not_queued
        (asserts zero items reach the queue -- i.e. zero LLM calls, not just
        zero structured saves). This test keeps its original scope: it is
        now a downstream safety net proving _save_llm_results itself never
        backfills a first structured artifact out of an unsuppressed round,
        independent of whether the caller should have queued in the first
        place."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results
        from video_transcript_api.utils.llm_status import CalibrationStatus

        existing_snapshot = {
            "llm_calibrated": "old calibrated text (never went through the "
                               "speaker-aware structured pipeline)",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
            # Deliberately no "llm_processed" key at all.
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[
                {"speaker_id": "S1", "speaker": "Alice", "text": "raw line one"},
                {"speaker_id": "S2", "speaker": "Bob", "text": "raw line two"},
            ],
            speaker_mapping={"S1": "Alice", "S2": "Bob"},
        )
        result_dict["stats"] = {"speaker_inference_source": "llm"}

        _save_llm_results(
            task_id="t-speaker-no-backfill-full-calibration",
            platform="youtube",
            media_id="media-no-backfill-full-calibration",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert structured_calls == [], (
            "a skip_calibration round's raw dialogs must never be persisted "
            "as the media's first structured artifact, even when the "
            "existing calibration is confirmed FULL -- DialogRenderer must "
            "keep falling back to the real calibrated plain text instead "
            "(red on the pre-H2 code: one save call here)"
        )
        # The genuinely real calibrated text must survive untouched --
        # suppress_calibration protects it independently of this guard.
        calibrated_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "calibrated"
        ]
        assert calibrated_calls == []

    def test_no_existing_structured_artifact_identity_fallback_still_does_not_backfill(
        self, monkeypatch,
    ):
        """Non-regression companion to the no-backfill test above: identity_
        fallback is gated at the very top of the refresh helper, before the
        old_structured-missing check ever runs -- so a transient LLM
        failure this round must still never create a first structured
        artifact out of nothing but placeholder labels, exactly like it
        must never overwrite an existing one. Unaffected by H2 (this
        function already returned before reaching the removed backfill
        branch), kept as a direct regression lock on the identity_fallback
        gate itself."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text (never went through the "
                               "speaker-aware structured pipeline)",
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[{"speaker_id": "S1", "speaker": "S1", "text": "raw"}],
            speaker_mapping={"S1": "S1"},  # identity fallback shape
        )
        result_dict["stats"] = {"speaker_inference_source": "identity_fallback"}

        _save_llm_results(
            task_id="t-speaker-backfill-identity-fallback",
            platform="youtube",
            media_id="media-backfill-identity-fallback",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert structured_calls == [], (
            "identity_fallback must not backfill a first structured "
            "artifact out of placeholder labels"
        )
        mock_cm.invalidate_speaker_mapping.assert_not_called()

    def test_full_save_path_identity_fallback_preserves_existing_real_names(
        self, monkeypatch,
    ):
        """R4 (PR3 review hardening): the identity_fallback protection above
        (test_identity_fallback_source_skips_refresh_and_preserves_existing_names)
        only covers the suppress_calibration branch (calibrate=False this
        round, only re-inferring names via
        _refresh_speaker_names_in_existing_structured_artifact). The OTHER
        branch -- the "complete structured save" path taken when this round
        also calibrate=True -- unconditionally wrote result_dict["structured_data"]
        with no speaker_inference_source check at all. A transient LLM
        failure degrading this round's speaker inference to identity
        fallback ({label: label}) would silently clobber the existing real
        names with placeholder labels, even though the task still completes
        successfully and the freshly-calibrated dialog TEXT is genuinely
        new and must still be saved.

        Reproduced with processing_options requesting the full pipeline
        (calibrate=True, summarize=True) -- the branch that must now apply
        the same identity_fallback protection while still persisting this
        round's real calibration output."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "Alice_real", "text": "old line one"},
                    {"speaker_id": "Speaker2", "speaker": "Bob_real", "text": "old line two"},
                ],
                "speaker_mapping": {"Speaker1": "Alice_real", "Speaker2": "Bob_real"},
            },
            # V3 (PR3 review hardening): restoration now only fires when this
            # round's recomputed input fingerprint matches the media's
            # persisted speaker_mapping.json artifact -- transcript_data is
            # what that recomputation reads (see
            # _restore_real_names_after_identity_fallback). A real
            # CacheManager.get_cache() always includes it; this mock must
            # supply the same shape so the "same diarization input" gate can
            # actually pass in this unit test's fully-mocked setup.
            "transcript_data": {
                "segments": [
                    {"speaker": "Speaker1", "text": "old line one"},
                    {"speaker": "Speaker2", "text": "old line two"},
                ],
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)
        # get_speaker_mapping is a MagicMock stand-in for
        # cache_manager.get_speaker_mapping -- the fingerprint gate only
        # checks that it returns a non-None value (this test doesn't
        # exercise cache_manager's own fingerprint-matching logic, that is
        # covered separately by the integration test suite's real
        # CacheManager-backed fingerprint-mismatch/-match scenarios).
        mock_cm.get_speaker_mapping.return_value = {
            "mapping": {"Speaker1": "Alice_real", "Speaker2": "Bob_real"},
            "meta": {},
        }

        result_dict = {
            "校对文本": "freshly calibrated text this round",
            "内容总结": None,
            "skip_summary": True,
            "stats": {"speaker_inference_source": "identity_fallback"},
            "models_used": {},
            "calibrate_success": True,
            "summary_success": False,
            "structured_data": {
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "Speaker1", "text": "new corrected line one"},
                    {"speaker_id": "Speaker2", "speaker": "Speaker2", "text": "new corrected line two"},
                ],
                "speaker_mapping": {"Speaker1": "Speaker1", "Speaker2": "Speaker2"},  # identity fallback shape
            },
        }

        _save_llm_results(
            task_id="t-full-save-identity-fallback",
            platform="youtube",
            media_id="media-full-save-identity-fallback",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": True, "summarize": True, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert len(structured_calls) == 1
        saved = structured_calls[0].kwargs["content"]
        # Real names preserved, keyed by speaker_id -- not the identity
        # fallback's placeholder labels.
        assert [d["speaker"] for d in saved["dialogs"]] == ["Alice_real", "Bob_real"]
        assert saved["speaker_mapping"] == {"Speaker1": "Alice_real", "Speaker2": "Bob_real"}
        # The freshly-calibrated TEXT (this round's real, new output) must
        # still be saved untouched -- the fix must not regress the actual
        # calibration content just because speaker inference degraded.
        assert [d["text"] for d in saved["dialogs"]] == [
            "new corrected line one", "new corrected line two",
        ]

        calibrated_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "calibrated"
        ]
        assert len(calibrated_calls) == 1
        assert calibrated_calls[0].kwargs["content"] == "freshly calibrated text this round"

    def test_full_save_path_real_inference_updates_names_normally(
        self, monkeypatch,
    ):
        """Sanity/regression companion to the fix above: when this round's
        speaker inference actually succeeds (source="llm", a genuine new
        mapping), the full save path must keep updating names normally --
        the identity_fallback guard must not accidentally suppress real
        updates."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "Alice_old", "text": "old line one"},
                ],
                "speaker_mapping": {"Speaker1": "Alice_old"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = {
            "校对文本": "freshly calibrated text",
            "内容总结": None,
            "skip_summary": True,
            "stats": {"speaker_inference_source": "llm"},
            "models_used": {},
            "calibrate_success": True,
            "summary_success": False,
            "structured_data": {
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "Alice_new_real", "text": "new corrected line one"},
                ],
                "speaker_mapping": {"Speaker1": "Alice_new_real"},
            },
        }

        _save_llm_results(
            task_id="t-full-save-real-inference",
            platform="youtube",
            media_id="media-full-save-real-inference",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": True, "summarize": True, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert len(structured_calls) == 1
        saved = structured_calls[0].kwargs["content"]
        assert [d["speaker"] for d in saved["dialogs"]] == ["Alice_new_real"]
        assert saved["speaker_mapping"] == {"Speaker1": "Alice_new_real"}

    def test_cache_hit_source_self_heals_when_mapping_and_dialogs_diverge(
        self, monkeypatch,
    ):
        """H6 (local codex review round 7): this test used to be named
        test_cache_hit_source_skips_refresh and assert the opposite (no
        write) under the premise that "a cache hit reflects a historically
        real mapping, the existing display artifact is already expected to
        be consistent with it". That premise is exactly the bug -- the
        persisted speaker_mapping.json (what a cache hit reads back,
        represented here by result_dict's freshly-supplied mapping) and the
        dialogs embedded in llm_processed.json can independently drift
        apart (partial historical writes, threshold changes across runs),
        and the old "cache_hit always skips" gate meant that divergence,
        once it happened, was permanent -- nothing ever re-synced the
        display back to the mapping. cache_hit must now re-run the same
        zero-LLM-cost, speaker_id-keyed reconciliation the "llm" source
        already uses (see test_llm_source_still_refreshes below):
        dialog[0]'s speaker_id "Speaker1" maps to "Alice2" now but the
        stored dialog still displays "Alice" -- that mismatch must be
        corrected."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "Alice", "text": "line one"},
                ],
                "speaker_mapping": {"Speaker1": "Alice"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[{"speaker_id": "Speaker1", "speaker": "Alice2", "text": "raw"}],
            speaker_mapping={"Speaker1": "Alice2"},
        )
        result_dict["stats"] = {"speaker_inference_source": "cache_hit"}

        _save_llm_results(
            task_id="t-cache-hit-selfheal",
            platform="youtube",
            media_id="media-cache-hit-selfheal",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert len(structured_calls) == 1, (
            "a cache-hit mapping that disagrees with the displayed dialog "
            "must self-heal the display, not be permanently skipped"
        )
        saved = structured_calls[0].kwargs["content"]
        assert saved["dialogs"][0]["speaker"] == "Alice2"
        assert saved["speaker_mapping"] == {"Speaker1": "Alice2"}

    def test_cache_hit_source_no_op_when_mapping_and_dialogs_already_consistent(
        self, monkeypatch,
    ):
        """H6 companion: the cache-hit self-heal above must not turn into an
        unconditional rewrite -- when the freshly cache-hit mapping already
        agrees with every dialog's displayed speaker (the common case, no
        drift), no write should happen at all."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "Alice", "text": "line one"},
                ],
                "speaker_mapping": {"Speaker1": "Alice"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[{"speaker_id": "Speaker1", "speaker": "Alice", "text": "raw"}],
            speaker_mapping={"Speaker1": "Alice"},
        )
        result_dict["stats"] = {"speaker_inference_source": "cache_hit"}

        _save_llm_results(
            task_id="t-cache-hit-consistent",
            platform="youtube",
            media_id="media-cache-hit-consistent",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert structured_calls == [], (
            "an already-consistent cache-hit mapping must not trigger a "
            "redundant write"
        )

    def test_cache_hit_source_old_format_dialogs_still_skip_refresh(self, monkeypatch):
        """H6 companion: the documented legacy-format limitation still
        applies to cache_hit -- dialogs without speaker_id cannot be
        precisely reconciled, so cache_hit must fall back to skipping the
        refresh exactly like every other source, not attempt a lossy
        display-name guess."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [{"speaker": "Alice_old", "text": "line one"}],
                "speaker_mapping": {"Speaker1": "Alice_old"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[{"speaker": "Alice_new", "text": "raw"}],
            speaker_mapping={"Speaker1": "Alice_new"},
        )
        result_dict["stats"] = {"speaker_inference_source": "cache_hit"}

        _save_llm_results(
            task_id="t-cache-hit-old-format",
            platform="youtube",
            media_id="media-cache-hit-old-format",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert structured_calls == []

    def test_llm_source_still_refreshes(self, monkeypatch):
        """Sanity check for the gate above: an explicit source="llm" tag
        (this round's genuine, freshly-persisted inference) must still allow
        the refresh to run -- the gate must not become a blanket no-op."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "Alice_old", "text": "line one"},
                ],
                "speaker_mapping": {"Speaker1": "Alice_old"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[{"speaker_id": "Speaker1", "speaker": "Alice_new", "text": "raw"}],
            speaker_mapping={"Speaker1": "Alice_new"},
        )
        result_dict["stats"] = {"speaker_inference_source": "llm"}

        _save_llm_results(
            task_id="t-llm-source-refresh",
            platform="youtube",
            media_id="media-llm-source-refresh",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert len(structured_calls) == 1
        saved = structured_calls[0].kwargs["content"]
        assert saved["dialogs"][0]["speaker"] == "Alice_new"

    def test_llm_source_self_heals_when_mapping_equal_but_dialogs_diverged(
        self, monkeypatch,
    ):
        """K5(a)（本地 codex review 第 8 轮，决策逻辑整体重构）：此前
        "整份 mapping 相等即跳过" 这条捷径只对非 cache_hit（llm/None）来源
        生效——但 mapping 整体相等并不能反推 dialogs 展示名此刻已经与它
        一致（可能是更早一轮已经写入 mapping，但展示刷新本身失败/被跳过
        留下的分叉）。这条捷径现在被彻底删除：llm 来源也要走逐条
        speaker_id 比对，即便本轮新映射与旧映射逐字相等，只要 dialogs
        展示名与其不一致，仍然需要刷新。
        """
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        mapping = {"Speaker1": "Alice"}
        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "STALE_NAME", "text": "line"},
                ],
                "speaker_mapping": dict(mapping),
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[{"speaker_id": "Speaker1", "speaker": "Alice", "text": "raw"}],
            speaker_mapping=dict(mapping),  # 与旧 mapping 逐字相等
        )
        result_dict["stats"] = {"speaker_inference_source": "llm"}

        _save_llm_results(
            task_id="t-llm-mapping-equal-dialogs-drift",
            platform="youtube",
            media_id="media-llm-equal-drift",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert len(structured_calls) == 1, (
            "mapping 整体相等不能作为跳过刷新的依据——dialogs 展示名早已与它"
            "不一致，必须逐条比对才能发现并自愈"
        )
        assert structured_calls[0].kwargs["content"]["dialogs"][0]["speaker"] == "Alice"

    def test_mixed_missing_speaker_id_skips_refresh_without_partial_commit(
        self, monkeypatch,
    ):
        """K5(b)：部分 dialog 缺 speaker_id 时，此前的 any() 驱动逻辑会让
        "可解析"的那部分 dialog 换上新姓名，同时把顶层 speaker_mapping
        整份替换——"不可解析"的那部分 dialog 却仍然停留旧姓名，产物内部
        自相矛盾（旧姓名不再能从新顶层 mapping 反查出任何含义）。现在改为
        整体前置校验：只要有一条不可解析，就整体跳过，不做任何改动——
        不是部分提交。
        """
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "Alice_old", "text": "line one"},
                    # 第二条缺 speaker_id：整体不可刷新。
                    {"speaker": "Bob_old", "text": "line two"},
                ],
                "speaker_mapping": {"Speaker1": "Alice_old"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[
                {"speaker_id": "Speaker1", "speaker": "Alice_new", "text": "raw"},
                {"speaker": "Bob_new", "text": "raw"},
            ],
            speaker_mapping={"Speaker1": "Alice_new"},
        )
        result_dict["stats"] = {"speaker_inference_source": "llm"}

        _save_llm_results(
            task_id="t-mixed-schema-skip",
            platform="youtube",
            media_id="media-mixed-schema",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert structured_calls == [], (
            "任何一条 dialog 无法解析都必须整体跳过，不能只刷新可解析的那部分"
            "——顶层 mapping 与部分旧姓名 dialog 会内部自相矛盾"
        )
        # 跳过不算失败：已经由 infer() 写盘的新 mapping 不回滚。
        mock_cm.invalidate_speaker_mapping.assert_not_called()

    def test_mixed_dict_and_non_dict_dialogs_skips_refresh_without_persisting(
        self, monkeypatch,
    ):
        """K3 (CI review round 3, minor): the existing structured artifact's
        dialogs mixing a well-formed dict entry with a malformed non-dict
        entry must be treated as not-refreshable as a whole (see
        TestStructuredArtifactIsRefreshable::
        test_dict_and_non_dict_mixed_dialog_entries_is_not_refreshable for
        the underlying helper-level coverage) -- exercised here through the
        real write path (_save_llm_results ->
        _refresh_speaker_names_in_existing_structured_artifact) to confirm
        the mixed shape is not queued for a partial refresh and nothing
        gets persisted."""
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "Alice_old", "text": "line one"},
                    "garbage",
                ],
                "speaker_mapping": {"Speaker1": "Alice_old"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[
                {"speaker_id": "Speaker1", "speaker": "Alice_new", "text": "raw"},
            ],
            speaker_mapping={"Speaker1": "Alice_new"},
        )
        result_dict["stats"] = {"speaker_inference_source": "llm"}

        _save_llm_results(
            task_id="t-mixed-dict-non-dict-skip",
            platform="youtube",
            media_id="media-mixed-dict-non-dict",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert structured_calls == [], (
            "a malformed non-dict entry mixed into dialogs must skip the "
            "refresh entirely, not persist a partial rewrite"
        )
        mock_cm.invalidate_speaker_mapping.assert_not_called()

    def test_speaker_id_not_covered_by_new_mapping_skips_refresh_without_partial_commit(
        self, monkeypatch,
    ):
        """K5(b) companion：所有 dialog 都带 speaker_id，但其中一条的
        speaker_id 未出现在本轮新映射的 key 里（例如本轮说话人数变少）——
        同样必须整体跳过，不能只刷新映射覆盖到的那部分。
        """
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "Alice_old", "text": "line one"},
                    {"speaker_id": "Speaker2", "speaker": "Bob_old", "text": "line two"},
                ],
                "speaker_mapping": {"Speaker1": "Alice_old", "Speaker2": "Bob_old"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)

        result_dict = self._make_result_dict(
            dialogs=[{"speaker_id": "Speaker1", "speaker": "Alice_new", "text": "raw"}],
            # 本轮新映射只覆盖 Speaker1，Speaker2 未出现在其中。
            speaker_mapping={"Speaker1": "Alice_new"},
        )
        result_dict["stats"] = {"speaker_inference_source": "llm"}

        _save_llm_results(
            task_id="t-unresolved-speaker-id-skip",
            platform="youtube",
            media_id="media-unresolved-speaker-id",
            use_speaker_recognition=True,
            result_dict=result_dict,
            calibrate_only=False,
            processing_options={
                "calibrate": False, "summarize": False, "infer_speaker_names": True,
            },
        )

        structured_calls = [
            c for c in mock_cm.save_llm_result.call_args_list
            if c.kwargs.get("llm_type") == "structured"
        ]
        assert structured_calls == []
        mock_cm.invalidate_speaker_mapping.assert_not_called()

    def test_cache_hit_source_refresh_failure_preserves_mapping_and_raises(
        self, monkeypatch,
    ):
        """K5(c)：cache_hit 来源写入展示产物失败时，不能像 llm 来源一样
        无条件回滚 speaker_mapping.json——这份映射是历史上已经真实推断、
        成功持久化过的有效资产，本轮并未产生新的 LLM 计算。回滚会让下
        一次相同 input_fingerprint 的请求被错误判定为缓存未命中，被迫
        重新真实调用一次 LLM。修复后：保留原 mapping 不回滚，但仍然如实
        raise 上报这次展示刷新失败，不静默声称成功。
        """
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "Speaker1", "speaker": "Alice_old", "text": "line one"},
                ],
                "speaker_mapping": {"Speaker1": "Alice_old"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)
        mock_cm.save_llm_result.return_value = False

        result_dict = self._make_result_dict(
            dialogs=[{"speaker_id": "Speaker1", "speaker": "Alice_new", "text": "raw"}],
            speaker_mapping={"Speaker1": "Alice_new"},
        )
        result_dict["stats"] = {"speaker_inference_source": "cache_hit"}

        with pytest.raises(Exception):
            _save_llm_results(
                task_id="t-cache-hit-refresh-fail",
                platform="youtube",
                media_id="media-cache-hit-refresh-fail",
                use_speaker_recognition=True,
                result_dict=result_dict,
                calibrate_only=False,
                processing_options={
                    "calibrate": False, "summarize": False, "infer_speaker_names": True,
                },
            )

        # 原有效 mapping 必须保留，不能被回滚——否则下一次相同指纹的请求
        # 会被迫重新真实调用一次 LLM。
        mock_cm.invalidate_speaker_mapping.assert_not_called()

    def test_llm_source_refresh_failure_still_rolls_back_mapping_and_raises(
        self, monkeypatch,
    ):
        """K5(c) companion／sanity：显式 source="llm" 时，写入失败仍然要
        回滚（与既有 test_refresh_save_failure_rolls_back_mapping_and_raises
        覆盖的隐式 None 来源同一条语义，这里显式打上 "llm" 标签，确认这次
        重构没有把回滚条件误改成只认 None，遗漏显式 "llm" 标签）。
        """
        from video_transcript_api.api.services.llm_ops import _save_llm_results

        existing_snapshot = {
            "llm_calibrated": "old calibrated text",
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "S1", "speaker": "Alice_old", "text": "line one"},
                ],
                "speaker_mapping": {"S1": "Alice_old"},
            },
        }
        mock_cm = self._patch_cache_manager(monkeypatch, existing_snapshot)
        mock_cm.save_llm_result.return_value = False

        result_dict = self._make_result_dict(
            dialogs=[{"speaker_id": "S1", "speaker": "Alice_new", "text": "raw"}],
            speaker_mapping={"S1": "Alice_new"},
        )
        result_dict["stats"] = {"speaker_inference_source": "llm"}

        with pytest.raises(Exception):
            _save_llm_results(
                task_id="t-llm-refresh-fail-explicit",
                platform="youtube",
                media_id="media-llm-refresh-fail-explicit",
                use_speaker_recognition=True,
                result_dict=result_dict,
                calibrate_only=False,
                processing_options={
                    "calibrate": False, "summarize": False, "infer_speaker_names": True,
                },
            )

        mock_cm.invalidate_speaker_mapping.assert_called_once_with(
            "youtube", "media-llm-refresh-fail-explicit",
        )


class TestRecalibrateRouteTaskStatusRow:
    """POST /api/recalibrate creates a new task_status row via a raw INSERT
    that bypasses cache_manager.create_task(). gate review found that INSERT
    omits submitted_by and processing_options, so both columns land NULL --
    unlike every task created through the normal /api/transcribe path.

    This test drives the real route (real CacheManager backed by a tmp_path
    sqlite db + real cache files on disk) end to end and inspects the new
    row through cache_manager.get_task_by_id(), the same read path the audit
    history endpoint uses.
    """

    _FAKE_USER = {
        "user_id": "recal-user-1",
        "api_key": "sk-test-recal",
        "is_legacy": True,
        "wechat_webhook": None,
    }

    def _seed_original_task(self, cache_manager):
        """Create the original (already cached) task /api/recalibrate looks
        up by view_token: a task_status row + a video_cache row with a real
        transcript file on disk (get_cache_by_view_token reads both)."""
        task_info = cache_manager.create_task(
            url="https://www.youtube.com/watch?v=abc123",
            platform="youtube",
            media_id="abc123",
        )
        cache_manager.save_cache(
            platform="youtube",
            url="https://www.youtube.com/watch?v=abc123",
            media_id="abc123",
            use_speaker_recognition=False,
            transcript_data="hello transcript body",
            transcript_type="capswriter",
            title="Demo title",
            author="Demo author",
            description="demo description",
        )
        return task_info["view_token"]

    def _build_client(self, cache_manager, monkeypatch):
        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.api.routes import tasks as tasks_route
        import video_transcript_api.api.context as context_module

        fake_llm_queue = queue.Queue(maxsize=10)
        monkeypatch.setattr(context_module, "get_llm_queue", lambda: fake_llm_queue)

        fake_user_manager = MagicMock()
        fake_user_manager.check_permission.return_value = True
        monkeypatch.setattr(tasks_route, "user_manager", fake_user_manager)
        monkeypatch.setattr(tasks_route, "cache_manager", cache_manager)
        monkeypatch.setattr(tasks_route, "audit_logger", MagicMock())

        app = FastAPI()
        app.include_router(tasks_route.router)

        async def _fake_verify_token():
            return self._FAKE_USER

        app.dependency_overrides[verify_token] = _fake_verify_token
        return TestClient(app)

    def test_recalibrate_persists_submitted_by_and_processing_options(
        self, tmp_path, monkeypatch,
    ):
        from video_transcript_api.api.processing_options import (
            normalize_processing_options,
        )
        from video_transcript_api.cache.cache_manager import CacheManager

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            client = self._build_client(cache_manager, monkeypatch)

            resp = client.post("/api/recalibrate", json={"view_token": view_token})

            assert resp.status_code == 200
            body = resp.json()
            assert body["code"] == 202
            new_task_id = body["data"]["task_id"]

            new_task = cache_manager.get_task_by_id(new_task_id)
            assert new_task is not None, "recalibrate must insert a new task_status row"

            assert new_task["submitted_by"] == self._FAKE_USER["user_id"], (
                "submitted_by must record who triggered the recalibrate, "
                "matching create_task()'s contract for every other task"
            )

            # recalibrate has no request-level processing_options field (see
            # RecalibrateRequest): it always runs calibration, and summarize/
            # infer_speaker_names default True too -- llm_ops._handle_llm_task
            # independently computes this exact same
            # normalize_processing_options(None) for the queued llm_task, so
            # persisting it here keeps the creation-time row and the eventual
            # terminal_snapshot's processing_options consistent.
            assert new_task["processing_options"] == normalize_processing_options(None)
        finally:
            cache_manager.close()


class TestRecalibrateQueueBackpressure:
    """M2 (local codex review round 10, findings b + c): recalibrate's async
    route called the synchronous queue.Queue.put(llm_task) with no timeout --
    when the LLM queue is full this blocks (holds) the calling coroutine,
    and because it's a plain synchronous call inside an async def, it
    blocks the whole event loop with it (every other request, health check,
    and graceful shutdown stalls for as long as the queue stays full).
    Fixed by switching to put_nowait, catching queue.Full, and returning 503
    instead of hanging forever.

    Finding c: once queue-full is reachable, the task_status row the raw
    INSERT above already created (status='processing') must be CAS'd to
    failed before the 503 goes out -- otherwise the client is left polling
    a task_id that no worker will ever pick up (mirrors
    TestTranscribeQueueBackpressure in test_api_routes.py for the sibling
    /api/transcribe path).
    """

    _FAKE_USER = {
        "user_id": "recal-user-backpressure",
        "api_key": "sk-test-recal-backpressure",
        "is_legacy": True,
        "wechat_webhook": None,
    }

    def _seed_original_task(self, cache_manager):
        task_info = cache_manager.create_task(
            url="https://www.youtube.com/watch?v=abc123",
            platform="youtube",
            media_id="abc123",
        )
        cache_manager.save_cache(
            platform="youtube",
            url="https://www.youtube.com/watch?v=abc123",
            media_id="abc123",
            use_speaker_recognition=False,
            transcript_data="hello transcript body",
            transcript_type="capswriter",
            title="Demo title",
            author="Demo author",
            description="demo description",
        )
        return task_info["view_token"]

    def _build_client(self, cache_manager, monkeypatch, *, llm_queue):
        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.api.routes import tasks as tasks_route
        import video_transcript_api.api.context as context_module

        monkeypatch.setattr(context_module, "get_llm_queue", lambda: llm_queue)

        fake_user_manager = MagicMock()
        fake_user_manager.check_permission.return_value = True
        monkeypatch.setattr(tasks_route, "user_manager", fake_user_manager)
        monkeypatch.setattr(tasks_route, "cache_manager", cache_manager)
        monkeypatch.setattr(tasks_route, "audit_logger", MagicMock())

        app = FastAPI()
        app.include_router(tasks_route.router)

        async def _fake_verify_token():
            return self._FAKE_USER

        app.dependency_overrides[verify_token] = _fake_verify_token
        return TestClient(app)

    def test_llm_queue_full_returns_503_and_marks_task_failed(self, tmp_path, monkeypatch):
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.utils.task_status import TaskStatus

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            monkeypatch.setattr(cache_manager, "generate_task_id", lambda: "task-recal-full")

            full_llm_queue = queue.Queue(maxsize=1)
            full_llm_queue.put_nowait({"placeholder": True})  # force queue.Full on next put_nowait

            client = self._build_client(cache_manager, monkeypatch, llm_queue=full_llm_queue)

            resp = client.post("/api/recalibrate", json={"view_token": view_token})

            assert resp.status_code == 503

            new_task = cache_manager.get_task_by_id("task-recal-full")
            assert new_task is not None, "the raw INSERT must still have created the task row"
            assert new_task["status"] == TaskStatus.FAILED
            assert "LLM 队列已满" in (new_task.get("error_message") or "")
            # update_task_status auto-builds terminal_snapshot for any
            # success/failed write (see its docstring) -- the same
            # lightweight pattern the worker's own failure path already
            # relies on (transcription.py's except-block call), not a
            # bespoke snapshot shape invented for this one call site.
            snapshot = new_task.get("terminal_snapshot")
            assert snapshot is not None, "failed terminal write must carry a snapshot"
            assert snapshot["status"] == TaskStatus.FAILED
            assert snapshot["error_message"] == "LLM 队列已满，重新校对提交被拒绝"
        finally:
            cache_manager.close()

    def test_llm_queue_full_cleanup_write_failure_still_returns_503(
        self, tmp_path, monkeypatch,
    ):
        """The failed-status CAS write is itself best-effort: if it raises
        (e.g. cache.db momentarily locked), that must be logged and
        swallowed, not let it mask the original queue-full 503 behind a
        generic 500.

        K1 (CI review round 3, major): this is exactly the double-failure
        request-path scenario -- the response must surface the fact that
        terminal-state cleanup itself failed (via a marker appended to
        detail), and the in-flight registry slot must still be released by
        the existing unconditional finally, not leaked."""
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.api.context import _InflightTaskRegistry
        from video_transcript_api.api.routes.tasks import _TERMINAL_WRITE_FAILURE_NOTE
        import video_transcript_api.api.context as context_module

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            monkeypatch.setattr(cache_manager, "generate_task_id", lambda: "task-recal-full-2")

            def _boom(*args, **kwargs):
                raise RuntimeError("db locked")

            monkeypatch.setattr(cache_manager, "update_task_status", _boom)

            full_llm_queue = queue.Queue(maxsize=1)
            full_llm_queue.put_nowait({"placeholder": True})

            client = self._build_client(cache_manager, monkeypatch, llm_queue=full_llm_queue)

            registry = _InflightTaskRegistry({"transcription": 5, "llm": 5})
            monkeypatch.setattr(context_module, "get_inflight_registry", lambda: registry)

            resp = client.post("/api/recalibrate", json={"view_token": view_token})

            assert resp.status_code == 503
            assert _TERMINAL_WRITE_FAILURE_NOTE in resp.json()["detail"], (
                "the double failure must be surfaced through the response "
                "body, not just logged server-side"
            )
            assert registry.size("llm") == 0, (
                "the in-flight quota must still be released even when the "
                "terminal-state cleanup write itself failed"
            )
        finally:
            cache_manager.close()

    def test_llm_queue_with_room_still_returns_202(self, tmp_path, monkeypatch):
        """Regression guard mirroring TestRecalibrateRouteTaskStatusRow:
        put_nowait must not change behavior when the queue has room."""
        from video_transcript_api.cache.cache_manager import CacheManager

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            roomy_llm_queue = queue.Queue(maxsize=10)
            client = self._build_client(cache_manager, monkeypatch, llm_queue=roomy_llm_queue)

            resp = client.post("/api/recalibrate", json={"view_token": view_token})

            assert resp.status_code == 200
            assert resp.json()["code"] == 202
            assert roomy_llm_queue.qsize() == 1
        finally:
            cache_manager.close()


class TestRecalibrateNonCapacityExceptions:
    """T1 (local codex review round 14): the window between the raw INSERT
    landing the task row (status='processing') and the LLM queue accepting
    the task (put_nowait succeeding) previously only CAS'd the row to failed
    for the queue.Full branch (see TestRecalibrateQueueBackpressure above).
    Two other exception sources in that same window were left uncovered:

    1. The FunASR transcript formatting step (funasr_client.
       format_transcript_with_speakers) had *no* try/except at all -- a
       legal-JSON-but-wrong-shape transcript_data (e.g. a list instead of a
       dict) makes its internal `.get()` chain raise, which used to bubble
       straight out of the route as an unhandled 500 with the task row
       stuck in 'processing' forever.
    2. Any non-queue.Full exception from llm_queue.put_nowait fell through
       to the generic `except Exception as e` clause, which only returned
       500 without touching the task row.

    Both must now be CAS'd to failed the same way queue.Full already is,
    and the CAS write's own failure must not mask the original 500.
    """

    _FAKE_USER = {
        "user_id": "recal-user-noncapacity",
        "api_key": "sk-test-recal-noncapacity",
        "is_legacy": True,
        "wechat_webhook": None,
    }

    def _seed_original_task(self, cache_manager, *, transcript_data="hello transcript body",
                             transcript_type="capswriter"):
        task_info = cache_manager.create_task(
            url="https://www.youtube.com/watch?v=abc123",
            platform="youtube",
            media_id="abc123",
        )
        cache_manager.save_cache(
            platform="youtube",
            url="https://www.youtube.com/watch?v=abc123",
            media_id="abc123",
            use_speaker_recognition=False,
            transcript_data=transcript_data,
            transcript_type=transcript_type,
            title="Demo title",
            author="Demo author",
            description="demo description",
        )
        return task_info["view_token"]

    def _build_client(self, cache_manager, monkeypatch, *, llm_queue):
        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.api.routes import tasks as tasks_route
        import video_transcript_api.api.context as context_module

        monkeypatch.setattr(context_module, "get_llm_queue", lambda: llm_queue)

        fake_user_manager = MagicMock()
        fake_user_manager.check_permission.return_value = True
        monkeypatch.setattr(tasks_route, "user_manager", fake_user_manager)
        monkeypatch.setattr(tasks_route, "cache_manager", cache_manager)
        monkeypatch.setattr(tasks_route, "audit_logger", MagicMock())

        app = FastAPI()
        app.include_router(tasks_route.router)

        async def _fake_verify_token():
            return self._FAKE_USER

        app.dependency_overrides[verify_token] = _fake_verify_token
        return TestClient(app)

    # -- FunASR transcript formatting throws (malformed-shape transcript_data) --

    def test_funasr_format_exception_returns_500_and_marks_task_failed(
        self, tmp_path, monkeypatch,
    ):
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.utils.task_status import TaskStatus

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            # Legal JSON but the wrong shape: format_transcript_with_speakers
            # calls transcription_result.get("segments", []) internally --
            # a list has no .get(), so this reproduces a real "shape
            # damaged" payload rather than an invented failure mode.
            view_token = self._seed_original_task(
                cache_manager,
                transcript_data=["broken-shape-not-a-dict"],
                transcript_type="funasr",
            )
            monkeypatch.setattr(cache_manager, "generate_task_id", lambda: "task-recal-format-1")

            client = self._build_client(
                cache_manager, monkeypatch, llm_queue=queue.Queue(maxsize=10),
            )

            resp = client.post("/api/recalibrate", json={"view_token": view_token})

            assert resp.status_code == 500

            new_task = cache_manager.get_task_by_id("task-recal-format-1")
            assert new_task is not None, "the raw INSERT must still have created the task row"
            assert new_task["status"] == TaskStatus.FAILED
            assert "转录数据格式化失败" in (new_task.get("error_message") or "")
            snapshot = new_task.get("terminal_snapshot")
            assert snapshot is not None, "failed terminal write must carry a snapshot"
            assert snapshot["status"] == TaskStatus.FAILED
        finally:
            cache_manager.close()

    def test_funasr_format_exception_cleanup_write_failure_still_returns_500(
        self, tmp_path, monkeypatch,
    ):
        """The failed-status CAS write is itself best-effort: if it raises,
        that must be logged and swallowed, not let it mask the original
        formatting-failure 500.

        K1 (CI review round 3, major): this is exactly the double-failure
        request-path scenario -- the response must surface the fact that
        terminal-state cleanup itself failed (via a marker appended to
        detail), and the in-flight registry slot must still be released by
        the existing unconditional finally, not leaked."""
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.api.context import _InflightTaskRegistry
        from video_transcript_api.api.routes.tasks import _TERMINAL_WRITE_FAILURE_NOTE
        import video_transcript_api.api.context as context_module

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(
                cache_manager,
                transcript_data=["broken-shape-not-a-dict"],
                transcript_type="funasr",
            )
            monkeypatch.setattr(cache_manager, "generate_task_id", lambda: "task-recal-format-2")

            def _boom(*args, **kwargs):
                raise RuntimeError("db locked")

            monkeypatch.setattr(cache_manager, "update_task_status", _boom)

            client = self._build_client(
                cache_manager, monkeypatch, llm_queue=queue.Queue(maxsize=10),
            )

            registry = _InflightTaskRegistry({"transcription": 5, "llm": 5})
            monkeypatch.setattr(context_module, "get_inflight_registry", lambda: registry)

            resp = client.post("/api/recalibrate", json={"view_token": view_token})

            assert resp.status_code == 500
            assert _TERMINAL_WRITE_FAILURE_NOTE in resp.json()["detail"], (
                "the double failure must be surfaced through the response "
                "body, not just logged server-side"
            )
            assert registry.size("llm") == 0, (
                "the in-flight quota must still be released even when the "
                "terminal-state cleanup write itself failed"
            )
        finally:
            cache_manager.close()

    # -- Generic (non queue.Full) exception while enqueueing to the LLM queue --

    def test_generic_llm_enqueue_exception_returns_500_and_marks_task_failed(
        self, tmp_path, monkeypatch,
    ):
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.utils.task_status import TaskStatus

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            monkeypatch.setattr(cache_manager, "generate_task_id", lambda: "task-recal-enqueue-1")

            broken_queue = MagicMock()
            broken_queue.put_nowait.side_effect = RuntimeError("unexpected enqueue failure")

            client = self._build_client(cache_manager, monkeypatch, llm_queue=broken_queue)

            resp = client.post("/api/recalibrate", json={"view_token": view_token})

            assert resp.status_code == 500

            new_task = cache_manager.get_task_by_id("task-recal-enqueue-1")
            assert new_task is not None, "the raw INSERT must still have created the task row"
            assert new_task["status"] == TaskStatus.FAILED
            assert "任务加入队列失败" in (new_task.get("error_message") or "")
            snapshot = new_task.get("terminal_snapshot")
            assert snapshot is not None, "failed terminal write must carry a snapshot"
            assert snapshot["status"] == TaskStatus.FAILED
        finally:
            cache_manager.close()

    def test_generic_llm_enqueue_exception_cleanup_write_failure_still_returns_500(
        self, tmp_path, monkeypatch,
    ):
        """The failed-status CAS write is itself best-effort: if it raises,
        that must be logged and swallowed, not let it mask the original
        enqueue-failure 500 -- the 24h reconciliation sweep is the only
        remaining backstop, so the original response must not regress.

        K1 (CI review round 3, major): this is exactly the double-failure
        request-path scenario -- the response must surface the fact that
        terminal-state cleanup itself failed (via a marker appended to
        detail), and the in-flight registry slot must still be released by
        the existing unconditional finally, not leaked."""
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.api.context import _InflightTaskRegistry
        from video_transcript_api.api.routes.tasks import _TERMINAL_WRITE_FAILURE_NOTE
        import video_transcript_api.api.context as context_module

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            monkeypatch.setattr(cache_manager, "generate_task_id", lambda: "task-recal-enqueue-2")

            def _boom(*args, **kwargs):
                raise RuntimeError("db locked")

            monkeypatch.setattr(cache_manager, "update_task_status", _boom)

            broken_queue = MagicMock()
            broken_queue.put_nowait.side_effect = RuntimeError("unexpected enqueue failure")

            client = self._build_client(cache_manager, monkeypatch, llm_queue=broken_queue)

            registry = _InflightTaskRegistry({"transcription": 5, "llm": 5})
            monkeypatch.setattr(context_module, "get_inflight_registry", lambda: registry)

            resp = client.post("/api/recalibrate", json={"view_token": view_token})

            assert resp.status_code == 500
            assert _TERMINAL_WRITE_FAILURE_NOTE in resp.json()["detail"], (
                "the double failure must be surfaced through the response "
                "body, not just logged server-side"
            )
            assert registry.size("llm") == 0, (
                "the in-flight quota must still be released even when the "
                "terminal-state cleanup write itself failed"
            )
        finally:
            cache_manager.close()


class TestRecalibrateInflightRegistryAdmission:
    """P1 (local codex review round 12): mirrors
    TestTranscribeInflightRegistryAdmission in test_api_routes.py for the
    /api/recalibrate route. Registration happens right after
    cache_manager.generate_task_id(), before the raw INSERT, using the
    "llm" kind bucket -- recalibrate skips transcription entirely and goes
    straight to the LLM queue (see api/routes/tasks.py's recalibrate())."""

    _FAKE_USER = {
        "user_id": "recal-user-registry",
        "api_key": "sk-test-recal-registry",
        "is_legacy": True,
        "wechat_webhook": None,
    }

    def _seed_original_task(self, cache_manager):
        task_info = cache_manager.create_task(
            url="https://www.youtube.com/watch?v=abc123",
            platform="youtube",
            media_id="abc123",
        )
        cache_manager.save_cache(
            platform="youtube",
            url="https://www.youtube.com/watch?v=abc123",
            media_id="abc123",
            use_speaker_recognition=False,
            transcript_data="hello transcript body",
            transcript_type="capswriter",
            title="Demo title",
            author="Demo author",
            description="demo description",
        )
        return task_info["view_token"]

    def _build_client(self, cache_manager, monkeypatch, *, llm_queue, inflight_registry):
        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.api.routes import tasks as tasks_route
        import video_transcript_api.api.context as context_module

        # recalibrate() locally imports both get_inflight_registry and
        # get_llm_queue from context (`from ..context import
        # get_inflight_registry, get_llm_queue`), so patches must target the
        # source module, not a module-level tasks_route attribute -- same
        # reasoning as the existing get_llm_queue patches in this file.
        monkeypatch.setattr(context_module, "get_llm_queue", lambda: llm_queue)
        monkeypatch.setattr(
            context_module, "get_inflight_registry", lambda: inflight_registry
        )

        fake_user_manager = MagicMock()
        fake_user_manager.check_permission.return_value = True
        monkeypatch.setattr(tasks_route, "user_manager", fake_user_manager)
        monkeypatch.setattr(tasks_route, "cache_manager", cache_manager)
        monkeypatch.setattr(tasks_route, "audit_logger", MagicMock())

        app = FastAPI()
        app.include_router(tasks_route.router)

        async def _fake_verify_token():
            return self._FAKE_USER

        app.dependency_overrides[verify_token] = _fake_verify_token
        return TestClient(app)

    def test_registry_full_returns_503_without_inserting_task_row(
        self, tmp_path, monkeypatch,
    ):
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.api.context import _InflightTaskRegistry

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            monkeypatch.setattr(
                cache_manager, "generate_task_id", lambda: "task-recal-registry-full"
            )

            full_registry = _InflightTaskRegistry({"transcription": 1, "llm": 1})
            full_registry.try_register("llm", "already-in-flight")

            client = self._build_client(
                cache_manager, monkeypatch,
                llm_queue=queue.Queue(maxsize=10),
                inflight_registry=full_registry,
            )

            resp = client.post("/api/recalibrate", json={"view_token": view_token})

            assert resp.status_code == 503
            assert cache_manager.get_task_by_id("task-recal-registry-full") is None, (
                "registry-full rejection must happen before the raw INSERT "
                "-- no row should exist for the would-be task_id"
            )
        finally:
            cache_manager.close()

    def test_registry_slot_released_when_insert_raises(self, tmp_path, monkeypatch):
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.api.context import _InflightTaskRegistry

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            monkeypatch.setattr(
                cache_manager, "generate_task_id", lambda: "task-recal-insert-fail"
            )

            registry = _InflightTaskRegistry({"transcription": 1, "llm": 1})
            client = self._build_client(
                cache_manager, monkeypatch,
                llm_queue=queue.Queue(maxsize=10),
                inflight_registry=registry,
            )

            # Surgical failure injection: the raw INSERT's parameter tuple
            # builds json.dumps(recalibrate_processing_options, ...) as its
            # very last value, the only json.dumps call in tasks.py's
            # recalibrate() -- rebinding tasks_route's own `json` name (not
            # the global json module) fails exactly that statement without
            # disturbing any earlier lookup/ownership-check step.
            from video_transcript_api.api.routes import tasks as tasks_route

            class _BoomJson:
                def dumps(self, *args, **kwargs):
                    raise RuntimeError("db down")

            monkeypatch.setattr(tasks_route, "json", _BoomJson())

            resp = client.post("/api/recalibrate", json={"view_token": view_token})

            assert resp.status_code == 500
            assert registry.size("llm") == 0, (
                "registration must be released when the INSERT step itself "
                "fails, otherwise the slot leaks forever"
            )
        finally:
            cache_manager.close()

    def test_registry_slot_released_when_llm_queue_full(self, tmp_path, monkeypatch):
        """Defense-in-depth path: the llm_queue's own maxsize should rarely
        if ever be hit now that admission is gated by the registry first,
        but if it is, the registration must still be released -- otherwise
        the slot leaks even though the task row was already CAS'd to
        failed (see the existing TestRecalibrateQueueBackpressure class for
        the CAS-to-failed behavior itself)."""
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.api.context import _InflightTaskRegistry

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            monkeypatch.setattr(
                cache_manager, "generate_task_id", lambda: "task-recal-queue-full"
            )

            full_llm_queue = queue.Queue(maxsize=1)
            full_llm_queue.put_nowait({"placeholder": True})

            registry = _InflightTaskRegistry({"transcription": 5, "llm": 5})
            client = self._build_client(
                cache_manager, monkeypatch,
                llm_queue=full_llm_queue,
                inflight_registry=registry,
            )

            resp = client.post("/api/recalibrate", json={"view_token": view_token})

            assert resp.status_code == 503
            assert registry.size("llm") == 0
        finally:
            cache_manager.close()

    def test_registry_slot_still_held_after_successful_admission(
        self, tmp_path, monkeypatch,
    ):
        """Registration is only released when the LLM worker's future
        completes (RuntimeContext.track_future's completion callback) --
        there is no worker consuming this test's llm_queue, so a
        successful 202 response must leave the slot occupied."""
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.api.context import _InflightTaskRegistry

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            monkeypatch.setattr(
                cache_manager, "generate_task_id", lambda: "task-recal-held"
            )

            registry = _InflightTaskRegistry({"transcription": 5, "llm": 5})
            client = self._build_client(
                cache_manager, monkeypatch,
                llm_queue=queue.Queue(maxsize=10),
                inflight_registry=registry,
            )

            resp = client.post("/api/recalibrate", json={"view_token": view_token})

            assert resp.status_code == 200
            assert resp.json()["code"] == 202
            assert registry.size("llm") == 1
        finally:
            cache_manager.close()


class TestRecalibrateOwnership:
    """K1（本地 codex review 第 8 轮）：POST /api/recalibrate 此前只检查
    通用 recalibrate 权限 + view_token 是否存在，不核验原任务归属——任何
    有 recalibrate 权限的用户拿到别人公开分享的 view_token，就能触发重新
    处理，覆盖共享媒体的校对/说话人产物、消耗对方的 LLM 配额。

    这里驱动真实路由（真实 CacheManager + 真实 AuditLogger，均落地到
    tmp_path 下的 sqlite 文件，不用 MagicMock——MagicMock 的
    `_get_cursor().fetchone()` 默认返回一个恒真的 Mock 对象，会让
    legacy 兜底分支永远"命中"，掩盖真实的归属判定行为），核实
    check_view_token_ownership 的判定分支在 recalibrate 入口真正生效：
    跨用户拒绝、提交者本人放行、legacy 存量任务通过审计行兜底放行、
    完全无归属证据时 fail-closed 拒绝。
    """

    def _seed_original_task(self, cache_manager, *, submitted_by):
        """创建 recalibrate 要查找的原始任务：一个 task_status 行 +
        一个带真实转录文件的 video_cache 行（get_cache_by_view_token
        同时读这两处）。"""
        task_info = cache_manager.create_task(
            url="https://www.youtube.com/watch?v=owner-task",
            platform="youtube",
            media_id="owner-task",
            submitted_by=submitted_by,
        )
        cache_manager.save_cache(
            platform="youtube",
            url="https://www.youtube.com/watch?v=owner-task",
            media_id="owner-task",
            use_speaker_recognition=False,
            transcript_data="hello transcript body",
            transcript_type="capswriter",
            title="Demo title",
            author="Demo author",
            description="demo description",
        )
        return task_info

    def _build_client(self, cache_manager, audit_logger, monkeypatch, *, user_id):
        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.api.routes import tasks as tasks_route
        import video_transcript_api.api.context as context_module

        fake_llm_queue = queue.Queue(maxsize=10)
        monkeypatch.setattr(context_module, "get_llm_queue", lambda: fake_llm_queue)

        fake_user_manager = MagicMock()
        fake_user_manager.check_permission.return_value = True
        monkeypatch.setattr(tasks_route, "user_manager", fake_user_manager)
        monkeypatch.setattr(tasks_route, "cache_manager", cache_manager)
        monkeypatch.setattr(tasks_route, "audit_logger", audit_logger)

        app = FastAPI()
        app.include_router(tasks_route.router)

        async def _fake_verify_token():
            return {"user_id": user_id, "api_key": f"sk-{user_id}", "wechat_webhook": None}

        app.dependency_overrides[verify_token] = _fake_verify_token
        return TestClient(app)

    def _make_backends(self, tmp_path):
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.utils.logging.audit_logger import AuditLogger

        cache_manager = CacheManager(str(tmp_path / "cache"))
        audit_logger = AuditLogger(db_path=str(tmp_path / "audit.db"))
        return cache_manager, audit_logger

    def test_cross_user_recalibrate_is_rejected(self, tmp_path, monkeypatch):
        cache_manager, audit_logger = self._make_backends(tmp_path)
        try:
            task_info = self._seed_original_task(cache_manager, submitted_by="owner-user")
            client = self._build_client(
                cache_manager, audit_logger, monkeypatch, user_id="attacker-user",
            )

            resp = client.post(
                "/api/recalibrate", json={"view_token": task_info["view_token"]},
            )

            assert resp.status_code == 403
        finally:
            cache_manager.close()

    def test_submitter_can_recalibrate_own_task(self, tmp_path, monkeypatch):
        cache_manager, audit_logger = self._make_backends(tmp_path)
        try:
            task_info = self._seed_original_task(cache_manager, submitted_by="owner-user")
            client = self._build_client(
                cache_manager, audit_logger, monkeypatch, user_id="owner-user",
            )

            resp = client.post(
                "/api/recalibrate", json={"view_token": task_info["view_token"]},
            )

            assert resp.status_code == 200
            assert resp.json()["code"] == 202
        finally:
            cache_manager.close()

    def test_legacy_task_submitter_falls_back_to_audit_log_ownership(
        self, tmp_path, monkeypatch,
    ):
        """submitted_by 列为空的存量任务（本 PR 迁移前提交，或迁移后调用方
        未显式传入）：唯一的归属证据是 /api/transcribe 端点留下的审计行，
        legacy 兜底命中即放行——不能因为新增的归属校验而让老任务的原提交者
        再也无法 recalibrate 自己的任务。"""
        cache_manager, audit_logger = self._make_backends(tmp_path)
        try:
            task_info = self._seed_original_task(cache_manager, submitted_by=None)
            audit_logger.log_api_call(
                api_key="sk-legacy-user",
                user_id="legacy-user",
                endpoint="/api/transcribe",
                task_id=task_info["task_id"],
            )
            client = self._build_client(
                cache_manager, audit_logger, monkeypatch, user_id="legacy-user",
            )

            resp = client.post(
                "/api/recalibrate", json={"view_token": task_info["view_token"]},
            )

            assert resp.status_code == 200
            assert resp.json()["code"] == 202
        finally:
            cache_manager.close()

    def test_legacy_task_without_any_attribution_evidence_is_rejected(
        self, tmp_path, monkeypatch,
    ):
        """既无 submitted_by 也无提交类审计行的存量任务：完全没有归属证据
        可考，fail-closed 拒绝——不能因为"审计缺失"就默认放行，与
        /api/audit/summary 的 H1 修复保持同一个安全默认。"""
        cache_manager, audit_logger = self._make_backends(tmp_path)
        try:
            task_info = self._seed_original_task(cache_manager, submitted_by=None)
            client = self._build_client(
                cache_manager, audit_logger, monkeypatch, user_id="anyone",
            )

            resp = client.post(
                "/api/recalibrate", json={"view_token": task_info["view_token"]},
            )

            assert resp.status_code == 403
        finally:
            cache_manager.close()

    def test_ownership_check_runs_through_to_thread(self, tmp_path, monkeypatch):
        """N2（本地 codex review 第 11 轮）：check_view_token_ownership 内部
        是多次同步 SQLite 查询（默认 busy timeout ~5s）。recalibrate 是
        async 路由，此前直接同步调用会整段阻塞事件循环——与
        audit.py::get_task_summary 对同一个函数的调用方式（asyncio.
        to_thread 包装）不一致。这里用一层 spy 包住 tasks 模块引用的
        asyncio.to_thread，锁死 recalibrate 确实把 check_view_token_
        ownership 交给线程池执行，而不是在事件循环里裸跑；spy 透传给真实
        实现，不改变路由的可观察行为（既有的放行/拒绝断言仍然成立）。"""
        cache_manager, audit_logger = self._make_backends(tmp_path)
        try:
            task_info = self._seed_original_task(cache_manager, submitted_by="owner-user")
            client = self._build_client(
                cache_manager, audit_logger, monkeypatch, user_id="owner-user",
            )

            from video_transcript_api.api.routes import tasks as tasks_route

            real_to_thread = asyncio.to_thread
            to_thread_calls = []

            async def _spy_to_thread(func, *args, **kwargs):
                to_thread_calls.append(func)
                return await real_to_thread(func, *args, **kwargs)

            monkeypatch.setattr(tasks_route.asyncio, "to_thread", _spy_to_thread)

            resp = client.post(
                "/api/recalibrate", json={"view_token": task_info["view_token"]},
            )

            assert resp.status_code == 200
            assert resp.json()["code"] == 202
            assert tasks_route.check_view_token_ownership in to_thread_calls
        finally:
            cache_manager.close()
