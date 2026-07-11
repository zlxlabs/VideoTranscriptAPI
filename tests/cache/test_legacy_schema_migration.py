"""Regression test: upgrading a very old on-disk schema (task_status.view_token
still has a UNIQUE constraint, predating the honest-status-model columns) must
end up with every current-schema column present and usable -- not just
transiently.

Root cause being fixed (codex-review R1, item 1): _migrate_database() detected
the legacy view_token UNIQUE constraint, called _rebuild_task_status_table()
(which recreated the table WITHOUT the later-added columns), then `return`ed
immediately -- skipping migrations 2-5 that would otherwise add
llm_config/download_url/error_message/calibration_status/summary_status. Old
databases upgraded straight from the UNIQUE-constraint era therefore never
gained the honest-status-model columns, and the first LLM-completion UPDATE
against them raised "sqlite3.OperationalError: no such column:
calibration_status".

All console output must be in English only (no emoji, no Chinese).
"""
import sqlite3

from src.video_transcript_api.cache.cache_manager import CacheManager


def _create_legacy_db(db_path):
    """Hand-build a pre-honest-status-model schema: task_status.view_token
    still has its old UNIQUE constraint, and none of the columns added by
    migrations 2-5 (llm_config/download_url/error_message/calibration_status/
    summary_status) exist yet -- matching the oldest real-world on-disk shape
    this migration path is meant to handle."""
    conn = sqlite3.connect(str(db_path))
    conn.execute('''
        CREATE TABLE video_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            author TEXT,
            description TEXT,
            media_id TEXT NOT NULL,
            use_speaker_recognition BOOLEAN NOT NULL DEFAULT 0,
            files_loc TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(platform, media_id, use_speaker_recognition)
        )
    ''')
    conn.execute('''
        CREATE TABLE task_status (
            task_id TEXT PRIMARY KEY,
            view_token TEXT NOT NULL UNIQUE,
            url TEXT NOT NULL,
            platform TEXT,
            media_id TEXT,
            use_speaker_recognition BOOLEAN DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'queued',
            title TEXT,
            author TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            cache_id INTEGER
        )
    ''')
    conn.execute(
        "INSERT INTO task_status (task_id, view_token, url, platform, media_id, status) "
        "VALUES ('t1', 'vt1', 'https://example.com/v1', 'youtube', 'vid1', 'queued')"
    )
    conn.commit()
    conn.close()


class TestLegacyUniqueConstraintMigration:
    """CacheManager(db_path=<legacy db>) must fully migrate a view_token-UNIQUE
    era database to the current schema in one pass."""

    def test_rebuild_includes_all_current_columns(self, tmp_path):
        """After migrating a legacy view_token-UNIQUE db, every column the
        current schema expects (including calibration_status/summary_status
        added by migration 5, which used to be skipped by the early return)
        must exist."""
        db_path = tmp_path / "legacy.db"
        _create_legacy_db(db_path)

        cm = CacheManager(cache_dir=str(tmp_path / "cache"), db_path=str(db_path))
        try:
            with sqlite3.connect(str(db_path)) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(task_status)")}

            for expected in (
                "llm_config", "download_url", "error_message",
                "calibration_status", "summary_status",
            ):
                assert expected in columns, f"missing column after migration: {expected}"

            # Migration 1's own purpose (drop the UNIQUE constraint) must also
            # still hold -- the fix must not regress that.
            with cm._get_cursor() as cursor:
                cursor.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_status'"
                )
                table_sql = cursor.fetchone()[0]
            assert not ("UNIQUE" in table_sql and "view_token" in table_sql)
        finally:
            cm.close()

    def test_update_task_status_with_llm_status_does_not_raise(self, tmp_path):
        """The concrete symptom from the bug report: an LLM-completion UPDATE
        that sets calibration_status/summary_status against a
        freshly-upgraded legacy db must not raise "no such column"."""
        db_path = tmp_path / "legacy.db"
        _create_legacy_db(db_path)

        cm = CacheManager(cache_dir=str(tmp_path / "cache"), db_path=str(db_path))
        try:
            cm.update_task_status(
                "t1", "success",
                calibration_status="full",
                summary_status="generated",
            )
            row = cm.get_task_by_id("t1")
            assert row["calibration_status"] == "full"
            assert row["summary_status"] == "generated"
        finally:
            cm.close()

    def test_migration_is_idempotent_across_two_cache_manager_instances(self, tmp_path):
        """Reopening the same (already-migrated) db a second time must be a
        no-op -- no errors, columns still present, existing row preserved."""
        db_path = tmp_path / "legacy.db"
        _create_legacy_db(db_path)

        cm1 = CacheManager(cache_dir=str(tmp_path / "cache"), db_path=str(db_path))
        cm1.close()

        cm2 = CacheManager(cache_dir=str(tmp_path / "cache"), db_path=str(db_path))
        try:
            row = cm2.get_task_by_id("t1")
            assert row is not None
            assert row["view_token"] == "vt1"

            with sqlite3.connect(str(db_path)) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(task_status)")}
            for expected in (
                "llm_config", "download_url", "error_message",
                "calibration_status", "summary_status",
            ):
                assert expected in columns
        finally:
            cm2.close()
