import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from capswriter_asr import Transcript

from video_transcript_api.transcriber.capswriter_client import (
    CapsWriterClient,
    Config,
    _create_segments_from_capswriter,
    _find_token_idx,
    _token_prefixes,
)


def _make_client(output_dir: Path) -> CapsWriterClient:
    with patch.object(CapsWriterClient, "__init__", lambda self: None):
        client = CapsWriterClient()
    client.output_dir = str(output_dir)
    client.max_retries = 1
    client.retry_delay = 0
    client.log = MagicMock()
    return client


def _valid_result() -> dict:
    text_accu = "Hello, world! Next sentence."
    tokens = list(text_accu)
    return {
        "task_id": "contract-task",
        "text": "independent echo transcript",
        "text_accu": text_accu,
        "tokens": tokens,
        "timestamps": [index * 0.1 for index in range(len(tokens))],
        "duration": 4.0,
        "time_start": 1.0,
        "time_complete": 2.0,
    }


def _assert_contract_failure_without_outputs(
    tmp_path: Path, result: dict, condition: str
) -> None:
    client = _make_client(tmp_path)
    with patch.object(Config, "generate_txt", True), patch.object(
        Config, "generate_merge_txt", False
    ), patch.object(Config, "generate_json", True), patch.object(
        Config, "generate_funasr_compat", True
    ), patch(
        "video_transcript_api.transcriber.capswriter_client.logger.warning"
    ) as warning:
        with pytest.raises(ValueError, match="CAPSWRITER_CONTRACT_FAILED"):
            asyncio.run(client._save_results(tmp_path / "audio.mp3", result))

    assert list(tmp_path.iterdir()) == []
    assert condition in warning.call_args.args[0]


def test_missing_text_accu_fails_before_any_output(tmp_path):
    result = _valid_result()
    del result["text_accu"]

    _assert_contract_failure_without_outputs(
        tmp_path, result, "condition=text_accu_missing_or_empty"
    )


def test_token_timestamp_length_mismatch_fails_before_any_output(tmp_path):
    result = _valid_result()
    result["timestamps"].pop()

    _assert_contract_failure_without_outputs(
        tmp_path, result, "condition=tokens_timestamps_length_mismatch"
    )


def test_token_join_mismatch_fails_before_any_output(tmp_path):
    result = _valid_result()
    result["text_accu"] = "Hello, world! Next sentencX."

    _assert_contract_failure_without_outputs(
        tmp_path, result, "condition=tokens_join_text_accu_mismatch"
    )


def test_sidecar_failure_is_a_failed_transcription_without_outputs(tmp_path):
    client = _make_client(tmp_path)
    result = _valid_result()
    transcript = Transcript(
        text=result["text"],
        tokens=result["tokens"],
        timestamps=result["timestamps"],
        duration=result["duration"],
        raw=result,
    )

    with patch.object(Config, "generate_txt", True), patch.object(
        Config, "generate_merge_txt", False
    ), patch.object(Config, "generate_json", False), patch.object(
        Config, "generate_funasr_compat", True
    ), patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        return_value=transcript,
    ), patch(
        "video_transcript_api.transcriber.capswriter_client._create_segments_from_capswriter",
        side_effect=RuntimeError("sidecar construction failed"),
    ):
        success, generated_files = client.transcribe_file(str(tmp_path / "audio.mp3"))

    assert (success, generated_files) == (False, [])
    assert list(tmp_path.iterdir()) == []
    assert all(
        "可忽略此警告" not in call.args[0]
        for call in getattr(client.log, "call_args_list", [])
        if call.args
    )


def test_prefix_sum_uses_raw_tokens_and_repeated_times_without_zero_segments():
    tokens = ["Hello", " ", "，", "world", "!", " ", "Next", "。"]
    timestamps = [0.0, 0.5, 0.5, 0.5, 1.0, 1.0, 1.5, 1.5]
    text = "".join(tokens)
    segments = _create_segments_from_capswriter(
        text="wrong echo text",
        tokens=tokens,
        timestamps=timestamps,
        duration=2.0,
    )

    assert "".join(segment["text"] for segment in segments) == text
    prefixes = _token_prefixes(tokens)
    assert prefixes == [0, 5, 6, 7, 12, 13, 14, 18, 19]
    assert all(
        earlier <= later for earlier, later in zip(timestamps, timestamps[1:])
    )
    assert any(
        earlier == later for earlier, later in zip(timestamps, timestamps[1:])
    )

    for index, segment in enumerate(segments):
        start = segment["char_start"]
        end = segment["char_end"]
        assert segment["text"] == text[start:end]
        start_token = _find_token_idx(prefixes, start)
        assert segment["start_token_idx"] == start_token
        assert segment["start_time"] == timestamps[start_token]
        if index + 1 < len(segments):
            next_token = _find_token_idx(prefixes, segments[index + 1]["char_start"])
            assert segment["end_time"] == timestamps[next_token]
        else:
            assert segment["end_time"] == timestamps[-1]
        assert segment["start_time"] != segment["end_time"]


def test_discards_partial_artifacts_when_writing_fails(tmp_path, monkeypatch):
    """写盘中途失败必须清理本次已写出的产物，且不得报告成功。

    全部产物已在内存构建完成，失败只发生在写盘阶段。若不清理，输出目录会留下
    半组文件，调用方可能把它当成本次成功结果——与「侧车失败也算成功」同类（#121）。
    """
    import json as _json

    client = _make_client(tmp_path)
    result = _valid_result()
    real_dump = _json.dump
    calls = {"n": 0}

    def flaky_dump(payload, fh, **kwargs):
        # Config.generate_json=False，故 json.dump 只被 funasr 侧车调用一次；
        # 此时 txt 已先行落盘，正好制造「半组文件」场景。
        calls["n"] += 1
        raise OSError("disk full")

    monkeypatch.setattr(
        "video_transcript_api.transcriber.capswriter_client.json.dump", flaky_dump
    )

    with pytest.raises(OSError):
        asyncio.run(client._save_results(tmp_path / "audio.webm", result))

    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == [], f"写盘失败后残留产物: {leftovers}"
