"""
LLM task concurrency test.

Verifies that multiple LLM tasks can run concurrently via the thread pool,
not serialized by the queue processor.

All console output must be in English only (no emoji, no Chinese).
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from video_transcript_api.api.services import llm_ops as llm_ops_module


class _DummyQueue:
    def __init__(self):
        self.completed = 0

    def task_done(self):
        self.completed += 1


class _DummyCacheManager:
    def __init__(self):
        self.saved = []

    def save_llm_result(self, *, platform, media_id, use_speaker_recognition, llm_type, content):
        self.saved.append(
            {
                "platform": platform,
                "media_id": media_id,
                "llm_type": llm_type,
                "content": content,
            }
        )

    def get_task_by_id(self, task_id):
        return {"view_token": f"token-{task_id}"}

    def update_task_llm_config(self, task_id, models_used):
        pass

    def save_llm_status(self, *, platform, media_id, use_speaker_recognition,
                         calibration_status=None, calibration_stats=None,
                         summary_status=None):
        """No-op stand-in for the honest-status-model llm_status.json writer.

        Intentionally does NOT append to self.saved (that list is asserted
        against as "2 tasks x 2 results each" -- calibrated + summary text).
        """
        return True

    def update_task_status(self, task_id, status, **kwargs):
        pass


class _DummyNotifier:
    def __init__(self, webhook=None):
        self.webhook = webhook
        self.sent = []

    def send_text(self, message, skip_risk_control=False):
        self.sent.append((self.webhook, message))

    def _clean_url(self, url):
        return url


@pytest.fixture()
def patched_llm_environment(monkeypatch):
    """Patch llm_ops module dependencies for isolated testing."""
    dummy_queue = _DummyQueue()
    dummy_cache = _DummyCacheManager()
    sent_long_text = []

    def fake_send_long_text(**kwargs):
        sent_long_text.append(kwargs)

    def fake_wechat_notifier(webhook=None):
        return _DummyNotifier(webhook)

    class _FakeRouter:
        def send_long_text(self, **kwargs):
            sent_long_text.append(kwargs)
            return {"fake": True}
        def send_text(self, content, **kwargs):
            return {"fake": True}
        def notify_task_status(self, **kwargs):
            return {"fake": True}

    # Patch llm_ops module-level variables
    monkeypatch.setattr(llm_ops_module, "llm_task_queue", dummy_queue)
    monkeypatch.setattr(llm_ops_module, "cache_manager", dummy_cache)
    monkeypatch.setattr(llm_ops_module, "send_long_text_wechat", fake_send_long_text)
    monkeypatch.setattr(llm_ops_module, "WechatNotifier", fake_wechat_notifier)
    monkeypatch.setattr(llm_ops_module, "get_notification_router", lambda: _FakeRouter())
    monkeypatch.setattr(llm_ops_module, "get_base_url", lambda: "https://fake-base")
    monkeypatch.setattr(llm_ops_module, "time", SimpleNamespace(sleep=lambda *_: None))

    return {
        "queue": dummy_queue,
        "cache": dummy_cache,
        "sent_long_text": sent_long_text,
    }


def test_llm_tasks_run_concurrently(monkeypatch, patched_llm_environment):
    """Two LLM tasks should run concurrently (not serialized)."""
    barrier = threading.Barrier(2, timeout=2)
    event_log = []

    # Mock llm_coordinator.process to use barrier for synchronization
    def mock_coordinator_process(**kwargs):
        task_title = kwargs.get("title", "unknown")
        event_log.append(("start", task_title))
        barrier.wait()  # Both tasks must reach here before either proceeds
        event_log.append(("after_barrier", task_title))
        return {
            "calibrated_text": f"calibrated-{task_title}",
            "summary_text": f"summary-{task_title}",
            # summary_status must mirror the fact that a real summary_text was
            # produced -- this mock stands in for LLMCoordinator.process(),
            # which always sets stats.summary_status alongside summary_text.
            "stats": {
                "original_length": 10, "calibrated_length": 8, "summary_length": 5,
                "calibration_status": "full", "summary_status": "generated",
            },
            "models_used": {},
        }

    mock_coordinator = MagicMock()
    mock_coordinator.process = mock_coordinator_process
    monkeypatch.setattr(llm_ops_module, "llm_coordinator", mock_coordinator)

    # Disable task_lock to allow true concurrency
    from contextlib import contextmanager

    @contextmanager
    def noop_lock(task_id):
        yield

    monkeypatch.setattr(llm_ops_module, "task_lock", noop_lock)

    tasks = []
    for idx in range(2):
        tasks.append({
            "task_id": f"task-{idx}",
            "url": f"https://example.com/video/{idx}",
            "platform": "youtube",
            "media_id": f"vid-{idx}",
            "video_title": f"Video {idx}",
            "author": f"Author {idx}",
            "description": "desc",
            "transcript": f"Transcript {idx}",
            "use_speaker_recognition": False,
            "transcription_data": None,
            "wechat_webhook": None,
            "is_generic": False,
        })

    threads = [
        threading.Thread(target=llm_ops_module._handle_llm_task, args=(task,))
        for task in tasks
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive(), "LLM worker thread did not finish"

    # Both start events should appear before any after_barrier event
    start_events = [evt for evt, _ in event_log if evt == "start"]
    assert len(start_events) == 2, f"Expected 2 start events, got {len(start_events)}"

    # Queue should have completed both tasks
    assert patched_llm_environment["queue"].completed == 2

    # Both tasks should have sent long text notifications
    assert len(patched_llm_environment["sent_long_text"]) == 2

    # Each task saves calibrated and summary results
    assert len(patched_llm_environment["cache"].saved) == 4  # 2 tasks x 2 results each
