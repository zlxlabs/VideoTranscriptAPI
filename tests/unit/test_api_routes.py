"""
API route unit tests.

Covers:
- Audit routes: GET /api/audit/stats, GET /api/audit/calls
- Task routes: POST /api/transcribe, GET /api/task/{task_id}, GET /api/webhook-stats
- User routes: GET /api/users/profile
- Views: GET /robots.txt, GET /sitemap.xml
- Health endpoint is tested in test_health.py (not duplicated here)

All console output must be in English only (no emoji, no Chinese).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from video_transcript_api.utils.url_parser import ParsedURL

# ---------------------------------------------------------------------------
# Helpers: build a minimal FastAPI app with mocked dependencies
# ---------------------------------------------------------------------------

# A fake user_info dict returned by the mocked verify_token dependency.
_FAKE_USER_INFO = {
    "user_id": "test-user",
    "api_key": "sk-test-key-123456",
    "wechat_webhook": None,
}


async def _fake_verify_token():
    """Replacement for the real verify_token dependency."""
    return _FAKE_USER_INFO


def _build_test_app() -> FastAPI:
    """Create a FastAPI app with all route routers included and deps overridden.

    We patch module-level singletons that are evaluated at import time
    (logger, config, audit_logger, cache_manager, user_manager, etc.)
    before importing the router modules.
    """
    app = FastAPI()

    # We need to override verify_token globally via dependency_overrides
    from video_transcript_api.api.services.transcription import verify_token
    from video_transcript_api.api.routes import audit, tasks, users, views

    app.include_router(audit.router)
    app.include_router(tasks.router)
    app.include_router(users.router)
    app.include_router(views.router)

    app.dependency_overrides[verify_token] = _fake_verify_token

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_audit_logger():
    """Patch the audit_logger used in audit and tasks routes."""
    mock = MagicMock()
    mock.get_user_stats.return_value = {"total_calls": 10}
    mock.get_recent_calls.return_value = [
        {"endpoint": "/api/transcribe", "timestamp": "2025-01-01T00:00:00"}
    ]
    mock.log_api_call.return_value = None
    with patch(
        "video_transcript_api.api.routes.audit.audit_logger", mock
    ), patch(
        "video_transcript_api.api.routes.tasks.audit_logger", mock
    ):
        yield mock


@pytest.fixture()
def mock_user_manager():
    """Patch user_manager used in audit and users routes."""
    mock = MagicMock()
    mock.is_multi_user_mode.return_value = False
    mock.get_user_count.return_value = 1
    mock._mask_api_key.return_value = "sk-****5678"
    with patch(
        "video_transcript_api.api.routes.audit.user_manager", mock
    ), patch(
        "video_transcript_api.api.routes.users.user_manager", mock
    ):
        yield mock


@pytest.fixture()
def mock_cache_manager():
    """Patch cache_manager used in tasks and views routes."""
    mock = MagicMock()
    # P1 (local codex review round 12): /api/transcribe now pre-generates
    # task_id via cache_manager.generate_task_id() *before* calling
    # create_task(task_id=...), so the two must agree on the same fixed id
    # -- mirrors the pattern test_recalibrate.py already uses
    # (monkeypatch.setattr(cache_manager, "generate_task_id", lambda: ...)).
    mock.generate_task_id.return_value = "task-abc-123"
    mock.create_task.return_value = {
        "task_id": "task-abc-123",
        "view_token": "vt-xyz-789",
    }
    with patch(
        "video_transcript_api.api.routes.tasks.cache_manager", mock
    ), patch(
        "video_transcript_api.api.routes.views.cache_manager", mock
    ):
        yield mock


@pytest.fixture()
def mock_task_queue():
    """Patch get_task_queue to return an asyncio.Queue."""
    q = asyncio.Queue(maxsize=10)
    with patch(
        "video_transcript_api.api.routes.tasks.get_task_queue", return_value=q
    ):
        yield q


@pytest.fixture()
def mock_send_notification():
    """Patch wechat notification sending."""
    with patch(
        "video_transcript_api.api.routes.tasks.send_view_link_wechat"
    ) as mock:
        yield mock


@pytest.fixture()
def mock_base_url():
    """Patch get_base_url used in views."""
    with patch(
        "video_transcript_api.api.routes.views.get_base_url",
        return_value="https://example.com",
    ):
        yield


@pytest.fixture()
def client(
    mock_audit_logger,
    mock_user_manager,
    mock_cache_manager,
    mock_task_queue,
    mock_send_notification,
    mock_base_url,
):
    """Create a TestClient with all mocks applied."""
    app = _build_test_app()
    return TestClient(app)


# ===========================================================================
# Audit routes
# ===========================================================================


class TestAuditStats:
    """Tests for GET /api/audit/stats."""

    def test_get_stats_default_days(self, client, mock_audit_logger, mock_user_manager):
        resp = client.get("/api/audit/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 200
        assert "user_stats" in body["data"]
        mock_audit_logger.get_user_stats.assert_called_once_with("test-user", 30)

    def test_get_stats_custom_days(self, client, mock_audit_logger):
        resp = client.get("/api/audit/stats?days=7")
        assert resp.status_code == 200
        mock_audit_logger.get_user_stats.assert_called_once_with("test-user", 7)

    def test_get_stats_includes_multi_user_info(self, client, mock_user_manager):
        resp = client.get("/api/audit/stats")
        body = resp.json()
        assert "is_multi_user_mode" in body["data"]
        assert "total_users" in body["data"]

    def test_get_stats_error_returns_500(self, client, mock_audit_logger):
        mock_audit_logger.get_user_stats.side_effect = RuntimeError("db error")
        resp = client.get("/api/audit/stats")
        assert resp.status_code == 500


class TestAuditCalls:
    """Tests for GET /api/audit/calls."""

    def test_get_calls_default_limit(self, client, mock_audit_logger):
        resp = client.get("/api/audit/calls")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 200
        assert "calls" in body["data"]
        mock_audit_logger.get_recent_calls.assert_called_once_with("test-user", 100)

    def test_get_calls_custom_limit(self, client, mock_audit_logger):
        resp = client.get("/api/audit/calls?limit=10")
        assert resp.status_code == 200
        mock_audit_logger.get_recent_calls.assert_called_once_with("test-user", 10)

    @pytest.mark.parametrize("bad_limit", [0, -1, 10001])
    def test_get_calls_rejects_out_of_range_limit(self, client, bad_limit):
        """ci-gate review: limit was previously unbounded (plain int), so
        limit=0/negative/huge values would reach the DB query unchecked --
        aligned with /history's existing Query(ge=1, le=10000) bound."""
        resp = client.get(f"/api/audit/calls?limit={bad_limit}")
        assert resp.status_code == 422

    def test_get_calls_error_returns_500(self, client, mock_audit_logger):
        mock_audit_logger.get_recent_calls.side_effect = RuntimeError("db error")
        resp = client.get("/api/audit/calls")
        assert resp.status_code == 500

    def test_get_calls_missing_user_id_denied_not_leaked_globally(
        self, mock_audit_logger, mock_user_manager, mock_cache_manager,
        mock_task_queue, mock_send_notification, mock_base_url,
    ):
        """ci-gate review (local, follow-up on the /summary user_id-missing
        finding): audit_logger.get_recent_calls() treats user_id=None as
        "return every user's calls" (a deliberate admin/CLI escape hatch) --
        but this HTTP endpoint must never let a caller with a misconfigured
        (missing) user_id trigger that global view and read every tenant's
        URL/IP/User-Agent/task_id. Must be denied outright, not silently
        fall through to the global query."""
        async def _fake_verify_token_missing_user_id():
            return {"api_key": "misconfigured-key", "wechat_webhook": None}

        from video_transcript_api.api.services.transcription import verify_token

        app = _build_test_app()
        app.dependency_overrides[verify_token] = _fake_verify_token_missing_user_id
        client = TestClient(app)

        resp = client.get("/api/audit/calls")

        assert resp.status_code == 401
        mock_audit_logger.get_recent_calls.assert_not_called()


