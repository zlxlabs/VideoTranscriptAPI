"""Unit tests for the unified "timed segments" read adapter.

Covers three public functions in video_transcript_api.transcriber.segments:
- parse_time_to_seconds: tolerant time value parsing (never raises).
- normalize_segments: raw dict/list -> canonical list[dict] with the
  "text is never dropped" invariant.
- load_segments: reads transcript_funasr.json / transcript_capswriter.json
  from a cache directory and returns normalized segments.

All console output in this file must be English only (no emoji, no Chinese),
per project convention.
"""

import json

import pytest

from video_transcript_api.transcriber.segments import (
    load_segments,
    normalize_segments,
    parse_time_to_seconds,
)


# ---------------------------------------------------------------------------
# parse_time_to_seconds
# ---------------------------------------------------------------------------

class TestParseTimeToSeconds:
    def test_float_passthrough(self):
        assert parse_time_to_seconds(12.5) == 12.5

    def test_int_converted_to_float(self):
        assert parse_time_to_seconds(41) == 41.0

    def test_zero_is_valid_not_falsy_none(self):
        # 0 is a legitimate timestamp (start of media), must not collapse to None
        assert parse_time_to_seconds(0) == 0.0
        assert parse_time_to_seconds("0") == 0.0

    def test_numeric_string(self):
        assert parse_time_to_seconds("12.5") == 12.5

    def test_hh_mm_ss_string(self):
        # llm_processed.json dialogs use "00:00:41" style timestamps
        assert parse_time_to_seconds("00:00:41") == 41.0
        assert parse_time_to_seconds("01:02:03") == 3723.0

    def test_mm_ss_string(self):
        assert parse_time_to_seconds("02:03") == 123.0

    def test_none_returns_none(self):
        assert parse_time_to_seconds(None) is None

    def test_empty_string_returns_none(self):
        assert parse_time_to_seconds("") is None
        assert parse_time_to_seconds("   ") is None

    def test_garbage_string_returns_none(self):
        assert parse_time_to_seconds("not-a-time") is None
        assert parse_time_to_seconds("aa:bb:cc") is None

    def test_negative_number_returns_none(self):
        assert parse_time_to_seconds(-5) is None
        assert parse_time_to_seconds(-5.0) is None
        assert parse_time_to_seconds("-5") is None

    def test_wrong_number_of_colon_parts_returns_none(self):
        assert parse_time_to_seconds("1:2:3:4") is None
        assert parse_time_to_seconds(":") is None

    def test_garbage_type_returns_none(self):
        assert parse_time_to_seconds([1, 2, 3]) is None
        assert parse_time_to_seconds({"a": 1}) is None

    def test_never_raises_on_any_input(self):
        # Defensive sweep across weird inputs -- must not throw.
        for value in (object(), b"bytes", float("nan"), float("inf")):
            parse_time_to_seconds(value)

    def test_non_finite_float_returns_none(self):
        # float('inf')/nan must never be treated as a valid timestamp --
        # downstream int(inf) conversions would crash otherwise.
        assert parse_time_to_seconds(float("inf")) is None
        assert parse_time_to_seconds(float("-inf")) is None
        assert parse_time_to_seconds(float("nan")) is None

    def test_non_finite_string_returns_none(self):
        assert parse_time_to_seconds("inf") is None
        assert parse_time_to_seconds("-inf") is None
        assert parse_time_to_seconds("nan") is None

    def test_overflowing_numeric_string_returns_none(self):
        # float("1e309") silently overflows to inf in Python (no exception);
        # must be caught by the finiteness check rather than accepted as a
        # huge-but-valid timestamp.
        assert parse_time_to_seconds("1e309") is None
        assert parse_time_to_seconds("-1e309") is None


# ---------------------------------------------------------------------------
# normalize_segments
# ---------------------------------------------------------------------------

