"""T5 / P0-1: terminal resolver exceptions must reach the user instead of being
swallowed into the default failure path.

We drive the real process_transcription metadata stage with a fake downloader
that raises a terminal resolver exception, and assert the message surfaces.
Console output English only.
"""

from unittest.mock import MagicMock

import pytest

import video_transcript_api.api.services.transcription as svc
from video_transcript_api.errors import (
    NonVideoContentError,
    ResolverAuthError,
    NetworkError,
)


class _RaisingDownloader:
    """Stand-in downloader whose get_metadata raises a chosen exception."""

    def __init__(self, exc):
        self._exc = exc

    def can_handle(self, url):
        return True

    def get_metadata(self, url):
        raise self._exc

    def get_download_info(self, url):
        raise self._exc

    def get_subtitle(self, url):
        return None


@pytest.fixture
def patched_env(monkeypatch):
    """Stub out singletons so the function reaches the metadata stage on cache miss."""
    fake_cache = MagicMock()
    fake_cache.get_cache.return_value = None  # force cache miss
    monkeypatch.setattr(svc, "cache_manager", fake_cache)
    monkeypatch.setattr(svc, "get_notification_router", lambda: MagicMock())
    return fake_cache


DOUYIN_URL = "https://www.douyin.com/video/7123456789"


def _run(exc):
    return svc.process_transcription(task_id="t-test", url=DOUYIN_URL)


class TestTerminalPropagation:
    def test_non_video_content_surfaces(self, patched_env, monkeypatch):
        exc = NonVideoContentError("该内容无可转录视频: image post")
        monkeypatch.setattr(svc, "create_downloader", lambda url: _RaisingDownloader(exc))
        result = _run(exc)
        assert result["status"] == "failed"
        assert "无可转录视频" in result["message"]

    def test_auth_error_surfaces(self, patched_env, monkeypatch):
        exc = ResolverAuthError("解析服务鉴权失败")
        monkeypatch.setattr(svc, "create_downloader", lambda url: _RaisingDownloader(exc))
        result = _run(exc)
        assert result["status"] == "failed"
        assert "鉴权失败" in result["message"]

    def test_transient_network_error_is_swallowed_not_surfaced(self, patched_env, monkeypatch):
        """Non-terminal (retryable) errors keep the old swallow behavior:
        metadata fetch fails softly, task continues toward download (and fails
        later with a generic download error), NOT propagated as the resolver msg."""
        exc = NetworkError("temporary blip")
        monkeypatch.setattr(svc, "create_downloader", lambda url: _RaisingDownloader(exc))
        result = _run(exc)
        # task still ends failed (no download), but not via terminal re-raise:
        # message should be the download-failure path, not "转录任务异常: temporary blip"
        assert result["status"] == "failed"
        assert "temporary blip" not in result.get("message", "")
