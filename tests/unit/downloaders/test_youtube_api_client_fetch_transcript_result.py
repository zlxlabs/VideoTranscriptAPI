"""
Unit tests for YouTubeApiClient.fetch_transcript_result() wiring.

fetch_transcript() is the historical, still-used-internally entry point that
returns plain text only. fetch_transcript_result() is the new sibling that
also returns timestamp segments, needed to wire time information through the
"API Server" branch of YoutubeDownloader.get_subtitle_result().

These tests stub out network calls (create_and_wait / download_content) so
no real HTTP request happens.

All console output must be in English only (no emoji, no Chinese).
"""

from unittest.mock import Mock

from video_transcript_api.downloaders.subtitle_types import SubtitleResult
from video_transcript_api.downloaders.youtube_api_client import (
    FileInfo,
    TaskResult,
    YouTubeApiClient,
)


def _make_client() -> YouTubeApiClient:
    return YouTubeApiClient({"base_url": "http://example.com", "api_key": "k"})


def test_fetch_transcript_result_returns_subtitle_result_with_segments():
    client = _make_client()

    task_result = TaskResult(
        task_id="t1",
        status="completed",
        video_id="v1",
        video_info=None,
        audio=None,
        transcript=FileInfo(url="/api/v1/files/v1.srt", language="en"),
        cache_hit=False,
        has_transcript=True,
        audio_fallback=False,
    )
    client.create_and_wait = Mock(return_value=task_result)
    client.download_content = Mock(
        return_value=(
            "1\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "Hello world\n"
        )
    )

    result = client.fetch_transcript_result("v1")

    assert isinstance(result, SubtitleResult)
    assert result.text == "Hello world"
    assert result.segments == [
        {"start_time": 1.0, "end_time": 2.0, "text": "Hello world"},
    ]


def test_fetch_transcript_result_none_when_no_transcript():
    client = _make_client()

    task_result = TaskResult(
        task_id="t1",
        status="completed",
        video_id="v1",
        video_info=None,
        audio=None,
        transcript=None,
        cache_hit=False,
        has_transcript=False,
        audio_fallback=True,
    )
    client.create_and_wait = Mock(return_value=task_result)

    assert client.fetch_transcript_result("v1") is None


def test_fetch_transcript_still_returns_plain_text_unchanged():
    """fetch_transcript() (existing public entry) must keep returning str."""
    client = _make_client()

    task_result = TaskResult(
        task_id="t1",
        status="completed",
        video_id="v1",
        video_info=None,
        audio=None,
        transcript=FileInfo(url="/api/v1/files/v1.srt", language="en"),
        cache_hit=False,
        has_transcript=True,
        audio_fallback=False,
    )
    client.create_and_wait = Mock(return_value=task_result)
    client.download_content = Mock(
        return_value=(
            "1\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "Hello world\n"
        )
    )

    text = client.fetch_transcript("v1")

    assert text == "Hello world"
    assert isinstance(text, str)