class TestNormalizeSegments:
    def test_production_funasr_format_start_end_time(self):
        # Real production shape: top-level dict with "segments", fields
        # named start_time/end_time (float seconds), plus speaker/words.
        raw = {
            "segments": [
                {
                    "start_time": 0.0,
                    "end_time": 3.2,
                    "text": "hello world",
                    "speaker": "SPEAKER_00",
                    "words": [{"word": "hello", "start": 0.0, "end": 0.5}],
                }
            ]
        }
        result = normalize_segments(raw)
        assert result == [
            {
                "start_time": 0.0,
                "end_time": 3.2,
                "text": "hello world",
                "speaker": "SPEAKER_00",
            }
        ]

    def test_legacy_start_end_naming(self):
        # Repo's own old test fixtures use "start"/"end" (no _time suffix).
        raw = {
            "segments": [
                {"start": 1.0, "end": 2.5, "text": "legacy segment"},
            ]
        }
        result = normalize_segments(raw)
        assert result == [
            {"start_time": 1.0, "end_time": 2.5, "text": "legacy segment"}
        ]

    def test_hh_mm_ss_string_time(self):
        raw = [{"start_time": "00:00:41", "end_time": "00:00:45", "text": "hi"}]
        result = normalize_segments(raw)
        assert result == [{"start_time": 41.0, "end_time": 45.0, "text": "hi"}]

    def test_bad_time_keeps_text_sets_time_none(self):
        # The "text is never lost" law: broken/missing time must not drop
        # the segment, only null out the offending timestamp.
        raw = [
            {"start_time": None, "end_time": 5.0, "text": "start missing"},
            {"start_time": -1, "end_time": 5.0, "text": "negative start"},
            {"start_time": "garbage", "end_time": 5.0, "text": "garbage start"},
        ]
        result = normalize_segments(raw)
        assert result == [
            {"start_time": None, "end_time": 5.0, "text": "start missing"},
            {"start_time": None, "end_time": 5.0, "text": "negative start"},
            {"start_time": None, "end_time": 5.0, "text": "garbage start"},
        ]

    def test_missing_speaker_not_fabricated(self):
        # No "unknown" placeholder -- key must simply be absent.
        raw = [{"start_time": 0.0, "end_time": 1.0, "text": "no speaker field"}]
        result = normalize_segments(raw)
        assert result == [{"start_time": 0.0, "end_time": 1.0, "text": "no speaker field"}]
        assert "speaker" not in result[0]

    def test_bare_list_input(self):
        raw = [{"start_time": 0.0, "end_time": 1.0, "text": "bare list"}]
        result = normalize_segments(raw)
        assert result == [{"start_time": 0.0, "end_time": 1.0, "text": "bare list"}]

    def test_wrapped_dict_input(self):
        raw = {"segments": [{"start_time": 0.0, "end_time": 1.0, "text": "wrapped"}]}
        result = normalize_segments(raw)
        assert result == [{"start_time": 0.0, "end_time": 1.0, "text": "wrapped"}]

    def test_empty_text_entries_are_skipped(self):
        raw = [
            {"start_time": 0.0, "end_time": 1.0, "text": ""},
            {"start_time": 1.0, "end_time": 2.0, "text": "   "},
            {"start_time": 2.0, "end_time": 3.0},  # missing text key entirely
            {"start_time": 3.0, "end_time": 4.0, "text": "kept"},
        ]
        result = normalize_segments(raw)
        assert result == [{"start_time": 3.0, "end_time": 4.0, "text": "kept"}]

    def test_empty_list_returns_none(self):
        assert normalize_segments([]) is None
        assert normalize_segments({"segments": []}) is None

    def test_all_invalid_entries_returns_none(self):
        raw = [{"start_time": 0.0, "end_time": 1.0, "text": ""}]
        assert normalize_segments(raw) is None

    def test_none_input_returns_none(self):
        assert normalize_segments(None) is None

    def test_malformed_top_level_returns_none(self):
        assert normalize_segments("not a dict or list") is None
        assert normalize_segments({"no_segments_key": []}) is None
        assert normalize_segments({"segments": "not a list"}) is None

    def test_non_dict_items_are_skipped(self):
        raw = ["not a dict", {"start_time": 0.0, "end_time": 1.0, "text": "kept"}]
        result = normalize_segments(raw)
        assert result == [{"start_time": 0.0, "end_time": 1.0, "text": "kept"}]


# ---------------------------------------------------------------------------
# load_segments
# ---------------------------------------------------------------------------

