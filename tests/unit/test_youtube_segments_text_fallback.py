"""Regression: empty subtitle text with non-empty segments must rebuild the
transcript body from segments -- on BOTH YouTube subtitle paths.

Real scenario: pure-numeric subtitle body lines (e.g. "2024") are skipped by
the legacy SRT text extraction (they look like cue index lines) while the
segments extraction keeps them per the "text is never lost" invariant --
yielding text == "" with valid segments. Before the fix, both paths wrote the
empty body to cache and handed the empty body to the LLM stage even though the
subtitle content was right there in the segments.

Covers:
- YouTube API Server fast path (api_result["transcript"] / transcript_segments)
- get_subtitle_result() platform-subtitle path (SubtitleResult.text / .segments)

All console output must be English only (no emoji, no Chinese).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import video_transcript_api.api.services.transcription as svc
from video_transcript_api.downloaders.models import DownloadInfo, VideoMetadata
from video_transcript_api.downloaders.subtitle_types import SubtitleResult


class _DummyQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class _RecordingCacheManager:
    """Cache-miss double that records save_cache calls and wins every CAS."""

    def __init__(self):
        self.saved = []

    def get_cache(self, platform=None, media_id=None, url=None, use_speaker_recognition=None):
        return None

    def save_cache(self, **kwargs):
        self.saved.append(kwargs)
        return {"files_loc": "dummy"}

    def update_task_status(self, task_id, status, **kwargs):
        return True

    def get_task_by_id(self, task_id):
        return None


_SEGMENTS = [
    {"start_time": 0.0, "end_time": 1.0, "text": "2024"},
    {"start_time": 1.0, "end_time": 2.5, "text": "hello world"},
]

_YT_URL = "https://www.youtube.com/watch?v=abc123"


def _make_youtube_downloader(*, use_api_server, subtitle_result=None, api_result=None):
    """Fake whose class name is exactly 'YoutubeDownloader' -- the service
    dispatches on __class__.__name__, so the name is load-bearing."""

    class YoutubeDownloader:
        def __init__(self):
            self.use_api_server = use_api_server

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
                download_url="http://example.com/audio.mp3",
                file_ext="mp3",
                filename="audio.mp3",
            )

        def fetch_for_transcription(self, url, speaker_recognition):
            return api_result

        def get_subtitle_result(self, url):
            return subtitle_result

    return YoutubeDownloader()


@pytest.fixture
def patched_env(monkeypatch):
    cache = _RecordingCacheManager()
    queue = _DummyQueue()
    monkeypatch.setattr(svc, "cache_manager", cache)
    monkeypatch.setattr(svc, "llm_task_queue", queue)
    monkeypatch.setattr(svc, "get_notification_router", lambda: MagicMock())
    return cache, queue


def _assert_subtitle_content_everywhere(result, cache, queue):
    assert result["status"] == "success"
    # Returned body carries the subtitle content.
    assert "2024" in result["data"]["transcript"]
    assert "hello world" in result["data"]["transcript"]

    # Cached body carries the subtitle content.
    assert cache.saved, "subtitle must be written to cache"
    saved = cache.saved[0]
    assert saved["transcript_type"] == "capswriter"
    assert "2024" in saved["transcript_data"]
    assert "hello world" in saved["transcript_data"]
    assert saved["extra_json_data"] == {"segments": _SEGMENTS}

    # LLM handoff payload carries the subtitle content.
    assert len(queue.items) == 1
    payload = queue.items[0]
    assert "2024" in payload["transcript"]
    assert "hello world" in payload["transcript"]


def test_youtube_api_fast_path_rebuilds_text_from_segments(patched_env, monkeypatch):
    cache, queue = patched_env
    api_result = {
        "video_id": "abc123",
        "video_title": "test title",
        "author": "test author",
        "description": "test desc",
        "platform": "youtube",
        "need_transcription": False,
        "transcript": "",
        "transcript_segments": _SEGMENTS,
        "audio_path": None,
    }
    downloader = _make_youtube_downloader(use_api_server=True, api_result=api_result)
    monkeypatch.setattr(svc, "create_downloader", lambda url: downloader)

    result = svc.process_transcription(
        task_id="task-yt-api-segments-only",
        url=_YT_URL,
        use_speaker_recognition=False,
    )

    _assert_subtitle_content_everywhere(result, cache, queue)


def test_get_subtitle_result_path_rebuilds_text_from_segments(patched_env, monkeypatch):
    cache, queue = patched_env
    subtitle_result = SubtitleResult(text="", segments=_SEGMENTS)
    downloader = _make_youtube_downloader(
        use_api_server=False, subtitle_result=subtitle_result
    )
    monkeypatch.setattr(svc, "create_downloader", lambda url: downloader)

    result = svc.process_transcription(
        task_id="task-yt-subtitle-segments-only",
        url=_YT_URL,
        use_speaker_recognition=False,
    )

    _assert_subtitle_content_everywhere(result, cache, queue)
