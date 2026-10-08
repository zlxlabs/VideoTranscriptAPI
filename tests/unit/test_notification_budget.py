"""End-to-end notification budget for transcription and terminal workers."""
import re
import threading
from datetime import datetime as RealDateTime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from video_transcript_api.api.services import llm_ops, transcription
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.downloaders.generic import GenericDownloader
from video_transcript_api.downloaders.models import DownloadInfo, VideoMetadata
from video_transcript_api.downloaders.subtitle_types import SubtitleResult
from video_transcript_api.utils.llm_status import CalibrationStatus, ChaptersStatus
from video_transcript_api.utils.notifications import slow_alert
from video_transcript_api.utils.notifications.channel import build_task_status_content
from video_transcript_api.utils.task_status import TaskStatus

SUMMARY = "persisted notification-budget summary fixture"
CALIBRATED = "calibrated notification-budget transcript fixture"


class RecordingRouter:
    def __init__(self):
        self.notifications = []

    def notify_task_status(self, **kwargs):
        self.notifications.append(kwargs)
        return {"wechat": True}


def _platform_downloader(media, *, subtitle=None, download=True):
    class Downloader:
        use_api_server = False

        def get_metadata(self, _url):
            return VideoMetadata("video-1", "youtube", "Budget title", "Budget author")

        def get_subtitle_result(self, _url):
            return subtitle

        def get_download_info(self, _url):
            return DownloadInfo("https://cdn.example.test/audio.mp3", "mp3", "audio.mp3")

        def download_file(self, _url, _name):
            return str(media) if download else None

    return Downloader()


def _youtube_api_downloader(media):
    def fetch(_self, _url, _speaker):
        return {
            "video_id": "video-1", "video_title": "Budget title",
            "author": "Budget author", "description": "", "platform": "youtube",
            "need_transcription": False, "transcript": "youtube api subtitle fixture",
        }

    return type(
        "YoutubeDownloader",
        (),
        {
            "use_api_server": True,
            "get_metadata": lambda self, _url: VideoMetadata(
                "video-1", "youtube", "Budget title", "Budget author",
            ),
            "fetch_for_transcription": fetch,
        },
    )()


@pytest.fixture
def harness(tmp_path, monkeypatch):
    cm = CacheManager(cache_dir=str(tmp_path / "cache"))
    queue = SimpleNamespace(items=[], completed=0)
    queue.put = queue.items.append
    queue.task_done = lambda: setattr(queue, "completed", queue.completed + 1)
    router = RecordingRouter()
    media = tmp_path / "audio.mp3"
    media.write_bytes(b"audio")
    monkeypatch.setattr(transcription, "cache_manager", cm)
    monkeypatch.setattr(transcription, "llm_task_queue", queue)
    monkeypatch.setattr(transcription, "get_notification_router", lambda: router)
    monkeypatch.setattr(transcription, "_ensure_audio_track", lambda *_: 12.0)
    monkeypatch.setattr(transcription, "Transcriber", lambda: SimpleNamespace(
        transcribe=lambda *_a, **_k: {"transcript": "downloaded audio transcript fixture"},
    ))
    monkeypatch.setattr(llm_ops, "cache_manager", cm)
    monkeypatch.setattr(llm_ops, "llm_task_queue", queue)
    monkeypatch.setattr(llm_ops, "get_notification_router", lambda: router)

    def handoff(task_id, payload, *, calibrating_status_kwargs, **_kwargs):
        cm.update_task_status(task_id, TaskStatus.CALIBRATING, **calibrating_status_kwargs)
        queue.put(payload)

    monkeypatch.setattr(transcription, "_handoff_to_llm_stage", handoff)
    monkeypatch.setattr(transcription, "create_downloader", lambda _url: _platform_downloader(media))
    yield cm, queue, router, media
    cm.close()


def _new_task(cm):
    task_id = cm.create_task(
        url="https://www.youtube.com/watch?v=budget",
    )["task_id"]
    cm.update_task_status(task_id, TaskStatus.PROCESSING)
    return task_id


