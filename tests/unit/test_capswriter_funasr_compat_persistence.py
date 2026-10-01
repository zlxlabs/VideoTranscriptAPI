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
    _create_segments_from_capswriter,
)
from video_transcript_api.transcriber.token_timeline import canonical_projection


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
    """A result whose first sentence has valid times and whose (overlong)
    second sentence carries NaN / Inf / missing (None) timestamps.

    tokens are one character each *in the current token style* (tokens carry
    their own punctuation, spaces included) — the old fixture built tokens
    from the punctuation-stripped text, which silently encoded the historical
    coordinate convention and made the new token styles untestable.
    """
    s1 = "前面的句子时间有效。"
    s2 = "这是一个超长句子，" + "填" * 340 + "，用来触发切分逻辑。"
    text = s1 + s2

    tokens = list(text)

    s1_token_len = len(s1)
    timestamps = []
    for i in range(len(tokens)):
        if i < s1_token_len:
            timestamps.append(round(i * 0.1, 2))
        else:
            timestamps.append(float("nan"))
    # Sprinkle Inf and missing (None) into the long-sentence range.
    timestamps[s1_token_len] = float("inf")
    timestamps[-1] = None

    result = {
        "task_id": "task-bad-times",
        "text": text,
        "tokens": tokens,
        "timestamps": timestamps,
        "duration": 12.0,
        "time_complete": 3.0,
        "time_start": 1.0,
    }
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
    assert canonical_projection(text) != ""

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


def test_english_text_conservation_survives_merging():
    """Opus 发现 C 的验证：英文正文在「合并 + 拼接」路径上是否丢空格。

    新实现合并时取区间并集、段落文本由 ``text[span]`` 切出（不拼字符串），因此
    英文句间空格必须原样保留——这条断言就是那道回归防线（``...god.Next...``）。
    """
    text = (
        "Hello there my friend. This is a long enough english sentence to "
        "trigger merging behaviour. "
        "God. Next sentence follows right after this one, and it is also "
        "quite long so that merge logic kicks in properly. "
        "Finally we end the transcript here."
    )
    words = text.split(" ")
    tokens = [" " + word for word in words]
    timestamps = [round(index * 0.2, 2) for index in range(len(tokens))]

    segments = _create_segments_from_capswriter(
        text=text, tokens=tokens, timestamps=timestamps
    )

    assert len(segments) > 1, "fixture must actually exercise the merging path"
    joined = "".join(seg["text"] for seg in segments)
    assert joined == text, "English inter-sentence spaces must not be swallowed"
    assert ". God. Next" in joined, "no 'god.Next' space-eating regression"
