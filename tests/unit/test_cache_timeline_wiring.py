"""T1 unit tests: CapsWriter/YouTube timeline wiring + get_cache segments readback.

Covers:
- save_cache(extra_json_data=...) writes transcript_capswriter.json
- load_segments / get_cache can read segments back
- bad times (NaN/Inf/invalid) become None; text is never dropped
- YoutubeDownloader.get_subtitle thin-delegates to get_subtitle_result
- capswriter _split_long_segment refuses non-finite times

All console output must be English only (no emoji, no Chinese).
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.downloaders.subtitle_types import SubtitleResult
from video_transcript_api.downloaders.youtube import YoutubeDownloader
from video_transcript_api.transcriber.capswriter_client import (
    _create_segments_from_capswriter,
    _split_long_segment,
)
from video_transcript_api.transcriber.segments import load_segments, normalize_segments
from video_transcript_api.utils.text_split import split_oversized_text


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "cache"


@pytest.fixture
def cm(cache_dir):
    manager = CacheManager(cache_dir=str(cache_dir))
    yield manager
    manager.close()


def _extra_segments_payload():
    return {
        "segments": [
            {"start_time": 0.0, "end_time": 1.5, "text": "Hello timeline"},
            {"start_time": 1.5, "end_time": 3.0, "text": "Second cue"},
        ]
    }


# ---------------------------------------------------------------------------
# A/C: extra_json_data round-trip via save_cache / load_segments / get_cache
# ---------------------------------------------------------------------------


class TestCacheTimelineRoundTrip:
    def test_save_extra_json_written_and_load_segments_reads_back(self, cm, cache_dir):
        extra = _extra_segments_payload()
        result = cm.save_cache(
            platform="youtube",
            url="https://example.com/t1-roundtrip",
            media_id="t1-roundtrip",
            use_speaker_recognition=False,
            transcript_data="Hello timeline Second cue",
            transcript_type="capswriter",
            title="T1",
            author="tester",
            description="",
            extra_json_data=extra,
        )
        assert result is not None

        json_files = list(cache_dir.rglob("transcript_capswriter.json"))
        assert len(json_files) == 1
        on_disk = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert on_disk["segments"][0]["text"] == "Hello timeline"

        segments = load_segments(json_files[0].parent)
        assert segments is not None
        assert len(segments) == 2
        assert segments[0]["start_time"] == 0.0
        assert segments[0]["end_time"] == 1.5
        assert segments[0]["text"] == "Hello timeline"
        assert segments[1]["text"] == "Second cue"

    def test_get_cache_includes_segments_field(self, cm, cache_dir):
        extra = _extra_segments_payload()
        cm.save_cache(
            platform="youtube",
            url="https://example.com/t1-get-cache",
            media_id="t1-get-cache",
            use_speaker_recognition=False,
            transcript_data="Hello timeline Second cue",
            transcript_type="capswriter",
            title="T1 get_cache",
            author="tester",
            description="",
            extra_json_data=extra,
        )

        cached = cm.get_cache(platform="youtube", media_id="t1-get-cache")
        assert cached is not None
        assert "segments" in cached
        assert cached["segments"][0]["text"] == "Hello timeline"
        assert cached["segments"][1]["start_time"] == 1.5
        # transcript text still available
        assert "Hello timeline" in cached["transcript_data"]

    def test_get_cache_omits_segments_when_no_sidecar(self, cm):
        cm.save_cache(
            platform="youtube",
            url="https://example.com/t1-no-side",
            media_id="t1-no-side",
            use_speaker_recognition=False,
            transcript_data="plain text only",
            transcript_type="capswriter",
            title="no side",
            author="tester",
            description="",
        )
        cached = cm.get_cache(platform="youtube", media_id="t1-no-side")
        assert cached is not None
        assert "segments" not in cached
        assert cached["transcript_data"] == "plain text only"

    def test_youtube_platform_subtitle_extra_json_contract(self, cm, cache_dir):
        """Contract used by transcription when get_subtitle_result has segments."""
        subtitle_result = SubtitleResult(
            text="cue one cue two",
            segments=[
                {"start_time": 0.0, "end_time": 1.0, "text": "cue one"},
                {"start_time": 1.0, "end_time": 2.0, "text": "cue two"},
            ],
        )
        extra = (
            {"segments": subtitle_result.segments}
            if subtitle_result.segments
            else None
        )
        assert extra is not None

        result = cm.save_cache(
            platform="youtube",
            url="https://www.youtube.com/watch?v=t1yt",
            media_id="t1yt",
            use_speaker_recognition=False,
            transcript_data=subtitle_result.text,
            transcript_type="capswriter",
            title="yt sub",
            author="yt",
            description="",
            extra_json_data=extra,
        )
        assert result is not None
        segments = load_segments(
            next(cache_dir.rglob("transcript_capswriter.json")).parent
        )
        assert segments is not None
        assert [s["text"] for s in segments] == ["cue one", "cue two"]

        cached = cm.get_cache(platform="youtube", media_id="t1yt")
        assert cached["segments"][0]["end_time"] == 1.0

    def test_no_segments_extra_json_none_still_succeeds(self, cm):
        """Honest degradation: missing segments must not fail the task."""
        result = cm.save_cache(
            platform="youtube",
            url="https://example.com/t1-none",
            media_id="t1-none",
            use_speaker_recognition=False,
            transcript_data="ok without timeline",
            transcript_type="capswriter",
            extra_json_data=None,
        )
        assert result is not None
        cached = cm.get_cache(platform="youtube", media_id="t1-none")
        assert cached is not None
        assert "segments" not in cached


# ---------------------------------------------------------------------------
# Overwrite without timeline must drop the stale sidecar
# ---------------------------------------------------------------------------


class TestOverwriteWithoutTimelineDropsStaleSidecar:
    """Regression: overwriting a cache entry with new text but no timeline
    (extra_json_data=None) must not leave the previous
    transcript_capswriter.json sidecar behind -- otherwise get_cache() /
    load_segments() keep reading the OLD segments back and mix them with the
    NEW transcript text. The orphan cleanup at save time only triggers when
    no live video_cache row exists, so it never covers this overwrite path."""

    def test_overwrite_with_none_extra_json_removes_old_sidecar(self, cm, cache_dir):
        cm.save_cache(
            platform="youtube",
            url="https://example.com/t1-overwrite",
            media_id="t1-overwrite",
            use_speaker_recognition=False,
            transcript_data="old text with timeline",
            transcript_type="capswriter",
            title="T1 overwrite",
            author="tester",
            description="",
            extra_json_data=_extra_segments_payload(),
        )
        assert list(cache_dir.rglob("transcript_capswriter.json"))

        result = cm.save_cache(
            platform="youtube",
            url="https://example.com/t1-overwrite",
            media_id="t1-overwrite",
            use_speaker_recognition=False,
            transcript_data="brand new text without timeline",
            transcript_type="capswriter",
            title="T1 overwrite",
            author="tester",
            description="",
            extra_json_data=None,
        )
        assert result is not None

        # The stale sidecar must be gone from disk.
        assert not list(cache_dir.rglob("transcript_capswriter.json"))

        cached = cm.get_cache(platform="youtube", media_id="t1-overwrite")
        assert cached is not None
        assert cached["transcript_data"] == "brand new text without timeline"
        assert cached.get("segments") in (None, [])

        # Direct adapter readback must also come up empty.
        assert load_segments(Path(cached["file_path"])) is None


# ---------------------------------------------------------------------------
# Bad times: text never dropped
# ---------------------------------------------------------------------------


class TestBadTimesHonestDegradation:
    def test_nan_inf_times_normalized_to_none_text_kept(self):
        raw = {
            "segments": [
                {"start_time": float("nan"), "end_time": 1.0, "text": "nan start"},
                {"start_time": 0.0, "end_time": float("inf"), "text": "inf end"},
                {"start_time": float("-inf"), "end_time": float("nan"), "text": "both bad"},
                {"start_time": 1.0, "end_time": 2.0, "text": "good"},
            ]
        }
        segs = normalize_segments(raw)
        assert segs is not None
        assert len(segs) == 4
        assert segs[0]["text"] == "nan start"
        assert segs[0]["start_time"] is None
        assert segs[1]["text"] == "inf end"
        assert segs[1]["end_time"] is None
        assert segs[2]["start_time"] is None
        assert segs[2]["end_time"] is None
        assert segs[2]["text"] == "both bad"
        assert segs[3]["start_time"] == 1.0
        assert segs[3]["end_time"] == 2.0

    def test_save_and_load_bad_times_via_capswriter_json(self, cm, cache_dir):
        extra = {
            "segments": [
                {"start_time": float("nan"), "end_time": 1.0, "text": "keep me"},
                {"start_time": 2.0, "end_time": 3.0, "text": "also keep"},
            ]
        }
        # JSON cannot encode NaN by default with allow_nan=False; dump via
        # ensure_ascii path that Python's json allows by default (NaN token).
        cm.save_cache(
            platform="youtube",
            url="https://example.com/t1-bad",
            media_id="t1-bad",
            use_speaker_recognition=False,
            transcript_data="keep me also keep",
            transcript_type="capswriter",
            extra_json_data=extra,
        )
        side = next(cache_dir.rglob("transcript_capswriter.json"))
        # Reload through adapter (parse_time_to_seconds rejects NaN)
        segs = load_segments(side.parent)
        assert segs is not None
        assert segs[0]["text"] == "keep me"
        assert segs[0]["start_time"] is None
        assert segs[1]["text"] == "also keep"

        cached = cm.get_cache(platform="youtube", media_id="t1-bad")
        assert cached["segments"][0]["text"] == "keep me"
        assert cached["segments"][0]["start_time"] is None


# ---------------------------------------------------------------------------
# D: get_subtitle thin-delegates to get_subtitle_result
# ---------------------------------------------------------------------------


class TestGetSubtitleDelegation:
    def test_get_subtitle_returns_text_from_get_subtitle_result(self):
        downloader = YoutubeDownloader()
        expected = SubtitleResult(
            text="delegated text",
            segments=[{"start_time": 0.0, "end_time": 1.0, "text": "delegated text"}],
        )
        downloader.get_subtitle_result = Mock(return_value=expected)

        text = downloader.get_subtitle("https://www.youtube.com/watch?v=abc12345678")

        assert text == "delegated text"
        downloader.get_subtitle_result.assert_called_once()

    def test_get_subtitle_returns_none_when_result_is_none(self):
        downloader = YoutubeDownloader()
        downloader.get_subtitle_result = Mock(return_value=None)

        text = downloader.get_subtitle("https://www.youtube.com/watch?v=abc12345678")

        assert text is None

    def test_get_subtitle_end_to_end_still_returns_plain_string(self):
        """Regression: public get_subtitle API stays str|None after delegation."""
        downloader = YoutubeDownloader()
        downloader.config["youtube_api_server"] = {"enabled": False}
        downloader._youtube_api_client = None

        expected = SubtitleResult(
            text="Hello world",
            segments=[
                {"start_time": 0.0, "end_time": 1.0, "text": "Hello"},
                {"start_time": 1.0, "end_time": 2.0, "text": "world"},
            ],
        )
        downloader._fetch_youtube_transcript_result = Mock(return_value=expected)

        text = downloader.get_subtitle("https://www.youtube.com/watch?v=test")
        assert text == "Hello world"
        assert isinstance(text, str)

        result = downloader.get_subtitle_result("https://www.youtube.com/watch?v=test")
        assert result.text == text
        assert result.segments is not None


# ---------------------------------------------------------------------------
# E: capswriter _split_long_segment isfinite guard
# ---------------------------------------------------------------------------


class TestSplitLongSegmentIsfinite:
    def test_finite_times_split_normally(self):
        segment = {
            "start_time": 0.0,
            "end_time": 10.0,
            "text": "aaaa，" + "b" * 50 + "，" + "c" * 50,
            "length": len("aaaa，" + "b" * 50 + "，" + "c" * 50),
        }
        parts = _split_long_segment(segment, max_len=60)
        assert len(parts) >= 2
        for part in parts:
            assert math.isfinite(part["start_time"])
            assert math.isfinite(part["end_time"])
            assert part["text"]

    def test_interpolated_times_are_contiguous_and_last_end_is_exact(self):
        text = "aa，" + "b" * 50 + "，" + "c" * 50
        segment = {
            "start_time": 1.234,
            "end_time": 10.987,
            "text": text,
            "length": len(text),
        }

        parts = _split_long_segment(segment, max_len=30)

        assert len(parts) >= 2
        assert parts[0]["start_time"] == 1.23
        assert parts[-1]["end_time"] == 10.99
        for previous, current in zip(parts, parts[1:]):
            assert current["start_time"] == previous["end_time"]
            assert current["start_time"] <= current["end_time"]

    def test_nan_start_does_not_emit_nan_times(self):
        text = "part one，" + "x" * 40 + "，" + "part two trailing text"
        segment = {
            "start_time": float("nan"),
            "end_time": 10.0,
            "text": text,
            "length": len(text),
        }
        parts = _split_long_segment(segment, max_len=50)
        assert parts
        joined = "".join(p["text"] for p in parts)
        assert "part one" in joined
        assert "part two" in joined
        for part in parts:
            st = part["start_time"]
            et = part["end_time"]
            if st is not None:
                assert math.isfinite(st)
            if et is not None:
                assert math.isfinite(et)

    def test_inf_duration_does_not_emit_inf_times(self):
        text = "alpha，" + "y" * 40 + "，" + "omega trailing"
        segment = {
            "start_time": 1.0,
            "end_time": float("inf"),
            "text": text,
            "length": len(text),
        }
        parts = _split_long_segment(segment, max_len=50)
        assert parts
        for part in parts:
            st = part["start_time"]
            et = part["end_time"]
            if st is not None:
                assert math.isfinite(st)
            if et is not None:
                assert math.isfinite(et)
        assert any("alpha" in p["text"] for p in parts)
        assert any("omega" in p["text"] for p in parts)


# ---------------------------------------------------------------------------
# F: capswriter _split_long_segment inverted interval (end < start)
# ---------------------------------------------------------------------------


class TestSplitLongSegmentInvertedTimes:
    """An inverted interval (end_time < start_time) is not a usable timeline:
    interpolating over it would emit a monotonically decreasing time axis.
    The text must be kept, but all times must degrade to None."""

    def test_inverted_interval_degrades_times_to_none_text_kept(self):
        text = "part one，" + "x" * 40 + "，" + "part two trailing text"
        segment = {
            "start_time": 10.0,
            "end_time": 5.0,  # inverted: end < start
            "text": text,
            "length": len(text),
        }
        parts = _split_long_segment(segment, max_len=50)
        assert len(parts) >= 2
        joined = "".join(p["text"] for p in parts)
        assert "part one" in joined
        assert "part two" in joined
        for part in parts:
            assert part["start_time"] is None
            assert part["end_time"] is None

    def test_output_never_contains_end_before_start(self):
        """Whatever the input, output must never contain end_time < start_time."""
        text = "alpha，" + "y" * 40 + "，" + "omega trailing"
        segment = {
            "start_time": 8.0,
            "end_time": 3.0,  # inverted
            "text": text,
            "length": len(text),
        }
        parts = _split_long_segment(segment, max_len=50)
        assert parts
        for part in parts:
            st = part["start_time"]
            et = part["end_time"]
            if st is not None and et is not None:
                assert et >= st

    def test_zero_span_interval_preserves_legacy_times(self):
        text = "part one，" + "x" * 40 + "，part two"
        segment = {
            "start_time": 10.0,
            "end_time": 10.0,
            "text": text,
            "length": len(text),
        }

        parts = _split_long_segment(segment, max_len=50)

        assert len(parts) >= 2
        assert all(part["start_time"] == 10.0 for part in parts)
        assert all(part["end_time"] == 10.0 for part in parts)
        assert len({part["start_time"] for part in parts}) == 1
        assert len({part["end_time"] for part in parts}) == 1
        assert "".join(part["text"] for part in parts) == text


# ---------------------------------------------------------------------------
# G: length-cap fallback when no secondary punctuation is available
#    (issue #142: the cap must stay enforceable on punctuation-free long text)
# ---------------------------------------------------------------------------


def _call_with_watchdog(func, *args, timeout=10.0):
    """Call ``func`` in a daemon thread and turn a hang into a failed assertion.

    The shared fallback helper loops while the remainder exceeds the cap, so a
    missing ``max_len > 0`` guard means an infinite loop rather than a slow
    call. Without this wrapper a red run would hang the whole test process
    instead of reporting a failure.
    """
    box = {}

    def _run():
        try:
            box["value"] = func(*args)
        except BaseException as exc:  # re-raised on the calling thread
            box["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), (
        f"{getattr(func, '__name__', func)} did not return within {timeout}s "
        f"(infinite loop?) args={args[1:]!r}"
    )
    if "error" in box:
        raise box["error"]
    return box["value"]


class TestSharedOversizedTextFallback:
    """The single shared implementation both producers call (#142): it cuts on
    whitespace when there is one, hard-cuts at the cap when there is not, never
    drops or reorders characters, and refuses a non-positive cap instead of
    spinning forever."""

    def test_whitespace_is_preferred_when_available(self):
        text = " ".join(["word"] * 400)

        pieces, whitespace_cuts, hard_cuts = split_oversized_text(text, 300)

        assert all(len(piece) <= 300 for piece in pieces)
        assert "".join(pieces) == text
        assert whitespace_cuts == len(pieces) - 1
        assert hard_cuts == 0

    def test_hard_cut_when_no_whitespace_and_no_punctuation(self):
        text = "词" * 800

        pieces, whitespace_cuts, hard_cuts = split_oversized_text(text, 300)

        assert [len(piece) for piece in pieces] == [300, 300, 200]
        assert "".join(pieces) == text
        assert whitespace_cuts == 0
        assert hard_cuts == 2

    @pytest.mark.parametrize("bad_max_len", [0, -1])
    def test_non_positive_max_len_fails_fast(self, bad_max_len):
        with pytest.raises(ValueError):
            _call_with_watchdog(split_oversized_text, "词" * 100, bad_max_len)


class TestSplitLongSegmentHardCap:
    """max_len is a producer-side cap, not a best-effort hint: when the comma
    level cannot cut (no CJK/ASCII comma at all, or a single part longer than
    the cap), the segment must still come back fully split, text intact and
    the timeline monotonic."""

    def test_no_secondary_punct_with_whitespace_splits_on_whitespace(self):
        text = " ".join(["word"] * 400)  # 1999 chars, single spaces, no comma
        segment = {
            "start_time": 0.0,
            "end_time": 200.0,
            "text": text,
            "length": len(text),
        }

        parts = _split_long_segment(segment, max_len=300)

        assert len(parts) > 1
        assert all(len(part["text"]) <= 300 for part in parts)
        assert "".join(part["text"] for part in parts) == text
        for part in parts:
            assert part["length"] == len(part["text"])
        starts = [part["start_time"] for part in parts]
        ends = [part["end_time"] for part in parts]
        assert starts == sorted(starts)
        assert all(end >= start for start, end in zip(starts, ends))
        assert starts[0] == 0.0
        assert ends[-1] == 200.0

    def test_no_punct_no_whitespace_hard_cuts_without_losing_characters(self):
        text = "词" * 800
        segment = {
            "start_time": 0.0,
            "end_time": 80.0,
            "text": text,
            "length": len(text),
        }

        parts = _split_long_segment(segment, max_len=300)

        assert [len(part["text"]) for part in parts] == [300, 300, 200]
        assert "".join(part["text"] for part in parts) == text
        for part in parts:
            assert part["start_time"] <= part["end_time"]
        for previous, current in zip(parts, parts[1:]):
            assert current["start_time"] == previous["end_time"]

    def test_single_overlong_part_between_commas_is_capped(self):
        text = "开头，" + "x" * 500 + "，结尾"
        segment = {
            "start_time": 1.0,
            "end_time": 9.0,
            "text": text,
            "length": len(text),
        }

        parts = _split_long_segment(segment, max_len=300)

        assert all(len(part["text"]) <= 300 for part in parts)
        assert "".join(part["text"] for part in parts) == text
        assert all(part["start_time"] <= part["end_time"] for part in parts)

    def test_hard_cap_keeps_existing_punctuation_split_behaviour(self):
        text = "aaaa，" + "b" * 50 + "，" + "c" * 50
        segment = {
            "start_time": 0.0,
            "end_time": 10.0,
            "text": text,
            "length": len(text),
        }

        parts = _split_long_segment(segment, max_len=60)

        # Punctuation split already satisfies the cap: no fallback reshaping.
        assert [part["text"] for part in parts] == ["aaaa，" + "b" * 50 + "，", "c" * 50]

    def test_create_segments_from_capswriter_caps_punctuation_free_input(self):
        body = "词" * 800
        segments = _create_segments_from_capswriter(
            tokens=list(body),
            timestamps=[index * 0.1 for index in range(len(body))],
            min_len=80,
            max_len=300,
        )

        assert len(segments) > 1
        assert all(len(segment["text"]) <= 300 for segment in segments)
        assert "".join(segment["text"] for segment in segments) == body
        assert all(
            segment["start_time"] <= segment["end_time"] for segment in segments
        )
