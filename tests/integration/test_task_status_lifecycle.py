"""Integration tests for the task-status state machine across the API + DB.

The status now lives in the persistent task_status table (single source of
truth). These tests drive a real (tmp) CacheManager through the lifecycle and
assert GET /api/task reflects each state with the right HTTP code / payload —
covering the new calibrating state, failed-with-error, and crash recovery.

All console output must be in English only (no emoji, no Chinese).
"""

import pytest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.task_status import TaskStatus


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


@pytest.fixture
def client(cm):
    from video_transcript_api.api.services.transcription import verify_token
    from video_transcript_api.api.routes import tasks

    app = FastAPI()
    app.include_router(tasks.router)
    app.dependency_overrides[verify_token] = lambda: {
        "user_id": "u1", "api_key": "sk-x", "wechat_webhook": None,
    }
    with patch("video_transcript_api.api.routes.task_status_view.cache_manager", cm), patch(
        "video_transcript_api.api.routes.task_status_view.audit_logger"
    ):
        yield TestClient(app)


def _task(cm, url="https://example.com/v1"):
    return cm.create_task(url=url)["task_id"]


class TestLifecycle:
    def test_full_lifecycle_queued_to_success(self, client, cm):
        task_id = _task(cm)

        # queued (set by create_task)
        body = client.get(f"/api/task/{task_id}").json()
        assert body["code"] == 202 and body["data"]["status"] == "queued"
        assert body["data"]["progress"] is None

        # processing
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        body = client.get(f"/api/task/{task_id}").json()
        assert body["code"] == 202 and body["data"]["status"] == "processing"

        # calibrating (transcript done, LLM running) — the bug this fix targets:
        # the task must NOT report success here.
        cm.update_task_status(task_id, TaskStatus.CALIBRATING, title="T", author="A")
        body = client.get(f"/api/task/{task_id}").json()
        assert body["code"] == 202
        assert body["data"]["status"] == "calibrating"

        cm.update_task_status(
            task_id,
            TaskStatus.CALIBRATING,
            progress={"stage": "notes", "done": 1, "total": 3},
        )
        body = client.get(f"/api/task/{task_id}").json()
        assert body["code"] == 202
        assert body["data"]["progress"] == {
            "stage": "notes", "done": 1, "total": 3
        }

        # success (LLM artifacts persisted)
        cm.update_task_status(task_id, TaskStatus.SUCCESS)
        body = client.get(f"/api/task/{task_id}").json()
        assert body["code"] == 200
        assert body["data"]["status"] == "success"
        assert body["data"]["view_token"]

    def test_invalid_progress_is_ignored_without_changing_status_mapping(self, client, cm):
        task_id = _task(cm, "https://example.com/invalid-progress")
        with cm._get_cursor() as cursor:
            cursor.execute(
                "UPDATE task_status SET progress = ? WHERE task_id = ?",
                ("not-json", task_id),
            )

        response = client.get(f"/api/task/{task_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["code"] == 202
        assert body["data"]["status"] == TaskStatus.QUEUED
        assert body["data"]["progress"] is None

    def test_failed_surfaces_error(self, client, cm):
        task_id = _task(cm, "https://example.com/fail")
        cm.update_task_status(
            task_id, TaskStatus.FAILED, error_message="LLM处理失败: boom"
        )
        resp = client.get(f"/api/task/{task_id}")
        body = resp.json()
        assert body["code"] == 500
        assert body["data"]["status"] == "failed"
        assert body["data"]["error"] == "LLM处理失败: boom"

    def test_unknown_task_404(self, client):
        assert client.get("/api/task/does-not-exist").status_code == 404


class TestCrashRecoveryViaApi:
    def test_orphaned_calibrating_becomes_failed_after_recovery(self, client, cm):
        task_id = _task(cm, "https://example.com/orphan")
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)

        # Before recovery: still in-flight.
        assert client.get(f"/api/task/{task_id}").json()["code"] == 202

        # Simulate restart sweep.
        cm.recover_orphaned_tasks()

        body = client.get(f"/api/task/{task_id}").json()
        assert body["code"] == 500
        assert body["data"]["status"] == "failed"