class TestLoadSegments:
    def test_loads_from_transcript_funasr_json(self, tmp_path):
        data = {
            "segments": [
                {"start_time": 0.0, "end_time": 1.0, "text": "funasr segment", "speaker": "S0"}
            ]
        }
        (tmp_path / "transcript_funasr.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        result = load_segments(tmp_path)
        assert result == [
            {"start_time": 0.0, "end_time": 1.0, "text": "funasr segment", "speaker": "S0"}
        ]

    def test_loads_from_transcript_capswriter_json_when_funasr_absent(self, tmp_path):
        data = {"segments": [{"start_time": 0.0, "end_time": 1.0, "text": "capswriter segment"}]}
        (tmp_path / "transcript_capswriter.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        result = load_segments(tmp_path)
        assert result == [{"start_time": 0.0, "end_time": 1.0, "text": "capswriter segment"}]

    def test_funasr_takes_priority_over_capswriter(self, tmp_path):
        funasr_data = {"segments": [{"start_time": 0.0, "end_time": 1.0, "text": "from funasr"}]}
        capswriter_data = {"segments": [{"start_time": 0.0, "end_time": 1.0, "text": "from capswriter"}]}
        (tmp_path / "transcript_funasr.json").write_text(
            json.dumps(funasr_data, ensure_ascii=False), encoding="utf-8"
        )
        (tmp_path / "transcript_capswriter.json").write_text(
            json.dumps(capswriter_data, ensure_ascii=False), encoding="utf-8"
        )
        result = load_segments(tmp_path)
        assert result == [{"start_time": 0.0, "end_time": 1.0, "text": "from funasr"}]

    def test_missing_files_returns_none(self, tmp_path):
        assert load_segments(tmp_path) is None

    def test_corrupted_json_returns_none(self, tmp_path):
        (tmp_path / "transcript_funasr.json").write_text("{not valid json", encoding="utf-8")
        assert load_segments(tmp_path) is None

    def test_corrupted_funasr_falls_back_to_valid_capswriter(self, tmp_path):
        # Design decision: a broken/empty primary source is treated as "no
        # data available there", so the adapter tries the next-priority
        # source rather than giving up immediately.
        (tmp_path / "transcript_funasr.json").write_text("{not valid json", encoding="utf-8")
        capswriter_data = {"segments": [{"start_time": 0.0, "end_time": 1.0, "text": "fallback"}]}
        (tmp_path / "transcript_capswriter.json").write_text(
            json.dumps(capswriter_data, ensure_ascii=False), encoding="utf-8"
        )
        result = load_segments(tmp_path)
        assert result == [{"start_time": 0.0, "end_time": 1.0, "text": "fallback"}]

    def test_txt_only_capswriter_returns_none(self, tmp_path):
        # transcript_capswriter.txt has no timing info at all; not a
        # supported segment source (json only).
        (tmp_path / "transcript_capswriter.txt").write_text("plain text, no timing", encoding="utf-8")
        assert load_segments(tmp_path) is None

    def test_invalid_utf8_in_funasr_falls_back_to_valid_capswriter(self, tmp_path):
        # UnicodeDecodeError is a ValueError subclass, NOT an OSError, so the
        # bare (OSError, json.JSONDecodeError) except tuple used to miss it and
        # let it propagate uncaught -- violating this module's "never raises"
        # contract. A corrupt primary source must be treated the same as a
        # missing/malformed one: fall back to the next-priority source.
        (tmp_path / "transcript_funasr.json").write_bytes(
            b'{"segments": [{"start_time": 0.0, "end_time": 1.0, "text": "bad \xff\xfe byte"}]}'
        )
        capswriter_data = {
            "segments": [{"start_time": 0.0, "end_time": 1.0, "text": "fallback ok"}]
        }
        (tmp_path / "transcript_capswriter.json").write_text(
            json.dumps(capswriter_data, ensure_ascii=False), encoding="utf-8"
        )
        result = load_segments(tmp_path)
        assert result == [{"start_time": 0.0, "end_time": 1.0, "text": "fallback ok"}]

    def test_invalid_utf8_only_returns_none_without_raising(self, tmp_path):
        (tmp_path / "transcript_funasr.json").write_bytes(
            b'{"segments": [{"start_time": 0.0, "end_time": 1.0, "text": "bad \xff\xfe byte"}]}'
        )
        assert load_segments(tmp_path) is None
