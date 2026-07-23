"""
History route tests.

Covers:
- GET /api/audit/history  : filter combos, pagination, empty, audit snapshots, DB locked
- GET /api/audit/filter-options : distinct webhook/platform/author per API key
- GET /api/audit/summary  : happy path, 404, 202 (processing), 403 (cross-user)

Strategy: a real audit SQLite database (not mocks) exercises the production queries.
Module-level singletons are patched to point at the temporary logger.

All console output must be in English only (no emoji, no Chinese).
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.logging.audit_logger import AuditLogger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_KEY      = "sk-test-key-123456"
_API_KEY_MASK = "sk-t**********3456"   # len=18, first4="sk-t", last4="3456"
_WEBHOOK_A    = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=aaa"
_WEBHOOK_B    = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=bbb"

# A DIFFERENT full API key that collides with _API_KEY under _mask_api_key()
# (same length=18, same first4="sk-t", same last4="3456", different middle) --
# used to prove the auth/filter boundary is user_id, not the masked key,
# since two distinct tenants can legitimately produce the same mask.
_COLLIDING_API_KEY = "sk-tCOLLIDEXYZ3456"
assert len(_COLLIDING_API_KEY) == len(_API_KEY)
assert _COLLIDING_API_KEY != _API_KEY


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_pair(tmp_path):
    """
    Create a real audit.db and a legacy-shaped cache.db in a temp directory.

    Returns a dict with:
      - audit_logger: real AuditLogger instance
      - cache_db_path: str path to the cache SQLite file
      - insert_task: helper to archive an audit-owned task snapshot
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
                    calibration_status=None, summary_status=None,
                    chapters_status=None,
                    submitted_by=None):
        al.archive_task_snapshot({
            "task_id": task_id,
            "view_token": view_token,
            "platform": platform,
            "title": title,
            "author": author,
            "status": status,
            "calibration_status": calibration_status,
            "summary_status": summary_status,
            "chapters_status": chapters_status,
            "submitted_by": submitted_by,
        })

    return {
        "audit_logger": al,
        "cache_db_path": cache_db_path,
        "insert_task": insert_task,
    }


@pytest.fixture()
def history_client(db_pair):
    """
    Build a TestClient with /api/audit/* routes, backed by real temp DBs.
    Patches audit.audit_logger to the real temporary AuditLogger.
    """
    al = db_pair["audit_logger"]

    async def _fake_verify_token():
        return {"user_id": "test-user", "api_key": _API_KEY, "wechat_webhook": None}

    from video_transcript_api.api.services.transcription import verify_token
    from video_transcript_api.api.routes import audit

    app = FastAPI()
    app.include_router(audit.router)
    app.dependency_overrides[verify_token] = _fake_verify_token

    with patch("video_transcript_api.api.routes.audit.audit_logger", al):
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


