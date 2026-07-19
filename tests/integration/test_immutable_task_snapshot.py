import json
import threading
from pathlib import Path

import pytest

from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.task_status import TaskStatus


@pytest.fixture
def manager(tmp_path):
    value = CacheManager(str(tmp_path / "cache"))
    yield value
    value.close()


def test_task_persists_normalized_options_submitter_and_terminal_snapshot(manager):
    task_id = manager.create_task(
        "https://example.com/video",
        processing_options={
            "calibrate": False,
            "summarize": True,
            "infer_speaker_names": False,
        },
        submitted_by="user-1",
    )["task_id"]
    assert manager.update_task_status(
        task_id,
        TaskStatus.SUCCESS,
        terminal_snapshot={"title": "first", "summary_status": "generated"},
    ) is True

    task = manager.get_task_by_id(task_id)
    assert task["processing_options"] == {
        "calibrate": False,
        "summarize": True,
        "infer_speaker_names": False,
    }
    assert task["submitted_by"] == "user-1"
    assert task["terminal_snapshot"] == {
        "status": "success",
        "title": "first",
        "summary_status": "generated",
    }


def test_terminal_snapshot_is_write_once_even_with_force(manager):
    task_id = manager.create_task("https://example.com/write-once")["task_id"]
    assert manager.update_task_status(
        task_id, TaskStatus.SUCCESS, terminal_snapshot={"version": 1}
    ) is True
    assert manager.update_task_status(
        task_id,
        TaskStatus.FAILED,
        force=True,
        error_message="late worker",
        terminal_snapshot={"version": 2},
    ) is False
    task = manager.get_task_by_id(task_id)
    assert task["status"] == TaskStatus.SUCCESS
    assert task["terminal_snapshot"] == {"status": "success", "version": 1}
    assert task["error_message"] is None


def test_terminal_snapshot_uses_authoritative_transition_fields(manager):
    task_id = manager.create_task("https://example.com/authoritative")['task_id']

    assert manager.update_task_status(
        task_id,
        TaskStatus.FAILED,
        platform="youtube",
        error_message="actual failure",
        terminal_snapshot={
            "status": "success",
            "platform": "bilibili",
            "error_message": "forged",
        },
    ) is True

    snapshot = manager.get_task_by_id(task_id)["terminal_snapshot"]
    assert snapshot["status"] == TaskStatus.FAILED
    assert snapshot["platform"] == "youtube"
    assert snapshot["error_message"] == "actual failure"


def test_concurrent_terminal_compare_and_set_has_one_winner(manager):
    task_id = manager.create_task("https://example.com/race")["task_id"]
    barrier = threading.Barrier(3)
    results = []

    def finish(status, winner):
        barrier.wait()
        results.append(
            manager.update_task_status(
                task_id, status, terminal_snapshot={"winner": winner}
            )
        )

    threads = [
        threading.Thread(target=finish, args=(TaskStatus.SUCCESS, "success")),
        threading.Thread(target=finish, args=(TaskStatus.FAILED, "failed")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]
    task = manager.get_task_by_id(task_id)
    assert task["terminal_snapshot"]["winner"] in {"success", "failed"}


def test_save_llm_status_returns_merged_snapshot(manager):
    manager.save_cache(
        "youtube", "https://example.com/v", "v", False, "raw", "plain"
    )
    first = manager.save_llm_status(
        "youtube", "v", False, calibration_status="full"
    )
    second = manager.save_llm_status(
        "youtube", "v", False, summary_status="generated"
    )
    assert first["calibration_status"] == "full"
    assert second["calibration_status"] == "full"
    assert second["summary_status"] == "generated"
    assert json.loads(
        (Path(manager.get_cache("youtube", "v", False)["file_path"]) / "llm_status.json").read_text()
    ) == second


def test_repository_failures_are_not_reported_as_zero(manager, monkeypatch):
    def fail_cursor():
        raise OSError("disk unavailable")

    monkeypatch.setattr(manager, "_get_cursor", fail_cursor)
    with pytest.raises(OSError, match="disk unavailable"):
        manager.cleanup_task_status(30)
    with pytest.raises(OSError, match="disk unavailable"):
        manager.recover_orphaned_tasks()
