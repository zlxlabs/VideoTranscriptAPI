import threading
from types import SimpleNamespace

import pytest

from video_transcript_api.api.services import transcription as transcription_module


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
                "speaker": use_speaker_recognition,
            }
        )

    def get_task_by_id(self, task_id):
        return {"view_token": f"token-{task_id}"}


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
    dummy_queue = _DummyQueue()
    dummy_cache = _DummyCacheManager()
    sent_long_text = []
    completion_messages = []

    def fake_send_long_text(**kwargs):
        sent_long_text.append(kwargs)

    def fake_wechat_notifier(webhook=None):
        notifier = _DummyNotifier(webhook)
        completion_messages.append(notifier)
        return notifier

    monkeypatch.setattr(transcription_module, "llm_task_queue", dummy_queue)
    monkeypatch.setattr(transcription_module, "cache_manager", dummy_cache)
    monkeypatch.setattr(transcription_module, "send_long_text_wechat", fake_send_long_text)
    monkeypatch.setattr(transcription_module, "WechatNotifier", fake_wechat_notifier)
    monkeypatch.setattr(transcription_module, "get_base_url", lambda: "https://fake-base")
    monkeypatch.setattr(transcription_module, "time", SimpleNamespace(sleep=lambda *_: None))

    return {
        "queue": dummy_queue,
        "cache": dummy_cache,
        "sent_long_text": sent_long_text,
        "completion_messages": completion_messages,
    }


def test_llm_tasks_run_concurrently(monkeypatch, patched_llm_environment):
    barrier = threading.Barrier(2, timeout=2)
    event_log = []

    class _DummyProcessor:
        def process_llm_task(self, llm_task):
            task_id = llm_task["task_id"]
            event_log.append(("start", task_id))
            barrier.wait()
            event_log.append(("after_barrier", task_id))
            return {
                "校对文本": f"校对-{task_id}",
                "内容总结": f"总结-{task_id}",
                "skip_summary": False,
                "stats": {"original_length": 10, "calibrated_length": 8, "summary_length": 5},
            }

    monkeypatch.setattr(transcription_module, "enhanced_llm_processor", _DummyProcessor())

    tasks = []
    for idx in range(2):
        task_id = f"task-{idx}"
        tasks.append(
            {
                "task_id": task_id,
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
            }
        )

    threads = [
        threading.Thread(target=transcription_module._handle_llm_task, args=(task,))
        for task in tasks
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive(), "LLM worker thread did not finish"

    # First two entries should both be start events, proving tasks progressed together.
    assert [evt for evt, _ in event_log[:2]] == ["start", "start"]
    assert patched_llm_environment["queue"].completed == len(tasks)
    assert len(patched_llm_environment["sent_long_text"]) == len(tasks)
    # Each task saves calibrated and summary results.
    assert len(patched_llm_environment["cache"].saved) == len(tasks) * 2
