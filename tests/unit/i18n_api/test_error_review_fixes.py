"""Regression tests for API error review fixes."""

import re
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from video_transcript_api.api.context import ApiError, install_api_error_handlers
from video_transcript_api.i18n import t
from video_transcript_api.utils.logging.logger import load_config

_CJK = re.compile(r"[\u3400-\u9fff]")


@pytest.fixture
def language_config():
    config = load_config()
    had_key = "language" in config
    previous = config.get("language")

    def set_language(language):
        if language is None:
            config.pop("language", None)
        else:
            config["language"] = language

    try:
        yield set_language
    finally:
        if had_key:
            config["language"] = previous
        else:
            config.pop("language", None)


def test_english_queue_errors_describe_distinct_capacity_limits(language_config):
    language_config("en")

    llm_queue_full = t("error.llm_queue_full", note="")
    transcription_queue_full = t("error.transcription_queue_full", note="")
    task_capacity_reached = t("error.task_capacity_reached")

    assert llm_queue_full != transcription_queue_full
    assert llm_queue_full != task_capacity_reached
    assert transcription_queue_full != task_capacity_reached


def test_task_route_preserves_plain_http_exception_status(language_config, monkeypatch):
    language_config("en")
    from video_transcript_api.api.routes import task_status_view, tasks
    from video_transcript_api.api.services.transcription import verify_token

    cache_manager = MagicMock()
    cache_manager.get_task_by_id.side_effect = HTTPException(418, detail="teapot")
    monkeypatch.setattr(task_status_view, "cache_manager", cache_manager)
    app = FastAPI()
    install_api_error_handlers(app)
    app.include_router(tasks.router)
    app.dependency_overrides[verify_token] = lambda: {"user_id": "test-user", "api_key": "test-key"}

    response = TestClient(app).get("/api/task/probe")

    assert response.status_code == 418
    assert response.json() == {"detail": "teapot", "error_code": "api_error"}


def test_audit_route_preserves_plain_http_exception_status(language_config, monkeypatch):
    language_config("en")
    from video_transcript_api.api.routes import audit
    from video_transcript_api.api.services.transcription import verify_token

    audit_logger = MagicMock()
    audit_logger.get_recent_calls.side_effect = HTTPException(418, detail="teapot")
    monkeypatch.setattr(audit, "audit_logger", audit_logger)
    app = FastAPI()
    install_api_error_handlers(app)
    app.include_router(audit.router)
    app.dependency_overrides[verify_token] = lambda: {"user_id": "test-user", "api_key": "test-key"}

    response = TestClient(app).get("/api/audit/calls")

    assert response.status_code == 418
    assert response.json() == {"detail": "teapot", "error_code": "api_error"}


def test_error_handler_serializes_error_code_and_preserves_fastapi_422(language_config):
    language_config("en")
    app = FastAPI()
    install_api_error_handlers(app)

    @app.get("/api/rejected")
    async def rejected():
        raise ApiError(status_code=401, error_code="authorization_token_invalid")

    @app.get("/api/validated")
    async def validated(value: int):
        return {"value": value}

    @app.get("/api/explicit-422")
    async def explicit_422():
        raise HTTPException(status_code=422, detail="explicit rejection")

    @app.get("/api/no-content")
    async def no_content():
        raise HTTPException(status_code=204, detail="discarded response body")

    client = TestClient(app)
    error_response = client.get("/api/rejected")
    validation_response = client.get("/api/validated?value=invalid")
    explicit_422_response = client.get("/api/explicit-422")
    no_content_response = client.get("/api/no-content")

    assert error_response.status_code == 401
    assert error_response.json() == {
        "detail": "The authorization token is invalid",
        "error_code": "authorization_token_invalid",
    }
    assert validation_response.status_code == 422
    assert set(validation_response.json()) == {"detail"}
    assert explicit_422_response.status_code == 422
    assert explicit_422_response.json() == {
        "detail": "explicit rejection",
        "error_code": "api_error",
    }
    assert no_content_response.status_code == 204
    assert no_content_response.content == b""
    assert "content-type" not in no_content_response.headers


@pytest.mark.parametrize(
    ("error_code", "values", "expected_zh"),
    [
        ("upload_metadata", {"detail": "invalid field"}, "上传元数据无效：invalid field"),
        ("upload_idempotency", {"detail": "key conflict"}, "上传幂等请求无法受理：key conflict"),
        (
            "task_failure.media_admission_failed",
            {"error": "audio track missing"},
            "媒体准入失败：audio track missing",
        ),
    ],
)
def test_upload_failures_have_localized_prefixes(language_config, error_code, values, expected_zh):
    language_config("zh-CN")
    assert t(f"error.{error_code}", **values) == expected_zh

    language_config("en")
    english = t(f"error.{error_code}", **values)
    assert all(value in english for value in values.values())
    assert _CJK.search(english) is None
