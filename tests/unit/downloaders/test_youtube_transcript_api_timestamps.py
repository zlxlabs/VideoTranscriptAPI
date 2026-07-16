"""
Unit tests for YoutubeDownloader._fetch_youtube_transcript_result() (the
youtube-transcript-api path) with timestamps.

This is the first of the three YouTube subtitle parsing paths described in
the task. It used to only keep item.text, discarding item.start/item.duration
entirely. These tests lock down:

- Text output stays byte-identical to the historical algorithm (join by
  space, each item.text.strip()).
- segments carries start_time/end_time (seconds, start + duration) / text.
- Timestamp extraction failures (missing/non-numeric .start or .duration on
  a snippet) never break text, and never drop that snippet from segments
  either ("text is never lost" invariant): only the offending snippet's
  start_time/end_time is nulled out; the rest of the batch is untouched.
- Existing control-flow sentinels (IP_BLOCKED / TRANSCRIPTS_DISABLED / None)
  are preserved as plain string / None, only the success path now returns a
  SubtitleResult.
- _fetch_youtube_transcript() (existing internal entry, still mocked as a
  plain string by other test files) keeps returning plain text on success.

All console output must be in English only (no emoji, no Chinese).
"""

from unittest.mock import Mock

from youtube_transcript_api._errors import IpBlocked, TranscriptsDisabled

from video_transcript_api.downloaders.subtitle_types import SubtitleResult
from video_transcript_api.downloaders.youtube import YoutubeDownloader


class FakeSnippet:
    """Stand-in for youtube_transcript_api.FetchedTranscriptSnippet."""

    def __init__(self, text, start, duration):
        self.text = text
        self.start = start
        self.duration = duration


class FakeSnippetMissingDuration:
    """Snippet missing the .duration attribute entirely (malformed data)."""

    def __init__(self, text, start):
        self.text = text
        self.start = start


class FakeTranscriptListing:
    def __init__(self, language_code, is_generated=False):
        self.language_code = language_code
        self.is_generated = is_generated


def _make_downloader() -> YoutubeDownloader:
    return YoutubeDownloader()


def test_transcript_result_has_segments_with_start_end_time():
    downloader = _make_downloader()
    downloader.ytt_api = Mock()
    downloader.ytt_api.list = Mock(return_value=[FakeTranscriptListing("zh-CN")])
    downloader.ytt_api.fetch = Mock(return_value=[
        FakeSnippet("Hello", 0.0, 1.5),
        FakeSnippet("world", 1.5, 2.0),
    ])

    result = downloader._fetch_youtube_transcript_result("video123")

    assert isinstance(result, SubtitleResult)
    assert result.text == "Hello world"
    assert result.segments == [
        {"start_time": 0.0, "end_time": 1.5, "text": "Hello"},
        {"start_time": 1.5, "end_time": 3.5, "text": "world"},
    ]
    downloader.ytt_api.fetch.assert_called_once_with("video123", languages=["zh-CN"])


def test_backward_compatible_entry_still_returns_plain_text():
    """_fetch_youtube_transcript() (existing internal entry, mocked elsewhere
    in the suite as a plain string) keeps returning str on success."""
    downloader = _make_downloader()
    downloader.ytt_api = Mock()
    downloader.ytt_api.list = Mock(return_value=[FakeTranscriptListing("en")])
    downloader.ytt_api.fetch = Mock(return_value=[
        FakeSnippet("  padded  ", 0.0, 1.0),
        FakeSnippet("text", 1.0, 1.0),
    ])

    text = downloader._fetch_youtube_transcript("video123")

    assert text == "padded text"
    assert isinstance(text, str)


def test_missing_duration_attribute_keeps_snippet_with_none_end_time():
    """A snippet missing .duration must still land in segments (text is
    never lost) -- only its end_time is nulled out, since end_time depends
    on both start and duration."""
    downloader = _make_downloader()
    downloader.ytt_api = Mock()
    downloader.ytt_api.list = Mock(return_value=[FakeTranscriptListing("en")])
    downloader.ytt_api.fetch = Mock(return_value=[
        FakeSnippetMissingDuration("Hello world", 0.0),
    ])

    result = downloader._fetch_youtube_transcript_result("video123")

    assert result.text == "Hello world"
    assert result.segments == [
        {"start_time": 0.0, "end_time": None, "text": "Hello world"},
    ]