@contextmanager
def _client_as(al, user_id):
    """Build a TestClient for /api/audit/* authenticated as an arbitrary
    user_id. `history_client` is hardcoded to "test-user"; the ownership-
    boundary tests below need two distinct identities (a real submitter and
    an observer who merely polled) in the same test."""
    async def _fake_verify_token():
        return {"user_id": user_id, "api_key": f"sk-{user_id}", "wechat_webhook": None}

    from video_transcript_api.api.services.transcription import verify_token
    from video_transcript_api.api.routes import audit

    app = FastAPI()
    app.include_router(audit.router)
    app.dependency_overrides[verify_token] = _fake_verify_token

    with patch("video_transcript_api.api.routes.audit.audit_logger", al):
        yield TestClient(app)


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

    def test_polling_same_task_repeatedly_collapses_to_one_history_row(self, history_client):
        """Codex-reported gap: history was driven by api_audit_logs *rows*,
        not tasks. GET /api/task/{task_id} writes one audit row per poll
        (allowed by design -- see get_task_status), and those polling rows
        carry empty video_url/wechat_webhook (the endpoint never accepts or
        records either). A submitter polling their own task 3 times used to
        turn into 4 rows for the same task_id in their own history --
        polluting total/pagination and burying the real submission fields
        under mostly-empty poll rows. The fix collapses every task_id down
        to its single submission-endpoint row."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        al.log_api_call(
            api_key=_API_KEY, user_id="test-user", endpoint="/api/transcribe",
            video_url="https://youtube.com/watch?v=poll-dedup",
            task_id="task-poll", status_code=202, wechat_webhook=_WEBHOOK_A,
        )
        insert("task-poll", "vt-poll", submitted_by="test-user")

        for _ in range(3):
            al.log_api_call(
                api_key=_API_KEY, user_id="test-user",
                endpoint="/api/task/task-poll", task_id="task-poll",
            )

        # Sanity check the fixture actually wrote 4 raw rows for this task --
        # otherwise this test would pass trivially without exercising the
        # dedup fix at all.
        raw_conn = sqlite3.connect(al.db_path)
        try:
            raw_count = raw_conn.execute(
                "SELECT COUNT(*) FROM api_audit_logs WHERE task_id = ?",
                ("task-poll",),
            ).fetchone()[0]
            submission_request_time = raw_conn.execute(
                "SELECT request_time FROM api_audit_logs "
                "WHERE task_id = ? AND endpoint = '/api/transcribe'",
                ("task-poll",),
            ).fetchone()[0]
        finally:
            raw_conn.close()
        assert raw_count == 4

        resp = client.get("/api/audit/history?status=all")
        assert resp.status_code == 200
        data = resp.json()["data"]
        items = [i for i in data["items"] if i["task_id"] == "task-poll"]
        assert len(items) == 1
        item = items[0]
        assert item["video_url"] == "https://youtube.com/watch?v=poll-dedup"
        assert item["wechat_webhook"] == _WEBHOOK_A
        assert item["request_time"] == submission_request_time
        assert data["total"] == 1

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

    def test_pre_submission_null_task_id_row_excluded_from_status_all(self, history_client):
        """Local codex review round 5, F3: routes/tasks.py's
        transcribe_video()/recalibrate() both log a "request arrived" audit
        row *before* task_id exists (task_id=None -- see the very first
        audit_logger.log_api_call() call at the top of each handler, used so
        a failure before task creation still leaves a trail), then log a
        second row with the real task_id once the task is actually created.
        Both rows share the same endpoint ("/api/transcribe" or
        "/api/recalibrate", both in SUBMISSION_ENDPOINTS) but differ in
        task_id (NULL vs real) -- so the existing "collapse every task_id
        down to one row" dedup (proven by the polling test above) does NOT
        collapse them, since they are two genuinely different task_id
        values from get_history()'s point of view.

        Under the default status='success' filter this is invisible: the
        NULL-task_id row never joins to a task_audit_snapshots row, so its
        COALESCE(s.status, 'unknown') never equals 'success' and it's
        filtered out incidentally. But status='all' has no such filter --
        every real submission produces one genuine row (with task_id, view
        token, title) plus one phantom row (task_id/view_token/title all
        null), corrupting total/pagination for exactly the view (status=all)
        that's supposed to show everything.

        Reproduced directly: one real submission, modeled as the actual
        production call sequence -- pre-flight log_api_call with no task_id,
        then the real log_api_call + insert_task with task_id="task-real".
        status=all must show exactly 1 item / total=1, not 2."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        # Pre-flight row: mirrors the literal call at the top of
        # transcribe_video()/recalibrate() -- endpoint set, task_id not
        # passed at all (defaults to None in AuditLogger.log_api_call).
        al.log_api_call(
            api_key=_API_KEY, user_id="test-user", endpoint="/api/transcribe",
            video_url="https://youtube.com/watch?v=real",
        )
        # Real row: written once create_task() actually produced a task_id.
        _log(al, "task-real")
        insert("task-real", "vt-real", submitted_by="test-user")

        resp = client.get("/api/audit/history?status=all")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["task_id"] == "task-real"

    def test_response_includes_view_token_and_title(self, history_client):
        """Items should carry view_token and title from the audit snapshot."""
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

    def test_expired_content_clears_live_capability(self, history_client):
        client, setup = history_client
        al = setup["audit_logger"]
        _log(al, "task-expired")
        setup["insert_task"]("task-expired", "view-secret")
        al.expire_task_snapshot("task-expired")

        response = client.get("/api/audit/history")
        item = next(
            value for value in response.json()["data"]["items"]
            if value["task_id"] == "task-expired"
        )
        assert item["view_token"] is None
        assert item["content_expired"] is True

    def test_response_includes_calibration_and_summary_status(self, history_client):
        """Items should carry the honest-status-model columns from the cache JOIN
        (frontend does not consume them yet, but the API must surface them)."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "task-status")
        insert(
            "task-status",
            "vt-status",
            calibration_status="partial",
            summary_status="generated",
            chapters_status="generated",
        )

        resp = client.get("/api/audit/history")
        assert resp.status_code == 200
        item = next(i for i in resp.json()["data"]["items"] if i["task_id"] == "task-status")
        assert item["calibration_status"] == "partial"
        assert item["summary_status"] == "generated"
        assert item["chapters_status"] == "generated"

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
        assert item["chapters_status"] is None

    def test_disabled_status_values_pass_through_without_error(self, history_client):
        """The per-task processing-depth feature introduces a new 'disabled'
        value for both columns (user explicitly turned off calibrate/summarize).
        The history endpoint must surface it as a plain string like any other
        status, not choke on an unrecognized value."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        _log(al, "task-disabled")
        insert("task-disabled", "vt-disabled", calibration_status="disabled",
               summary_status="disabled")

        resp = client.get("/api/audit/history")
        assert resp.status_code == 200
        item = next(i for i in resp.json()["data"]["items"] if i["task_id"] == "task-disabled")
        assert item["calibration_status"] == "disabled"
        assert item["summary_status"] == "disabled"

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

    def test_masked_key_collision_does_not_leak_across_tenants(self, history_client):
        """ci-gate review (cloud CI): the tenant boundary must be user_id, not
        the truncated api_key_masked -- two DIFFERENT real API keys that
        happen to share the same first-4/last-4 characters (and therefore
        the same mask) must NOT see each other's history. Before the fix,
        the WHERE clause filtered on api_key_masked, so this exact scenario
        would have leaked "other-user"'s task into "test-user"'s results."""
        client, setup = history_client
        al = setup["audit_logger"]
        insert = setup["insert_task"]

        al.log_api_call(
            api_key=_COLLIDING_API_KEY,
            user_id="other-user",
            endpoint="/api/transcribe",
            task_id="colliding-task",
        )
        insert("colliding-task", "vt-colliding")

        resp = client.get("/api/audit/history?status=all")
        assert resp.status_code == 200
        task_ids = {i["task_id"] for i in resp.json()["data"]["items"]}
        assert "colliding-task" not in task_ids

    def test_task_survives_missing_submission_log_row(self, history_client):
        """Local codex review round 6, G1: AuditLogger.log_api_call() swallows
        every SQLite exception and returns False (see the bare `except
        Exception` in log_api_call), and every caller in routes/tasks.py
        ignores that return value. Before this fix, get_history() was still
        driven by api_audit_logs as its FROM table -- task_audit_snapshots
        (submitted_by, the actual authoritative ownership record) only ever
        appeared as a JOIN condition, never as the row source. So if the
        *submission* audit-log write for a task failed for any reason (disk
        full, lock timeout...), there was no api_audit_logs row to drive the
        query at all, and the task -- despite having a complete, correctly
        attributed task_audit_snapshots row -- vanished from the submitter's
        history permanently. repair_task_snapshots only ever backfills
        missing *snapshot* rows, never missing *log* rows, so this was not
        self-healing either.

        Reproduced directly: archive a snapshot with submitted_by="test-user"
        and deliberately skip the corresponding al.log_api_call() call
        entirely (modeling the write failure). The task must still appear in
        the submitter's history. Fields that only ever existed in the
        (missing) log row -- video_url, wechat_webhook, api_key_masked --
        degrade to None rather than the whole record disappearing;
        request_time falls back to the snapshot's own completed_at/
        archived_at so ordering and date filters keep working."""
        client, setup = history_client
        insert = setup["insert_task"]

        # No al.log_api_call() at all for this task_id.
        insert("task-log-missing", "vt-log-missing", submitted_by="test-user")

        resp = client.get("/api/audit/history?status=all")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        task_ids = {i["task_id"] for i in data["items"]}
        assert "task-log-missing" in task_ids

        item = next(i for i in data["items"] if i["task_id"] == "task-log-missing")
        assert item["video_url"] is None
        assert item["wechat_webhook"] is None
        assert item["api_key_masked"] is None
        # Falls back to the snapshot's own timestamp instead of vanishing --
        # ordering/date-filter semantics keep working even without the log row.
        assert item["request_time"] is not None


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

    def test_masked_key_collision_does_not_leak_across_tenants(self, history_client):
        """Same rationale as the /history collision test above -- a colliding
        api_key_masked must not surface another tenant's webhook."""
        client, setup = history_client
        al = setup["audit_logger"]

        al.log_api_call(
            api_key=_COLLIDING_API_KEY,
            user_id="other-user",
            endpoint="/api/transcribe",
            wechat_webhook="https://colliding-webhook.example.com",
        )

        resp = client.get("/api/audit/filter-options")
        assert resp.status_code == 200
        assert "https://colliding-webhook.example.com" not in resp.json()["data"]["webhooks"]

    def test_platform_and_author_survive_missing_submission_log_row(self, history_client):
        """Same G1 driving-table fix as the /history test above, applied to
        the platform/author queries here: they must also flip to being
        driven by task_audit_snapshots (with the shared attribution helper),
        not api_audit_logs -- otherwise a missing submission log row hides
        the task's platform/author from the filter dropdown too, even though
        the snapshot has a correctly attributed submitted_by."""
        client, setup = history_client
        insert = setup["insert_task"]

        insert(
            "task-log-missing", "vt-log-missing",
            platform="youtube", author="Chan Missing", submitted_by="test-user",
        )

        resp = client.get("/api/audit/filter-options")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "youtube" in data["platforms"]
        assert "Chan Missing" in data["authors"]


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
         patch("video_transcript_api.api.routes.audit.get_cache_manager", return_value=mock_cache), \
         patch("video_transcript_api.api.routes.audit.ViewTokenResolver", side_effect=lambda manager: manager):
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
        client, db_pair, mock_cache = summary_setup
        al = db_pair["audit_logger"]
        # Ownership evidence: with the H1 fail-closed default, a task with
        # zero attribution evidence anywhere is denied (this is the actual
        # bug being fixed -- see TestSummaryEndpoint.test_h1_* below), so
        # this fixture must establish real ownership like the other summary
        # tests do, to isolate what's actually under test here: the 202
        # response shape for an in-flight task.
        _log(al, "task-proc")
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

    def test_masked_key_collision_returns_403_not_leaked_summary(self, summary_setup):
        """Same collision scenario as /history and /filter-options above --
        a task owned by a colliding-but-different api_key must still return
        403, not leak its summary to the current user_id."""
        client, db_pair, mock_cache = summary_setup
        al = db_pair["audit_logger"]

        al.log_api_call(
            api_key=_COLLIDING_API_KEY,
            user_id="other-user",
            endpoint="/api/transcribe",
            task_id="task-colliding",
        )

        mock_cache.get_task_by_view_token.return_value = {"task_id": "task-colliding", "status": "success"}
        mock_cache.get_view_data_by_token.return_value = {"status": "success", "summary": "secret"}

        resp = client.get("/api/audit/summary?view_token=vt-colliding")
        assert resp.status_code == 403

    def test_ownership_check_exception_denies_access_fail_closed(self, summary_setup):
        """ci-gate review (cloud CI): if the ownership-check query itself
        raises (DB lock, connection error, etc.), the request must be denied
        (503), not silently granted access. The previous fail-open behavior
        would let ANY authenticated caller read a summary they don't own
        whenever the audit DB hiccups."""
        client, db_pair, mock_cache = summary_setup
        al = db_pair["audit_logger"]
        _log(al, "task-boom")

        mock_cache.get_task_by_view_token.return_value = {"task_id": "task-boom", "status": "success"}
        mock_cache.get_view_data_by_token.return_value = {"status": "success", "summary": "secret"}

        with patch.object(
            al, "_get_cursor", side_effect=RuntimeError("audit db unavailable")
        ):
            resp = client.get("/api/audit/summary?view_token=vt-boom")

        assert resp.status_code == 503

    def test_missing_user_id_does_not_bypass_ownership_check(self, db_pair):
        """ci-gate review (local, follow-up on the cloud CI findings above):
        a caller whose user_info lacks user_id entirely (a misconfigured
        multi-user entry -- _load_users_config() only warns, it doesn't
        reject the entry, so validate_token() still issues a token with
        user_id=None) must NOT have the ownership check skipped outright.
        Before the fix, `if task_id and user_id:` short-circuited to False
        for such a caller, bypassing the check completely and granting
        access to ANY task's summary regardless of its real owner."""
        al = db_pair["audit_logger"]

        # A real owner logs a task under their own user_id.
        al.log_api_call(
            api_key="owner-key",
            user_id="real-owner",
            endpoint="/api/transcribe",
            task_id="task-owned-by-someone-else",
        )

        mock_cache = MagicMock()
        mock_cache.db_path = Path(db_pair["cache_db_path"])
        mock_cache.get_task_by_view_token.return_value = {
            "task_id": "task-owned-by-someone-else", "status": "success",
        }
        mock_cache.get_view_data_by_token.return_value = {
            "status": "success", "summary": "secret",
        }

        async def _fake_verify_token_missing_user_id():
            # api_key present, user_id missing (misconfigured entry).
            return {"api_key": "misconfigured-key", "wechat_webhook": None}

        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.api.routes import audit

        app = FastAPI()
        app.include_router(audit.router)
        app.dependency_overrides[verify_token] = _fake_verify_token_missing_user_id

        with patch("video_transcript_api.api.routes.audit.audit_logger", al), \
             patch("video_transcript_api.api.routes.audit.get_cache_manager", return_value=mock_cache), \
             patch("video_transcript_api.api.routes.audit.ViewTokenResolver", side_effect=lambda manager: manager):
            client = TestClient(app)
            resp = client.get("/api/audit/summary?view_token=vt-someone-else")

        assert resp.status_code == 403

    def test_missing_user_id_and_no_audit_record_denies_access(self, db_pair):
        """H1 (local codex review round 7): this test used to assert the
        opposite (200) under the name
        test_missing_user_id_still_allows_task_with_no_audit_record,
        documenting a deliberate "compatible with legacy data" carve-out --
        a task with NO submitted_by evidence anywhere (no task_audit_snapshots
        row, no cache.db submitted_by, no api_audit_logs row) was allowed
        through unconditionally when the ownership check found "no snapshot
        at all" for the view_token.

        That carve-out was actually the same fail-open bug this round fixes
        for the cross-user case (see test below): "no attribution evidence
        found anywhere" was being treated as "allow", when the only safe
        default once every layer (audit.db snapshot, audit.db legacy
        submission log, cache.db task_status) has been consulted and found
        nothing is fail-closed (403), matching the spec's "legacy 兜底也无
        证据时默认拒绝". A misconfigured caller with no user_id can still
        reach their own task through the ordinary attribution match (see
        the pure-legacy test below) -- this test only pins down the
        genuinely-zero-evidence case, which must now deny."""
        al = db_pair["audit_logger"]

        mock_cache = MagicMock()
        mock_cache.db_path = Path(db_pair["cache_db_path"])
        mock_cache.get_task_by_view_token.return_value = {
            "task_id": "task-no-audit-record", "status": "success",
        }
        mock_cache.get_view_data_by_token.return_value = {
            "status": "success", "summary": "legacy summary",
        }

        async def _fake_verify_token_missing_user_id():
            return {"api_key": "misconfigured-key", "wechat_webhook": None}

        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.api.routes import audit

        app = FastAPI()
        app.include_router(audit.router)
        app.dependency_overrides[verify_token] = _fake_verify_token_missing_user_id

        with patch("video_transcript_api.api.routes.audit.audit_logger", al), \
             patch("video_transcript_api.api.routes.audit.get_cache_manager", return_value=mock_cache), \
             patch("video_transcript_api.api.routes.audit.ViewTokenResolver", side_effect=lambda manager: manager):
            client = TestClient(app)
            resp = client.get("/api/audit/summary?view_token=vt-no-record")

        assert resp.status_code == 403

    def test_h1_cross_user_denied_when_snapshot_missing_but_cache_shows_other_owner(
        self, db_pair
    ):
        """H1 (local codex review round 7): reproduces the actual fail-open
        bug directly. The task has never been archived to
        task_audit_snapshots (still in-flight) and its submission-endpoint
        api_audit_logs row is missing entirely (write failure, or simply
        never written) -- so the *only* attribution evidence anywhere is
        cache.db's task_status.submitted_by, surfaced via
        list_tasks_by_view_token. That is exactly the shape the third-level
        cache.db check was supposed to cover, but the old code only used it
        for a *positive* match (`candidate.get("submitted_by") == user_id`)
        and silently ignored it as *exclusion* evidence: when the loop found
        no match, the function fell through to "does task_audit_snapshots
        have ANY row for this view_token?" -- found none -- and returned
        True (fail-open), granting a cross-user requester full summary
        access despite cache.db clearly showing the task belongs to someone
        else.

        Fix: any layer's non-null submitted_by that doesn't match the caller
        is exclusion evidence -- once *any* evidence resolves the task to
        someone else, "no audit snapshot" must not override it back to
        fail-open."""
        mock_cache = MagicMock()
        mock_cache.db_path = Path(db_pair["cache_db_path"])
        mock_cache.get_task_by_view_token.return_value = {
            "task_id": "task-h1-victim", "status": "success",
        }
        mock_cache.get_view_data_by_token.return_value = {
            "status": "success", "summary": "victim's private summary",
        }
        # Only evidence anywhere: cache.db shows the task belongs to
        # "victim". No task_audit_snapshots row, no api_audit_logs row.
        mock_cache.list_tasks_by_view_token.return_value = [
            {"task_id": "task-h1-victim", "submitted_by": "victim"},
        ]

        al = db_pair["audit_logger"]

        async def _fake_verify_token_attacker():
            return {"user_id": "attacker", "api_key": "sk-attacker", "wechat_webhook": None}

        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.api.routes import audit

        app = FastAPI()
        app.include_router(audit.router)
        app.dependency_overrides[verify_token] = _fake_verify_token_attacker

        with patch("video_transcript_api.api.routes.audit.audit_logger", al), \
             patch("video_transcript_api.api.routes.audit.get_cache_manager", return_value=mock_cache), \
             patch("video_transcript_api.api.routes.audit.ViewTokenResolver", side_effect=lambda manager: manager):
            client = TestClient(app)
            resp = client.get("/api/audit/summary?view_token=vt-h1-crossuser")

        assert resp.status_code == 403

    def test_h1_pure_legacy_task_still_allows_real_submitter(self, db_pair):
        """H1 counterpart to the two tests above: a genuinely pre-migration
        task where submitted_by is NULL everywhere (never archived, and
        cache.db never got a submitted_by value either) must still be
        reachable by its real submitter through the legacy
        api_audit_logs-based fallback -- the fail-closed default introduced
        by this round only kicks in once every layer (including the legacy
        fallback) has been consulted and found nothing."""
        al = db_pair["audit_logger"]
        al.log_api_call(
            api_key=_API_KEY,
            user_id="test-user",
            endpoint="/api/transcribe",
            task_id="task-h1-legacy",
        )

        mock_cache = MagicMock()
        mock_cache.db_path = Path(db_pair["cache_db_path"])
        mock_cache.get_task_by_view_token.return_value = {
            "task_id": "task-h1-legacy", "status": "success",
        }
        mock_cache.get_view_data_by_token.return_value = {
            "status": "success", "summary": "legacy submitter's summary",
        }
        # cache.db also has no submitted_by for this legacy row.
        mock_cache.list_tasks_by_view_token.return_value = [
            {"task_id": "task-h1-legacy", "submitted_by": None},
        ]

        async def _fake_verify_token():
            return {"user_id": "test-user", "api_key": _API_KEY, "wechat_webhook": None}

        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.api.routes import audit

        app = FastAPI()
        app.include_router(audit.router)
        app.dependency_overrides[verify_token] = _fake_verify_token

        with patch("video_transcript_api.api.routes.audit.audit_logger", al), \
             patch("video_transcript_api.api.routes.audit.get_cache_manager", return_value=mock_cache), \
             patch("video_transcript_api.api.routes.audit.ViewTokenResolver", side_effect=lambda manager: manager):
            client = TestClient(app)
            resp = client.get("/api/audit/summary?view_token=vt-h1-legacy")

        assert resp.status_code == 200
        assert resp.json()["data"]["summary"] == "legacy submitter's summary"

    def test_shared_view_token_grants_access_to_any_submitter_not_just_priority_pick(
        self, db_pair
    ):
        """T4 (local Codex review round 4): CacheManager.create_task
        deliberately shares one view_token across repeat submissions of the
        same URL. get_task_by_view_token() picks *one* priority task for
        content display (success > other > failed, newest first within a
        tier -- see CacheManager._TASK_STATUS_PRIORITY_ORDER_BY), but the
        old ownership check only validated that single selected task's
        submitted_by. So every *other* legitimate submitter sharing the same
        view_token got a stable 403 -- and recalibrate changing which task
        the priority selection lands on could even flip who is locked out,
        since ownership followed content selection instead of being
        independent of it.

        Reproduced directly: user-a and user-b each submitted the same URL
        (sharing view_token "vt-shared"), each with their own successful
        task snapshot recorded under their own submitted_by. cache_manager
        .get_task_by_view_token is mocked to always return user-b's task
        (simulating it being the priority pick regardless of who is asking)
        -- proving authorization must not simply defer to whichever task
        got selected for content. Both user-a and user-b must be able to
        read the summary through their shared token (and both see the same
        selected content -- content selection stays decoupled from who is
        authorized); user-c, who only ever polled progress (GET
        /api/task/{task_id}, not a submission endpoint), must still get
        403."""
        al = db_pair["audit_logger"]
        db_pair["insert_task"]("task-a", "vt-shared", status="success", submitted_by="user-a")
        db_pair["insert_task"]("task-b", "vt-shared", status="success", submitted_by="user-b")
        # user-c only ever polled progress on task-b -- a designed-for
        # read-only capability that must never grant summary access.
        al.log_api_call(
            api_key="key-c", user_id="user-c",
            endpoint="/api/task/task-b", task_id="task-b",
        )

        mock_cache = MagicMock()
        mock_cache.db_path = Path(db_pair["cache_db_path"])
        # Priority selection always lands on user-b's task, regardless of
        # who is asking -- this is the crux of the bug: content selection
        # and authorization must not be the same decision.
        mock_cache.get_task_by_view_token.return_value = {
            "task_id": "task-b", "status": "success",
        }
        mock_cache.get_view_data_by_token.return_value = {
            "status": "success", "summary": "shared content",
        }

        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.api.routes import audit

        def _client_for(user_id):
            async def _fake_verify_token():
                return {"user_id": user_id, "api_key": f"sk-{user_id}", "wechat_webhook": None}

            app = FastAPI()
            app.include_router(audit.router)
            app.dependency_overrides[verify_token] = _fake_verify_token
            return TestClient(app)

        with patch("video_transcript_api.api.routes.audit.audit_logger", al), \
             patch("video_transcript_api.api.routes.audit.get_cache_manager", return_value=mock_cache), \
             patch("video_transcript_api.api.routes.audit.ViewTokenResolver", side_effect=lambda manager: manager):
            resp_a = _client_for("user-a").get("/api/audit/summary?view_token=vt-shared")
            resp_b = _client_for("user-b").get("/api/audit/summary?view_token=vt-shared")
            resp_c = _client_for("user-c").get("/api/audit/summary?view_token=vt-shared")

        assert resp_a.status_code == 200, resp_a.text
        assert resp_a.json()["data"]["summary"] == "shared content"
        assert resp_b.status_code == 200, resp_b.text
        assert resp_b.json()["data"]["summary"] == "shared content"
        assert resp_c.status_code == 403

    def test_own_unarchived_task_under_shared_view_token_grants_access(self, db_pair):
        """Local codex review round 5, F2 -- closes the "known limitation"
        the round-4 fix (test above) deliberately left open in its own
        docstring: the two-level ownership check only ever looks at
        task_audit_snapshots, which is populated *only when a task reaches
        a terminal state* (see AuditLogger.archive_task_snapshot's call
        sites: on completion, or via repair_task_snapshots backfill). If the
        current caller's own submission under a shared view_token is still
        queued/processing, it has no task_audit_snapshots row at all yet --
        so both the fast-path (_owns_task on the selected task) and the
        sibling-snapshot fallback (level 2) miss it entirely, and the
        legitimate submitter gets a 403 for their own in-flight task merely
        because someone else's *older* submission of the same URL (which
        shares the view_token) happens to have already finished and become
        the priority pick.

        Reproduced directly: user-a's task is still "processing" and has
        NEVER been archived to task_audit_snapshots (only exists in
        cache.db's task_status, modeled here via the new
        CacheManager.list_tasks_by_view_token). user-b's task with the same
        URL already completed and IS archived, and get_task_by_view_token
        is mocked to always return user-b's task as the priority pick
        (mirroring the round-4 test's pattern). user-a must still get 200 --
        and an unrelated user-x, who has no association with this
        view_token at all (not even via the new cache.db-backed check),
        must still get 403, proving the fix only widens access to genuine
        submitters instead of granting it unconditionally."""
        al = db_pair["audit_logger"]
        db_pair["insert_task"]("task-b-done", "vt-inflight", status="success", submitted_by="user-b")

        mock_cache = MagicMock()
        mock_cache.db_path = Path(db_pair["cache_db_path"])
        # Priority pick always lands on user-b's already-terminal task,
        # regardless of who is asking -- content selection stays decoupled
        # from authorization (same premise as the round-4 test above).
        mock_cache.get_task_by_view_token.return_value = {
            "task_id": "task-b-done", "status": "success",
        }
        mock_cache.get_view_data_by_token.return_value = {
            "status": "success", "summary": "shared content",
        }
        # user-a's own submission never made it to task_audit_snapshots
        # (still processing) -- it only shows up here, in cache.db's
        # task_status, alongside user-b's (also present there, terminal or
        # not doesn't matter for this query).
        mock_cache.list_tasks_by_view_token.return_value = [
            {"task_id": "task-a-inflight", "submitted_by": "user-a"},
            {"task_id": "task-b-done", "submitted_by": "user-b"},
        ]

        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.api.routes import audit

        def _client_for(user_id):
            async def _fake_verify_token():
                return {"user_id": user_id, "api_key": f"sk-{user_id}", "wechat_webhook": None}

            app = FastAPI()
            app.include_router(audit.router)
            app.dependency_overrides[verify_token] = _fake_verify_token
            return TestClient(app)

        with patch("video_transcript_api.api.routes.audit.audit_logger", al), \
             patch("video_transcript_api.api.routes.audit.get_cache_manager", return_value=mock_cache), \
             patch("video_transcript_api.api.routes.audit.ViewTokenResolver", side_effect=lambda manager: manager):
            resp_a = _client_for("user-a").get("/api/audit/summary?view_token=vt-inflight")
            resp_x = _client_for("user-x").get("/api/audit/summary?view_token=vt-inflight")

        assert resp_a.status_code == 200, resp_a.text
        assert resp_a.json()["data"]["summary"] == "shared content"
        assert resp_x.status_code == 403

    def test_r1_revoked_sharer_cannot_ride_stale_cache_row_to_read_summary(
        self, tmp_path,
    ):
        """R1 (PR3 review hardening): check_view_token_ownership trusted
        CacheManager.list_tasks_by_view_token's rows as positive attribution
        evidence unconditionally -- but a task's revocation (expire_task_
        snapshot) only tombstones its task_audit_snapshots row (view_token
        cleared, content_expired=1); it never touches the cache.db task_
        status row (that's step 3 of the archive -> expire -> delete
        sequence, which can be interrupted by a crash between steps 2 and
        3 -- see get_task_by_view_token's own K4 fix for the exact same
        residue shape). So user-a's already-revoked task can still surface
        through list_tasks_by_view_token with submitted_by="user-a" intact,
        letting user-a pass the ownership check for user-b's still-valid
        task sharing the same view_token and read user-b's summary.

        Reproduced against the real CacheManager + AuditLogger (not
        MagicMock) so the actual list_tasks_by_view_token filtering runs:
        user-a's task is archived then expired (simulating the crash-
        interrupted cleanup) while its task_status row is left in place;
        user-b's task under the same view_token is never revoked. user-a
        must get 403; user-b (the real owner) must not be blocked by the
        fix."""
        cache_manager = CacheManager(str(tmp_path / "cache"))
        audit_logger = AuditLogger(db_path=str(tmp_path / "audit.db"))
        # Wired the same way api/context.py wires it in production -- only
        # then does list_tasks_by_view_token's content_expired filtering
        # activate (see CacheManager.audit_logger's docstring at its call
        # sites).
        cache_manager.audit_logger = audit_logger
        try:
            shared_url = "https://example.com/r1-shared-video"
            task_a = cache_manager.create_task(
                url=shared_url, platform="youtube", media_id="r1-shared",
                submitted_by="user-a",
            )
            task_b = cache_manager.create_task(
                url=shared_url, platform="youtube", media_id="r1-shared",
                submitted_by="user-b",
            )
            assert task_a["view_token"] == task_b["view_token"]
            view_token = task_a["view_token"]

            # Simulate the crash-interrupted cleanup: archive + expire
            # user-a's task_audit_snapshots row (tombstoning it), but never
            # reach the DELETE FROM task_status step -- the row stays live
            # in cache.db exactly as CacheManager.list_tasks_by_view_token
            # would read it.
            audit_logger.archive_task_snapshot({
                "task_id": task_a["task_id"],
                "view_token": view_token,
                "platform": "youtube",
                "title": "Revoked title",
                "author": "Revoked author",
                "status": "success",
                "calibration_status": None,
                "summary_status": None,
                "submitted_by": "user-a",
            })
            audit_logger.expire_task_snapshot(task_a["task_id"])

            from video_transcript_api.api.services.transcription import verify_token
            from video_transcript_api.api.routes import audit

            def _client_for(user_id):
                async def _fake_verify_token():
                    return {"user_id": user_id, "api_key": f"sk-{user_id}", "wechat_webhook": None}

                app = FastAPI()
                app.include_router(audit.router)
                app.dependency_overrides[verify_token] = _fake_verify_token
                return TestClient(app)

            with patch("video_transcript_api.api.routes.audit.audit_logger", audit_logger), \
                 patch("video_transcript_api.api.routes.audit.get_cache_manager", return_value=cache_manager):
                resp_a = _client_for("user-a").get(f"/api/audit/summary?view_token={view_token}")
                resp_b = _client_for("user-b").get(f"/api/audit/summary?view_token={view_token}")

            assert resp_a.status_code == 403, resp_a.text
            assert resp_b.status_code != 403, resp_b.text
        finally:
            cache_manager.close()

    def test_expired_snapshot_task_id_branch_no_longer_grants_ownership(self, db_pair):
        """W1（PR3 review hardening 二轮）: check_view_token_ownership 直查
        `task_audit_snapshots` 的候选收集查询是 `WHERE view_token = ? OR
        task_id = ?`——expire_task_snapshot 撤销一条快照时只清空它自己
        那一行的 view_token（置 NULL，堵住 view_token 分支）并把
        content_expired 置 1，但 task_id/submitted_by 原样保留。若查询不
        过滤 content_expired，OR 的 task_id 分支仍会把已撤销快照的
        submitted_by 当正面归属证据采信——传入的 task_id 参数一旦等于一个
        已撤销任务的 task_id（例如同一 view_token 下发生过 crash-
        interrupted 的部分撤销、或调用方直接以这个 task_id 发起归属校验），
        原提交者就能绕开撤销继续拿到 recalibrate/summary 授权。修法：
        WHERE 子句补 `AND COALESCE(content_expired, 0) = 0`，与
        CacheManager.list_tasks_by_view_token 对同一份撤销残留问题的既有
        修法（R1）同语义。

        直接调用 check_view_token_ownership（而非走完整路由）：生产两个
        调用点（get_task_summary/recalibrate）派生 task_id 的方式（经
        get_task_by_view_token 的 K4 过滤）已经不会把一个刚好当下已撤销的
        task_id 作为 primary 传入，但函数自身仍必须独立正确处理传入的
        task_id 恰好指向一条已撤销快照的情况——不能依赖调用方总是先过滤好，
        这是与 R1 同款的纵深防御修法，直接对函数下钻测试与本文件里的
        R1 用例（test_r1_revoked_sharer_cannot_ride_stale_cache_row_to_
        read_summary）风格一致。
        """
        al = db_pair["audit_logger"]
        db_pair["insert_task"](
            "task-expired-x", "vt-original", submitted_by="user-a",
        )
        al.expire_task_snapshot("task-expired-x")

        # 该任务的撤销残留：task_audit_snapshots 行的 view_token 已清空，
        # 但 task_id/submitted_by 仍在——只有靠 task_id 分支才能查到它，
        # 用来精确复现本条修复要堵的那个分支，与 cache.db 侧（task_status）
        # 的残留完全无关，因此 list_tasks_by_view_token 留空即可隔离变量。
        mock_cache = MagicMock()
        mock_cache.list_tasks_by_view_token.return_value = []

        from video_transcript_api.api.routes.audit import check_view_token_ownership

        owned = check_view_token_ownership(
            "vt-unrelated", "task-expired-x", "user-a", mock_cache, al,
        )
        assert owned is False, (
            "已撤销快照不应再经 task_id 分支给原提交者授权（红：旧代码在"
            "这里返回 True）"
        )

    def test_live_snapshot_task_id_branch_still_grants_ownership(self, db_pair):
        """非回归：未过期快照走 task_id 分支的正常归属判定不受本轮修复
        影响——`AND COALESCE(content_expired, 0) = 0` 只排除已撤销的行，
        对 content_expired 为 0（或列缺省为 NULL，COALESCE 归一到 0）的
        正常快照必须原样放行。"""
        al = db_pair["audit_logger"]
        db_pair["insert_task"](
            "task-live-y", "vt-original", submitted_by="user-a",
        )

        mock_cache = MagicMock()
        mock_cache.list_tasks_by_view_token.return_value = []

        from video_transcript_api.api.routes.audit import check_view_token_ownership

        owned = check_view_token_ownership(
            "vt-unrelated", "task-live-y", "user-a", mock_cache, al,
        )
        assert owned is True

    def test_revoked_task_no_longer_falls_back_to_legacy_audit_row(self, db_pair):
        """Z1（PR3 review hardening 本轮，fail-closed 修复）：check_view_
        token_ownership 此前把已撤销（content_expired=1）的快照整个从
        submitted_by_by_task 的更新里过滤掉，只留下 621 行预置的
        `{task_id: None}`——与"从未归档过的纯 legacy 任务"完全同一副
        `submitted_by is None` 面孔，656 行的兜底循环因此分不清两者，把
        已撤销任务也当纯 legacy 送进 _legacy_owns_task。而该任务的原
        提交者本来就应该留下过一条 /api/transcribe 审计行（否则它一开始
        也成不了"原提交者"），于是这行旧审计记录被当作归属证据，让撤销后
        的任务重新拿到摘要/recalibrate 授权——撤销的单调性被绕过。

        本用例精确复现：先归档、再撤销一个任务的快照（含它自己的提交类
        审计行），验证撤销后 legacy 兜底不应该再命中。"""
        al = db_pair["audit_logger"]
        db_pair["insert_task"](
            "task-revoked-z1", "vt-original", submitted_by="user-a",
        )
        al.expire_task_snapshot("task-revoked-z1")
        # 该用户确实提交过这个任务——旧代码会把这条提交类审计行当作
        # "纯 legacy 归属未知"的兜底证据来用，本条修复必须堵住这条路。
        al.log_api_call(
            api_key="sk-user-a",
            user_id="user-a",
            endpoint="/api/transcribe",
            task_id="task-revoked-z1",
        )

        mock_cache = MagicMock()
        mock_cache.list_tasks_by_view_token.return_value = []

        from video_transcript_api.api.routes.audit import check_view_token_ownership

        owned = check_view_token_ownership(
            "vt-unrelated", "task-revoked-z1", "user-a", mock_cache, al,
        )
        assert owned is False, (
            "已撤销任务不应再经 legacy 兜底重新获得授权"
            "（红：旧代码在这里返回 True）"
        )
        # 未走完整 HTTP 路由：get_task_summary/recalibrate 派生 primary
        # task_id 的方式（经 get_task_by_view_token 的 K4 过滤）本就不会把
        # 一个当下已撤销的 task_id 直接喂给 check_view_token_ownership 做
        # primary——同 W1（test_expired_snapshot_task_id_branch_no_longer_
        # grants_ownership）的既有说明。这里直接下钻测函数本身，是本条
        # 修复要堵的那个分支的精确复现，也是纵深防御：不依赖调用方总是先
        # 过滤好。check_view_token_ownership 是 get_task_summary 与
        # routes/tasks.py::recalibrate 共用的唯一判定实现（K1 抽取的目的
        # 正是让两处不必各自测一遍），这一处函数级红/绿证据即覆盖两个
        # 调用点。

    def test_revoked_task_cannot_gain_positive_authorization_via_cache_candidate(
        self, db_pair,
    ):
        """L3（CI review 第 5 轮 P1）：check_view_token_ownership 把已撤销
        task_id 加入 revoked_task_ids 后，cache 候选循环（list_tasks_by_
        view_token 的结果）此前完全不检查这个集合，仍无条件把该 task_id 的
        submitted_by 写进 submitted_by_by_task；随后 `any(v == user_id ...)`
        紧接着就跑，比"检查 revoked_task_ids"的 legacy 兜底循环（Z1、W1
        两轮已经加固过的分支）还要早——revoked_task_ids 因此只保护了后面
        legacy 兜底这一条路径，cache 正向授权路径完全绕开。真实生产环境的
        CacheManager.list_tasks_by_view_token 自己已经有一层过滤（R1 修复，
        对着 self.audit_logger 逐行查 content_expired），但
        check_view_token_ownership 不能依赖调用方总是把这层过滤做对——这里
        直接注入一个不做该过滤的 CacheManager 替身（模拟不同实现/竞态窗口
        /测试替身），验证函数自身必须独立堵住这条路径，属于纵深防御，与
        Z1/W1 对 legacy 兜底分支的既有修法同一原则。"""
        al = db_pair["audit_logger"]
        db_pair["insert_task"](
            "task-revoked-l3", "vt-shared-l3", submitted_by="user-a",
        )
        al.expire_task_snapshot("task-revoked-l3")

        # 精确复现这条缺口：注入的 CacheManager 替身仍然把已撤销任务的
        # 原始 submitted_by 原样交出来（不做 content_expired 过滤）。
        mock_cache = MagicMock()
        mock_cache.list_tasks_by_view_token.return_value = [
            {"task_id": "task-revoked-l3", "submitted_by": "user-a"},
        ]

        from video_transcript_api.api.routes.audit import check_view_token_ownership

        owned = check_view_token_ownership(
            "vt-shared-l3", "task-revoked-l3", "user-a", mock_cache, al,
        )
        assert owned is False, (
            "已撤销任务的原提交者不应再经 cache 候选路径重新获得授权"
            "（红：旧代码在这里返回 True）"
        )

    def test_unrevoked_task_under_shared_view_token_still_grants_real_submitter(
        self, db_pair,
    ):
        """非回归：同一 view_token 下另一个从未被撤销的任务，其真实提交者
        经 cache 候选路径仍必须正常放行——L3 的修复只应该排除确实在
        revoked_task_ids 里的 task_id，不能连带把整条 cache 证据路径堵死。"""
        al = db_pair["audit_logger"]
        db_pair["insert_task"](
            "task-revoked-l3b", "vt-shared-l3b", submitted_by="user-a",
        )
        al.expire_task_snapshot("task-revoked-l3b")

        mock_cache = MagicMock()
        mock_cache.list_tasks_by_view_token.return_value = [
            {"task_id": "task-revoked-l3b", "submitted_by": "user-a"},
            {"task_id": "task-live-l3b", "submitted_by": "user-b"},
        ]

        from video_transcript_api.api.routes.audit import check_view_token_ownership

        owned_revoked_submitter = check_view_token_ownership(
            "vt-shared-l3b", "task-revoked-l3b", "user-a", mock_cache, al,
        )
        owned_live_submitter = check_view_token_ownership(
            "vt-shared-l3b", "task-revoked-l3b", "user-b", mock_cache, al,
        )
        assert owned_revoked_submitter is False
        assert owned_live_submitter is True, (
            "同一 view_token 下另一个未撤销任务的真实提交者不应被连带误伤"
        )

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


