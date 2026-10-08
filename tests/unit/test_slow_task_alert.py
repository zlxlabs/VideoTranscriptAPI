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


def test_media_duration_recorded_in_terminal_snapshot(tmp_path, monkeypatch):
    from video_transcript_api.api.services import llm_ops, transcription
    from video_transcript_api.cache.cache_manager import CacheManager
    from video_transcript_api.downloaders.models import DownloadInfo, VideoMetadata
    from video_transcript_api.utils.task_status import TaskStatus

    cm = CacheManager(cache_dir=str(tmp_path / "cache"))
    queue = SimpleNamespace(items=[], completed=0)
    queue.put = queue.items.append
    queue.task_done = lambda: setattr(queue, "completed", queue.completed + 1)
    recording = SimpleNamespace(
        notify_task_status=lambda **_kw: {"wechat": True},
        send_rich=lambda *_a, **_k: {},
    )
    media = tmp_path / "audio.mp3"
    media.write_bytes(b"audio")

    class Downloader:
        use_api_server = False

        def get_metadata(self, _url):
            return VideoMetadata("video-1", "youtube", "Slow title", "Slow author")

        def get_subtitle_result(self, _url):
            return None

        def get_download_info(self, _url):
            return DownloadInfo("https://cdn.example.test/audio.mp3", "mp3", "audio.mp3")

        def download_file(self, _url, _name):
            return str(media)

    result_dict = {
        "校对文本": "slow-alert calibrated fixture", "内容总结": "slow-alert summary",
        "skip_summary": False,
        "stats": {
            "original_length": 24, "calibrated_length": 28,
            "summary_length": 18, "calibration_status": "full",
            "summary_status": "generated",
        },
        "models_used": {},
    }
    monkeypatch.setattr(transcription, "cache_manager", cm)
    monkeypatch.setattr(transcription, "llm_task_queue", queue)
    monkeypatch.setattr(transcription, "get_notification_router", lambda: recording)
    monkeypatch.setattr(transcription, "_ensure_audio_track", lambda *_: 3600.0)
    monkeypatch.setattr(transcription, "Transcriber", lambda: SimpleNamespace(
        transcribe=lambda *_a, **_k: {"transcript": "slow-alert transcript"},
    ))
    monkeypatch.setattr(transcription, "create_downloader", lambda _url: Downloader())
    monkeypatch.setattr(llm_ops, "cache_manager", cm)
    monkeypatch.setattr(llm_ops, "llm_task_queue", queue)
    monkeypatch.setattr(llm_ops, "get_notification_router", lambda: recording)
    monkeypatch.setattr(llm_ops, "llm_coordinator", SimpleNamespace(
        process=lambda **_kw: object(),
    ))
    monkeypatch.setattr(llm_ops, "_build_result_dict", lambda _value: result_dict)
    monkeypatch.setattr(llm_ops, "_save_llm_results", lambda **_kw: {
        "calibration_status": "full", "summary_status": "generated",
        "chapters_status": "disabled",
    })
    monkeypatch.setattr(llm_ops, "_prepare_llm_content", lambda *_a, **_k: "input")
    monkeypatch.setattr(llm_ops, "_requires_llm_title", lambda *_a, **_k: False)

    task_id = cm.create_task(url="https://www.youtube.com/watch?v=slowalert")["task_id"]
    try:
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        result = transcription.process_transcription(
            task_id=task_id, url="https://www.youtube.com/watch?v=slowalert",
            use_speaker_recognition=False, wechat_webhook=None, download_url=None,
            metadata_override=None,
            processing_options={"calibrate": True, "summarize": True, "chapters": False},
        )
        assert result["status"] == "success"
        assert len(queue.items) == 1
        llm_ops._handle_llm_task(queue.items.pop())
        snapshot = cm.get_task_by_id(task_id)["terminal_snapshot"]
        assert snapshot["observability"]["media_duration_s"] == 3600.0
    finally:
        slow_alert.cancel_all(task_id)
        cm.close()
