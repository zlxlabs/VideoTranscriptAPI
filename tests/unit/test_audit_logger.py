"""
AuditLogger unit tests.

Covers:
- SQL parameterization (no format string injection)
- Connection reuse via threading.local
- Schema migration system (version check, upgrade flow)
- Core CRUD operations (log, query, stats, cleanup)

All console output must be in English only (no emoji, no Chinese).
"""

import os
import sys
import sqlite3
import tempfile
import threading

import pytest


from video_transcript_api.utils.logging.audit_logger import AuditLogger, CURRENT_SCHEMA_VERSION


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a temporary database path for each test."""
    return str(tmp_path / "test_audit.db")


@pytest.fixture
def audit_logger(tmp_db):
    """Create a fresh AuditLogger instance with a temp database."""
    return AuditLogger(db_path=tmp_db)


# ============================================================
# SQL Parameterization Tests
# ============================================================


class TestSQLParameterization:
    """Verify that days parameters are not interpolated via format strings."""

    def test_get_user_stats_with_malicious_days(self, audit_logger):
        """days parameter should not allow SQL injection."""
        # If format() were used, this would cause a SQL syntax error or injection
        # With parameterized queries, it should safely handle any integer
        result = audit_logger.get_user_stats("test_user", days=1)
        assert "error" not in result
        assert result["total_calls"] == 0

    def test_get_all_users_stats_with_large_days(self, audit_logger):
        """Large days value should work without format string issues."""
        result = audit_logger.get_all_users_stats(days=99999)
        assert isinstance(result, list)

    def test_cleanup_old_logs_parameterized(self, audit_logger):
        """cleanup_old_logs should use parameterized queries."""
        deleted = audit_logger.cleanup_old_logs(days=1)
        assert deleted == 0


# ============================================================
# Connection Reuse Tests
# ============================================================


class TestConnectionReuse:
    """Verify threading.local connection reuse pattern."""

    def test_same_thread_reuses_connection(self, audit_logger):
        """Multiple calls on the same thread should reuse the connection."""
        conn1 = audit_logger._get_connection()
        conn2 = audit_logger._get_connection()
        assert conn1 is conn2

    def test_different_threads_get_different_connections(self, audit_logger):
        """Each thread should get its own independent connection."""
        connections = {}
        barrier = threading.Barrier(2)

        def get_conn(name):
            connections[name] = audit_logger._get_connection()
            barrier.wait()

        t1 = threading.Thread(target=get_conn, args=("t1",))
        t2 = threading.Thread(target=get_conn, args=("t2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert connections["t1"] is not connections["t2"]

    def test_context_manager_commits_on_success(self, audit_logger):
        """_get_cursor should auto-commit on successful operations."""
        with audit_logger._get_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM api_audit_logs")
            count = cursor.fetchone()[0]
        assert count == 0

    def test_context_manager_rolls_back_on_error(self, audit_logger):
        """_get_cursor should rollback on exception."""
        # Insert a record first
        audit_logger.log_api_call(
            api_key="test1234test",
            user_id="user1",
            endpoint="/test"
        )

        # Attempt a failing operation
        try:
            with audit_logger._get_cursor() as cursor:
                cursor.execute("DELETE FROM api_audit_logs")
                raise ValueError("simulated error")
        except ValueError:
            pass

        # Record should still exist due to rollback
        calls = audit_logger.get_recent_calls(limit=10)
        assert len(calls) == 1


# ============================================================
# Schema Migration Tests
# ============================================================


class TestSchemaMigration:
    """Verify the schema version check and migration system."""

    def test_fresh_database_gets_current_version(self, tmp_db):
        """A new database should be at CURRENT_SCHEMA_VERSION."""
        logger_instance = AuditLogger(db_path=tmp_db)
        with logger_instance._get_cursor() as cursor:
            version = logger_instance._get_schema_version(cursor)
        assert version == CURRENT_SCHEMA_VERSION

    def test_schema_version_table_exists(self, tmp_db):
        """schema_version table should be created on init."""
        logger_instance = AuditLogger(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_migration_creates_audit_logs_table(self, tmp_db):
        """v1 migration should create api_audit_logs with correct columns."""
        logger_instance = AuditLogger(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(api_audit_logs)")
        columns = {col[1] for col in cursor.fetchall()}
        conn.close()

        expected_columns = {
            "id", "api_key_masked", "user_id", "endpoint", "video_url",
            "request_time", "processing_time_ms", "status_code",
            "task_id", "user_agent", "remote_ip", "wechat_webhook"
        }
        assert expected_columns == columns

    def test_migration_creates_indexes(self, tmp_db):
        """v1 migration should create performance indexes."""
        logger_instance = AuditLogger(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        conn.close()

        expected = {"idx_api_key", "idx_user_id", "idx_request_time", "idx_endpoint"}
        assert expected.issubset(indexes)

    def test_reinit_does_not_duplicate(self, tmp_db):
        """Calling _init_database twice should not fail or duplicate data."""
        logger_instance = AuditLogger(db_path=tmp_db)
        # Reinitialize - should be idempotent
        logger_instance._init_database()
        with logger_instance._get_cursor() as cursor:
            version = logger_instance._get_schema_version(cursor)
        assert version == CURRENT_SCHEMA_VERSION

    def test_existing_db_without_version_table_migrates(self, tmp_db):
        """A pre-existing database without schema_version should be migrated."""
        # Create a database with only the audit logs table (simulating old version)
        conn = sqlite3.connect(tmp_db)
        conn.execute('''
            CREATE TABLE api_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key_masked TEXT NOT NULL,
                user_id TEXT,
                endpoint TEXT NOT NULL,
                video_url TEXT,
                request_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                processing_time_ms INTEGER,
                status_code INTEGER,
                task_id TEXT,
                user_agent TEXT,
                remote_ip TEXT
            )
        ''')
        # Insert a test record to verify data preservation
        conn.execute(
            "INSERT INTO api_audit_logs (api_key_masked, endpoint) VALUES (?, ?)",
            ("test****test", "/old-endpoint")
        )
        conn.commit()
        conn.close()

        # Init AuditLogger on existing db - should add schema_version
        logger_instance = AuditLogger(db_path=tmp_db)
        with logger_instance._get_cursor() as cursor:
            version = logger_instance._get_schema_version(cursor)
        assert version == CURRENT_SCHEMA_VERSION

        # Old data should be preserved
        calls = logger_instance.get_recent_calls(limit=10)
        assert len(calls) == 1
        assert calls[0]["endpoint"] == "/old-endpoint"


# ============================================================
# Core CRUD Operations Tests
# ============================================================


class TestCRUDOperations:
    """Verify basic log, query, stats, and cleanup operations."""

    def test_log_api_call_success(self, audit_logger):
        """log_api_call should insert a record and return True."""
        result = audit_logger.log_api_call(
            api_key="abcd1234efgh5678",
            user_id="user1",
            endpoint="/api/transcribe",
            video_url="https://example.com/video",
            processing_time_ms=1500,
            status_code=200,
            task_id="task-001"
        )
        assert result is True

        calls = audit_logger.get_recent_calls(limit=1)
        assert len(calls) == 1
        assert calls[0]["endpoint"] == "/api/transcribe"
        assert calls[0]["api_key_masked"] == "abcd********5678"

    def test_mask_api_key_short_key(self, audit_logger):
        """Short API keys should be fully masked."""
        assert audit_logger._mask_api_key("abc") == "****"
        assert audit_logger._mask_api_key("") == "****"
        assert audit_logger._mask_api_key(None) == "****"

    def test_mask_api_key_normal_key(self, audit_logger):
        """Normal API keys should show first 4 and last 4 chars."""
        result = audit_logger._mask_api_key("abcdefghijklmnop")
        assert result == "abcd********mnop"

    def test_get_user_stats_with_data(self, audit_logger):
        """get_user_stats should return correct statistics."""
        for i in range(5):
            audit_logger.log_api_call(
                api_key="testkey12345678",
                user_id="statsuser",
                endpoint="/api/transcribe" if i < 3 else "/api/status",
                processing_time_ms=100 * (i + 1),
                status_code=200
            )

        stats = audit_logger.get_user_stats("statsuser", days=1)
        assert stats["total_calls"] == 5
        assert stats["avg_processing_time_ms"] == 300.0
        assert len(stats["endpoint_stats"]) == 2

    def test_get_recent_calls_with_user_filter(self, audit_logger):
        """get_recent_calls should filter by user_id when provided."""
        audit_logger.log_api_call(api_key="key12345678", user_id="alice", endpoint="/a")
        audit_logger.log_api_call(api_key="key12345678", user_id="bob", endpoint="/b")
        audit_logger.log_api_call(api_key="key12345678", user_id="alice", endpoint="/c")

        alice_calls = audit_logger.get_recent_calls(user_id="alice")
        assert len(alice_calls) == 2
        assert all(c["user_id"] == "alice" for c in alice_calls)

    def test_get_recent_calls_respects_limit(self, audit_logger):
        """get_recent_calls should respect the limit parameter."""
        for i in range(10):
            audit_logger.log_api_call(
                api_key="key12345678", user_id="user1", endpoint=f"/ep{i}"
            )

        calls = audit_logger.get_recent_calls(limit=3)
        assert len(calls) == 3

    def test_cleanup_old_logs(self, audit_logger):
        """cleanup_old_logs should delete records older than specified days."""
        # Insert records with old timestamps
        conn = sqlite3.connect(audit_logger.db_path)
        conn.execute(
            "INSERT INTO api_audit_logs (api_key_masked, endpoint, request_time) VALUES (?, ?, ?)",
            ("test****test", "/old", "2020-01-01 00:00:00")
        )
        conn.execute(
            "INSERT INTO api_audit_logs (api_key_masked, endpoint, request_time) VALUES (?, ?, ?)",
            ("test****test", "/new", "2099-01-01 00:00:00")
        )
        conn.commit()
        conn.close()

        deleted = audit_logger.cleanup_old_logs(days=1)
        assert deleted == 1

        calls = audit_logger.get_recent_calls(limit=10)
        assert len(calls) == 1
        assert calls[0]["endpoint"] == "/new"

    def test_get_all_users_stats(self, audit_logger):
        """get_all_users_stats should return stats for all active users."""
        audit_logger.log_api_call(api_key="key12345678", user_id="user_a", endpoint="/a")
        audit_logger.log_api_call(api_key="key12345678", user_id="user_b", endpoint="/b")
        audit_logger.log_api_call(api_key="key12345678", user_id="user_a", endpoint="/c")

        all_stats = audit_logger.get_all_users_stats(days=1)
        assert len(all_stats) == 2
        user_ids = {s["user_id"] for s in all_stats}
        assert user_ids == {"user_a", "user_b"}


# ============================================================
# WAL Mode Tests
# ============================================================


class TestWALMode:
    """Verify WAL mode is enabled on connections."""

    def test_wal_mode_enabled(self, audit_logger):
        """Connection should use WAL journal mode."""
        conn = audit_logger._get_connection()
        cursor = conn.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        assert mode == "wal"


# ============================================================
# Migration V2 Tests
# ============================================================


class TestMigrateV2:
    """Verify that _migrate_v2 adds wechat_webhook column and index."""

    def test_fresh_db_has_wechat_webhook_column(self, tmp_db):
        """A freshly created DB should include wechat_webhook column."""
        AuditLogger(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(api_audit_logs)")
        columns = {col[1] for col in cursor.fetchall()}
        conn.close()
        assert "wechat_webhook" in columns

    def test_fresh_db_has_wechat_webhook_index(self, tmp_db):
        """A freshly created DB should have idx_wechat_webhook index."""
        AuditLogger(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_wechat_webhook'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_v1_db_migrates_to_v2(self, tmp_db):
        """An existing v1 DB (without wechat_webhook) should gain the column after migration."""
        # Manually create a v1-style database
        conn = sqlite3.connect(tmp_db)
        conn.execute('''CREATE TABLE schema_version (version INTEGER NOT NULL)''')
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.execute('''
            CREATE TABLE api_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key_masked TEXT NOT NULL,
                user_id TEXT,
                endpoint TEXT NOT NULL,
                video_url TEXT,
                request_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                processing_time_ms INTEGER,
                status_code INTEGER,
                task_id TEXT,
                user_agent TEXT,
                remote_ip TEXT
            )
        ''')
        conn.execute(
            "INSERT INTO api_audit_logs (api_key_masked, endpoint) VALUES (?, ?)",
            ("test****test", "/v1-record")
        )
        conn.commit()
        conn.close()

        # Init should detect v1 and run _migrate_v2
        logger_instance = AuditLogger(db_path=tmp_db)

        # Column should now exist
        conn = sqlite3.connect(tmp_db)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(api_audit_logs)")
        columns = {col[1] for col in cursor.fetchall()}
        conn.close()
        assert "wechat_webhook" in columns

        # Schema version should be updated
        with logger_instance._get_cursor() as cursor:
            version = logger_instance._get_schema_version(cursor)
        assert version == CURRENT_SCHEMA_VERSION

        # Old records should be preserved
        calls = logger_instance.get_recent_calls(limit=10)
        assert len(calls) == 1
        assert calls[0]["endpoint"] == "/v1-record"

    def test_v2_migration_is_idempotent(self, tmp_db):
        """Running _init_database twice on a v2 DB must not raise errors."""
        logger_instance = AuditLogger(db_path=tmp_db)
        logger_instance._init_database()  # should not raise
        with logger_instance._get_cursor() as cursor:
            version = logger_instance._get_schema_version(cursor)
        assert version == CURRENT_SCHEMA_VERSION


# ============================================================
# Webhook Logging Tests
# ============================================================


class TestWebhookLogging:
    """Verify that wechat_webhook is stored and retrievable via log_api_call."""

    def test_log_api_call_stores_webhook(self, audit_logger):
        """wechat_webhook passed to log_api_call should be persisted in DB."""
        webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123"
        audit_logger.log_api_call(
            api_key="testkey12345678",
            user_id="user1",
            endpoint="/api/transcribe",
            task_id="task-001",
            wechat_webhook=webhook_url,
        )

        conn = sqlite3.connect(audit_logger.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT wechat_webhook FROM api_audit_logs WHERE task_id = ?", ("task-001",))
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == webhook_url

    def test_log_api_call_without_webhook_stores_null(self, audit_logger):
        """Omitting wechat_webhook should store NULL (backward compat)."""
        audit_logger.log_api_call(
            api_key="testkey12345678",
            user_id="user1",
            endpoint="/api/transcribe",
            task_id="task-002",
        )

        conn = sqlite3.connect(audit_logger.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT wechat_webhook FROM api_audit_logs WHERE task_id = ?", ("task-002",))
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] is None

    def test_multiple_webhooks_stored_independently(self, audit_logger):
        """Different tasks can have different webhook addresses."""
        audit_logger.log_api_call(
            api_key="testkey12345678", user_id="u1", endpoint="/api/transcribe",
            task_id="t1", wechat_webhook="https://hook.example.com/key=aaa",
        )
        audit_logger.log_api_call(
            api_key="testkey12345678", user_id="u1", endpoint="/api/transcribe",
            task_id="t2", wechat_webhook="https://hook.example.com/key=bbb",
        )

        conn = sqlite3.connect(audit_logger.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT task_id, wechat_webhook FROM api_audit_logs ORDER BY task_id"
        )
        rows = {row[0]: row[1] for row in cursor.fetchall()}
        conn.close()

        assert rows["t1"] == "https://hook.example.com/key=aaa"
        assert rows["t2"] == "https://hook.example.com/key=bbb"


# ============================================================
# Migration V3 Tests (llm_usage table)
# ============================================================


class TestMigrateV3:
    """Verify that _migrate_v3 creates the llm_usage table and indexes."""

    def test_fresh_db_has_llm_usage_table(self, tmp_db):
        """A freshly created DB should include the llm_usage table."""
        AuditLogger(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_usage'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_fresh_db_llm_usage_columns(self, tmp_db):
        """llm_usage table should have the expected columns."""
        AuditLogger(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(llm_usage)")
        columns = {col[1] for col in cursor.fetchall()}
        conn.close()

        expected_columns = {
            "id", "task_id", "stage", "model", "prompt_tokens",
            "completion_tokens", "total_tokens", "duration_ms",
            "usage_missing", "created_at",
        }
        assert expected_columns == columns

    def test_fresh_db_has_llm_usage_indexes(self, tmp_db):
        """A freshly created DB should have llm_usage indexes on task_id/created_at."""
        AuditLogger(db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_llm_usage_%'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        conn.close()
        assert {"idx_llm_usage_task_id", "idx_llm_usage_created_at"}.issubset(indexes)

    def test_v2_db_migrates_to_v3(self, tmp_db):
        """An existing v2 DB (without llm_usage table) should gain it after migration."""
        # Manually create a v2-style database (schema_version=2, no llm_usage table)
        conn = sqlite3.connect(tmp_db)
        conn.execute('''CREATE TABLE schema_version (version INTEGER NOT NULL)''')
        conn.execute("INSERT INTO schema_version (version) VALUES (2)")
        conn.execute('''
            CREATE TABLE api_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key_masked TEXT NOT NULL,
                user_id TEXT,
                endpoint TEXT NOT NULL,
                video_url TEXT,
                request_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                processing_time_ms INTEGER,
                status_code INTEGER,
                task_id TEXT,
                user_agent TEXT,
                remote_ip TEXT,
                wechat_webhook TEXT
            )
        ''')
        conn.commit()
        conn.close()

        logger_instance = AuditLogger(db_path=tmp_db)

        conn = sqlite3.connect(tmp_db)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_usage'"
        )
        assert cursor.fetchone() is not None
        conn.close()

        with logger_instance._get_cursor() as cursor:
            version = logger_instance._get_schema_version(cursor)
        assert version == CURRENT_SCHEMA_VERSION

    def test_v3_migration_is_idempotent(self, tmp_db):
        """Running the v2->v3 migration twice on the same DB must not raise errors."""
        # Build a v2 DB first
        conn = sqlite3.connect(tmp_db)
        conn.execute('''CREATE TABLE schema_version (version INTEGER NOT NULL)''')
        conn.execute("INSERT INTO schema_version (version) VALUES (2)")
        conn.execute('''
            CREATE TABLE api_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key_masked TEXT NOT NULL,
                user_id TEXT,
                endpoint TEXT NOT NULL,
                video_url TEXT,
                request_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                processing_time_ms INTEGER,
                status_code INTEGER,
                task_id TEXT,
                user_agent TEXT,
                remote_ip TEXT,
                wechat_webhook TEXT
            )
        ''')
        conn.commit()
        conn.close()

        # First migration run (v2 -> v3)
        logger_instance = AuditLogger(db_path=tmp_db)
        # Second run against the now-v3 database must be a no-op, not an error
        logger_instance._init_database()

        with logger_instance._get_cursor() as cursor:
            version = logger_instance._get_schema_version(cursor)
        assert version == CURRENT_SCHEMA_VERSION

        conn = sqlite3.connect(tmp_db)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(llm_usage)")
        columns = {col[1] for col in cursor.fetchall()}
        conn.close()
        assert "task_id" in columns
        assert "usage_missing" in columns