def _seed_cache(cm, full):
    cm.save_cache(
        platform="youtube", url="https://www.youtube.com/watch?v=budget",
        media_id="budget", use_speaker_recognition=False,
        transcript_data="cached transcript fixture", transcript_type="capswriter",
        title="Budget title", author="Budget author", description="",
    )
    if full:
        for layer, text in (("calibrated", CALIBRATED), ("summary", SUMMARY)):
            cm.save_llm_result(
                platform="youtube", media_id="budget", use_speaker_recognition=False,
                llm_type=layer, content=text,
            )
        cm.save_llm_status(
            platform="youtube", media_id="budget", use_speaker_recognition=False,
            calibration_status=CalibrationStatus.FULL, summary_status="generated",
            chapters_status=ChaptersStatus.SKIPPED_SHORT,
        )


def _transcribe(harness, task_id, monkeypatch, path):
    cm, queue, _router, media = harness
    if path in ("partial-cache", "full-cache"):
        _seed_cache(cm, full=path == "full-cache")
    if path == "platform-subtitle":
        downloader = _platform_downloader(
            media, subtitle=SubtitleResult(text="platform subtitle fixture"),
        )
        monkeypatch.setattr(transcription, "create_downloader", lambda _url: downloader)
    elif path == "youtube-api-subtitle":
        downloader = _youtube_api_downloader(media)
        monkeypatch.setattr(transcription, "create_downloader", lambda _url: downloader)
    elif path == "download-failure":
        downloader = _platform_downloader(media, download=False)
        monkeypatch.setattr(transcription, "create_downloader", lambda _url: downloader)
    return transcription.process_transcription(
        task_id=task_id, url="https://www.youtube.com/watch?v=budget",
        use_speaker_recognition=False, wechat_webhook=None, download_url=None,
        metadata_override=None,
        processing_options={"calibrate": True, "summarize": True, "chapters": False},
    )


def _run_llm(harness, monkeypatch, fail=False):
    cm, queue, _router, _media = harness
    assert len(queue.items) == 1
    task = queue.items.pop()
    result = {
        "校对文本": CALIBRATED, "内容总结": SUMMARY, "skip_summary": False,
        "stats": {
            "original_length": 47, "calibrated_length": len(CALIBRATED),
            "summary_length": len(SUMMARY), "calibration_status": CalibrationStatus.FULL,
            "summary_status": "generated",
        },
        "models_used": {},
    }

    def process(**_kwargs):
        if fail:
            raise RuntimeError("summary stage fixture failure")
        return object()

    monkeypatch.setattr(llm_ops, "llm_coordinator", SimpleNamespace(process=process))
    monkeypatch.setattr(llm_ops, "_build_result_dict", lambda _value: result)
    monkeypatch.setattr(llm_ops, "_save_llm_results", lambda **_kwargs: {
        "calibration_status": CalibrationStatus.FULL,
        "summary_status": "generated", "chapters_status": "disabled",
    })
    monkeypatch.setattr(llm_ops, "_prepare_llm_content", lambda *_a, **_k: "input")
    monkeypatch.setattr(llm_ops, "_requires_llm_title", lambda *_a, **_k: False)
    monkeypatch.setattr(llm_ops, "_generate_title_if_needed", lambda _t, title, _s: title)
    failures = []

    def run():
        try:
            llm_ops._handle_llm_task(task)
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert failures == []
    assert queue.completed == 1
    return cm


def _assert_notice(harness, icon, *, summary=None, error=None):
    cm, _queue, router, _media = harness
    assert len(router.notifications) == 1
    kwargs = router.notifications[0]
    content = build_task_status_content(
        url=kwargs.get("url", ""), status=kwargs["status"], error=kwargs.get("error"),
        title=kwargs.get("title"), author=kwargs.get("author"),
        view_url=kwargs.get("view_url"), task_id=kwargs.get("task_id"),
        completion_body=kwargs.get("completion_body"),
    )
    heading = content.splitlines()[0]
    assert re.fullmatch(r"(✅|❌) \[#[0-9a-f]{6}\] .+", heading)
    assert heading.startswith(icon)
    if summary is not None:
        assert summary in content
    if error is not None:
        assert error in content


