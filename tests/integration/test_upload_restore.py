"""Real SQLite/file restore and guard-only consumer regression contracts."""

from datetime import datetime, timedelta, timezone
import hashlib
import io
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import uuid
import wave

import pytest

from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.logging.audit_logger import AuditLogger

A_GUARD_ONLY_SHA = "6af391d20edab8dfd8b320ce6b91d4d19e3260e2"


def _new_upload(manager, *, retention="never"):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    intent = manager.register_local_upload(
        owner_user_id="restore-owner",
        idempotency_key=f"{now_ms}-{uuid.uuid4()}",
        retention=retention,
        intent_metadata={"filename": "synthetic.wav", "retention": retention},
    )
    return manager.accept_local_upload(
        intent["upload_id"],
        media_id=f"upload_{uuid.uuid4().hex}",
        title="sandbox restore fixture",
    )


def _write_real_result(manager, upload):
    stored = manager.save_cache(
        platform="local_upload",
        url="",
        media_id=upload["media_id"],
        use_speaker_recognition=False,
        transcript_data="Producer-created restore transcript.",
        transcript_type="capswriter",
        title="sandbox restore fixture",
        author="",
        description="",
    )
    assert stored is not None
    manager.update_task_status(
        upload["root_task_id"],
        "success",
        platform="local_upload",
        media_id=upload["media_id"],
        title="sandbox restore fixture",
    )
    transcript_file = Path(stored["transcript_file"])
    assert transcript_file.read_bytes() == b"Producer-created restore transcript."
    return transcript_file


def _age_real_consumers(manager, upload):
    old = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
    with manager._get_cursor() as cursor:
        cursor.execute(
            "UPDATE video_cache SET updated_at = ? WHERE platform = 'local_upload' AND media_id = ?",
            (old, upload["media_id"]),
        )
        cursor.execute(
            "UPDATE task_status SET completed_at = ?, created_at = ? WHERE task_id = ?",
            (old, old, upload["root_task_id"]),
        )


