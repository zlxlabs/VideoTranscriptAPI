"""Local-upload temporary-byte admission contracts."""

from types import SimpleNamespace

import pytest

from video_transcript_api.api.services import transcription
from video_transcript_api.transcriber.capswriter_client import _atomic_write_text


def _admission_fixture(tmp_path, monkeypatch, accepted=True):
    task_id = "task-budget"
    task_dir = tmp_path / f"task_{task_id}"
    task_dir.mkdir()
    (task_dir / "upload-source.bin").write_bytes(b"source")
    (task_dir / "result.txt").write_bytes(b"old")
    reservations = []
    runtime = SimpleNamespace(
        reserve_upload_temp=lambda task, size, budget, base_dir: (
            reservations.append((task, size, budget, base_dir)) or accepted
        )
    )
    temp_manager = SimpleNamespace(
        base_dir=tmp_path,
        get_task_dir=lambda task: task_dir if task == task_id else None,
    )
    config = {
        "storage": {
            "upload_limits": {"upload_temp_budget_mib": 1},
        }
    }
    monkeypatch.setattr(transcription, "get_runtime", lambda: runtime)
    monkeypatch.setattr(transcription, "get_temp_manager", lambda: temp_manager)
    monkeypatch.setattr(transcription, "get_config", lambda: config)
    return task_id, task_dir, reservations


def test_write_admission_reserves_source_existing_target_and_utf8_tmp(
    tmp_path, monkeypatch
):
    task_id, task_dir, reservations = _admission_fixture(tmp_path, monkeypatch)
    admission = transcription._local_upload_write_admission(task_id)

    _atomic_write_text(
        task_dir / "result.txt",
        "你好",
        write_admission=admission,
    )

    assert reservations == [
        (
            task_id,
            len(b"source") + len(b"old") + len("你好".encode("utf-8")),
            1024 * 1024,
            str(tmp_path),
        )
    ]
    assert (task_dir / "result.txt").read_bytes() == "你好".encode("utf-8")


def test_write_admission_rejection_creates_no_tmp_file(tmp_path, monkeypatch):
    task_id, task_dir, reservations = _admission_fixture(
        tmp_path, monkeypatch, accepted=False
    )
    admission = transcription._local_upload_write_admission(task_id)

    with pytest.raises(RuntimeError, match="write admission rejected"):
        _atomic_write_text(
            task_dir / "result.txt",
            "new output",
            write_admission=admission,
        )

    assert len(reservations) == 1
    assert sorted(path.name for path in task_dir.iterdir()) == [
        "result.txt",
        "upload-source.bin",
    ]
    assert (task_dir / "result.txt").read_bytes() == b"old"