# ===========================================================================
# Observer-audit capability escalation (Codex gate finding, 2026-07-16)
#
# GET /api/task/{task_id} is a deliberately-allowed "progress query"
# capability: any authenticated user may poll any task_id (see
# routes/tasks.py::get_task_status -- verify_token only checks the caller
# holds *a* valid token, not that they submitted this specific task). But
# every poll also calls audit_logger.log_api_call(endpoint=f"/api/task/
# {task_id}", user_id=<poller>, task_id=task_id), which writes a row into
# api_audit_logs under the poller's own user_id.
#
# Before this fix, both /api/audit/history's tenant filter (`a.user_id = ?`)
# and /api/audit/summary's ownership check
# (`SELECT 1 FROM api_audit_logs WHERE task_id = ? AND user_id = ?`) treated
# ANY api_audit_logs row as proof of "this task belongs to this user" --
# so a single poll of someone else's task_id was enough to (a) make that
# task (and its real snapshot: title/platform/status/...) appear in the
# poller's own history list, and (b) pass the poller's own summary-detail
# ownership check. That is progress-query capability silently promoted into
# full history/content-access capability, which the product's own capability
# model says must not happen (content access is meant to be gated by
# view_token, not user_id; user_id is only meant to gate submission
# attribution).
#
# The fix anchors ownership on task_audit_snapshots.submitted_by (populated
# once, at task creation, from whoever called /api/transcribe or
# /api/recalibrate -- see routes/tasks.py) instead of "any api_audit_logs
# row exists". Rows created before this PR's migration have submitted_by
# permanently NULL, so a legacy fallback still consults api_audit_logs for
# those -- but scoped to SUBMISSION_ENDPOINTS only, closing the exact same
# gap for old data.
class TestObserverAuditCapabilityEscalation:
    def test_polling_someone_elses_task_does_not_grant_history_ownership(self, db_pair):
        al = db_pair["audit_logger"]
        insert = db_pair["insert_task"]

        # A submits task-a; the archived snapshot records A as the submitter.
        al.log_api_call(
            api_key="key-a", user_id="user-a",
            endpoint="/api/transcribe", task_id="task-a",
        )
        insert("task-a", "vt-a", submitted_by="user-a")

        # B polls task-a's progress -- allowed by design, but must not make
        # task-a appear in B's own history.
        al.log_api_call(
            api_key="key-b", user_id="user-b",
            endpoint="/api/task/task-a", task_id="task-a",
        )

        with _client_as(al, "user-b") as client:
            resp = client.get("/api/audit/history?status=all")
        assert resp.status_code == 200
        task_ids = {i["task_id"] for i in resp.json()["data"]["items"]}
        assert "task-a" not in task_ids

    def test_polling_someone_elses_task_does_not_grant_summary_access(self, db_pair):
        al = db_pair["audit_logger"]
        insert = db_pair["insert_task"]

        al.log_api_call(
            api_key="key-a", user_id="user-a",
            endpoint="/api/transcribe", task_id="task-a",
        )
        insert("task-a", "vt-a", submitted_by="user-a")
        al.log_api_call(
            api_key="key-b", user_id="user-b",
            endpoint="/api/task/task-a", task_id="task-a",
        )

        mock_cache = MagicMock()
        mock_cache.db_path = Path(db_pair["cache_db_path"])
        mock_cache.get_task_by_view_token.return_value = {
            "task_id": "task-a", "status": "success",
        }
        mock_cache.get_view_data_by_token.return_value = {
            "status": "success", "summary": "secret",
        }

        async def _fake_verify_token_b():
            return {"user_id": "user-b", "api_key": "key-b", "wechat_webhook": None}

        from video_transcript_api.api.services.transcription import verify_token
        from video_transcript_api.api.routes import audit

        app = FastAPI()
        app.include_router(audit.router)
        app.dependency_overrides[verify_token] = _fake_verify_token_b

        with patch("video_transcript_api.api.routes.audit.audit_logger", al), \
             patch("video_transcript_api.api.routes.audit.get_cache_manager", return_value=mock_cache), \
             patch("video_transcript_api.api.routes.audit.ViewTokenResolver", side_effect=lambda manager: manager):
            client = TestClient(app)
            resp = client.get("/api/audit/summary?view_token=vt-a")

        assert resp.status_code == 403

    def test_task_owner_history_unaffected_by_others_polling(self, db_pair):
        """A's own history must still show task-a, both before and after B
        (or A) polls it -- the fix must not turn "has been polled by someone"
        into "excluded from the true owner's history" either."""
        al = db_pair["audit_logger"]
        insert = db_pair["insert_task"]

        al.log_api_call(
            api_key="key-a", user_id="user-a",
            endpoint="/api/transcribe", task_id="task-a",
        )
        insert("task-a", "vt-a", submitted_by="user-a")
        al.log_api_call(
            api_key="key-b", user_id="user-b",
            endpoint="/api/task/task-a", task_id="task-a",
        )
        al.log_api_call(
            api_key="key-a", user_id="user-a",
            endpoint="/api/task/task-a", task_id="task-a",
        )

        with _client_as(al, "user-a") as client:
            resp = client.get("/api/audit/history?status=all")
        assert resp.status_code == 200
        task_ids = {i["task_id"] for i in resp.json()["data"]["items"]}
        assert "task-a" in task_ids

    def test_recalibrate_submitter_gets_new_task_ownership_not_original_owner(self, db_pair):
        """recalibrate (routes/tasks.py::recalibrate) creates a brand-new
        task_id and INSERTs submitted_by=<caller> directly, even when the
        caller is recalibrating someone else's original view_token -- this
        is an intentional product invariant ("submission always grants
        ownership of the task it creates"), distinct from the polling case
        above which must grant nothing."""
        al = db_pair["audit_logger"]
        insert = db_pair["insert_task"]

        # Original task, owned by A.
        al.log_api_call(
            api_key="key-a", user_id="user-a",
            endpoint="/api/transcribe", task_id="task-a",
        )
        insert("task-a", "vt-a", submitted_by="user-a")

        # B calls /api/recalibrate against A's view_token; tasks.py creates
        # a new task_id stamped submitted_by=B, later archived as its own
        # snapshot (same view_token, different task_id).
        al.log_api_call(
            api_key="key-b", user_id="user-b",
            endpoint="/api/recalibrate", task_id="task-a-recal",
        )
        insert("task-a-recal", "vt-a", submitted_by="user-b")

        with _client_as(al, "user-b") as client:
            resp = client.get("/api/audit/history?status=all")
        assert resp.status_code == 200
        task_ids = {i["task_id"] for i in resp.json()["data"]["items"]}
        assert "task-a-recal" in task_ids
        assert "task-a" not in task_ids

    def test_polling_someone_elses_task_does_not_leak_filter_options(self, db_pair):
        """Same escalation as the /history case above, one endpoint over:
        get_filter_options()'s platform/author queries used to JOIN
        task_audit_snapshots on `a.user_id = ?` alone, with no attribution
        predicate at all -- B polling A's task_id was enough to pull A's
        task's platform/author into B's own filter dropdown options."""
        al = db_pair["audit_logger"]
        insert = db_pair["insert_task"]

        al.log_api_call(
            api_key="key-a", user_id="user-a",
            endpoint="/api/transcribe", task_id="task-a",
        )
        insert("task-a", "vt-a", platform="bilibili", author="Author A", submitted_by="user-a")

        # B polls task-a's progress -- allowed by design, but must not leak
        # task-a's platform/author into B's filter-options.
        al.log_api_call(
            api_key="key-b", user_id="user-b",
            endpoint="/api/task/task-a", task_id="task-a",
        )

        with _client_as(al, "user-b") as client:
            resp = client.get("/api/audit/filter-options")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "bilibili" not in data["platforms"]
        assert "Author A" not in data["authors"]

    def test_task_owner_filter_options_unaffected_by_others_polling(self, db_pair):
        """A's own filter-options must still surface task-a's platform/author,
        both before and after B (or A) polls it."""
        al = db_pair["audit_logger"]
        insert = db_pair["insert_task"]

        al.log_api_call(
            api_key="key-a", user_id="user-a",
            endpoint="/api/transcribe", task_id="task-a",
        )
        insert("task-a", "vt-a", platform="bilibili", author="Author A", submitted_by="user-a")
        al.log_api_call(
            api_key="key-b", user_id="user-b",
            endpoint="/api/task/task-a", task_id="task-a",
        )
        al.log_api_call(
            api_key="key-a", user_id="user-a",
            endpoint="/api/task/task-a", task_id="task-a",
        )

        with _client_as(al, "user-a") as client:
            resp = client.get("/api/audit/filter-options")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "bilibili" in data["platforms"]
        assert "Author A" in data["authors"]
