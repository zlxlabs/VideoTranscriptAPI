"""Ensure process_transcription reuses the URLParser-normalized URL downstream."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import video_transcript_api.api.services.transcription as svc
from video_transcript_api.utils.url_parser import ParsedURL


SHORT_URL = "https://b23.tv/short-code"
NORMALIZED_URL = "https://www.bilibili.com/video/BV1AoEg6SEW4?p=2"


class _RecordingDownloader:
    """Minimal downloader that records URLs passed through the cache-miss flow."""

    def __init__(self):
        self.metadata_urls = []
        self.download_info_urls = []

    def get_metadata(self, url):
        self.metadata_urls.append(url)
        return SimpleNamespace(
            video_id="BV1AoEg6SEW4",
            title="Test video",
            author="Test author",
            description="Test description",
            platform="bilibili",
        )

    def get_download_info(self, url):
        self.download_info_urls.append(url)
        return None


def test_bilibili_normalized_url_is_reused_for_metadata_and_download_info(monkeypatch):
    """A b23 URL resolved once by URLParser must not reach downstream downloaders."""
    fake_cache = MagicMock()
    fake_cache.get_cache.return_value = None
    monkeypatch.setattr(svc, "cache_manager", fake_cache)
    monkeypatch.setattr(svc, "get_notification_router", lambda: MagicMock())

    parsed_url = ParsedURL(
        platform="bilibili",
        video_id="BV1AoEg6SEW4",
        normalized_url=NORMALIZED_URL,
        is_short_url=True,
        original_url=SHORT_URL,
    )
    monkeypatch.setattr(
        "video_transcript_api.utils.url_parser.URLParser.parse",
        lambda _self, url: parsed_url,
    )

    downloader = _RecordingDownloader()
    factory_urls = []

    def create_recording_downloader(url):
        factory_urls.append(url)
        return downloader

    monkeypatch.setattr(svc, "create_downloader", create_recording_downloader)

    result = svc.process_transcription(task_id="normalized-url", url=SHORT_URL)

    assert result["status"] == "failed"
    assert factory_urls == [NORMALIZED_URL]
    assert downloader.metadata_urls == [NORMALIZED_URL]
    assert downloader.download_info_urls == [NORMALIZED_URL]
