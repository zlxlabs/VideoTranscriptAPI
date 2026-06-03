"""Unit tests for CacheManager task-status guards and crash recovery.

Covers:
- update_task_status terminal-state stickiness (success/failed not clobbered)
- force=True explicit reset (recalibrate path)
- recover_orphaned_tasks() sweep on startup
- calibrating status round-trips

All console output must be in English only (no emoji, no Chinese).
"""

import pytest

from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.task_status import TaskStatus


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _new_task(cm, url="https://example.com/v1"):
    return cm.create_task(url=url)["task_id"]


class TestCalibratingStatus:
    def test_calibrating_round_trips(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)
        assert cm.get_task_by_id(task_id)["status"] == "calibrating"


class TestTerminalStickiness:
    """success / failed are terminal and must not be overwritten by late writes."""

    def test_success_not_overwritten_by_processing(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.SUCCESS)
        # A slow/stale worker tries to regress the state.
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        assert cm.get_task_by_id(task_id)["status"] == "success"

    def test_success_not_overwritten_by_failed(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.SUCCESS)
        cm.update_task_status(task_id, TaskStatus.FAILED)
        assert cm.get_task_by_id(task_id)["status"] == "success"

    def test_failed_not_overwritten_by_success(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.FAILED)
        cm.update_task_status(task_id, TaskStatus.SUCCESS)
        assert cm.get_task_by_id(task_id)["status"] == "failed"

    def test_non_terminal_transitions_allowed(self, cm):
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        cm.update_task_status(task_id, TaskStatus.CALIBRATING)
        cm.update_task_status(task_id, TaskStatus.SUCCESS)
        assert cm.get_task_by_id(task_id)["status"] == "success"

    def test_force_overwrites_terminal(self, cm):
        """recalibrate explicitly resets a finished task back to processing."""
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.SUCCESS)
        cm.update_task_status(task_id, TaskStatus.PROCESSING, force=True)
        assert cm.get_task_by_id(task_id)["status"] == "processing"


class TestRecoverOrphanedTasks:
    """On boot, in-flight tasks (lost with the in-memory queues) are failed."""

    def test_sweeps_non_terminal_to_failed(self, cm):
        queued = _new_task(cm, "https://example.com/q")
        processing = _new_task(cm, "https://example.com/p")
        calibrating = _new_task(cm, "https://example.com/c")
        cm.update_task_status(processing, TaskStatus.PROCESSING)
        cm.update_task_status(calibrating, TaskStatus.CALIBRATING)

        recovered = cm.recover_orphaned_tasks()

        assert recovered == 3
        assert cm.get_task_by_id(queued)["status"] == "failed"
        assert cm.get_task_by_id(processing)["status"] == "failed"
        assert cm.get_task_by_id(calibrating)["status"] == "failed"

    def test_terminal_tasks_untouched(self, cm):
        done = _new_task(cm, "https://example.com/done")
        failed = _new_task(cm, "https://example.com/failed")
        cm.update_task_status(done, TaskStatus.SUCCESS)
        cm.update_task_status(failed, TaskStatus.FAILED)

        recovered = cm.recover_orphaned_tasks()

        assert recovered == 0
        assert cm.get_task_by_id(done)["status"] == "success"
        assert cm.get_task_by_id(failed)["status"] == "failed"

    def test_sets_completed_at_on_recovered(self, cm):
        processing = _new_task(cm, "https://example.com/p2")
        cm.update_task_status(processing, TaskStatus.PROCESSING)
        cm.recover_orphaned_tasks()
        assert cm.get_task_by_id(processing)["completed_at"] is not None
