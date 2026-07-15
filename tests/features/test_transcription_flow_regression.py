import types
from unittest.mock import MagicMock

import pytest

import video_transcript_api.api.services.transcription as transcription
from video_transcript_api.downloaders.models import VideoMetadata, DownloadInfo
from video_transcript_api.utils.llm_status import CalibrationStatus


class DummyQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class DummyNotifier:
    def __init__(self, webhook=None):
        self.webhook = webhook
        self.messages = []

    def notify_task_status(self, *args, **kwargs):
        self.messages.append(("notify", args, kwargs))

    def send_text(self, text, **kwargs):
        self.messages.append(("send_text", text, kwargs))

    def _clean_url(self, url):
        return url


class DummyCacheManager:
    def __init__(self, cache_data=None):
        self.cache_data = cache_data
        self.saved = []
        self.status_updates = []
        self.tasks = {}

    def get_cache(self, platform, media_id, use_speaker_recognition):
        return self.cache_data

    def save_cache(self, **kwargs):
        self.saved.append(kwargs)
        return True

    def update_task_status(self, task_id, status, **kwargs):
        self.status_updates.append((task_id, status, kwargs))

    def get_task_by_id(self, task_id):
        return self.tasks.get(task_id)


class DummyTranscriber:
    def __init__(self, *args, **kwargs):
        pass

    def transcribe(self, local_file, output_base):
        return {"transcript": "transcribed text"}


class DummyFunASR:
    def __init__(self, *args, **kwargs):
        pass

    def transcribe_sync(self, local_file):
        return {
            "formatted_text": "funasr text",
            "transcription_result": [{"speaker": "spk_0", "text": "hello"}],
        }

    def format_transcript_with_speakers(self, data):
        return "funasr formatted"


class YoutubeDownloader:
    def __init__(self, subtitle=None, download_url="http://example.com/audio.mp3", filename="test.mp3"):
        self._subtitle = subtitle
        self._download_url = download_url
        self._filename = filename
        self.use_api_server = False

    def get_metadata(self, url):
        return VideoMetadata(
            video_id="abc123",
            platform="youtube",
            title="test title",
            author="test author",
            description="test desc",
        )

    def get_download_info(self, url):
        return DownloadInfo(
            download_url=self._download_url,
            file_ext="mp3",
            filename=self._filename,
        )

    def get_subtitle(self, url):
        return self._subtitle

    def download_file(self, url, filename):
        return "C:/tmp/test.mp3"

    def fetch_for_transcription(self, *args, **kwargs):
        raise AssertionError("fetch_for_transcription should not be called in this test")


class GenericDownloader:
    def __init__(self):
        self.calls = []

    def download_file(self, url, filename):
        self.calls.append((url, filename))
        return "C:/tmp/direct.mp3"


@pytest.fixture
def patch_runtime(monkeypatch):
    queue = DummyQueue()
    monkeypatch.setattr(transcription, "llm_task_queue", queue)
    monkeypatch.setattr(transcription, "WechatNotifier", DummyNotifier)
    monkeypatch.setattr(transcription, "send_long_text_wechat", lambda *args, **kwargs: None)
    monkeypatch.setattr(transcription, "Transcriber", DummyTranscriber)
    monkeypatch.setattr(transcription, "FunASRSpeakerClient", DummyFunASR)
    monkeypatch.setattr(transcription, "get_base_url", lambda: "http://test")
    return queue


def test_flow_cache_hit(monkeypatch, patch_runtime):
    cache_data = {
        "platform": "youtube",
        "media_id": "abc123",
        "title": "cached title",
        "author": "cached author",
        "description": "cached desc",
        "transcript_type": "capswriter",
        "transcript_data": "cached transcript",
        "use_speaker_recognition": False,
        "llm_calibrated": "calibrated",
        "llm_summary": "summary",
    }
    cache_manager = DummyCacheManager(cache_data=cache_data)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    def fail_create_downloader(url):
        raise AssertionError("create_downloader should not be called on cache hit")

    monkeypatch.setattr(transcription, "create_downloader", fail_create_downloader)

    result = transcription.process_transcription(
        task_id="task_cache_hit",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
    )

    assert result["status"] == "success"
    assert result["data"]["cached"] is True
    assert len(patch_runtime.items) == 0


def test_flow_cache_hit_with_disabled_calibration_notifies_disclaimer(monkeypatch, patch_runtime):
    """ci-gate review: a full cache hit whose calibration layer was produced
    by a prior calibrate=False run (locally-formatted placeholder text, not
    real LLM calibration) must disclose this in the notification -- otherwise
    a user who requested calibrate=False & summarize=True the first time,
    then triggers this exact "everything's cached" branch on a later
    request, silently receives a summary/"calibrated" text built from
    unedited ASR output with zero indication of that fact."""
    cache_data = {
        "platform": "youtube",
        "media_id": "abc123",
        "title": "cached title",
        "author": "cached author",
        "description": "cached desc",
        "transcript_type": "capswriter",
        "transcript_data": "cached transcript",
        "use_speaker_recognition": False,
        "llm_calibrated": "locally formatted placeholder (never LLM-calibrated)",
        "llm_summary": "summary built from that placeholder",
        "llm_status": {
            "calibration_status": CalibrationStatus.DISABLED,
            "summary_status": "generated",
        },
    }
    cache_manager = DummyCacheManager(cache_data=cache_data)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    def fail_create_downloader(url):
        raise AssertionError("create_downloader should not be called on cache hit")

    monkeypatch.setattr(transcription, "create_downloader", fail_create_downloader)

    notification_router = MagicMock()
    notification_router.send_long_text = MagicMock()
    notification_router.send_text = MagicMock()
    monkeypatch.setattr(transcription, "get_notification_router", lambda: notification_router)

    result = transcription.process_transcription(
        task_id="task_cache_hit_disabled_calibration",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
        # 必须显式传 calibrate=False，与本次请求一致，才能让分层缓存命中判定
        # 走到"全命中"分支——calibrate_requested 默认(None)按 True 兜底，
        # 会误判成"要求真校对但缓存里没有"，转而重新排队补校对，根本不会
        # 触碰到这里要验证的通知拼接代码。
        processing_options={"calibrate": False, "summarize": True},
    )

    assert result["status"] == "success"
    assert result["data"]["cached"] is True

    assert notification_router.send_long_text.called
    sent_text = notification_router.send_long_text.call_args.kwargs["text"]
    assert "未启用" in sent_text


