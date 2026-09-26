"""Producer-to-SQLite-to-CLI observability contract tests."""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from video_transcript_api.api.services import llm_ops
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.llm.processors.notes_processor import (
    compute_notes_anchor_fingerprint,
)
from video_transcript_api.utils.llm_status import (
    CalibrationStatus,
    ChaptersStatus,
    NotesStatus,
    SummaryStatus,
)
from video_transcript_api.utils.logging.audit_logger import AuditLogger
from video_transcript_api.utils.logging.usage_recorder import UsageRecorder
from video_transcript_api.utils.perf_tracker import PerfTracker
from video_transcript_api.utils.task_status import TaskStatus


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "task_observability_report.py"


def _seed_notes_cache(cache_manager, media_id):
    segments = [
        {"start_time": 0, "end_time": 60, "speaker": "Host", "text": "transcript"}
    ]
    fingerprint = compute_notes_anchor_fingerprint(segments)
    cache_manager.save_cache(
        platform="youtube",
        url=f"https://example.com/{media_id}",
        media_id=media_id,
        use_speaker_recognition=False,
        transcript_data="raw transcript",
        transcript_type="capswriter",
        title="Observability demo",
        author="Fixture author",
    )
    for layer, content in (
        ("structured", {"dialogs": segments}),
        ("calibrated", "calibrated transcript"),
        ("summary", "summary artifact"),
        (
            "chapters",
            {"source": {"fingerprint": fingerprint}, "chapters": [{"title": "One"}]},
        ),
    ):
        cache_manager.save_llm_result(
            platform="youtube",
            media_id=media_id,
            use_speaker_recognition=False,
            llm_type=layer,
            content=content,
        )
    cache_manager.save_llm_status(
        platform="youtube",
        media_id=media_id,
        use_speaker_recognition=False,
        calibration_status=CalibrationStatus.FULL,
        summary_status=SummaryStatus.GENERATED,
        chapters_status=ChaptersStatus.GENERATED,
    )
    cached = cache_manager.get_cache(
        "youtube", media_id, use_speaker_recognition=False
    )
    return cached["file_path"], fingerprint


def _notes_task(task_id, media_id, cache_dir, tracker):
    return {
        "task_id": task_id,
        "url": f"https://example.com/{media_id}",
        "display_url": f"https://example.com/{media_id}",
        "platform": "youtube",
        "media_id": media_id,
        "video_title": "Observability demo",
        "author": "Fixture author",
        "description": "",
        "transcript": "calibrated transcript",
        "use_speaker_recognition": False,
        "transcription_data": None,
        "cache_dir": cache_dir,
        "notification_webhooks": {},
        "perf_tracker": tracker,
        "processing_options": {
            "calibrate": False,
            "summarize": False,
            "infer_speaker_names": False,
            "chapters": False,
            "notes": True,
        },
    }


def _run_notes_worker(cache_manager, task, notes_result):
    coordinator = SimpleNamespace(
        llm_client=MagicMock(),
        config=SimpleNamespace(get_models=lambda: {}),
    )
    processor = MagicMock()
    processor.process.side_effect = lambda **kwargs: (
        kwargs["progress_callback"](1, 1) or notes_result
    )
    router = MagicMock()
    with (
        patch.object(llm_ops, "cache_manager", cache_manager),
        patch.object(llm_ops, "llm_coordinator", coordinator),
        patch.object(llm_ops, "llm_task_queue", MagicMock()),
        patch.object(llm_ops, "NotesProcessor", MagicMock(return_value=processor)),
        patch.object(llm_ops, "get_notification_router", lambda: router),
    ):
        llm_ops._handle_llm_task(task)


