"""Failure early-return paths in process_transcription must persist
TaskStatus.FAILED to the task DB (single source of truth), so callers polling
GET /api/task see the terminal state instead of a forever-'processing' task.

Regression for prod incident 2026-07-09: a connection-refused download left
the task stuck in 'processing'; the live-recorder confirm loop polled it
uselessly until its 6h confirm_timeout fallback.

Console output English only.
"""

from unittest.mock import MagicMock

import pytest

import video_transcript_api.api.services.transcription as svc
from video_transcript_api.cache.cache_manager import CacheManager

RECORDER_URL = "recorder://test-source/live_123/abc456"
DOWNLOAD_URL = "http://127.0.0.1:9/files/recording.mp4"


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


@pytest.fixture
def env(cm, monkeypatch):
    """Real (tmp) CacheManager as the status source of truth; mute notifications.

    The SSRF validator does real DNS resolution and blocks non-allowlisted
    private IPs — orthogonal to the behavior under test, so stub it out.
    Both entry points need stubbing: validate_url_safe (used for simple
    pass/fail gates) and validate_url_safe_with_ip (used by
    GenericDownloader's IP-pinned request path, see downloaders/generic.py
    and utils/pinned_ip_adapter.py — codex-review R5 #1) — otherwise
    GenericDownloader.get_metadata()/get_video_info() still hit the real,
    unstubbed DNS-rebinding-safe validator for RECORDER_URL's non-http
    scheme and raise for real.
    """
    monkeypatch.setattr(svc, "cache_manager", cm)
    monkeypatch.setattr(svc, "get_notification_router", lambda: MagicMock())
    monkeypatch.setattr(
        "video_transcript_api.utils.url_validator.validate_url_safe", lambda url: url
    )
    monkeypatch.setattr(
        "video_transcript_api.utils.url_validator.validate_url_safe_with_ip",
        lambda url: (url, None),
    )
    return cm


class TestDownloadFailurePersistsFailed:
    def test_download_file_none_persists_failed(self, env, cm, monkeypatch):
        """download_file returning None (e.g. connection refused after retries)
        must leave the task 'failed' in the DB, with the error recorded."""
        task_id = cm.create_task(url=RECORDER_URL)["task_id"]
        monkeypatch.setattr(
            "video_transcript_api.downloaders.generic.GenericDownloader.download_file",
            lambda self, url, filename=None: None,
        )

        result = svc.process_transcription(
            task_id=task_id, url=RECORDER_URL, download_url=DOWNLOAD_URL
        )

        assert result["status"] == "failed"
        task = cm.get_task_by_id(task_id)
        assert task["status"] == "failed"
        assert "下载文件失败" in (task.get("error_message") or "")


class _NoInfoDownloader:
    """Downloader whose metadata/download-info lookups all fail softly."""

    def can_handle(self, url):
        return True

    def get_metadata(self, url):
        raise Exception("metadata unavailable")

    def get_video_info(self, url):
        raise Exception("video info unavailable")

    def get_download_info(self, url):
        raise Exception("download info unavailable")

    def get_subtitle(self, url):
        return None


class TestNoDownloadInfoPersistsFailed:
    def test_no_download_info_persists_failed(self, env, cm, monkeypatch):
        """When neither subtitle nor download info is obtainable, the early
        return must leave the task 'failed' in the DB."""
        url = "https://www.douyin.com/video/7123456789"
        task_id = cm.create_task(url=url)["task_id"]
        monkeypatch.setattr(svc, "create_downloader", lambda u: _NoInfoDownloader())

        result = svc.process_transcription(task_id=task_id, url=url)

        assert result["status"] == "failed"
        task = cm.get_task_by_id(task_id)
        assert task["status"] == "failed"
        assert "无法获取下载信息" in (task.get("error_message") or "")


YOUTUBE_URL = "https://www.youtube.com/watch?v=abc123def45"


def _make_youtube_downloader(fetch_exc):
    """Fake with the exact class name / attrs the API-server branch checks for."""

    class YoutubeDownloader(_NoInfoDownloader):
        use_api_server = True

        def fetch_for_transcription(self, url, use_speaker_recognition):
            raise fetch_exc

    return YoutubeDownloader()


class TestYoutubeApiFailurePersistsFailed:
    def test_api_error_persists_failed(self, env, cm, monkeypatch):
        """YouTubeApiError from the API server fast path must persist 'failed'."""
        from video_transcript_api.downloaders.youtube_api_errors import YouTubeApiError

        task_id = cm.create_task(url=YOUTUBE_URL)["task_id"]
        exc = YouTubeApiError("VIDEO_UNAVAILABLE", "Video unavailable")
        monkeypatch.setattr(svc, "create_downloader", lambda u: _make_youtube_downloader(exc))

        result = svc.process_transcription(task_id=task_id, url=YOUTUBE_URL)

        assert result["status"] == "failed"
        task = cm.get_task_by_id(task_id)
        assert task["status"] == "failed"
        assert "YouTube API Server error" in (task.get("error_message") or "")

    def test_unexpected_error_persists_failed(self, env, cm, monkeypatch):
        """Non-YouTubeApiError exceptions in the same branch must also persist."""
        task_id = cm.create_task(url=YOUTUBE_URL)["task_id"]
        exc = RuntimeError("connection reset")
        monkeypatch.setattr(svc, "create_downloader", lambda u: _make_youtube_downloader(exc))

        result = svc.process_transcription(task_id=task_id, url=YOUTUBE_URL)

        assert result["status"] == "failed"
        task = cm.get_task_by_id(task_id)
        assert task["status"] == "failed"
        assert "unexpected error" in (task.get("error_message") or "")
