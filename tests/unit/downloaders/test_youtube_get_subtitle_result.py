"""
Unit tests for YoutubeDownloader.get_subtitle_result() (new entry point) and
an end-to-end regression check that get_subtitle() (the existing, externally
called entry point in api/services/transcription.py) keeps returning a plain
string identical to its historical behavior now that the internal parsing
helpers return SubtitleResult.

get_subtitle_result() mirrors get_subtitle()'s branch/fallback logic exactly,
but returns SubtitleResult (text + segments) instead of a plain string, so it
can be wired into time-aware features later.

All console output must be in English only (no emoji, no Chinese).
"""

from unittest.mock import Mock

from video_transcript_api.downloaders.subtitle_types import SubtitleResult
from video_transcript_api.downloaders.youtube import YoutubeDownloader


class FakeSnippet:
    def __init__(self, text, start, duration):
        self.text = text
        self.start = start
        self.duration = duration


class FakeTranscriptListing:
    def __init__(self, language_code, is_generated=False):
        self.language_code = language_code
        self.is_generated = is_generated


def _make_downloader() -> YoutubeDownloader:
    return YoutubeDownloader()


# ---------------------------------------------------------------------------
# API Server branch (use_api_server = True)
# ---------------------------------------------------------------------------

def test_api_server_success_returns_subtitle_result():
    downloader = _make_downloader()
    downloader.config["youtube_api_server"] = {"enabled": True, "base_url": "http://x", "api_key": "k"}

    expected = SubtitleResult(text="hello", segments=[{"start_time": 0.0, "end_time": 1.0, "text": "hello"}])
    downloader._youtube_api_client = Mock()
    downloader._youtube_api_client.fetch_transcript_result = Mock(return_value=expected)

    result = downloader.get_subtitle_result("https://www.youtube.com/watch?v=test")

    assert result is expected
    downloader._youtube_api_client.fetch_transcript_result.assert_called_once()


def test_api_server_no_transcript_returns_none_without_tikhub_fallback():
    downloader = _make_downloader()
    downloader.config["youtube_api_server"] = {"enabled": True, "base_url": "http://x", "api_key": "k"}

    downloader._youtube_api_client = Mock()
    downloader._youtube_api_client.fetch_transcript_result = Mock(return_value=None)
    downloader._get_subtitle_result_with_tikhub_api = Mock()

    result = downloader.get_subtitle_result("https://www.youtube.com/watch?v=test")

    assert result is None
    assert not downloader._get_subtitle_result_with_tikhub_api.called


def test_api_server_failure_falls_back_to_tikhub_result():
    downloader = _make_downloader()
    downloader.config["youtube_api_server"] = {"enabled": True, "base_url": "http://x", "api_key": "k"}

    downloader._youtube_api_client = Mock()
    downloader._youtube_api_client.fetch_transcript_result = Mock(side_effect=Exception("boom"))

    tikhub_result = SubtitleResult(text="from tikhub", segments=None)
    downloader._get_subtitle_result_with_tikhub_api = Mock(return_value=tikhub_result)

    result = downloader.get_subtitle_result("https://www.youtube.com/watch?v=test")

    assert result is tikhub_result


# ---------------------------------------------------------------------------
# Local branch (use_api_server = False)
# ---------------------------------------------------------------------------

def test_local_success_returns_subtitle_result():
    downloader = _make_downloader()
    downloader.config["youtube_api_server"] = {"enabled": False}
    downloader._youtube_api_client = None

    expected = SubtitleResult(text="local text", segments=[{"start_time": 0.0, "end_time": 1.0, "text": "local text"}])
    downloader._fetch_youtube_transcript_result = Mock(return_value=expected)

    result = downloader.get_subtitle_result("https://www.youtube.com/watch?v=test")

    assert result is expected


def test_local_ip_blocked_falls_back_to_tikhub_result():
    downloader = _make_downloader()
    downloader.config["youtube_api_server"] = {"enabled": False}
    downloader._youtube_api_client = None

    downloader._fetch_youtube_transcript_result = Mock(return_value="IP_BLOCKED")
    tikhub_result = SubtitleResult(text="from tikhub after ip block", segments=None)
    downloader._get_subtitle_result_with_tikhub_api = Mock(return_value=tikhub_result)

    result = downloader.get_subtitle_result("https://www.youtube.com/watch?v=test")

    assert result is tikhub_result


def test_local_transcripts_disabled_returns_none():
    downloader = _make_downloader()
    downloader.config["youtube_api_server"] = {"enabled": False}
    downloader._youtube_api_client = None

    downloader._fetch_youtube_transcript_result = Mock(return_value="TRANSCRIPTS_DISABLED")
    downloader._get_subtitle_result_with_tikhub_api = Mock()

    result = downloader.get_subtitle_result("https://www.youtube.com/watch?v=test")

    assert result is None
    assert not downloader._get_subtitle_result_with_tikhub_api.called


# ---------------------------------------------------------------------------
# End-to-end regression: get_subtitle() (existing, externally-called entry
# point) must keep returning a plain str identical to before, driven through
# the real (non-mocked) _fetch_youtube_transcript -> _fetch_youtube_transcript_result
# chain -- only the network-facing ytt_api is stubbed.
# ---------------------------------------------------------------------------

def test_get_subtitle_end_to_end_unchanged_while_get_subtitle_result_carries_segments():
    downloader = _make_downloader()
    downloader.config["youtube_api_server"] = {"enabled": False}
    downloader._youtube_api_client = None
    downloader.ytt_api = Mock()
    downloader.ytt_api.list = Mock(return_value=[FakeTranscriptListing("en")])
    downloader.ytt_api.fetch = Mock(return_value=[
        FakeSnippet("Hello", 0.0, 1.0),
        FakeSnippet("world", 1.0, 1.0),
    ])

    text = downloader.get_subtitle("https://www.youtube.com/watch?v=test")
    assert text == "Hello world"
    assert isinstance(text, str)

    result = downloader.get_subtitle_result("https://www.youtube.com/watch?v=test")
    assert isinstance(result, SubtitleResult)
    assert result.text == text
    assert result.segments == [
        {"start_time": 0.0, "end_time": 1.0, "text": "Hello"},
        {"start_time": 1.0, "end_time": 2.0, "text": "world"},
    ]
