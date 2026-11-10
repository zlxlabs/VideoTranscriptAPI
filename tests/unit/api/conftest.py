"""Fixtures for the GET /api/task/{task_id} long-poll tests (design C3).

Mirrors the fixture pattern of tests/unit/test_api_routes.py but scoped to
what this suite needs: tasks router (status endpoint) + health router
(/livez probe used to prove the event loop is not blocked while a wait
request is suspended).

The status endpoint implementation lives in routes/task_status_view.py
(tasks.py is over the file-size ratchet), so cache_manager / audit_logger
are patched there.
"""

from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

_FAKE_USER_INFO = {
    "user_id": "test-user",
    "api_key": "sk-test-key-123456",
    "wechat_webhook": None,
}


async def _fake_verify_token():
    return _FAKE_USER_INFO


def _build_app() -> FastAPI:
    app = FastAPI()
    from video_transcript_api.api.context import install_api_error_handlers

    install_api_error_handlers(app)

    from video_transcript_api.api.routes import health, tasks
    from video_transcript_api.api.services.transcription import verify_token

    app.include_router(tasks.router)
    app.include_router(health.router)
    app.dependency_overrides[verify_token] = _fake_verify_token
    return app


@pytest.fixture()
def mock_audit_logger():
    mock = MagicMock()
    with patch(
        "video_transcript_api.api.routes.task_status_view.audit_logger", mock
    ):
        yield mock


@pytest.fixture()
def mock_cache_manager():
    mock = MagicMock()
    with patch(
        "video_transcript_api.api.routes.task_status_view.cache_manager", mock
    ):
        yield mock


@pytest.fixture()
def client(mock_audit_logger, mock_cache_manager):
    return TestClient(_build_app())


@pytest_asyncio.fixture()
async def async_client(mock_audit_logger, mock_cache_manager):
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
