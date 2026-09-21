"""Unit tests for the export-file-path resolution helper in views.

All console output must be in English only (no emoji, no Chinese).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_transcript_api.api.routes.views import (
    generate_download_filename,
    handle_page_export,
    handle_raw_export,
    resolve_export_file_path,
)
from video_transcript_api.utils.llm_status import SummaryStatus


def _content_view_data(tmp_path, status):
    (tmp_path / "llm_calibrated.txt").write_text(
        "calibrated transcript body",
        encoding="utf-8",
    )
    return {
        "status": status,
        "cache_dir": str(tmp_path),
        "title": "Demo",
        "platform": "youtube",
        "url": "https://example.com/demo",
        "view_token": "view-interrupted",
    }


class TestResolveExportFilePath:
    def test_calibrated(self, tmp_path):
        p = resolve_export_file_path(str(tmp_path), "calibrated")
        assert p == tmp_path / "llm_calibrated.txt"

    def test_summary(self, tmp_path):
        p = resolve_export_file_path(str(tmp_path), "summary")
        assert p == tmp_path / "llm_summary.txt"

    def test_notes(self, tmp_path):
        p = resolve_export_file_path(str(tmp_path), "notes")
        assert p == tmp_path / "llm_notes.txt"

    def test_transcript_prefers_funasr_when_present(self, tmp_path):
        (tmp_path / "transcript_funasr.json").write_text("{}", encoding="utf-8")
        p = resolve_export_file_path(str(tmp_path), "transcript")
        assert p == tmp_path / "transcript_funasr.json"

    def test_transcript_falls_back_to_capswriter(self, tmp_path):
        # No funasr file present -> capswriter path (even if it doesn't exist yet).
        p = resolve_export_file_path(str(tmp_path), "transcript")
        assert p == tmp_path / "transcript_capswriter.txt"

    def test_unsupported_returns_none(self, tmp_path):
        assert resolve_export_file_path(str(tmp_path), "bogus") is None


def test_raw_notes_export_includes_notes_metadata(tmp_path):
    (tmp_path / "llm_notes.txt").write_text(
        "## Chapter\n- Detailed note.",
        encoding="utf-8",
    )
    response = handle_raw_export(
        {
            "status": "success",
            "cache_dir": str(tmp_path),
            "title": "Demo",
            "platform": "youtube",
            "url": "https://example.com/demo",
            "view_token": "view-notes",
            "notes_state": "generated",
        },
        "notes",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "Type: 详细笔记" in body
    assert "## Chapter" in body
    assert response.headers["x-content-type"] == "notes"


def test_notes_download_filename_uses_notes_label():
    assert (
        generate_download_filename("Demo", "youtube", "notes")
        == "Demo-详细笔记-YouTube.txt"
    )


def test_page_notes_export_renders_notes_content(tmp_path):
    (tmp_path / "llm_notes.txt").write_text(
        "## Chapter\n- Detailed note.",
        encoding="utf-8",
    )
    response = handle_page_export(
        {
            "status": "success",
            "cache_dir": str(tmp_path),
            "title": "Demo",
            "platform": "youtube",
            "url": "https://example.com/demo",
            "view_token": "view-notes",
            "notes_state": "generated",
        },
        "notes",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<title>Demo - 详细笔记</title>" in body
    assert '<h2 id="chapter">Chapter</h2>' in body
    assert "<li>Detailed note.</li>" in body


def test_notes_export_hides_artifact_without_generated_status(tmp_path):
    (tmp_path / "llm_notes.txt").write_text(
        "orphan notes artifact",
        encoding="utf-8",
    )
    response = handle_raw_export(
        {
            "status": "success",
            "cache_dir": str(tmp_path),
            "title": "Demo",
            "platform": "youtube",
            "view_token": "view-notes",
            "notes_state": "failed",
        },
        "notes",
    )

    assert response.status_code == 404
    assert "orphan notes artifact" not in response.body.decode("utf-8")


def test_raw_export_treats_interrupted_like_success(tmp_path):
    response = handle_raw_export(_content_view_data(tmp_path, "interrupted"), "calibrated")

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "calibrated transcript body" in body


def test_page_export_treats_interrupted_like_success(tmp_path):
    response = handle_page_export(_content_view_data(tmp_path, "interrupted"), "calibrated")

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "calibrated transcript body" in body


def test_raw_export_failed_without_payload_still_errors(tmp_path):
    response = handle_raw_export(
        {"status": "failed", "cache_dir": str(tmp_path), "title": "Demo"},
        "calibrated",
    )
    assert response.status_code == 500
    assert "calibrated transcript body" not in response.body.decode("utf-8")


def test_audit_summary_serves_interrupted_payload():
    """task_status stays failed; view_data interrupted is still summary-capable."""
    mock_cache = MagicMock()
    mock_cache.get_task_by_view_token.return_value = {
        "task_id": "task-interrupted",
        "status": "failed",
        "error_message": "orphan recovered after deploy restart",
    }
    mock_cache.get_view_data_by_token.return_value = {
        "status": "interrupted",
        "summary": "partial summary from before the restart",
        "summary_state": SummaryStatus.GENERATED,
    }

    async def _fake_verify_token():
        return {"user_id": "test-user", "api_key": "sk-test", "wechat_webhook": None}

    from video_transcript_api.api.services.transcription import verify_token
    from video_transcript_api.api.routes import audit

    app = FastAPI()
    app.include_router(audit.router)
    app.dependency_overrides[verify_token] = _fake_verify_token

    with patch.object(audit, "check_view_token_ownership", return_value=True), \
         patch.object(audit, "get_cache_manager", return_value=mock_cache), \
         patch.object(audit, "ViewTokenResolver", side_effect=lambda manager: manager):
        resp = TestClient(app).get("/api/audit/summary?view_token=vt-interrupted")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["code"] == 200
    assert payload["data"]["summary"] == "partial summary from before the restart"


def test_audit_summary_failed_without_payload_keeps_202():
    mock_cache = MagicMock()
    mock_cache.get_task_by_view_token.return_value = {
        "task_id": "task-failed",
        "status": "failed",
        "error_message": "download failed",
    }
    mock_cache.get_view_data_by_token.return_value = {
        "status": "failed",
        "message": "download failed",
    }

    async def _fake_verify_token():
        return {"user_id": "test-user", "api_key": "sk-test", "wechat_webhook": None}

    from video_transcript_api.api.services.transcription import verify_token
    from video_transcript_api.api.routes import audit

    app = FastAPI()
    app.include_router(audit.router)
    app.dependency_overrides[verify_token] = _fake_verify_token

    with patch.object(audit, "check_view_token_ownership", return_value=True), \
         patch.object(audit, "get_cache_manager", return_value=mock_cache), \
         patch.object(audit, "ViewTokenResolver", side_effect=lambda manager: manager):
        resp = TestClient(app).get("/api/audit/summary?view_token=vt-failed")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["code"] == 202
    assert payload["data"]["summary"] == ""
    assert payload["data"]["status"] == "failed"