def _resolver_process(cache_dir, token, *, enabled):
    code = (
        "import sys; "
        "from video_transcript_api.cache.cache_manager import CacheManager; "
        "from video_transcript_api.api.services.view_token_resolver import ViewTokenResolver; "
        "cm=CacheManager(cache_dir=sys.argv[1]); "
        "value=ViewTokenResolver(cm).get_view_data_by_token(sys.argv[2]); "
        "print('RESULT=' + str((value or {}).get('status', 'NONE'))); "
        "print('BODY=' + str((value or {}).get('transcript', 'NONE'))); cm.close()"
    )
    env = os.environ.copy()
    env["VTA_UPLOADS_ENABLED"] = "true" if enabled else "false"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    return subprocess.run(
        [sys.executable, "-c", code, str(cache_dir), token],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


def test_snapshot_restore_keeps_external_gate_off_and_protects_long_result(tmp_path):
    source_cache = tmp_path / "producer-cache"
    producer = CacheManager(cache_dir=str(source_cache))
    upload = _new_upload(producer)
    transcript_file = _write_real_result(producer, upload)
    _age_real_consumers(producer, upload)

    # Snapshot the actual consumer-created database and artifact bytes while the
    # share is active; the snapshot does not include the deployment env gate.
    producer.close()
    snapshot_cache = tmp_path / "pre-revocation-snapshot"
    shutil.copytree(source_cache, snapshot_cache)
    snapshot_db_bytes = (snapshot_cache / "cache.db").read_bytes()
    snapshot_transcript = transcript_file.relative_to(source_cache)
    expected_transcript_bytes = (snapshot_cache / snapshot_transcript).read_bytes()

    after_snapshot = CacheManager(cache_dir=str(source_cache))
    revoked = after_snapshot.revoke_local_upload(upload["upload_id"])
    assert revoked["revoked_at"] is not None
    after_snapshot.close()

    # Restore only this known-empty sandbox. The external gate stays false and
    # is passed to a fresh consumer process, not restored from SQLite.
    restored_cache = tmp_path / "restored-cache"
    shutil.copytree(snapshot_cache, restored_cache)
    assert (restored_cache / "cache.db").read_bytes() == snapshot_db_bytes
    restored_transcript = restored_cache / snapshot_transcript
    assert restored_transcript.read_bytes() == expected_transcript_bytes
    restored = CacheManager(cache_dir=str(restored_cache))
    record = restored.get_local_upload_by_id(upload["upload_id"])
    assert record["revoked_at"] is None
    assert record["retention"] == "never"
    restored.audit_logger = AuditLogger(str(tmp_path / "restore-audit.db"))

    current = _resolver_process(restored_cache, upload["view_token"], enabled=False)
    assert current.returncode == 0, current.stderr
    assert "RESULT=NONE" in current.stdout
    assert "BODY=NONE" in current.stdout
    assert "UPLOAD_DISABLED" in current.stdout

    # This dangerous positive control applies only to the pytest restore copy:
    # its old snapshot still has revoked_at=NULL, not a production reopen permit.
    enabled = _resolver_process(restored_cache, upload["view_token"], enabled=True)
    assert enabled.returncode == 0, enabled.stderr
    assert "RESULT=success" in enabled.stdout
    assert f"BODY={expected_transcript_bytes.decode('utf-8')}" in enabled.stdout.splitlines()

    invalid_token = "upload_invalid-restored-view-token"
    assert invalid_token != upload["view_token"]
    invalid = _resolver_process(restored_cache, invalid_token, enabled=True)
    assert invalid.returncode == 0, invalid.stderr
    assert "RESULT=NONE" in invalid.stdout
    assert "BODY=NONE" in invalid.stdout
    assert "UPLOAD_DISABLED" not in invalid.stdout

    # Exercise both actual current cleaners against stale-age but effective
    # never-expiring result and its required root resolver row.
    assert restored.cleanup_old_cache(days=1) == 0
    assert restored_transcript.read_bytes() == expected_transcript_bytes
    assert restored.cleanup_task_status(retention_days=1, cache_retention_days=1) == 0
    assert restored.get_task_by_id(upload["root_task_id"]) is not None
    restored.close()


def test_local_capacity_probe_exercises_real_http_payload_and_loopback_overlap(tmp_path):
    sample = tmp_path / "expected-capacity-payload.wav"
    with wave.open(str(sample), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(48_000)
        writer.writeframes(b"\x00\x00" * (20 * 48_000))
    expected = sample.read_bytes()
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(repo / "scripts/perf/local_upload_capacity.py"), "--duration-seconds", "20"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )

    assert result.returncode == 0, result.stderr
    assert f"sample_bytes={len(expected)}_ffprobe_duration_seconds=20.000" in result.stdout
    assert f"received_payload_sha256={hashlib.sha256(expected).hexdigest()}" in result.stdout
    assert "raw_http_receipts=2_of_2" in result.stdout
    assert "controlled_url_upload_overlap=confirmed" in result.stdout
    temp_peak = next(
        line for line in result.stdout.splitlines()
        if line.startswith("temp_directory_peak_observed_bytes=")
    )
    assert int(temp_peak.split("=", 1)[1].split("_", 1)[0]) > 0
    assert "worker_terminal_states=failed,failed,failed" in result.stdout
    assert "asr_capacity=unknown_external_model_not_called" in result.stdout
    assert "UPLOAD_ENABLE_BLOCKED:" in result.stdout


def test_actual_guard_only_source_consumes_fixture_and_preserves_active_long_artifacts(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", A_GUARD_ONLY_SHA],
        check=False,
        capture_output=True,
        timeout=20,
    )
    assert archive.returncode == 0, archive.stderr.decode("utf-8", errors="replace")
    legacy_root = tmp_path / "guard-only-source"
    legacy_root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
        bundle.extractall(legacy_root, filter="data")

    producer_cache = tmp_path / "active-long-cache"
    producer = CacheManager(cache_dir=str(producer_cache))
    upload = _new_upload(producer, retention="never")
    transcript_file = _write_real_result(producer, upload)
    _age_real_consumers(producer, upload)
    producer.close()

    # Run the real A guard-only consumer source in a separate interpreter over
    # the current SQLite+file producer artifact; do not mock the historical code.
    consumer = (
        "import sys; "
        "from video_transcript_api.cache.cache_manager import CacheManager; "
        "from video_transcript_api.api.services.view_token_resolver import ViewTokenResolver; "
        "from video_transcript_api.utils.logging.audit_logger import AuditLogger; "
        "cm=CacheManager(cache_dir=sys.argv[1]); "
        "cm.audit_logger=AuditLogger(sys.argv[3]); "
        "resolver=ViewTokenResolver(cm); "
        "active=resolver.get_view_data_by_token(sys.argv[2]); "
        "print('ACTIVE_BODY=' + str((active or {}).get('transcript', 'NONE'))); "
        "print('UPLOAD_LEGACY_ALIAS=' + str(cm.get_task_by_view_token(sys.argv[2]))); "
        "print('BLANK_ALIAS=' + str(cm.get_task_by_view_token(''))); "
        "print('CACHE_CLEANED=' + str(cm.cleanup_old_cache(days=1))); "
        "print('TASK_CLEANED=' + str(cm.cleanup_task_status(retention_days=1, cache_retention_days=1))); "
        "print('ROOT_PRESENT=' + str(cm.get_task_by_id(sys.argv[4]) is not None)); cm.close()"
    )
    env = os.environ.copy()
    env["VTA_UPLOADS_ENABLED"] = "true"
    env["PYTHONPATH"] = str(legacy_root / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            consumer,
            str(producer_cache),
            upload["view_token"],
            str(tmp_path / "guard-only-audit.db"),
            upload["root_task_id"],
        ],
        cwd=legacy_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "ACTIVE_BODY=Producer-created restore transcript." in result.stdout
    assert "UPLOAD_LEGACY_ALIAS=None" in result.stdout
    assert "BLANK_ALIAS=None" in result.stdout
    assert "CACHE_CLEANED=0" in result.stdout
    assert "TASK_CLEANED=0" in result.stdout
    assert "ROOT_PRESENT=True" in result.stdout
    assert transcript_file.read_bytes() == b"Producer-created restore transcript."