def test_mixed_valid_and_broken_snippets_keep_all_text_in_segments():
    """Mixed batch: a normal snippet, one missing .duration, and another
    normal one. All three must appear in segments -- the broken one with
    end_time = None -- and text stays byte-identical to the legacy join."""
    downloader = _make_downloader()
    downloader.ytt_api = Mock()
    downloader.ytt_api.list = Mock(return_value=[FakeTranscriptListing("en")])
    downloader.ytt_api.fetch = Mock(return_value=[
        FakeSnippet("Hello", 0.0, 1.5),
        FakeSnippetMissingDuration("Broken timing", 1.5),
        FakeSnippet("world", 3.5, 1.0),
    ])

    result = downloader._fetch_youtube_transcript_result("video123")

    assert result.text == "Hello Broken timing world"
    assert result.segments == [
        {"start_time": 0.0, "end_time": 1.5, "text": "Hello"},
        {"start_time": 1.5, "end_time": None, "text": "Broken timing"},
        {"start_time": 3.5, "end_time": 4.5, "text": "world"},
    ]


def test_ip_blocked_sentinel_preserved():
    downloader = _make_downloader()
    downloader.ytt_api = Mock()
    downloader.ytt_api.list = Mock(side_effect=IpBlocked("video123"))

    result = downloader._fetch_youtube_transcript_result("video123")

    assert result == "IP_BLOCKED"


def test_transcripts_disabled_sentinel_preserved():
    downloader = _make_downloader()
    downloader.ytt_api = Mock()
    downloader.ytt_api.list = Mock(side_effect=TranscriptsDisabled("video123"))

    result = downloader._fetch_youtube_transcript_result("video123")

    assert result == "TRANSCRIPTS_DISABLED"


def test_no_available_languages_returns_none():
    downloader = _make_downloader()
    downloader.ytt_api = Mock()
    downloader.ytt_api.list = Mock(return_value=[])

    result = downloader._fetch_youtube_transcript_result("video123")

    assert result is None


def test_empty_transcript_falls_back_to_none():
    """When the only available language yields no text at all, the method
    keeps returning None (matching the historical empty-subtitle behavior)."""
    downloader = _make_downloader()
    downloader.ytt_api = Mock()
    downloader.ytt_api.list = Mock(return_value=[FakeTranscriptListing("fr")])
    downloader.ytt_api.fetch = Mock(return_value=[])

    result = downloader._fetch_youtube_transcript_result("video123")

    assert result is None


def test_astronomically_large_start_overflows_to_none_start_time():
    """A JSON-legal but astronomically large integer time value (e.g. 10**400,
    which can survive upstream deserialization as a legit Python int) makes
    float() raise OverflowError instead of returning inf or raising
    ValueError/TypeError. Before the fix this propagated out of the per-item
    try/except, was caught by the outer broad `except Exception` in
    _fetch_youtube_transcript_result, and silently lost the ENTIRE subtitle
    path (not just the one bad timestamp) -- every priority + fallback
    language attempt failed, returning None. After the fix, this degrades to
    start_time=None for just the offending snippet -- consistent with any
    other unparseable time -- and the rest of the batch (including its own
    text) is untouched."""
    downloader = _make_downloader()
    downloader.ytt_api = Mock()
    downloader.ytt_api.list = Mock(return_value=[FakeTranscriptListing("en")])
    downloader.ytt_api.fetch = Mock(return_value=[
        FakeSnippet("Hello", 10 ** 400, 1.5),
        FakeSnippet("world", 1.5, 2.0),
    ])

    result = downloader._fetch_youtube_transcript_result("video123")

    assert isinstance(result, SubtitleResult)
    assert result.text == "Hello world"
    assert result.segments == [
        {"start_time": None, "end_time": None, "text": "Hello"},
        {"start_time": 1.5, "end_time": 3.5, "text": "world"},
    ]


def test_astronomically_large_duration_overflows_to_none_end_time():
    """Same OverflowError risk applies to item.duration (used to compute
    end_time = start + duration): an astronomically large duration must not
    blow up the whole language attempt either. Only this snippet's end_time
    is nulled out; start_time and text stay intact."""
    downloader = _make_downloader()
    downloader.ytt_api = Mock()
    downloader.ytt_api.list = Mock(return_value=[FakeTranscriptListing("en")])
    downloader.ytt_api.fetch = Mock(return_value=[
        FakeSnippet("Hello", 0.0, 10 ** 400),
    ])

    result = downloader._fetch_youtube_transcript_result("video123")

    assert isinstance(result, SubtitleResult)
    assert result.text == "Hello"
    assert result.segments == [
        {"start_time": 0.0, "end_time": None, "text": "Hello"},
    ]
