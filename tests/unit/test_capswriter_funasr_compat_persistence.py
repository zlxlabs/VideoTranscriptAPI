"""Regression: FunASR-compat sidecar must be written even when some segment
times are invalid (NaN / Inf / missing).

Covers the real disk-writing path (CapsWriterClient._save_results): the stats
line used to compute total_duration unconditionally as
``seg["end_time"] - seg["start_time"]`` over segments whose times had been
degraded to None, crashing with TypeError and skipping
transcript_capswriter.json generation entirely (text was fine, but the whole
timeline sidecar silently went missing).

All console output must be English only (no emoji, no Chinese).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from video_transcript_api.transcriber.capswriter_client import (
    CapsWriterClient,
    Config,
)


def _make_client(output_dir: str) -> CapsWriterClient:
    """Build a client without touching project config files (same pattern as
    test_capswriter_retry.py)."""
    with patch.object(CapsWriterClient, "__init__", lambda self: None):
        client = CapsWriterClient()
    client.output_dir = str(output_dir)
    client.log = MagicMock()
    return client


@pytest.fixture
def compat_config(monkeypatch):
    """Pin the generate_* flags so the test does not depend on project config."""
    monkeypatch.setattr(Config, "generate_funasr_compat", True)
    monkeypatch.setattr(Config, "generate_txt", False)
    monkeypatch.setattr(Config, "generate_merge_txt", False)
    monkeypatch.setattr(Config, "generate_json", False)


def _build_result_payload():
    """Build a contract-valid result with invalid values only in later times."""
    s1 = "前面的句子时间有效。"
    s2 = "这是一个超长句子，" + "填" * 340 + "，用来触发切分逻辑。"
    text_accu = s1 + s2
    tokens = list(text_accu)

    s1_len = len(s1)
    timestamps = []
    for i in range(len(tokens)):
        if i < s1_len:
            timestamps.append(round(i * 0.1, 2))
        else:
            timestamps.append(float("nan"))
    # The second sentence starts with a finite timestamp; its later values
    # degrade to None and the last value exercises duration fallback.
    timestamps[s1_len] = 1.5
    timestamps[s1_len + 1] = float("inf")
    timestamps[-1] = None

    result = {
        "task_id": "task-bad-times",
        "text": "independent echo text",
        "text_accu": text_accu,
        "tokens": tokens,
        "timestamps": timestamps,
        "duration": 12.0,
        "time_complete": 3.0,
        "time_start": 1.0,
    }
    return result, text_accu


def test_funasr_compat_sidecar_written_despite_invalid_times(tmp_path, compat_config):
    client = _make_client(str(tmp_path))
    result, text = _build_result_payload()

    generated = asyncio.run(client._save_results(Path("audio.mp3"), result))

    funasr_file = tmp_path / "audio_funasr.json"
    assert funasr_file.exists(), (
        "FunASR compat sidecar must be written even when some segment times "
        "are invalid (NaN/Inf/missing)"
    )
    assert funasr_file in generated

    raw = funasr_file.read_text(encoding="utf-8")
    data = json.loads(raw)
    segments = data["segments"]
    assert segments, "segments must not be empty"

    # Text is never dropped: concatenating all segment texts reproduces the
    # full transcript body.
    assert "".join(seg["text"] for seg in segments) == text

    # Sentence boundaries use the next sentence's first token time.
    first = segments[0]
    assert first["start_time"] is not None
    assert first["end_time"] is not None
    assert first["end_time"] >= first["start_time"]
    assert first["end_time"] == 1.5

    # The final sentence falls back to duration when its last token time is
    # unavailable.
    assert segments[1]["start_time"] == 1.5
    assert segments[1]["end_time"] == 12.0

    # The on-disk JSON must be strict: no NaN / Infinity tokens.
    assert "NaN" not in raw
    assert "Infinity" not in raw
