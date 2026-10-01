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
    _validate_capswriter_contract,
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
    """A contract-valid payload whose first sentence keeps finite times and
    whose (overlong) second sentence ends on a missing (None) timestamp.

    The second sentence's first token carries a finite start so the first
    sentence keeps both ends; its last token is None, so every segment split
    out of it degrades honestly to None. An Inf in the middle of the sentence
    proves the emitted JSON stays strict no matter what the raw stream holds.
    """
    s1 = "前面的句子时间有效。"
    s2 = "这是一个超长句子，" + "填" * 340 + "，用来触发切分逻辑。"
    text = s1 + s2

    # One character per token, punctuation included: the body is "".join(tokens)
    # and carries its own punctuation, so the primary-punctuation split runs on
    # exactly this string.
    tokens = list(text)

    s2_start = len(s1)
    timestamps = [round(i * 0.05, 2) for i in range(len(tokens))]
    timestamps[s2_start] = 5.0
    timestamps[-1] = None
    timestamps[s2_start + 5] = float("inf")

    result = {
        "task_id": "task-bad-times",
        "text_accu": text,
        "tokens": tokens,
        "timestamps": timestamps,
        "duration": 12.0,
        "time_complete": 3.0,
        "time_start": 1.0,
    }
    _validate_capswriter_contract(result)
    return result, text


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

    # The valid first sentence keeps its finite times.
    first = segments[0]
    assert first["start_time"] is not None
    assert first["end_time"] is not None
    assert first["end_time"] >= first["start_time"]

    # Every invalid time degraded honestly to JSON null.
    for seg in segments[1:]:
        assert seg["start_time"] is None
        assert seg["end_time"] is None

    # The on-disk JSON must be strict: no NaN / Infinity tokens.
    assert "NaN" not in raw
    assert "Infinity" not in raw