def test_flow_cache_hit_with_full_calibration_has_no_disclaimer(monkeypatch, patch_runtime):
    """Sanity check: a normal, real calibration cache hit must NOT show the
    'calibration disabled' disclaimer."""
    cache_data = {
        "platform": "youtube",
        "media_id": "abc123",
        "title": "cached title",
        "author": "cached author",
        "description": "cached desc",
        "transcript_type": "capswriter",
        "transcript_data": "cached transcript",
        "use_speaker_recognition": False,
        "llm_calibrated": "calibrated",
        "llm_summary": "summary",
        "llm_status": {
            "calibration_status": CalibrationStatus.FULL,
            "summary_status": "generated",
        },
    }
    cache_manager = DummyCacheManager(cache_data=cache_data)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)
    monkeypatch.setattr(
        transcription, "create_downloader",
        lambda url: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    notification_router = MagicMock()
    notification_router.send_long_text = MagicMock()
    notification_router.send_text = MagicMock()
    monkeypatch.setattr(transcription, "get_notification_router", lambda: notification_router)

    result = transcription.process_transcription(
        task_id="task_cache_hit_full_calibration",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
        processing_options={"calibrate": True, "summarize": True},
    )

    assert result["status"] == "success"
    assert notification_router.send_long_text.called
    sent_text = notification_router.send_long_text.call_args.kwargs["text"]
    assert "未启用" not in sent_text


def test_flow_subtitle_preferred(monkeypatch, patch_runtime):
    cache_manager = DummyCacheManager(cache_data=None)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    downloader = YoutubeDownloader(subtitle="subtitle text")
    monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

    result = transcription.process_transcription(
        task_id="task_subtitle",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
    )

    assert result["status"] == "success"
    assert result["data"]["transcript"] == "subtitle text"
    assert cache_manager.saved
    saved = cache_manager.saved[0]
    assert saved["transcript_type"] == "capswriter"
    assert saved["transcript_data"] == "subtitle text"


def test_flow_download_capswriter(monkeypatch, patch_runtime):
    cache_manager = DummyCacheManager(cache_data=None)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    downloader = YoutubeDownloader(subtitle=None, download_url="http://example.com/audio.mp3", filename="audio.mp3")
    monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

    result = transcription.process_transcription(
        task_id="task_download_caps",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
    )

    assert result["status"] == "success"
    assert result["data"]["transcript"] == "transcribed text"
    assert cache_manager.saved
    saved = cache_manager.saved[0]
    assert saved["transcript_type"] == "capswriter"
    assert saved["use_speaker_recognition"] is False


def test_flow_download_funasr(monkeypatch, patch_runtime):
    cache_manager = DummyCacheManager(cache_data=None)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    downloader = YoutubeDownloader(subtitle=None, download_url="http://example.com/audio.mp3", filename="audio.mp3")
    monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

    result = transcription.process_transcription(
        task_id="task_download_funasr",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=True,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
    )

    assert result["status"] == "success"
    assert result["data"]["speaker_recognition"] is True
    assert cache_manager.saved
    saved = cache_manager.saved[0]
    assert saved["transcript_type"] == "funasr"
    assert saved["use_speaker_recognition"] is True


def test_flow_separate_download_url(monkeypatch, patch_runtime):
    cache_manager = DummyCacheManager(cache_data=None)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    metadata_downloader = YoutubeDownloader(subtitle=None)
    monkeypatch.setattr(transcription, "create_downloader", lambda url: metadata_downloader)

    generic_downloader = GenericDownloader()
    import video_transcript_api.downloaders.generic as generic_module
    monkeypatch.setattr(generic_module, "GenericDownloader", lambda: generic_downloader)

    result = transcription.process_transcription(
        task_id="task_separate_url",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url="http://example.com/file.mp3",
        metadata_override=None,
    )

    assert result["status"] == "success"
    assert generic_downloader.calls
    assert generic_downloader.calls[0][0] == "http://example.com/file.mp3"
    assert generic_downloader.calls[0][1] == "file.mp3"


def test_flow_download_url_skips_youtube_api(monkeypatch, patch_runtime):
    cache_manager = DummyCacheManager(cache_data=None)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    downloader = YoutubeDownloader(subtitle=None)
    downloader.use_api_server = True
    monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

    generic_downloader = GenericDownloader()
    import video_transcript_api.downloaders.generic as generic_module
    monkeypatch.setattr(generic_module, "GenericDownloader", lambda: generic_downloader)

    result = transcription.process_transcription(
        task_id="task_download_url_skip_api",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url="http://example.com/file.mp3",
        metadata_override=None,
    )

    assert result["status"] == "success"
    assert generic_downloader.calls
