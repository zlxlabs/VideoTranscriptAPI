"""
History route tests.

Covers:
- GET /api/audit/history  : filter combos, pagination, empty, cross-DB JOIN, DB locked
- GET /api/audit/filter-options : distinct webhook/platform/author per API key
- GET /api/audit/summary  : happy path, 404, 202 (processing), 403 (cross-user)

Strategy: real SQLite temp DBs (not mocks) so ATTACH JOIN actually runs.
Module-level singletons are patched to point at the temp files.

All console output must be in English only (no emoji, no Chinese).
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_transcript_api.utils.logging.audit_logger import AuditLogger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_KEY      = "sk-test-key-123456"
_API_KEY_MASK = "sk-t**********3456"   # len=18, first4="sk-t", last4="3456"
_WEBHOOK_A    = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=aaa"
_WEBHOOK_B    = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=bbb"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_pair(tmp_path):
    """
    Create a real audit.db + cache.db pair in a temp directory.

    Returns a dict with:
      - audit_logger: real AuditLogger instance
      - cache_db_path: str path to the cache SQLite file
      - insert_task: helper to insert a row in task_status
    """
    audit_db_path = str(tmp_path / "audit.db")
    cache_db_path = str(tmp_path / "cache.db")

    # Real AuditLogger (runs migrations, creates api_audit_logs)
    al = AuditLogger(db_path=audit_db_path)

    # Minimal cache.db matching production task_status schema
    # (includes calibration_status/summary_status: the honest-status-model
    # columns that /api/audit/history now selects)
    conn = sqlite3.connect(cache_db_path)
    conn.execute('''
        CREATE TABLE task_status (
            task_id    TEXT PRIMARY KEY,
            view_token TEXT NOT NULL,
            url        TEXT,
            platform   TEXT,
            media_id   TEXT,
            use_speaker_recognition INTEGER DEFAULT 0,
            status     TEXT NOT NULL DEFAULT 'queued',
            title      TEXT,
            author     TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            cache_id   TEXT,
            llm_config TEXT,
            download_url TEXT,
            calibration_status TEXT,
            summary_status TEXT
        )
    ''')
    conn.commit()
    conn.close()

    def insert_task(task_id, view_token, platform="youtube", title="Test Title",
                    author="Test Author", status="success",
                    calibration_status=None, summary_status=None):
        c = sqlite3.connect(cache_db_path)
        c.execute(
            "INSERT OR IGNORE INTO task_status "
            "(task_id, view_token, platform, title, author, status, "
            "calibration_status, summary_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, view_token, platform, title, author, status,
             calibration_status, summary_status),
        )
        c.commit()
        c.close()

    return {
        "audit_logger": al,
        "cache_db_path": cache_db_path,
        "insert_task": insert_task,
    }


@pytest.fixture()
def history_client(db_pair):
    """
    Build a TestClient with /api/audit/* routes, backed by real temp DBs.
    Patches:
      - audit.audit_logger  -> real AuditLogger (temp DB)
      - audit.get_cache_manager -> mock whose .db_path = temp cache.db
    """
    al = db_pair["audit_logger"]

    mock_cache = MagicMock()
    mock_cache.db_path = Path(db_pair["cache_db_path"])

    async def _fake_verify_token():
        return {"user_id": "test-user", "api_key": _API_KEY, "wechat_webhook": None}

    from video_transcript_api.api.services.transcription import verify_token
    from video_transcript_api.api.routes import audit

    app = FastAPI()
    app.include_router(audit.router)
    app.dependency_overrides[verify_token] = _fake_verify_token

    with patch("video_transcript_api.api.routes.audit.audit_logger", al), \
         patch("video_transcript_api.api.routes.audit.get_cache_manager", return_value=mock_cache):
        yield TestClient(app), db_pair


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(al, task_id, webhook=_WEBHOOK_A, status_code=202):
    """Insert one audit row using the real AuditLogger."""
    al.log_api_call(
        api_key=_API_KEY,
        user_id="test-user",
        endpoint="/api/transcribe",
        video_url="https://youtube.com/watch?v=abc",
        status_code=status_code,
        task_id=task_id,
        wechat_webhook=webhook,
    )


# ===========================================================================
# GET /api/audit/history
# ===========================================================================

class TestHistoryEndpoint:
    """Tests for GET /api/audit/history."""

    def test_empty_history_returns_empty_list(self, history_client):
        """No records -> 200 with empty items list."""
        client, _ = history_client
        resp = client.get("/api/audit/history")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0

    def test_returns_completed_by_default(self, history_client):
        """Default status filter returns only completed tasks (LEFT JOIN match)."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "task-done")
        _log(al, "task-proc")
        insert("task-done", "vt-done", status="success")
        insert("task-proc", "vt-proc", status="processing")

        resp = client.get("/api/audit/history")
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        task_ids = {i["task_id"] for i in items}
        assert "task-done" in task_ids
        assert "task-proc" not in task_ids

    def test_status_all_returns_every_record(self, history_client):
        """status=all should bypass the default completed filter."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "task-done")
        _log(al, "task-proc")
        insert("task-done", "vt-done", status="success")
        insert("task-proc", "vt-proc", status="processing")

        resp = client.get("/api/audit/history?status=all")
        assert resp.status_code == 200
        task_ids = {i["task_id"] for i in resp.json()["data"]["items"]}
        assert "task-done" in task_ids
        assert "task-proc" in task_ids

    def test_filter_by_webhook(self, history_client):
        """webhook filter should return only matching rows."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "ta", webhook=_WEBHOOK_A)
        _log(al, "tb", webhook=_WEBHOOK_B)
        insert("ta", "vt-a"); insert("tb", "vt-b")

        resp = client.get(f"/api/audit/history?webhook={_WEBHOOK_A}")
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["task_id"] == "ta"

    def test_filter_by_platform(self, history_client):
        """platform filter joins cache.db and returns matching rows only."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "yt-task"); _log(al, "bili-task")
        insert("yt-task", "vt-yt", platform="youtube")
        insert("bili-task", "vt-bili", platform="bilibili")

        resp = client.get("/api/audit/history?platform=youtube")
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert all(i["platform"] == "youtube" for i in items)
        task_ids = {i["task_id"] for i in items}
        assert "bili-task" not in task_ids

    def test_filter_by_author(self, history_client):
        """author filter returns only tasks from that channel."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "t1"); _log(al, "t2")
        insert("t1", "vt1", author="Channel A")
        insert("t2", "vt2", author="Channel B")

        resp = client.get("/api/audit/history?author=Channel+A")
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["author"] == "Channel A"

    def test_filter_by_multiple_authors(self, history_client):
        """author=A,B (comma-separated) returns tasks from both channels."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "t1"); _log(al, "t2"); _log(al, "t3")
        insert("t1", "vt1", author="Channel A")
        insert("t2", "vt2", author="Channel B")
        insert("t3", "vt3", author="Channel C")

        resp = client.get("/api/audit/history?author=Channel+A,Channel+B")
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        authors = {i["author"] for i in items}
        assert authors == {"Channel A", "Channel B"}
        assert resp.json()["data"]["total"] == 2

    def test_filter_by_single_author_still_works(self, history_client):
        """Single author (no comma) still works as exact match."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "t1"); _log(al, "t2")
        insert("t1", "vt1", author="Only This")
        insert("t2", "vt2", author="Not This")

        resp = client.get("/api/audit/history?author=Only+This")
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 1
        assert resp.json()["data"]["items"][0]["author"] == "Only This"

    def test_filter_by_date_range(self, history_client):
        """start_date and end_date filter by request_time."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        # Insert records at specific times via direct SQL
        conn = sqlite3.connect(al.db_path)
        conn.execute(
            "INSERT INTO api_audit_logs "
            "(api_key_masked, user_id, endpoint, task_id, wechat_webhook, request_time, status_code)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_API_KEY_MASK, "test-user", "/api/transcribe", "old-task",
             _WEBHOOK_A, "2023-01-01 10:00:00", 202),
        )
        conn.execute(
            "INSERT INTO api_audit_logs "
            "(api_key_masked, user_id, endpoint, task_id, wechat_webhook, request_time, status_code)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_API_KEY_MASK, "test-user", "/api/transcribe", "new-task",
             _WEBHOOK_A, "2025-06-15 10:00:00", 202),
        )
        conn.commit()
        conn.close()
        insert("old-task", "vt-old"); insert("new-task", "vt-new")

        resp = client.get("/api/audit/history?start_date=2025-01-01&end_date=2025-12-31")
        assert resp.status_code == 200
        task_ids = {i["task_id"] for i in resp.json()["data"]["items"]}
        assert "new-task" in task_ids
        assert "old-task" not in task_ids

    def test_pagination_limit_and_offset(self, history_client):
        """limit and offset should correctly slice results."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        for i in range(5):
            _log(al, f"task-{i}")
            insert(f"task-{i}", f"vt-{i}")

        resp = client.get("/api/audit/history?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["items"]) == 2
        assert data["total"] == 5

        resp2 = client.get("/api/audit/history?limit=2&offset=4")
        assert resp2.status_code == 200
        assert len(resp2.json()["data"]["items"]) == 1

    def test_total_reflects_filtered_count(self, history_client):
        """total should be the filtered count, not the overall table count."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "ta", webhook=_WEBHOOK_A)
        _log(al, "tb", webhook=_WEBHOOK_A)
        _log(al, "tc", webhook=_WEBHOOK_B)
        insert("ta", "vt-a"); insert("tb", "vt-b"); insert("tc", "vt-c")

        resp = client.get(f"/api/audit/history?webhook={_WEBHOOK_A}")
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 2

    def test_left_join_null_task_included_when_status_all(self, history_client):
        """Audit rows with no matching task_status (no cache record) still appear with status=all."""
        client, setup = history_client
        al = setup["audit_logger"]

        # Insert audit row but no matching cache row
        _log(al, "orphan-task")

        resp = client.get("/api/audit/history?status=all")
        assert resp.status_code == 200
        task_ids = {i["task_id"] for i in resp.json()["data"]["items"]}
        assert "orphan-task" in task_ids

    def test_response_includes_view_token_and_title(self, history_client):
        """Items should carry view_token and title from the cache JOIN."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "task-vt")
        insert("task-vt", "my-view-token-abc", title="My Video Title")

        resp = client.get("/api/audit/history")
        assert resp.status_code == 200
        item = next(i for i in resp.json()["data"]["items"] if i["task_id"] == "task-vt")
        assert item["view_token"] == "my-view-token-abc"
        assert item["title"] == "My Video Title"

    def test_response_includes_calibration_and_summary_status(self, history_client):
        """Items should carry the honest-status-model columns from the cache JOIN
        (frontend does not consume them yet, but the API must surface them)."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "task-status")
        insert("task-status", "vt-status", calibration_status="partial",
               summary_status="generated")

        resp = client.get("/api/audit/history")
        assert resp.status_code == 200
        item = next(i for i in resp.json()["data"]["items"] if i["task_id"] == "task-status")
        assert item["calibration_status"] == "partial"
        assert item["summary_status"] == "generated"

    def test_calibration_and_summary_status_null_when_not_set(self, history_client):
        """Tasks without these columns populated must not break the response (None, not KeyError)."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "task-nostatus")
        insert("task-nostatus", "vt-nostatus")

        resp = client.get("/api/audit/history")
        assert resp.status_code == 200
        item = next(i for i in resp.json()["data"]["items"] if i["task_id"] == "task-nostatus")
        assert item["calibration_status"] is None
        assert item["summary_status"] is None

    def test_api_key_masked_in_response(self, history_client):
        """Response data should include api_key_masked for localStorage key construction."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "task-x")
        insert("task-x", "vt-x")

        resp = client.get("/api/audit/history")
        assert resp.status_code == 200
        assert resp.json()["data"]["api_key_masked"] == _API_KEY_MASK

    def test_different_api_key_sees_no_records(self, history_client):
        """Records belonging to one API key should not be visible to another."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        # Insert record with a DIFFERENT key
        al.log_api_call(
            api_key="other-key-9999",
            user_id="other-user",
            endpoint="/api/transcribe",
            task_id="other-task",
        )
        insert("other-task", "vt-other")

        resp = client.get("/api/audit/history?status=all")
        assert resp.status_code == 200
        task_ids = {i["task_id"] for i in resp.json()["data"]["items"]}
        assert "other-task" not in task_ids


# ===========================================================================
# GET /api/audit/history — search (q parameter)
# ===========================================================================

class TestHistorySearch:
    """Tests for full-text search via ?q= parameter in /api/audit/history."""

    def test_search_by_title_returns_match(self, history_client):
        """q matching part of a title returns that task."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "task-a"); _log(al, "task-b")
        insert("task-a", "vt-a", title="Python 编程实战")
        insert("task-b", "vt-b", title="JavaScript 入门教程")

        resp = client.get("/api/audit/history?q=Python")
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        task_ids = {i["task_id"] for i in items}
        assert "task-a" in task_ids
        assert "task-b" not in task_ids

    def test_search_by_author_returns_match(self, history_client):
        """q matching part of author/channel name returns that task."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "ta"); _log(al, "tb")
        insert("ta", "vt-a", author="技术蛋老师")
        insert("tb", "vt-b", author="李永乐老师")

        resp = client.get("/api/audit/history?q=技术蛋")
        assert resp.status_code == 200
        task_ids = {i["task_id"] for i in resp.json()["data"]["items"]}
        assert "ta" in task_ids
        assert "tb" not in task_ids

    def test_search_by_video_url_returns_match(self, history_client):
        """q matching part of video_url returns that task."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        # Insert audit record with specific URL
        al.log_api_call(
            api_key=_API_KEY, user_id="test-user", endpoint="/api/transcribe",
            video_url="https://youtu.be/abc123xyz", status_code=202, task_id="task-url",
            wechat_webhook=_WEBHOOK_A,
        )
        al.log_api_call(
            api_key=_API_KEY, user_id="test-user", endpoint="/api/transcribe",
            video_url="https://bilibili.com/video/BV999", status_code=202, task_id="task-other-url",
            wechat_webhook=_WEBHOOK_A,
        )
        insert("task-url", "vt-url"); insert("task-other-url", "vt-other-url")

        resp = client.get("/api/audit/history?q=abc123xyz")
        assert resp.status_code == 200
        task_ids = {i["task_id"] for i in resp.json()["data"]["items"]}
        assert "task-url" in task_ids
        assert "task-other-url" not in task_ids

    def test_search_no_match_returns_empty(self, history_client):
        """q that matches nothing returns empty list and total=0."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "task-x")
        insert("task-x", "vt-x", title="机器学习入门")

        resp = client.get("/api/audit/history?q=不存在的关键词xyz")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0

    def test_search_empty_q_returns_all(self, history_client):
        """q='' (empty string) behaves same as no q — returns all records."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "t1"); _log(al, "t2")
        insert("t1", "vt1"); insert("t2", "vt2")

        resp = client.get("/api/audit/history?q=")
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 2

    def test_search_combined_with_platform_filter(self, history_client):
        """q + platform together narrow results correctly."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "yt"); _log(al, "bili")
        insert("yt",   "vt-yt",   title="AI 教程", platform="youtube")
        insert("bili", "vt-bili", title="AI 入门",  platform="bilibili")

        resp = client.get("/api/audit/history?q=AI&platform=youtube")
        assert resp.status_code == 200
        task_ids = {i["task_id"] for i in resp.json()["data"]["items"]}
        assert "yt" in task_ids
        assert "bili" not in task_ids

    def test_large_limit_for_client_side_filtering(self, history_client):
        """limit=10000 should be accepted (needed when client applies read filter)."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "t1"); insert("t1", "vt1")

        resp = client.get("/api/audit/history?limit=10000")
        assert resp.status_code == 200
        assert resp.json()["data"]["limit"] == 10000

    def test_search_total_reflects_filtered_count(self, history_client):
        """total in response equals the number of records matching q."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        for i in range(3):
            _log(al, f"match-{i}")
            insert(f"match-{i}", f"vt-m{i}", title="深度学习")
        _log(al, "nomatch")
        insert("nomatch", "vt-nm", title="其他内容")

        resp = client.get("/api/audit/history?q=深度学习")
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 3


# ===========================================================================
# GET /api/audit/filter-options
# ===========================================================================

class TestFilterOptionsEndpoint:
    """Tests for GET /api/audit/filter-options."""

    def test_empty_db_returns_empty_lists(self, history_client):
        """No audit records -> all filter options are empty lists."""
        client, _ = history_client
        resp = client.get("/api/audit/filter-options")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["webhooks"] == []
        assert data["platforms"] == []
        assert data["authors"] == []

    def test_returns_distinct_webhooks(self, history_client):
        """Repeated webhook calls should appear only once."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        for i in range(3):
            _log(al, f"t{i}", webhook=_WEBHOOK_A)
        _log(al, "t3", webhook=_WEBHOOK_B)
        for i in range(4):
            insert(f"t{i}", f"vt{i}")

        resp = client.get("/api/audit/filter-options")
        assert resp.status_code == 200
        webhooks = resp.json()["data"]["webhooks"]
        assert len(webhooks) == 2
        assert _WEBHOOK_A in webhooks
        assert _WEBHOOK_B in webhooks

    def test_returns_distinct_platforms(self, history_client):
        """Platforms are deduplicated from the cache JOIN."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        for i, plat in enumerate(["youtube", "youtube", "bilibili"]):
            _log(al, f"tp{i}")
            insert(f"tp{i}", f"vtp{i}", platform=plat)

        resp = client.get("/api/audit/filter-options")
        assert resp.status_code == 200
        platforms = resp.json()["data"]["platforms"]
        assert set(platforms) == {"youtube", "bilibili"}

    def test_returns_distinct_authors(self, history_client):
        """Authors are deduplicated, ordered by frequency."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        for i, author in enumerate(["Chan A", "Chan A", "Chan B"]):
            _log(al, f"ta{i}")
            insert(f"ta{i}", f"vta{i}", author=author)

        resp = client.get("/api/audit/filter-options")
        assert resp.status_code == 200
        authors = resp.json()["data"]["authors"]
        # Chan A appears twice so should come first
        assert authors[0] == "Chan A"
        assert "Chan B" in authors

    def test_webhooks_isolated_per_api_key(self, history_client):
        """Webhooks from another API key must not appear in filter-options."""
        client, setup = history_client
        al = setup["audit_logger"]

        al.log_api_call(
            api_key="other-key-9999",
            user_id="other",
            endpoint="/api/transcribe",
            wechat_webhook="https://other-webhook.example.com",
        )

        resp = client.get("/api/audit/filter-options")
        assert resp.status_code == 200
        assert "https://other-webhook.example.com" not in resp.json()["data"]["webhooks"]


# ===========================================================================
# GET /api/audit/summary
# ===========================================================================


@pytest.fixture()
def summary_setup(db_pair):
    """
    Fixture for summary endpoint tests.

    Yields (client, db_pair, mock_cache) with patches active for the entire test.
    Tests configure mock_cache return values before calling client.get().
    """
    al = db_pair["audit_logger"]

    mock_cache = MagicMock()
    mock_cache.db_path = Path(db_pair["cache_db_path"])

    async def _fake_verify_token():
        return {"user_id": "test-user", "api_key": _API_KEY, "wechat_webhook": None}

    from video_transcript_api.api.services.transcription import verify_token
    from video_transcript_api.api.routes import audit

    app = FastAPI()
    app.include_router(audit.router)
    app.dependency_overrides[verify_token] = _fake_verify_token

    # Keep patches active for the lifetime of the test (yield, not return)
    with patch("video_transcript_api.api.routes.audit.audit_logger", al), \
         patch("video_transcript_api.api.routes.audit.get_cache_manager", return_value=mock_cache):
        yield TestClient(app), db_pair, mock_cache


class TestSummaryEndpoint:
    """Tests for GET /api/audit/summary."""

    def test_returns_summary_for_completed_task(self, summary_setup):
        """Happy path: completed task with summary returns 200 and preview text (max 300 chars)."""
        client, db_pair, mock_cache = summary_setup
        al = db_pair["audit_logger"]
        _log(al, "task-sum")   # audit record lets auth pass

        long_summary = "A" * 500
        mock_cache.get_task_by_view_token.return_value = {"task_id": "task-sum", "status": "success"}
        mock_cache.get_view_data_by_token.return_value = {"status": "success", "summary": long_summary}

        resp = client.get("/api/audit/summary?view_token=vt-sum")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "success"
        assert len(data["summary"]) == 300
        assert data["summary"] == long_summary[:300]

    def test_returns_202_for_processing_task(self, summary_setup):
        """A task still processing returns code=202 with empty summary."""
        client, _, mock_cache = summary_setup
        mock_cache.get_task_by_view_token.return_value = {"task_id": "task-proc", "status": "processing"}

        resp = client.get("/api/audit/summary?view_token=vt-proc")
        assert resp.status_code == 200
        assert resp.json()["code"] == 202
        assert resp.json()["data"]["summary"] == ""

    def test_returns_404_for_unknown_view_token(self, summary_setup):
        """Unknown view_token returns 404."""
        client, _, mock_cache = summary_setup
        mock_cache.get_task_by_view_token.return_value = None

        resp = client.get("/api/audit/summary?view_token=does-not-exist")
        assert resp.status_code == 404

    def test_returns_403_for_cross_user_access(self, summary_setup):
        """A task belonging to a different API key returns 403."""
        client, db_pair, mock_cache = summary_setup
        al = db_pair["audit_logger"]

        # Audit record under a DIFFERENT API key (not _API_KEY)
        al.log_api_call(
            api_key="other-key-9999",
            user_id="other-user",
            endpoint="/api/transcribe",
            task_id="task-other",
        )

        mock_cache.get_task_by_view_token.return_value = {"task_id": "task-other", "status": "success"}
        mock_cache.get_view_data_by_token.return_value = {"status": "success", "summary": "secret"}

        resp = client.get("/api/audit/summary?view_token=vt-other")
        assert resp.status_code == 403

    def test_empty_summary_returns_none_not_placeholder(self, summary_setup):
        """Completed task with empty summary text returns 200 with summary=None
        (honest status model: no more '' or placeholder strings standing in
        for "no real summary content")."""
        client, db_pair, mock_cache = summary_setup
        al = db_pair["audit_logger"]
        _log(al, "task-nosumm")

        mock_cache.get_task_by_view_token.return_value = {"task_id": "task-nosumm", "status": "success"}
        mock_cache.get_view_data_by_token.return_value = {"status": "success", "summary": ""}

        resp = client.get("/api/audit/summary?view_token=vt-ns")
        assert resp.status_code == 200
        assert resp.json()["data"]["summary"] is None

    def test_summary_missing_key_returns_none_not_placeholder(self, summary_setup):
        """view_data without 'summary' key degrades gracefully to summary=None
        (not an empty string, not a "processing..." placeholder)."""
        client, db_pair, mock_cache = summary_setup
        al = db_pair["audit_logger"]
        _log(al, "task-nokey")

        mock_cache.get_task_by_view_token.return_value = {"task_id": "task-nokey", "status": "success"}
        mock_cache.get_view_data_by_token.return_value = {"status": "success"}  # no 'summary' key

        resp = client.get("/api/audit/summary?view_token=vt-nk")
        assert resp.status_code == 200
        assert resp.json()["data"]["summary"] is None

    def test_summary_status_surfaced_in_response(self, summary_setup):
        """The new summary_status field must be surfaced verbatim from view_data.summary_state
        so the frontend can eventually distinguish skipped/failed/pending."""
        client, db_pair, mock_cache = summary_setup
        al = db_pair["audit_logger"]
        _log(al, "task-failedsum")

        mock_cache.get_task_by_view_token.return_value = {"task_id": "task-failedsum", "status": "success"}
        mock_cache.get_view_data_by_token.return_value = {
            "status": "success", "summary": None, "summary_state": "failed",
        }

        resp = client.get("/api/audit/summary?view_token=vt-failedsum")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["summary"] is None
        assert data["summary_status"] == "failed"