def _run_report(cache_path, audit_path, since, until):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--cache-db",
            str(cache_path),
            "--audit-db",
            str(audit_path),
            "--since",
            since,
            "--until",
            until,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_notes_worker_snapshot_survives_cache_cleanup_and_cli_reports_real_failure(
    tmp_path,
):
    audit_path = tmp_path / "audit.db"
    cache_manager = CacheManager(str(tmp_path / "cache"))
    cache_path = cache_manager.db_path
    audit_logger = AuditLogger(str(audit_path))
    cache_manager.audit_logger = audit_logger
    usage = UsageRecorder(audit_logger)
    created_tasks = []
    try:
        for name, result_status in (("success", NotesStatus.GENERATED), ("failed", NotesStatus.FAILED)):
            media_id = f"media-{name}"
            cache_dir, fingerprint = _seed_notes_cache(cache_manager, media_id)
            task_id = cache_manager.create_task(
                url=f"https://example.com/{media_id}",
                platform="youtube",
                media_id=media_id,
            )["task_id"]
            cache_manager.update_task_status(task_id, TaskStatus.CALIBRATING)
            tracker = PerfTracker(task_id)
            with tracker.track("url_parse"):
                pass
            text = "## [00:00:00 - 00:01:00] Chapter\n- Notes" if result_status == NotesStatus.GENERATED else None
            result = SimpleNamespace(
                status=result_status,
                text=text,
                error="RAW-NOTES-FAILURE" if result_status == NotesStatus.FAILED else None,
                fingerprint=fingerprint,
                chapter_count=1,
            )
            _run_notes_worker(
                cache_manager,
                _notes_task(task_id, media_id, cache_dir, tracker),
                result,
            )
            created_tasks.append(task_id)
            assert cache_manager.get_task_by_id(task_id)["status"] == (
                TaskStatus.SUCCESS if result_status == NotesStatus.GENERATED else TaskStatus.FAILED
            )
            assert usage.record(
                task_id=task_id,
                stage="notes",
                model="fixture-model",
                prompt_tokens=11,
                completion_tokens=7,
                total_tokens=18,
                duration_ms=60000,
                usage_missing=False,
            )

        with audit_logger._get_cursor() as cursor:
            snapshots = {
                row[0]: json.loads(row[1])
                for row in cursor.execute(
                    "SELECT task_id, observability_json FROM task_audit_snapshots"
                )
            }
        success_snapshot = snapshots[created_tasks[0]]
        failed_snapshot = snapshots[created_tasks[1]]
        assert success_snapshot["notes_status"] == NotesStatus.GENERATED
        assert success_snapshot["stages"]["llm_processing"]["successes"] == 1
        assert failed_snapshot["notes_status"] == NotesStatus.FAILED
        assert failed_snapshot["stages"]["url_parse"]["successes"] == 1
        assert failed_snapshot["stages"]["llm_processing"]["failures"] == 1
        serialized = json.dumps(snapshots)
        assert "RAW-NOTES-FAILURE" not in serialized
        assert "https://example.com" not in serialized
        assert "Observability demo" not in serialized

        with cache_manager._get_cursor() as cursor:
            cursor.executemany(
                "DELETE FROM task_status WHERE task_id = ?",
                [(task_id,) for task_id in created_tasks],
            )
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        until = (now + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        result = _run_report(cache_path, audit_path, since, until)
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["tasks"]["count"] == 2
        assert report["tasks"]["notes_status"]["states"] == {
            "failed": 1,
            "generated": 1,
        }
        assert report["duration_ms"]["stages"]["llm_processing"]["failures"] == 1
        assert report["llm_usage"]["calls"] == 2
        assert report["llm_usage"]["duration_sum_ms"] == 120000
        assert report["duration_ms"]["end_to_end"]["p95"] < 120000
    finally:
        cache_manager.close()
        audit_logger.close()


def test_audit_v5_migration_preserves_rows_and_is_idempotent(tmp_path):
    audit_path = tmp_path / "audit-v5.db"
    with sqlite3.connect(audit_path) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version VALUES (5)")
        connection.execute(
            """CREATE TABLE task_audit_snapshots (
                task_id TEXT PRIMARY KEY, view_token TEXT, title TEXT, author TEXT,
                platform TEXT, status TEXT NOT NULL, calibration_status TEXT,
                summary_status TEXT, submitted_by TEXT, processing_options TEXT,
                completed_at TEXT, content_expired INTEGER NOT NULL DEFAULT 0,
                archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                chapters_status TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO task_audit_snapshots (task_id, status, title) VALUES (?, ?, ?)",
            ("historical-task", "success", "Historical"),
        )

    first = AuditLogger(str(audit_path))
    first.close()
    second = AuditLogger(str(audit_path))
    with second._get_cursor() as cursor:
        assert cursor.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        row = cursor.execute(
            "SELECT title, created_at, observability_json "
            "FROM task_audit_snapshots WHERE task_id = ?",
            ("historical-task",),
        ).fetchone()
        assert tuple(row) == ("Historical", None, None)
    second.close()


def test_startup_repair_archives_observation_without_backfilling_notes_from_media(
    tmp_path,
):
    cache_manager = CacheManager(str(tmp_path / "cache.db"))
    audit_logger = AuditLogger(str(tmp_path / "audit.db"))
    cache_manager.audit_logger = audit_logger
    try:
        cache_manager.save_cache(
            platform="youtube",
            url="https://example.com/repair",
            media_id="repair",
            use_speaker_recognition=False,
            transcript_data="current transcript",
            transcript_type="capswriter",
            title="Repair fixture",
            author="Fixture author",
        )
        cache_manager.save_llm_status(
            platform="youtube",
            media_id="repair",
            use_speaker_recognition=False,
            notes_status=NotesStatus.GENERATED,
        )
        assert cache_manager.get_cache(
            "youtube", "repair", use_speaker_recognition=False
        )["llm_status"]["notes_status"] == NotesStatus.GENERATED
        task_id = cache_manager.create_task(
            url="https://example.com/repair", platform="youtube", media_id="repair"
        )["task_id"]
        cache_manager.update_task_status(
            task_id,
            TaskStatus.FAILED,
            skip_archive=True,
            terminal_snapshot={
                "observability": {
                    "stages": {
                        "download": {
                            "elapsed_ms": 0,
                            "count": 1,
                            "successes": 0,
                            "failures": 1,
                        }
                    }
                },
            },
        )
        assert audit_logger.repair_task_snapshots(cache_manager) == 1
        snapshot = audit_logger.get_task_snapshot(task_id)
        assert snapshot["created_at"] is not None
        assert json.loads(snapshot["observability_json"]) == {
            "stages": {
                "download": {
                    "elapsed_ms": 0,
                    "count": 1,
                    "successes": 0,
                    "failures": 1,
                }
            },
        }
        assert "notes_status" not in json.loads(snapshot["observability_json"])
    finally:
        cache_manager.close()
        audit_logger.close()
