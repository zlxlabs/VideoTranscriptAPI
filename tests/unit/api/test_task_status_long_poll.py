"""Long-poll tests for GET /api/task/{task_id}?wait=N (notify-redesign C3).

Axis table from the task card:
1. terminal task + wait -> immediate return (< 1s)
2. in-flight task reaching terminal state during wait -> returns terminal soon
3. in-flight task the whole time -> returns ~after wait seconds, current status
4. wait=0 / wait=61 (out of range) -> 422
5. /livez probe answers while a wait request is suspended (event loop free)
6. one wait request -> exactly one audit row
7. response schema identical with and without wait

All console output must be in English only (no emoji, no Chinese).
"""

import asyncio
import time

import pytest

from video_transcript_api.api.routes import task_status_view


def _row(**overrides):
    row = {
        "task_id": "task-1",
        "view_token": "vt-1",
        "status": "queued",
        "title": "Demo",
        "author": "Alice",
        "platform": "youtube",
        "completed_at": None,
        "error_message": None,
    }
    row.update(overrides)
    return row


@pytest.fixture()
def fast_poll(monkeypatch):
    """Shrink the server-side poll interval so wait loops finish quickly."""
    monkeypatch.setattr(task_status_view, "TASK_WAIT_POLL_INTERVAL_SECONDS", 0.05)


class TestWaitOnTerminalTask:
    """Axis 1: terminal task + wait=N returns immediately."""

    @pytest.mark.usefixtures("fast_poll")
    def test_success_task_returns_immediately(
        self, client, mock_cache_manager, mock_audit_logger
    ):
        mock_cache_manager.get_task_by_id.return_value = _row(
            status="success", completed_at="2026-11-10T10:00:00"
        )
        t0 = time.monotonic()
        resp = client.get("/api/task/task-1?wait=60")
        elapsed = time.monotonic() - t0

        assert resp.status_code == 200
        assert resp.json()["code"] == 200
        assert resp.json()["data"]["status"] == "success"
        assert elapsed < 1.0

    def test_failed_task_returns_immediately(self, client, mock_cache_manager):
        mock_cache_manager.get_task_by_id.return_value = _row(
            status="failed", error_message="ASR timeout"
        )
        t0 = time.monotonic()
        resp = client.get("/api/task/task-1?wait=60")
        elapsed = time.monotonic() - t0

        # Business code lives in the body (HTTP layer stays 200).
        assert resp.json()["code"] == 500
        assert resp.json()["data"]["status"] == "failed"
        assert elapsed < 1.0


class TestWaitReachesTerminalState:
    """Axis 2: task enters terminal state while the request is waiting."""

    @pytest.mark.usefixtures("fast_poll")
    def test_processing_to_success_during_wait(
        self, client, mock_cache_manager, mock_audit_logger
    ):
        mock_cache_manager.get_task_by_id.side_effect = [
            _row(status="processing"),
            _row(status="processing"),
            _row(status="success", completed_at="2026-11-10T10:00:00"),
        ]
        t0 = time.monotonic()
        resp = client.get("/api/task/task-1?wait=30")
        elapsed = time.monotonic() - t0

        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 200
        assert body["data"]["status"] == "success"
        assert elapsed < 5.0

    @pytest.mark.usefixtures("fast_poll")
    def test_processing_to_failed_during_wait(
        self, client, mock_cache_manager, mock_audit_logger
    ):
        mock_cache_manager.get_task_by_id.side_effect = [
            _row(status="processing"),
            _row(status="calibrating"),
            _row(status="failed", error_message="boom"),
        ]
        resp = client.get("/api/task/task-1?wait=30")

        body = resp.json()
        assert body["code"] == 500
        assert body["data"]["status"] == "failed"
        assert body["data"]["error"] == "boom"


class TestWaitTimesOut:
    """Axis 3: task stays in-flight; response returns after ~wait seconds."""

    def test_returns_processing_after_wait_elapsed(
        self, client, mock_cache_manager, mock_audit_logger
    ):
        mock_cache_manager.get_task_by_id.return_value = _row(status="processing")
        t0 = time.monotonic()
        resp = client.get("/api/task/task-1?wait=2")
        elapsed = time.monotonic() - t0

        body = resp.json()
        assert body["code"] == 202
        assert body["data"]["status"] == "processing"
        assert 1.5 <= elapsed <= 5.0

    @pytest.mark.usefixtures("fast_poll")
    def test_not_found_returns_immediately_even_with_wait(
        self, client, mock_cache_manager, mock_audit_logger
    ):
        mock_cache_manager.get_task_by_id.return_value = None
        t0 = time.monotonic()
        resp = client.get("/api/task/missing-task?wait=30")
        elapsed = time.monotonic() - t0

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "task_id_not_found"
        assert elapsed < 1.0


