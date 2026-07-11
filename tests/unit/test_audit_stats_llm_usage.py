"""
GET /api/audit/stats -- llm_usage aggregation block tests.

Covers:
- /stats response includes a "llm_usage" block alongside "user_stats"
- llm_usage aggregates by stage (call_count/prompt/completion/total tokens,
  usage_missing_count) plus an overall "total"
- the existing "days" query param controls the llm_usage window too
- empty DB -> zeroed llm_usage structure, not an error

Strategy: real temp SQLite DBs (AuditLogger + UsageRecorder bound to the same
temp audit.db), matching the pattern used in test_history_routes.py. Module-
level singletons (audit.audit_logger / audit.usage_recorder) are patched to
point at the temp instances.

All console output must be in English only (no emoji, no Chinese).
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_transcript_api.utils.logging.audit_logger import AuditLogger
from video_transcript_api.utils.logging.usage_recorder import UsageRecorder

_API_KEY = "sk-test-key-123456"


@pytest.fixture()
def stats_client(tmp_path):
    """Build a TestClient for /api/audit/stats backed by real temp DBs."""
    audit_db_path = str(tmp_path / "audit.db")
    al = AuditLogger(db_path=audit_db_path)
    recorder = UsageRecorder(audit_logger=al)

    async def _fake_verify_token():
        return {"user_id": "test-user", "api_key": _API_KEY, "wechat_webhook": None}

    from video_transcript_api.api.services.transcription import verify_token
    from video_transcript_api.api.routes import audit

    app = FastAPI()
    app.include_router(audit.router)
    app.dependency_overrides[verify_token] = _fake_verify_token

    with patch("video_transcript_api.api.routes.audit.audit_logger", al), \
         patch("video_transcript_api.api.routes.audit.usage_recorder", recorder):
        yield TestClient(app), al, recorder


class TestStatsLLMUsageBlock:
    def test_stats_response_includes_llm_usage_block(self, stats_client):
        client, _, _ = stats_client
        resp = client.get("/api/audit/stats")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "llm_usage" in data
        assert "user_stats" in data  # existing block must remain intact

    def test_empty_db_returns_zeroed_llm_usage(self, stats_client):
        client, _, _ = stats_client
        resp = client.get("/api/audit/stats")
        llm_usage = resp.json()["data"]["llm_usage"]
        assert llm_usage["by_stage"] == []
        assert llm_usage["total"]["call_count"] == 0
        assert llm_usage["total"]["total_tokens"] == 0

    def test_llm_usage_aggregates_by_stage(self, stats_client):
        client, _, recorder = stats_client

        recorder.record(
            task_id="t1", stage="calibration", model="m1",
            prompt_tokens=100, completion_tokens=50, total_tokens=150,
            duration_ms=10, usage_missing=False,
        )
        recorder.record(
            task_id="t2", stage="calibration", model="m1",
            prompt_tokens=200, completion_tokens=100, total_tokens=300,
            duration_ms=20, usage_missing=False,
        )
        recorder.record(
            task_id="t3", stage="summary", model="m1",
            prompt_tokens=10, completion_tokens=10, total_tokens=20,
            duration_ms=5, usage_missing=True,
        )

        resp = client.get("/api/audit/stats")
        llm_usage = resp.json()["data"]["llm_usage"]

        by_stage = {row["stage"]: row for row in llm_usage["by_stage"]}
        assert by_stage["calibration"]["call_count"] == 2
        assert by_stage["calibration"]["total_tokens"] == 450
        assert by_stage["summary"]["call_count"] == 1
        assert by_stage["summary"]["usage_missing_count"] == 1

        assert llm_usage["total"]["call_count"] == 3
        assert llm_usage["total"]["total_tokens"] == 470

    def test_days_param_controls_llm_usage_window(self, stats_client):
        client, al, recorder = stats_client

        # Insert an old row directly (bypassing record()'s "now" timestamp)
        with al._get_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO llm_usage
                (task_id, stage, model, prompt_tokens, completion_tokens,
                 total_tokens, duration_ms, usage_missing, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("old-task", "calibration", "m", 10, 10, 20, 5, 0, "2020-01-01 00:00:00"),
            )

        recorder.record(
            task_id="new-task", stage="calibration", model="m",
            prompt_tokens=1, completion_tokens=1, total_tokens=2,
            duration_ms=1, usage_missing=False,
        )

        resp = client.get("/api/audit/stats", params={"days": 1})
        llm_usage = resp.json()["data"]["llm_usage"]
        assert llm_usage["total"]["call_count"] == 1
        assert llm_usage["total"]["total_tokens"] == 2
        assert llm_usage["days"] == 1