# ===========================================================================
# Task routes
# ===========================================================================


class TestTranscribeEndpoint:
    """Tests for POST /api/transcribe."""

    def test_transcribe_success(self, client, mock_cache_manager, mock_audit_logger):
        resp = client.post(
            "/api/transcribe",
            json={"url": "https://www.youtube.com/watch?v=abc123"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 202
        assert body["data"]["task_id"] == "task-abc-123"
        assert body["data"]["view_token"] == "vt-xyz-789"
        mock_cache_manager.create_task.assert_called_once()

    def test_transcribe_empty_url_returns_400(self, client):
        resp = client.post("/api/transcribe", json={"url": ""})
        assert resp.status_code == 400

    def test_transcribe_with_speaker_recognition(self, client, mock_cache_manager):
        resp = client.post(
            "/api/transcribe",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "use_speaker_recognition": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 202
        # Verify speaker recognition flag was passed
        call_kwargs = mock_cache_manager.create_task.call_args
        assert call_kwargs.kwargs.get("use_speaker_recognition") is True or \
               (call_kwargs.args and len(call_kwargs.args) > 1)

    def test_transcribe_with_download_url(self, client, mock_cache_manager):
        resp = client.post(
            "/api/transcribe",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "download_url": "https://cdn.example.com/video.mp4",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 202

    def test_transcribe_empty_download_url_normalized_to_none(
        self, client, mock_cache_manager
    ):
        resp = client.post(
            "/api/transcribe",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "download_url": "   ",
            },
        )
        assert resp.status_code == 200
        call_kwargs = mock_cache_manager.create_task.call_args
        assert call_kwargs.kwargs.get("download_url") is None

    def test_transcribe_logs_audit(self, client, mock_audit_logger):
        client.post(
            "/api/transcribe",
            json={"url": "https://www.youtube.com/watch?v=abc123"},
        )
        assert mock_audit_logger.log_api_call.call_count >= 2

    def test_transcribe_enqueues_preparsed_short_url_for_worker(
        self, client, mock_task_queue
    ):
        """A short URL is resolved by the API once and forwarded as a fact."""
        parsed_url = ParsedURL(
            platform="bilibili",
            video_id="BV1AoEg6SEW4",
            normalized_url="https://www.bilibili.com/video/BV1AoEg6SEW4?p=2",
            is_short_url=True,
            original_url="https://b23.tv/short-code",
        )
        with patch(
            "video_transcript_api.utils.url_parser.URLParser.parse",
            return_value=parsed_url,
        ):
            response = client.post(
                "/api/transcribe", json={"url": parsed_url.original_url}
            )

        assert response.status_code == 200
        queued_task = mock_task_queue.get_nowait()
        assert queued_task["url"] == parsed_url.original_url
        assert queued_task["preparsed_url"] == parsed_url
        assert queued_task["url_parse_attempted"] is True


class TestTranscribeQueueBackpressure:
    """M2 (local codex review round 10, finding a): task_queue is an
    asyncio.Queue, and `await queue.put(...)` waits forever for a free slot
    instead of raising -- the previously-declared `except asyncio.QueueFull`
    branch could never fire, dead code masquerading as backpressure. Fixed
    by switching to put_nowait, which does raise QueueFull immediately.

    Finding c (shared with recalibrate, see TestRecalibrateQueueBackpressure
    in test_recalibrate.py): once queue-full is actually reachable, the
    task_status row create_task() already wrote (status='queued') must be
    CAS'd to failed before the 503 goes out, otherwise the client is left
    polling a task_id that will never be picked up by any worker.
    """

    def test_queue_full_returns_503_and_marks_task_failed(
        self, mock_cache_manager, mock_audit_logger, mock_user_manager,
        mock_send_notification, mock_base_url,
    ):
        from video_transcript_api.utils.task_status import TaskStatus

        full_queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait({"placeholder": True})  # pre-fill: next put_nowait raises QueueFull

        with patch(
            "video_transcript_api.api.routes.tasks.get_task_queue",
            return_value=full_queue,
        ):
            app = _build_test_app()
            resp = TestClient(app).post(
                "/api/transcribe",
                json={"url": "https://www.youtube.com/watch?v=abc123"},
            )

        assert resp.status_code == 503
        mock_cache_manager.update_task_status.assert_called_once()
        call = mock_cache_manager.update_task_status.call_args
        assert call.args[0] == "task-abc-123"  # mock_cache_manager.create_task's fixed task_id
        assert call.args[1] == TaskStatus.FAILED
        assert "队列已满" in call.kwargs["error_message"]

    def test_queue_full_cleanup_write_failure_still_returns_503(
        self, mock_cache_manager, mock_audit_logger, mock_user_manager,
        mock_send_notification, mock_base_url,
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
        from video_transcript_api.api.context import _InflightTaskRegistry
        from video_transcript_api.api.routes.tasks import _TERMINAL_WRITE_FAILURE_NOTE

        mock_cache_manager.update_task_status.side_effect = RuntimeError("db locked")

        full_queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait({"placeholder": True})
        registry = _InflightTaskRegistry({"transcription": 5, "llm": 5})

        with patch(
            "video_transcript_api.api.routes.tasks.get_task_queue",
            return_value=full_queue,
        ), patch(
            "video_transcript_api.api.routes.tasks.get_inflight_registry",
            return_value=registry,
        ):
            app = _build_test_app()
            resp = TestClient(app).post(
                "/api/transcribe",
                json={"url": "https://www.youtube.com/watch?v=abc123"},
            )

        assert resp.status_code == 503
        assert _TERMINAL_WRITE_FAILURE_NOTE in resp.json()["detail"], (
            "the double failure must be surfaced through the response body, "
            "not just logged server-side"
        )
        assert registry.size("transcription") == 0, (
            "the in-flight quota must still be released even when the "
            "terminal-state cleanup write itself failed"
        )

    def test_queue_with_room_still_returns_202(
        self, client, mock_cache_manager,
    ):
        """Regression guard: put_nowait must not change behavior when the
        queue has room (mirrors test_transcribe_success but names the
        put_nowait switch explicitly as the thing under test here)."""
        resp = client.post(
            "/api/transcribe",
            json={"url": "https://www.youtube.com/watch?v=abc123"},
        )
        assert resp.status_code == 200
        assert resp.json()["code"] == 202
        mock_cache_manager.update_task_status.assert_not_called()

    def test_queue_full_writes_real_failed_row_with_snapshot(self, tmp_path, monkeypatch):
        """End-to-end against a real CacheManager (not the MagicMock used by
        the other tests in this class): confirms the task row actually
        lands in cache.db as failed with a populated terminal_snapshot, not
        just that update_task_status was called with the right mock args."""
        from video_transcript_api.api.routes import tasks as tasks_route
        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.utils.task_status import TaskStatus

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            monkeypatch.setattr(tasks_route, "cache_manager", cache_manager)
            monkeypatch.setattr(tasks_route, "audit_logger", MagicMock())

            full_queue = asyncio.Queue(maxsize=1)
            full_queue.put_nowait({"placeholder": True})
            monkeypatch.setattr(
                tasks_route, "get_task_queue", lambda: full_queue,
            )

            app = FastAPI()
            app.include_router(tasks_route.router)
            app.dependency_overrides[verify_token] = _fake_verify_token

            resp = TestClient(app).post(
                "/api/transcribe",
                json={"url": "https://www.youtube.com/watch?v=abc123"},
            )

            assert resp.status_code == 503

            rows = cache_manager.list_terminal_tasks(limit=10)
            assert len(rows) == 1, "the task create_task() wrote must have landed as a terminal row"
            new_task = cache_manager.get_task_by_id(rows[0]["task_id"])
            assert new_task["status"] == TaskStatus.FAILED
            assert "队列已满" in new_task["error_message"]
            snapshot = new_task.get("terminal_snapshot")
            assert snapshot is not None, "failed terminal write must carry a snapshot"
            assert snapshot["status"] == TaskStatus.FAILED
        finally:
            cache_manager.close()


class TestTranscribeGenericQueueException:
    """T1 (local codex review round 14): the window between the task row
    landing (create_task -> status='queued') and the queue handing the task
    off to a consumer (put_nowait succeeding) previously only CAS'd the row
    to failed for the asyncio.QueueFull branch (see
    TestTranscribeQueueBackpressure above). Any *other* exception raised
    while enqueueing (e.g. an unexpected error from the queue object itself)
    fell through to the generic `except Exception as queue_exc` clause,
    which only returned 500 without ever touching the already-created task
    row -- leaving the client polling a task_id no worker will ever pick up
    until the 24h reconciliation sweep catches it. Mirrors the QueueFull
    class's three tests but for this generic branch."""

    def test_generic_enqueue_exception_returns_500_and_marks_task_failed(
        self, mock_cache_manager, mock_audit_logger, mock_user_manager,
        mock_send_notification, mock_base_url,
    ):
        from video_transcript_api.utils.task_status import TaskStatus

        broken_queue = MagicMock()
        broken_queue.put_nowait.side_effect = RuntimeError("unexpected enqueue failure")

        with patch(
            "video_transcript_api.api.routes.tasks.get_task_queue",
            return_value=broken_queue,
        ):
            app = _build_test_app()
            resp = TestClient(app).post(
                "/api/transcribe",
                json={"url": "https://www.youtube.com/watch?v=abc123"},
            )

        assert resp.status_code == 500
        mock_cache_manager.update_task_status.assert_called_once()
        call = mock_cache_manager.update_task_status.call_args
        assert call.args[0] == "task-abc-123"  # mock_cache_manager.create_task's fixed task_id
        assert call.args[1] == TaskStatus.FAILED
        assert "任务加入队列失败" in call.kwargs["error_message"]

    def test_generic_enqueue_exception_cleanup_write_failure_still_returns_500(
        self, mock_cache_manager, mock_audit_logger, mock_user_manager,
        mock_send_notification, mock_base_url,
    ):
        """The failed-status CAS write is itself best-effort: if it raises
        (e.g. cache.db momentarily locked), that must be logged and
        swallowed, not let it mask the original enqueue-failure 500 behind
        an unrelated error -- the 24h reconciliation sweep is the only
        remaining backstop, so the original response must not regress.

        K1 (CI review round 3, major): this is exactly the double-failure
        request-path scenario -- the response must surface the fact that
        terminal-state cleanup itself failed (via a marker appended to
        detail), and the in-flight registry slot must still be released by
        the existing unconditional finally, not leaked."""
        from video_transcript_api.api.context import _InflightTaskRegistry
        from video_transcript_api.api.routes.tasks import _TERMINAL_WRITE_FAILURE_NOTE

        mock_cache_manager.update_task_status.side_effect = RuntimeError("db locked")

        broken_queue = MagicMock()
        broken_queue.put_nowait.side_effect = RuntimeError("unexpected enqueue failure")
        registry = _InflightTaskRegistry({"transcription": 5, "llm": 5})

        with patch(
            "video_transcript_api.api.routes.tasks.get_task_queue",
            return_value=broken_queue,
        ), patch(
            "video_transcript_api.api.routes.tasks.get_inflight_registry",
            return_value=registry,
        ):
            app = _build_test_app()
            resp = TestClient(app).post(
                "/api/transcribe",
                json={"url": "https://www.youtube.com/watch?v=abc123"},
            )

        assert resp.status_code == 500
        assert _TERMINAL_WRITE_FAILURE_NOTE in resp.json()["detail"], (
            "the double failure must be surfaced through the response body, "
            "not just logged server-side"
        )
        assert registry.size("transcription") == 0, (
            "the in-flight quota must still be released even when the "
            "terminal-state cleanup write itself failed"
        )

    def test_generic_enqueue_exception_writes_real_failed_row_with_snapshot(
        self, tmp_path, monkeypatch,
    ):
        """End-to-end against a real CacheManager (not the MagicMock used by
        the other tests in this class): confirms the task row actually
        lands in cache.db as failed with a populated terminal_snapshot, not
        just that update_task_status was called with the right mock args."""
        from video_transcript_api.api.routes import tasks as tasks_route
        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.cache.cache_manager import CacheManager
        from video_transcript_api.utils.task_status import TaskStatus

        cache_manager = CacheManager(str(tmp_path / "cache"))
        try:
            monkeypatch.setattr(tasks_route, "cache_manager", cache_manager)
            monkeypatch.setattr(tasks_route, "audit_logger", MagicMock())

            broken_queue = MagicMock()
            broken_queue.put_nowait.side_effect = RuntimeError("unexpected enqueue failure")
            monkeypatch.setattr(tasks_route, "get_task_queue", lambda: broken_queue)

            app = FastAPI()
            app.include_router(tasks_route.router)
            app.dependency_overrides[verify_token] = _fake_verify_token

            resp = TestClient(app).post(
                "/api/transcribe",
                json={"url": "https://www.youtube.com/watch?v=abc123"},
            )

            assert resp.status_code == 500

            rows = cache_manager.list_terminal_tasks(limit=10)
            assert len(rows) == 1, "the task create_task() wrote must have landed as a terminal row"
            new_task = cache_manager.get_task_by_id(rows[0]["task_id"])
            assert new_task["status"] == TaskStatus.FAILED
            assert "任务加入队列失败" in new_task["error_message"]
            snapshot = new_task.get("terminal_snapshot")
            assert snapshot is not None, "failed terminal write must carry a snapshot"
            assert snapshot["status"] == TaskStatus.FAILED
        finally:
            cache_manager.close()


class TestTranscribeInflightRegistryAdmission:
    """P1 (local codex review round 12): the queue-occupancy check above
    (TestTranscribeQueueBackpressure) only bounds items sitting in
    task_queue -- process_task_queue's consumer dequeues and immediately
    submits to an unbounded executor, freeing the queue slot before the
    work even starts (transcription.py:305/361/377). The fix moves the cap
    to admission: try_register before create_task/enqueue, release when the
    worker's future completes (see test_inflight_registry.py for the
    mechanism itself; these tests cover the /api/transcribe route's wiring
    to it)."""

    def test_registry_full_returns_503_without_creating_task_row(
        self, mock_cache_manager, mock_audit_logger, mock_user_manager,
        mock_send_notification, mock_base_url,
    ):
        from video_transcript_api.api.context import _InflightTaskRegistry

        full_registry = _InflightTaskRegistry({"transcription": 1, "llm": 1})
        full_registry.try_register("transcription", "already-in-flight")

        with patch(
            "video_transcript_api.api.routes.tasks.get_inflight_registry",
            return_value=full_registry,
        ):
            app = _build_test_app()
            resp = TestClient(app).post(
                "/api/transcribe",
                json={"url": "https://www.youtube.com/watch?v=abc123"},
            )

        assert resp.status_code == 503
        # "满载拒绝根本不落库" -- unlike the queue-full path (which CAS's an
        # already-created row to failed), registry-full rejection happens
        # before create_task is ever called.
        mock_cache_manager.create_task.assert_not_called()
        mock_cache_manager.update_task_status.assert_not_called()

    def test_registry_slot_released_when_create_task_raises(
        self, mock_cache_manager, mock_audit_logger, mock_user_manager,
        mock_send_notification, mock_base_url,
    ):
        from video_transcript_api.api.context import _InflightTaskRegistry

        registry = _InflightTaskRegistry({"transcription": 1, "llm": 1})
        mock_cache_manager.create_task.side_effect = RuntimeError("db down")

        with patch(
            "video_transcript_api.api.routes.tasks.get_inflight_registry",
            return_value=registry,
        ):
            app = _build_test_app()
            resp = TestClient(app).post(
                "/api/transcribe",
                json={"url": "https://www.youtube.com/watch?v=abc123"},
            )

        assert resp.status_code == 500
        assert registry.size("transcription") == 0, (
            "registration must be released when create_task itself fails, "
            "otherwise the slot is leaked forever"
        )

    def test_registry_slot_released_when_queue_full(
        self, mock_cache_manager, mock_audit_logger, mock_user_manager,
        mock_send_notification, mock_base_url,
    ):
        """Defense-in-depth path: the queue's own maxsize should rarely if
        ever be hit now that admission is gated by the registry first, but
        if it is (e.g. capacity mismatch), the registration must still be
        released -- otherwise the slot leaks even though the task row was
        already CAS'd to failed."""
        from video_transcript_api.api.context import _InflightTaskRegistry

        registry = _InflightTaskRegistry({"transcription": 5, "llm": 5})

        full_queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait({"placeholder": True})

        with patch(
            "video_transcript_api.api.routes.tasks.get_task_queue",
            return_value=full_queue,
        ), patch(
            "video_transcript_api.api.routes.tasks.get_inflight_registry",
            return_value=registry,
        ):
            app = _build_test_app()
            resp = TestClient(app).post(
                "/api/transcribe",
                json={"url": "https://www.youtube.com/watch?v=abc123"},
            )

        assert resp.status_code == 503
        assert registry.size("transcription") == 0

    def test_registry_slot_still_held_after_successful_admission(
        self, mock_cache_manager, mock_audit_logger, mock_user_manager,
        mock_send_notification, mock_base_url,
    ):
        """Registration is only released when the worker's future completes
        (RuntimeContext.track_future's completion callback) -- there is no
        real worker in this unit test (the mocked task_queue is never
        consumed), so a successful 202 response must leave the slot
        occupied, not free it on HTTP response alone."""
        from video_transcript_api.api.context import _InflightTaskRegistry

        registry = _InflightTaskRegistry({"transcription": 2, "llm": 2})
        roomy_queue = asyncio.Queue(maxsize=10)

        with patch(
            "video_transcript_api.api.routes.tasks.get_task_queue",
            return_value=roomy_queue,
        ), patch(
            "video_transcript_api.api.routes.tasks.get_inflight_registry",
            return_value=registry,
        ):
            app = _build_test_app()
            resp = TestClient(app).post(
                "/api/transcribe",
                json={"url": "https://www.youtube.com/watch?v=abc123"},
            )

        assert resp.status_code == 200
        assert resp.json()["code"] == 202
        assert registry.size("transcription") == 1


class TestGetTaskStatus:
    """Tests for GET /api/task/{task_id}.

    Status now comes from the persistent task_status table (single source of
    truth), read via cache_manager.get_task_by_id. The response data carries an
    explicit `status` field plus metadata; content is fetched via view_token.
    """

    def _row(self, **overrides):
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

    def test_task_not_found(self, client, mock_cache_manager):
        mock_cache_manager.get_task_by_id.return_value = None
        resp = client.get("/api/task/nonexistent-task")
        assert resp.status_code == 404

    def test_task_queued_returns_202(self, client, mock_cache_manager):
        mock_cache_manager.get_task_by_id.return_value = self._row(status="queued")
        body = client.get("/api/task/task-1").json()
        assert body["code"] == 202
        assert body["data"]["status"] == "queued"

    def test_task_processing_returns_202(self, client, mock_cache_manager):
        mock_cache_manager.get_task_by_id.return_value = self._row(status="processing")
        body = client.get("/api/task/task-1").json()
        assert body["code"] == 202
        assert body["data"]["status"] == "processing"

    def test_task_calibrating_returns_202(self, client, mock_cache_manager):
        # NEW state: transcript done, LLM calibration still running.
        mock_cache_manager.get_task_by_id.return_value = self._row(status="calibrating")
        body = client.get("/api/task/task-1").json()
        assert body["code"] == 202
        assert body["data"]["status"] == "calibrating"

    def test_task_success_returns_200_with_metadata(self, client, mock_cache_manager):
        mock_cache_manager.get_task_by_id.return_value = self._row(
            status="success", completed_at="2026-06-03T10:00:00"
        )
        body = client.get("/api/task/task-1").json()
        assert body["code"] == 200
        data = body["data"]
        assert data["status"] == "success"
        assert data["view_token"] == "vt-1"
        assert data["title"] == "Demo"
        assert data["author"] == "Alice"
        assert data["platform"] == "youtube"
        assert data["completed_at"] == "2026-06-03T10:00:00"
        # Inline transcript is intentionally dropped (fetch via view_token).
        assert "transcript" not in data

    def test_task_failed_returns_500_with_error(self, client, mock_cache_manager):
        mock_cache_manager.get_task_by_id.return_value = self._row(
            status="failed", error_message="ASR timeout"
        )
        resp = client.get("/api/task/task-1")
        body = resp.json()
        assert body["code"] == 500
        assert body["data"]["status"] == "failed"
        assert body["data"]["error"] == "ASR timeout"


class TestWebhookStatsEndpoint:
    """Tests for GET /api/webhook-stats (deprecated)."""

    def test_webhook_stats_returns_deprecated(self, client):
        resp = client.get("/api/webhook-stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["deprecated"] is True


class TestWebhookStatusEndpoint:
    """Tests for GET /api/webhook-status."""

    def test_webhook_status_returns_deprecated(self, client):
        resp = client.get(
            "/api/webhook-status?webhook_url=https://hook.example.com/abc"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["deprecated"] is True

    def test_webhook_status_truncates_long_url(self, client):
        long_url = "https://hook.example.com/" + "x" * 100
        resp = client.get(f"/api/webhook-status?webhook_url={long_url}")
        body = resp.json()
        assert body["data"]["webhook_url"].endswith("...")


# ===========================================================================
# User routes
# ===========================================================================


class TestUserProfile:
    """Tests for GET /api/users/profile."""

    def test_get_profile_success(self, client, mock_user_manager):
        resp = client.get("/api/users/profile")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 200
        assert "user_info" in body["data"]
        assert "is_multi_user_mode" in body["data"]

    def test_get_profile_masks_api_key(self, client, mock_user_manager):
        resp = client.get("/api/users/profile")
        body = resp.json()
        # The mock _mask_api_key returns "sk-****5678"
        assert body["data"]["user_info"]["api_key"] == "sk-****5678"
        mock_user_manager._mask_api_key.assert_called_once()

    def test_get_profile_error_returns_500(self, client, mock_user_manager):
        mock_user_manager.is_multi_user_mode.side_effect = RuntimeError("db error")
        resp = client.get("/api/users/profile")
        assert resp.status_code == 500


# ===========================================================================
# Views routes (public, no auth)
# ===========================================================================


class TestRobotsTxt:
    """Tests for GET /robots.txt."""

    def test_robots_txt_content(self, client):
        resp = client.get("/robots.txt")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/plain; charset=utf-8"
        text = resp.text
        assert "User-agent: *" in text
        assert "Disallow: /api/" in text
        assert "Sitemap:" in text
        assert "https://example.com/sitemap.xml" in text

    def test_robots_txt_allows_root(self, client):
        text = client.get("/robots.txt").text
        assert "Allow: /" in text


class TestSitemapXml:
    """Tests for GET /sitemap.xml."""

    def test_sitemap_xml_content(self, client):
        resp = client.get("/sitemap.xml")
        assert resp.status_code == 200
        assert "application/xml" in resp.headers["content-type"]
        text = resp.text
        assert "<urlset" in text
        assert "https://example.com/" in text

    def test_sitemap_xml_is_valid_xml(self, client):
        import xml.etree.ElementTree as ET
        resp = client.get("/sitemap.xml")
        # Should parse without errors
        ET.fromstring(resp.text)