class TestWaitValidation:
    """Axis 4: wait out of the 1-60 range is rejected with 422."""

    @pytest.mark.parametrize("bad_wait", ["0", "61", "-1", "abc"])
    def test_out_of_range_wait_returns_422(self, client, mock_cache_manager, bad_wait):
        mock_cache_manager.get_task_by_id.return_value = _row(status="processing")
        resp = client.get(f"/api/task/task-1?wait={bad_wait}")
        assert resp.status_code == 422

    def test_wait_boundary_values_are_accepted(self, client, mock_cache_manager):
        # 1 and 60 are valid; task is terminal so no suspension happens.
        mock_cache_manager.get_task_by_id.return_value = _row(status="success")
        for wait in (1, 60):
            resp = client.get(f"/api/task/task-1?wait={wait}")
            assert resp.status_code == 200


class TestEventLoopNotBlocked:
    """Axis 5: other requests are served while a wait request is suspended.

    /livez (same router family, zero-dependency liveness probe) is used
    instead of the deep /health check: /health dials real downstream
    websocket services, which makes the assertion depend on machines that
    happen to run CapsWriter. The blocking property under test (the wait
    handler must yield the event loop) is shared by both endpoints.
    """

    @pytest.mark.asyncio
    async def test_livez_answered_while_wait_suspended(
        self, async_client, mock_cache_manager
    ):
        mock_cache_manager.get_task_by_id.return_value = _row(status="processing")
        wait_req = asyncio.create_task(async_client.get("/api/task/task-1?wait=2"))
        await asyncio.sleep(0.3)

        t0 = time.monotonic()
        livez = await async_client.get("/livez")
        livez_elapsed = time.monotonic() - t0

        wait_resp = await wait_req

        assert livez.status_code == 200
        assert livez_elapsed < 1.0
        assert wait_resp.json()["code"] == 202
        assert wait_resp.json()["data"]["status"] == "processing"


class TestWaitAuditRows:
    """Axis 6: one wait request writes exactly one audit row."""

    @pytest.mark.usefixtures("fast_poll")
    def test_timed_out_wait_writes_single_audit_row(
        self, client, mock_cache_manager, mock_audit_logger
    ):
        mock_cache_manager.get_task_by_id.return_value = _row(status="processing")
        resp = client.get("/api/task/task-1?wait=1")

        assert resp.json()["code"] == 202
        assert mock_audit_logger.log_api_call.call_count == 1
        kwargs = mock_audit_logger.log_api_call.call_args.kwargs
        assert kwargs["status_code"] == 202
        assert kwargs["task_id"] == "task-1"

    @pytest.mark.usefixtures("fast_poll")
    def test_terminal_wait_writes_single_audit_row(
        self, client, mock_cache_manager, mock_audit_logger
    ):
        mock_cache_manager.get_task_by_id.side_effect = [
            _row(status="processing"),
            _row(status="success"),
        ]
        resp = client.get("/api/task/task-1?wait=10")

        assert resp.status_code == 200
        assert mock_audit_logger.log_api_call.call_count == 1


class TestWaitSchemaConsistency:
    """Axis 7: response body is identical with and without wait."""

    @pytest.mark.usefixtures("fast_poll")
    @pytest.mark.parametrize("status", ["processing", "success", "failed"])
    def test_same_body_with_and_without_wait(
        self, client, mock_cache_manager, mock_audit_logger, status
    ):
        row = _row(status=status, completed_at=None if status != "success" else "x")
        if status == "failed":
            row["error_message"] = "boom"
        mock_cache_manager.get_task_by_id.return_value = row

        without_wait = client.get("/api/task/task-1")
        with_wait = client.get("/api/task/task-1?wait=1")

        assert with_wait.status_code == without_wait.status_code
        assert with_wait.json() == without_wait.json()