@pytest.mark.parametrize(
    "path", ["download", "platform-subtitle", "youtube-api-subtitle", "partial-cache", "full-cache"],
)
def test_success_paths_send_one_completion_with_summary(harness, monkeypatch, path):
    cm, queue, _router, _media = harness
    task_id = _new_task(cm)
    result = _transcribe(harness, task_id, monkeypatch, path)
    assert result["status"] == "success"
    if path == "full-cache":
        assert queue.items == []
    else:
        _run_llm(harness, monkeypatch)
    _assert_notice(harness, "✅", summary=SUMMARY)
    row = cm.get_task_by_id(task_id)
    assert row["status"] == TaskStatus.SUCCESS
    assert row["terminal_snapshot"]["result"]["内容总结"] == SUMMARY


@pytest.mark.parametrize(
    ("path", "expected"),
    [("download-failure", "下载文件失败"), ("llm-failure", "summary stage fixture failure")],
)
def test_failure_paths_send_one_failure_notice(harness, monkeypatch, path, expected):
    cm, _queue, _router, _media = harness
    task_id = _new_task(cm)
    if path == "download-failure":
        assert _transcribe(harness, task_id, monkeypatch, path)["status"] == "failed"
    else:
        assert _transcribe(harness, task_id, monkeypatch, "download")["status"] == "success"
        _run_llm(harness, monkeypatch, fail=True)
    _assert_notice(harness, "❌", error=expected)
    assert cm.get_task_by_id(task_id)["status"] == TaskStatus.FAILED


def test_large_generic_download_sends_no_progress_notification(tmp_path, monkeypatch):
    import video_transcript_api.downloaders.generic as generic_module
    import video_transcript_api.utils.notifications as notifications

    sizes = [6 * 1024 * 1024] * 4 + [1024 * 1024]
    total = sum(sizes)

    class Response:
        headers = {"content-length": str(total)}
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size):
            assert chunk_size == 1024 * 1024
            for size in sizes:
                yield b"x" * size

    class Clock:
        ticks = 0
        @classmethod
        def now(cls):
            cls.ticks += 11
            return RealDateTime(2026, 1, 1) + timedelta(seconds=cls.ticks)

    downloader = GenericDownloader.__new__(GenericDownloader)
    downloader.temp_manager = SimpleNamespace(get_current_task_dir=lambda: tmp_path)
    downloader.max_download_bytes = 0
    downloader._validate_or_raise = lambda _url: None
    downloader._safe_request = lambda *_a, **_k: Response()
    monkeypatch.setattr(generic_module, "datetime", SimpleNamespace(datetime=Clock))
    notifier = MagicMock()
    monkeypatch.setattr(notifications, "WechatNotifier", notifier)
    output = downloader.download_file("https://files.example.test/large.mp4", "large.mp4")
    assert output == str(tmp_path / "large.mp4")
    assert (tmp_path / "large.mp4").stat().st_size == total > 20 * 1024 * 1024
    assert (96 % 30) < 10
    notifier.assert_not_called()


def test_normal_speed_task_sends_no_slow_alert(harness, monkeypatch):
    """正常速度的全流程任务(转录+LLM 成功终态)零条 ⏳。

    偏慢提醒(261007-notify-slim 卡 2)的预算约束:接线后的流水线会注册
    计时器,但正常速度的任务在门槛内到达终态,计时被终态取消,不得产生
    任何 ⏳ 富文本消息。
    """
    cm, _queue, router, _media = harness
    slow_alert_sends = []

    def record_send_rich(content, **_kwargs):
        slow_alert_sends.append(content)
        return {}

    monkeypatch.setattr(slow_alert, "get_notification_router", lambda: SimpleNamespace(
        send_rich=record_send_rich,
    ))
    task_id = _new_task(cm)
    assert _transcribe(harness, task_id, monkeypatch, "download")["status"] == "success"
    _run_llm(harness, monkeypatch)
    _assert_notice(harness, "✅", summary=SUMMARY)
    assert cm.get_task_by_id(task_id)["status"] == TaskStatus.SUCCESS
    assert slow_alert_sends == []
    slow_alert.cancel_all(task_id)
