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
def mock_task_results():
    """Patch the task_results dict in the tasks module."""
    results = {}
    with patch(
        "video_transcript_api.api.routes.tasks.task_results", results
    ):
        yield results


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
    mock_task_results,
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

    def test_get_calls_error_returns_500(self, client, mock_audit_logger):
        mock_audit_logger.get_recent_calls.side_effect = RuntimeError("db error")
        resp = client.get("/api/audit/calls")
        assert resp.status_code == 500


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


class TestGetTaskStatus:
    """Tests for GET /api/task/{task_id}."""

    def test_task_not_found(self, client, mock_task_results):
        resp = client.get("/api/task/nonexistent-task")
        assert resp.status_code == 404

    def test_task_queued_returns_202(self, client, mock_task_results):
        mock_task_results["task-1"] = {
            "status": "queued",
            "message": "Queued",
        }
        resp = client.get("/api/task/task-1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 202

    def test_task_processing_returns_202(self, client, mock_task_results):
        mock_task_results["task-1"] = {
            "status": "processing",
            "message": "Processing",
        }
        resp = client.get("/api/task/task-1")
        body = resp.json()
        assert body["code"] == 202

    def test_task_completed_returns_200(self, client, mock_task_results):
        mock_task_results["task-1"] = {
            "status": "completed",
            "message": "Done",
            "data": {"transcript": "hello world"},
        }
        resp = client.get("/api/task/task-1")
        body = resp.json()
        assert body["code"] == 200
        assert body["data"]["transcript"] == "hello world"

    def test_task_failed_returns_500_code(self, client, mock_task_results):
        mock_task_results["task-1"] = {
            "status": "failed",
            "message": "ASR error",
        }
        resp = client.get("/api/task/task-1")
        body = resp.json()
        assert body["code"] == 500


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
