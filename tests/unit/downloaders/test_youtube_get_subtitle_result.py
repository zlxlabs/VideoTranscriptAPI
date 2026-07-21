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


def test_api_server_empty_text_but_nonempty_segments_is_still_valid_subtitle():
    """Regression for the all-numeric-body SRT bug: the legacy line-based
    text extraction can end up with text="" (every cue body line looked like
    an index line) while the lookahead-based segments extraction still found
    real content. get_subtitle_result()'s validity check must accept this as
    "has subtitles" (text non-empty OR segments non-empty) rather than
    discarding the whole result -- including its timestamps -- as None."""
    downloader = _make_downloader()
    downloader.config["youtube_api_server"] = {"enabled": True, "base_url": "http://x", "api_key": "k"}

    all_digit_result = SubtitleResult(
        text="",
        segments=[
            {"start_time": 1.0, "end_time": 4.0, "text": "42"},
            {"start_time": 5.0, "end_time": 8.0, "text": "100"},
        ],
    )
    downloader._youtube_api_client = Mock()
    downloader._youtube_api_client.fetch_transcript_result = Mock(return_value=all_digit_result)
    downloader._get_subtitle_result_with_tikhub_api = Mock()

    result = downloader.get_subtitle_result("https://www.youtube.com/watch?v=test")

    assert result is all_digit_result
    assert result.text == ""
    assert result.segments == all_digit_result.segments
    assert not downloader._get_subtitle_result_with_tikhub_api.called


def test_api_server_empty_text_and_no_segments_still_treated_as_no_subtitle():
    """Sanity check for the other side of the validity check: text=="" AND
    segments is None/empty must still be treated as "no subtitles" (returns
    None, no TikHub fallback attempted -- API Server already confirmed)."""
    downloader = _make_downloader()
    downloader.config["youtube_api_server"] = {"enabled": True, "base_url": "http://x", "api_key": "k"}

    empty_result = SubtitleResult(text="", segments=None)
    downloader._youtube_api_client = Mock()
    downloader._youtube_api_client.fetch_transcript_result = Mock(return_value=empty_result)
    downloader._get_subtitle_result_with_tikhub_api = Mock()

    result = downloader.get_subtitle_result("https://www.youtube.com/watch?v=test")

    assert result is None
    assert not downloader._get_subtitle_result_with_tikhub_api.called


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


def test_local_empty_text_but_nonempty_segments_is_still_valid_subtitle():
    """Same validity-check unification applied to the local branch (line 563
    in youtube.py): a SubtitleResult with text=="" but non-empty segments
    must be returned as-is, not discarded as None. In practice
    _fetch_youtube_transcript_result() never actually produces this shape
    (its own per-language gate only accepts non-empty text), but the
    validity check at this call site is unified defensively so the two
    branches of get_subtitle_result() never diverge in what counts as "has
    subtitles"."""
    downloader = _make_downloader()
    downloader.config["youtube_api_server"] = {"enabled": False}
    downloader._youtube_api_client = None

    all_digit_result = SubtitleResult(
        text="",
        segments=[{"start_time": 1.0, "end_time": 4.0, "text": "42"}],
    )
    downloader._fetch_youtube_transcript_result = Mock(return_value=all_digit_result)
    downloader._get_subtitle_result_with_tikhub_api = Mock()

    result = downloader.get_subtitle_result("https://www.youtube.com/watch?v=test")

    assert result is all_digit_result
    assert not downloader._get_subtitle_result_with_tikhub_api.called


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


def test_get_subtitle_api_server_all_numeric_srt_still_returns_none_unchanged():
    """Locks the historical str-only semantics of get_subtitle() (API Server
    branch): it calls YouTubeApiClient.fetch_transcript() (str-returning,
    driven by parse_srt_to_text -- a completely separate code path from
    fetch_transcript_result()/parse_srt_to_subtitle_result()). For an
    all-numeric-body SRT, parse_srt_to_text() has always returned "" (each
    body line is mistaken for the next cue's index line), so get_subtitle()
    must keep returning None exactly as before -- this fix only changes
    get_subtitle_result()'s validity check, never get_subtitle()'s."""
    downloader = _make_downloader()
    downloader.config["youtube_api_server"] = {"enabled": True, "base_url": "http://x", "api_key": "k"}
    downloader._youtube_api_client = Mock()
    downloader._youtube_api_client.fetch_transcript = Mock(return_value="")
    downloader._get_subtitle_with_tikhub_api = Mock()

    result = downloader.get_subtitle("https://www.youtube.com/watch?v=test")

    assert result is None
    assert not downloader._get_subtitle_with_tikhub_api.called
