"""Unit tests for the /api/resummarize route.

POST /api/resummarize re-runs ONLY the summary layer for an existing task
(skipping download/transcription/calibration/chapters). It mirrors
/api/recalibrate's skeleton (permission, ownership, inflight registry,
queue backpressure) but builds a summary-only llm_task:
processing_options={"calibrate": False, "summarize": True,
"infer_speaker_names": False, "chapters": False}, transcript taken from the
cached llm_calibrated text (falling back to the raw transcript), and
transcription_data=None to force the plain-text route.

Tests cover:
- permission 403 (reuses the "recalibrate" permission name)
- cross-user ownership 403
- llm_task construction (summary-only options, transcript from
  llm_calibrated, transcription_data=None, cached_speaker_count handoff,
  no calibrate_only)
- llm_calibrated missing -> transcript falls back to the raw transcript
- summary already generated -> 400
- LLM queue full -> 503 and the inserted task row is CAS'd to failed
- inflight registry full -> 503 without inserting the task row
"""

import queue
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class TestResummarizeRoute:
    """Drive the real route with a real CacheManager (tmp_path sqlite db +
    real cache files on disk) and inspect both the queued llm_task and the
    freshly-inserted task_status row."""

    _FAKE_USER = {
        "user_id": "resum-user-1",
        "api_key": "sk-test-resum",
        "is_legacy": True,
        "wechat_webhook": None,
    }

    _EXPECTED_OPTIONS = {
        "calibrate": False,
        "summarize": True,
        "infer_speaker_names": False,
        "chapters": False,
    }

    def _seed_original_task(self, cache_manager, *, use_speaker_recognition=False,
                            with_calibrated=False, with_speaker_mapping=False):
        """Create the original (already cached) task /api/resummarize looks
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
            use_speaker_recognition=use_speaker_recognition,
            transcript_data="hello transcript body",
            transcript_type="capswriter",
            title="Demo title",
            author="Demo author",
            description="demo description",
        )
        if with_calibrated:
            cache_manager.save_llm_result(
                platform="youtube", media_id="abc123",
                use_speaker_recognition=use_speaker_recognition,
                llm_type="calibrated", content="REAL calibrated text",
            )
        if with_speaker_mapping:
            cache_manager.save_llm_result(
                platform="youtube", media_id="abc123",
                use_speaker_recognition=use_speaker_recognition,
                llm_type="structured",
                content={
                    "speaker_mapping": {"S1": "Alice", "S2": "Bob"},
                    "dialogs": [
                        {"speaker_id": "S1", "speaker": "Alice", "text": "hi"},
                    ],
                },
            )
        return task_info["view_token"]

    def _build_client(self, cache_manager, monkeypatch, *, llm_queue=None,
                      inflight_registry=None, permission=True):
        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.api.routes import tasks as tasks_route
        import video_transcript_api.api.context as context_module

        if llm_queue is None:
            llm_queue = queue.Queue(maxsize=10)
        monkeypatch.setattr(context_module, "get_llm_queue", lambda: llm_queue)
        if inflight_registry is not None:
            monkeypatch.setattr(
                context_module, "get_inflight_registry", lambda: inflight_registry
            )

        fake_user_manager = MagicMock()
        fake_user_manager.check_permission.return_value = permission
        monkeypatch.setattr(tasks_route, "user_manager", fake_user_manager)
        monkeypatch.setattr(tasks_route, "cache_manager", cache_manager)
        monkeypatch.setattr(tasks_route, "audit_logger", MagicMock())

        app = FastAPI()
        app.include_router(tasks_route.router)

        async def _fake_verify_token():
            return self._FAKE_USER

        app.dependency_overrides[verify_token] = _fake_verify_token
        return TestClient(app), llm_queue

    def test_permission_denied_returns_403(self, tmp_path, monkeypatch):
        """resummarize reuses the "recalibrate" permission name -- a user
        without it must be rejected before ever touching the cache."""
        from video_transcript_api.cache.cache_manager import CacheManager

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            client, _ = self._build_client(cache_manager, monkeypatch, permission=False)

            resp = client.post("/api/resummarize", json={"view_token": view_token})

            assert resp.status_code == 403
        finally:
            cache_manager.close()

    def test_llm_task_construction_summary_only(self, tmp_path, monkeypatch):
        """The queued llm_task must express "summary layer only": fixed
        processing_options, transcript from the cached llm_calibrated text,
        transcription_data=None (plain-text route, no second speaker-name
        inference), cached_speaker_count handed back from the persisted
        speaker_mapping, and no calibrate_only flag."""
        from video_transcript_api.cache.cache_manager import CacheManager

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(
                cache_manager,
                use_speaker_recognition=True,
                with_calibrated=True,
                with_speaker_mapping=True,
            )
            client, llm_queue = self._build_client(cache_manager, monkeypatch)

            resp = client.post("/api/resummarize", json={"view_token": view_token})

            assert resp.status_code == 200
            body = resp.json()
            assert body["code"] == 202

            llm_task = llm_queue.get_nowait()
            assert llm_task["processing_options"] == self._EXPECTED_OPTIONS
            assert llm_task["transcript"] == "REAL calibrated text"
            assert llm_task["transcription_data"] is None
            assert llm_task["cached_speaker_count"] == 2
            assert "calibrate_only" not in llm_task, (
                "resummarize must not set calibrate_only -- it is not a "
                "recalibration and must not hit llm_ops's preserve-summary branch"
            )

            new_task = cache_manager.get_task_by_id(body["data"]["task_id"])
            assert new_task is not None
            assert new_task["submitted_by"] == self._FAKE_USER["user_id"]
            assert new_task["processing_options"] == self._EXPECTED_OPTIONS
        finally:
            cache_manager.close()

    def test_transcript_falls_back_to_raw_transcript(self, tmp_path, monkeypatch):
        """When the original task never ran calibration (no llm_calibrated
        in the cache), the summary input falls back to the raw transcript --
        covers tasks submitted with calibrate disabled."""
        from video_transcript_api.cache.cache_manager import CacheManager

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            client, llm_queue = self._build_client(cache_manager, monkeypatch)

            resp = client.post("/api/resummarize", json={"view_token": view_token})

            assert resp.status_code == 200
            assert resp.json()["code"] == 202
            llm_task = llm_queue.get_nowait()
            assert llm_task["transcript"] == "hello transcript body"
            assert llm_task["cached_speaker_count"] is None
        finally:
            cache_manager.close()

    def test_summary_already_generated_returns_400(self, tmp_path, monkeypatch):
        """A task whose llm_summary.txt already exists (non-empty) and whose
        llm_status.summary_status is 'generated' must be rejected -- the
        endpoint exists to repair failed/missing summaries, not to re-burn
        LLM quota on an accidental double click."""
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.utils.llm_status import SummaryStatus

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager, with_calibrated=True)
            cache_manager.save_llm_result(
                platform="youtube", media_id="abc123",
                use_speaker_recognition=False,
                llm_type="summary", content="an existing summary",
            )
            cache_manager.save_llm_status(
                platform="youtube", media_id="abc123",
                use_speaker_recognition=False,
                summary_status=SummaryStatus.GENERATED,
            )
            client, llm_queue = self._build_client(cache_manager, monkeypatch)

            resp = client.post("/api/resummarize", json={"view_token": view_token})

            assert resp.status_code == 400
            assert "总结已存在" in resp.json()["detail"]
            assert llm_queue.empty(), "rejected request must not enqueue any llm_task"
        finally:
            cache_manager.close()

    def test_summary_failed_is_allowed(self, tmp_path, monkeypatch):
        """The actual repair scenario: summary_status=failed (or a leftover
        empty summary file) must pass the pre-check and be enqueued."""
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.utils.llm_status import SummaryStatus

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager, with_calibrated=True)
            cache_manager.save_llm_status(
                platform="youtube", media_id="abc123",
                use_speaker_recognition=False,
                summary_status=SummaryStatus.FAILED,
            )
            client, llm_queue = self._build_client(cache_manager, monkeypatch)

            resp = client.post("/api/resummarize", json={"view_token": view_token})

            assert resp.status_code == 200
            assert resp.json()["code"] == 202
            assert llm_queue.qsize() == 1
        finally:
            cache_manager.close()

    def test_llm_queue_full_returns_503_and_marks_task_failed(
        self, tmp_path, monkeypatch,
    ):
        """Mirrors TestRecalibrateQueueBackpressure: put_nowait raising
        queue.Full must return 503 AND CAS the already-inserted task row to
        failed -- otherwise the client polls a task_id no worker will ever
        pick up."""
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.utils.task_status import TaskStatus

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            monkeypatch.setattr(
                cache_manager, "generate_task_id", lambda: "task-resum-full"
            )

            full_llm_queue = queue.Queue(maxsize=1)
            full_llm_queue.put_nowait({"placeholder": True})

            client, _ = self._build_client(
                cache_manager, monkeypatch, llm_queue=full_llm_queue,
            )

            resp = client.post("/api/resummarize", json={"view_token": view_token})

            assert resp.status_code == 503

            new_task = cache_manager.get_task_by_id("task-resum-full")
            assert new_task is not None, "the raw INSERT must still have created the task row"
            assert new_task["status"] == TaskStatus.FAILED
            assert "LLM 队列已满" in (new_task.get("error_message") or "")
            snapshot = new_task.get("terminal_snapshot")
            assert snapshot is not None, "failed terminal write must carry a snapshot"
            assert snapshot["status"] == TaskStatus.FAILED
        finally:
            cache_manager.close()

    def test_registry_full_returns_503_without_inserting_task_row(
        self, tmp_path, monkeypatch,
    ):
        """Mirrors TestRecalibrateInflightRegistryAdmission: registry-full
        rejection happens before the raw INSERT -- no task row may exist for
        the would-be task_id."""
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.api.context import _InflightTaskRegistry

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            view_token = self._seed_original_task(cache_manager)
            monkeypatch.setattr(
                cache_manager, "generate_task_id", lambda: "task-resum-registry-full"
            )

            full_registry = _InflightTaskRegistry({"transcription": 1, "llm": 1})
            full_registry.try_register("llm", "already-in-flight")

            client, _ = self._build_client(
                cache_manager, monkeypatch, inflight_registry=full_registry,
            )

            resp = client.post("/api/resummarize", json={"view_token": view_token})

            assert resp.status_code == 503
            assert cache_manager.get_task_by_id("task-resum-registry-full") is None
        finally:
            cache_manager.close()


class TestResummarizeOwnership:
    """Mirrors TestRecalibrateOwnership: resummarize also creates a new
    task, burns LLM quota and overwrites the shared summary artifact, so a
    publicly shared view_token must not be enough -- only the original
    task's authoritative submitter may trigger it (fail-closed)."""

    def _seed_original_task(self, cache_manager, *, submitted_by):
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

    def _make_backends(self, tmp_path):
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.utils.logging.audit_logger import AuditLogger

        cache_manager = CacheManager(str(tmp_path / "cache"))
        audit_logger = AuditLogger(db_path=str(tmp_path / "audit.db"))
        return cache_manager, audit_logger

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

    def test_cross_user_resummarize_is_rejected(self, tmp_path, monkeypatch):
        cache_manager, audit_logger = self._make_backends(tmp_path)
        try:
            task_info = self._seed_original_task(cache_manager, submitted_by="owner-user")
            client = self._build_client(
                cache_manager, audit_logger, monkeypatch, user_id="attacker-user",
            )

            resp = client.post(
                "/api/resummarize", json={"view_token": task_info["view_token"]},
            )

            assert resp.status_code == 403
        finally:
            cache_manager.close()

    def test_submitter_can_resummarize_own_task(self, tmp_path, monkeypatch):
        cache_manager, audit_logger = self._make_backends(tmp_path)
        try:
            task_info = self._seed_original_task(cache_manager, submitted_by="owner-user")
            client = self._build_client(
                cache_manager, audit_logger, monkeypatch, user_id="owner-user",
            )

            resp = client.post(
                "/api/resummarize", json={"view_token": task_info["view_token"]},
            )

            assert resp.status_code == 200
            assert resp.json()["code"] == 202
        finally:
            cache_manager.close()
