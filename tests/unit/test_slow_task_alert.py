"""偏慢提醒(⏳)单元测试:门槛公式、每任务一条、终态取消、到点/终态竞态。

计时器门槛通过 monkeypatch 模块常量调小,不真实等待长门槛;发送侧用真实
NotificationRouter(仅把渠道替身掉)断言双渠道收到内容。
"""
import re
import threading
import time
from types import SimpleNamespace

import pytest

from video_transcript_api.utils.notifications import slow_alert
from video_transcript_api.utils.notifications.router import NotificationRouter


class FakeChannel:
    def __init__(self, name):
        self.name = name
        self.sent = []

    def send_rich(self, content, webhook=None, **kwargs):
        self.sent.append(content)
        return True


@pytest.fixture
def channels(monkeypatch):
    wecom = FakeChannel("wechat")
    feishu = FakeChannel("feishu")
    router = NotificationRouter.__new__(NotificationRouter)
    router.channels = [wecom, feishu]
    monkeypatch.setattr(slow_alert, "get_notification_router", lambda: router)
    yield wecom, feishu
    for task_id in list(slow_alert._TASKS):
        slow_alert.cancel_all(task_id)


def _wait_for(channel, count, timeout=5.0):
    deadline = time.monotonic() + timeout
    while len(channel.sent) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    return len(channel.sent)


def test_transcription_threshold_with_known_duration():
    assert slow_alert.transcription_threshold(3600.0) == pytest.approx(1140.0)


@pytest.mark.parametrize("raw", [None, 0, 0.0, -3, float("inf"), float("-inf"), float("nan")])
def test_transcription_threshold_unknown_duration_falls_back_to_constant(raw):
    assert slow_alert.transcription_threshold(raw) == (
        slow_alert.SLOW_TRANSCRIPTION_UNKNOWN_DURATION_SECONDS
    )


def test_slow_transcription_alerts_exactly_once_on_both_channels(channels, monkeypatch):
    wecom, feishu = channels
    monkeypatch.setattr(
        slow_alert, "SLOW_TRANSCRIPTION_UNKNOWN_DURATION_SECONDS", 0.05
    )
    slow_alert.track_transcription(
        "task_aabbcc112233", url="https://www.youtube.com/watch?v=x",
    )
    assert _wait_for(wecom, 1) == 1
    assert len(feishu.sent) == 1
    time.sleep(0.15)
    assert len(wecom.sent) == 1
    content = wecom.sent[0]
    assert re.match(r"^⏳ \[#[0-9a-f]{6}\] .+", content.splitlines()[0])
    assert "转录" in content
    assert "分钟" in content


def test_already_alerted_task_never_alerts_again_in_llm_stage(channels, monkeypatch):
    wecom, _feishu = channels
    monkeypatch.setattr(
        slow_alert, "SLOW_TRANSCRIPTION_UNKNOWN_DURATION_SECONDS", 0.05
    )
    monkeypatch.setattr(slow_alert, "SLOW_LLM_SECONDS", 0.05)
    slow_alert.track_transcription(
        "task_aabbcc112233", url="https://www.youtube.com/watch?v=x",
    )
    assert _wait_for(wecom, 1) == 1
    slow_alert.finish_transcription("task_aabbcc112233")
    slow_alert.track_llm("task_aabbcc112233")
    time.sleep(0.3)
    assert len(wecom.sent) == 1


def test_llm_queue_wait_is_not_counted(channels, monkeypatch):
    wecom, _feishu = channels
    monkeypatch.setattr(slow_alert, "SLOW_LLM_SECONDS", 0.2)
    slow_alert.track_transcription(
        "task_aabbcc112233", url="https://www.youtube.com/watch?v=x",
    )
    slow_alert.finish_transcription("task_aabbcc112233")
    time.sleep(0.4)
    assert len(wecom.sent) == 0
    slow_alert.track_llm("task_aabbcc112233")
    time.sleep(0.05)
    slow_alert.cancel_all("task_aabbcc112233")
    time.sleep(0.3)
    assert len(wecom.sent) == 0


def test_terminal_before_deadline_no_alert_and_timing_released(channels, monkeypatch):
    wecom, _feishu = channels
    monkeypatch.setattr(
        slow_alert, "SLOW_TRANSCRIPTION_UNKNOWN_DURATION_SECONDS", 0.1
    )
    slow_alert.track_transcription(
        "task_aabbcc112233", url="https://www.youtube.com/watch?v=x",
    )
    slow_alert.cancel_all("task_aabbcc112233")
    assert "task_aabbcc112233" not in slow_alert._TASKS
    time.sleep(0.3)
    assert len(wecom.sent) == 0


def test_deadline_and_terminal_race_never_alerts_after_cancel(channels, monkeypatch):
    wecom, feishu = channels
    monkeypatch.setattr(
        slow_alert, "SLOW_TRANSCRIPTION_UNKNOWN_DURATION_SECONDS", 0.05
    )
    for round_index in range(5):
        task_id = f"task_race{round_index:02d}abcdef"
        slow_alert.track_transcription(
            task_id, url="https://www.youtube.com/watch?v=x",
        )
        cancel_done = threading.Event()

        def terminal(tid=task_id, done=cancel_done):
            slow_alert.cancel_all(tid)
            done.set()

        threading.Thread(target=terminal).start()
        time.sleep(0.2)
        assert cancel_done.wait(timeout=1.0)
        count_at_cancel = len(wecom.sent) + len(feishu.sent)
        time.sleep(0.15)
        assert len(wecom.sent) + len(feishu.sent) == count_at_cancel
        assert count_at_cancel <= 2
        assert task_id not in slow_alert._TASKS
