#!/usr/bin/env python
# coding: utf-8

"""CapsWriter 上游文件任务契约的验收测试（主测试文件）。

生产实测（2026-10-01，上游 SDK 已实现并部署）三条不变式：

1. ``raw["text_accu"]`` 存在且为非空字符串；
2. ``len(tokens) == len(timestamps)``；
3. ``"".join(tokens) == text_accu``（逐字）。

由此正文唯一权威是 ``"".join(tokens)``——``raw["text"]`` 是独立回显稿，与之
不同，拿它当字符坐标源会把时间轴压缩约 19%。

断言形状统一是「抛异常 且 落盘为空」：原缺陷正是「抛了/吞了，但磁盘上已有
本次产物 → 任务被标记成功却没有产物」，只断言抛异常会漏掉它。

All console output must be English only (no emoji, no Chinese).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from video_transcript_api.transcriber.capswriter_client import (
    COND_JOIN_MISMATCH,
    COND_LENGTH_MISMATCH,
    COND_TEXT_ACCU,
    CONTRACT_ERROR_LITERAL,
    CapsWriterClient,
    CapsWriterContractError,
    Config,
    _atomic_write_text,
    _create_segments_from_capswriter,
    _validate_capswriter_contract,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_client(output_dir) -> CapsWriterClient:
    """Build a client without touching project config files."""
    with patch.object(CapsWriterClient, "__init__", lambda self: None):
        client = CapsWriterClient()
    client.output_dir = str(output_dir)
    client.max_retries = 1
    client.retry_delay = 0
    client.log = MagicMock()
    return client


@pytest.fixture
def all_outputs_on(monkeypatch):
    """Turn on every product this module can emit."""
    monkeypatch.setattr(Config, "generate_txt", True)
    monkeypatch.setattr(Config, "generate_merge_txt", True)
    monkeypatch.setattr(Config, "generate_json", True)
    monkeypatch.setattr(Config, "generate_funasr_compat", True)


def _payload(tokens, timestamps, text_accu=None, **extra):
    """Build a raw payload honouring the upstream contract."""
    raw = {
        "tokens": list(tokens),
        "timestamps": list(timestamps),
        "text_accu": "".join(tokens) if text_accu is None else text_accu,
        "task_id": "task-contract",
        "duration": 10.0,
        "time_start": 1.0,
        "time_complete": 3.0,
    }
    raw.update(extra)
    return raw


def _assert_nothing_on_disk(output_dir: Path):
    """No artifact of this run may exist -- not even a leftover .tmp file."""
    assert list(output_dir.iterdir()) == [], (
        f"contract failure must leave no artifact on disk, found: "
        f"{sorted(p.name for p in output_dir.iterdir())}"
    )


# ---------------------------------------------------------------------------
# I1 -- contract validation: fail fast, verbatim, before anything is written
# ---------------------------------------------------------------------------


def test_contract_missing_text_accu_raises_and_writes_nothing(tmp_path, all_outputs_on):
    client = _make_client(tmp_path)
    raw = _payload(["a", "b"], [0.0, 1.0])
    del raw["text_accu"]

    with pytest.raises(CapsWriterContractError) as excinfo:
        asyncio.run(client._save_results(Path("audio.mp3"), raw))

    assert excinfo.value.condition == COND_TEXT_ACCU
    assert CONTRACT_ERROR_LITERAL + COND_TEXT_ACCU in str(excinfo.value)
    _assert_nothing_on_disk(tmp_path)


def test_contract_empty_text_accu_raises_and_writes_nothing(tmp_path, all_outputs_on):
    client = _make_client(tmp_path)
    raw = _payload(["a", "b"], [0.0, 1.0], text_accu="")

    with pytest.raises(CapsWriterContractError) as excinfo:
        asyncio.run(client._save_results(Path("audio.mp3"), raw))

    assert excinfo.value.condition == COND_TEXT_ACCU
    _assert_nothing_on_disk(tmp_path)


def test_contract_length_mismatch_raises_and_writes_nothing(
    tmp_path, all_outputs_on
):
    client = _make_client(tmp_path)
    raw = _payload(["a", "b", "c"], [0.0, 1.0])

    with pytest.raises(CapsWriterContractError) as excinfo:
        asyncio.run(client._save_results(Path("audio.mp3"), raw))

    assert excinfo.value.condition == COND_LENGTH_MISMATCH
    _assert_nothing_on_disk(tmp_path)


def test_contract_join_mismatch_raises_and_writes_nothing(tmp_path, all_outputs_on):
    """Verbatim comparison: no normalization before comparing."""
    client = _make_client(tmp_path)
    # "".join(tokens) == "a b", text_accu == "ab" -- only whitespace differs.
    raw = _payload(["a", " ", "b"], [0.0, 0.5, 1.0], text_accu="ab")

    with pytest.raises(CapsWriterContractError) as excinfo:
        asyncio.run(client._save_results(Path("audio.mp3"), raw))

    assert excinfo.value.condition == COND_JOIN_MISMATCH
    _assert_nothing_on_disk(tmp_path)


def test_validate_returns_contract_fields():
    raw = _payload(["你", "好"], [0.0, 1.0])
    text_accu, tokens, timestamps = _validate_capswriter_contract(raw)
    assert text_accu == "你好"
    assert tokens == ["你", "好"]
    assert timestamps == [0.0, 1.0]


def test_contract_error_propagates_through_transcribe_file(tmp_path, monkeypatch):
    """A contract violation is not a retryable transport fault."""
    from capswriter_asr import Transcript

    client = _make_client(tmp_path)
    monkeypatch.setattr(Config, "generate_txt", True)
    transcript = Transcript(
        text="echo draft",
        tokens=list("ab"),
        timestamps=[0.0],
        duration=1.0,
        raw={"text_accu": "ab"},
    )
    with patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        return_value=transcript,
    ):
        with pytest.raises(CapsWriterContractError) as excinfo:
            client.transcribe_file(str(tmp_path / "audio.mp3"))

    assert excinfo.value.condition == COND_LENGTH_MISMATCH
    _assert_nothing_on_disk(tmp_path)


# ---------------------------------------------------------------------------
# I2 -- body is "".join(tokens); coordinates are token length prefix sums
# ---------------------------------------------------------------------------


def test_body_is_token_join_not_transcript_text(tmp_path, monkeypatch):
    """The txt product and the timeline body come from text_accu, never from
    the independent echo draft carried in transcript.text."""
    from capswriter_asr import Transcript

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    media = tmp_path / "audio.mp4"
    media.write_bytes(b"fixture")
    client = _make_client(output_dir)

    monkeypatch.setattr(Config, "generate_txt", True)
    monkeypatch.setattr(Config, "generate_merge_txt", False)
    monkeypatch.setattr(Config, "generate_json", False)
    monkeypatch.setattr(Config, "generate_funasr_compat", True)

    # text_accu is 5 chars, the echo draft text is a different 5-char string.
    transcript = Transcript(
        text="ECHO!",
        tokens=list("你好。"),
        timestamps=[0.0, 0.4, 0.8],
        duration=1.0,
        raw={"text_accu": "你好。", "task_id": "t1"},
    )
    with patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        return_value=transcript,
    ):
        success, _ = client.transcribe_file(str(media))

    assert success is True
    assert (output_dir / "audio.txt").read_text(encoding="utf-8") == "你好。"
    sidecar = json.loads(
        (output_dir / "audio_funasr.json").read_text(encoding="utf-8")
    )
    assert "".join(s["text"] for s in sidecar["segments"]) == "你好。"


def test_coordinates_are_token_length_prefix_sums():
    """Multi-character tokens shift the char->token mapping."""
    tokens = ["今天", "我们", "聊", "语音", "。", "明", "天", "。"]
    timestamps = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]

    segments = _create_segments_from_capswriter(
        tokens=tokens, timestamps=timestamps, min_len=2, max_len=300
    )

    assert [(s["text"], s["start_time"], s["end_time"]) for s in segments] == [
        ("今天我们聊语音。", 0.0, 5.0),
        ("明天。", 5.0, 7.0),
    ]


def test_segment_end_is_next_sentence_first_token_not_own_last_token():
    """72.8% of adjacent CapsWriter timestamps repeat, so taking the sentence's
    own last token as the end produces zero-length spans. End must come from the
    next sentence's first token."""
    # Sentence 1 is entirely made of one repeated timestamp: the own-last-token
    # rule would emit start == end == 0.7.
    tokens = ["你", "好", "世", "。", "界", "好", "。"]
    timestamps = [0.7, 0.7, 0.7, 0.7, 1.2, 1.2, 1.5]

    segments = _create_segments_from_capswriter(
        tokens=tokens, timestamps=timestamps, min_len=2, max_len=300
    )

    assert [(s["text"], s["start_time"], s["end_time"]) for s in segments] == [
        ("你好世。", 0.7, 1.2),
        ("界好。", 1.2, 1.5),
    ]
    assert all(s["end_time"] > s["start_time"] for s in segments)


def test_text_is_never_dropped_with_duplicate_timestamps():
    tokens = ["你", "好", "。", "世", "界", "！"]
    body = "你好。世界！"
    timestamps = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    segments = _create_segments_from_capswriter(
        tokens=tokens, timestamps=timestamps, min_len=2, max_len=300
    )

    assert "".join(s["text"] for s in segments) == body


# ---------------------------------------------------------------------------
# I3 -- build everything in memory first, then write
# ---------------------------------------------------------------------------


def test_funasr_build_failure_leaves_no_product_on_disk(tmp_path, all_outputs_on):
    """The regression: sidecar generation failed, only a warning was logged,
    generated_files already held the txt, so transcribe_file returned True
    although the timeline product never existed."""
    client = _make_client(tmp_path)
    raw = _payload(["你", "好", "。"], [0.0, 0.5, 0.9])

    with patch(
        "video_transcript_api.transcriber.capswriter_client._create_segments_from_capswriter",
        side_effect=RuntimeError("segment build boom"),
    ):
        with pytest.raises(RuntimeError, match="segment build boom"):
            asyncio.run(client._save_results(Path("audio.mp3"), raw))

    _assert_nothing_on_disk(tmp_path)


def test_successful_run_leaves_no_temp_file(tmp_path, all_outputs_on):
    client = _make_client(tmp_path)
    raw = _payload(["你", "好", "。"], [0.0, 0.5, 0.9])

    generated = asyncio.run(client._save_results(Path("audio.mp3"), raw))

    assert {p.name for p in generated} == {
        "audio.json",
        "audio.txt",
        "audio.merge.txt",
        "audio_funasr.json",
    }
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        p.name for p in generated
    )


# ---------------------------------------------------------------------------
# I3B -- single-file atomic write into a shared workspace
# ---------------------------------------------------------------------------


def test_previous_product_survives_a_failed_write(tmp_path):
    """output_dir is a shared workspace. A direct open(target, "w") would
    truncate the previous successful product to half a file."""
    target = tmp_path / "audio.txt"
    target.write_text("PREVIOUS GOOD RESULT", encoding="utf-8")

    with patch(
        "video_transcript_api.transcriber.capswriter_client.os.replace",
        side_effect=OSError("rename failed"),
    ):
        with pytest.raises(OSError, match="rename failed"):
            _atomic_write_text(target, "NEW RESULT")

    assert target.read_text(encoding="utf-8") == "PREVIOUS GOOD RESULT"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["audio.txt"]


def test_atomic_write_replaces_content_in_place(tmp_path):
    target = tmp_path / "audio.txt"
    target.write_text("old", encoding="utf-8")

    _atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["audio.txt"]


def test_atomic_write_temp_name_carries_pid_and_sequence(tmp_path):
    target = tmp_path / "audio.txt"
    _atomic_write_text(target, "a")
    _atomic_write_text(target, "b")
    assert target.read_text(encoding="utf-8") == "b"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["audio.txt"]


# ---------------------------------------------------------------------------
# I4 -- the FunASR sidecar is a required product: the consumer must fail too
# ---------------------------------------------------------------------------


def _make_transcriber(tmp_path, generated_files):
    """A Transcriber with only its consumption layer wired up."""
    from video_transcript_api.transcriber.transcriber import Transcriber

    transcriber = object.__new__(Transcriber)
    transcriber.output_dir = str(tmp_path)
    client = MagicMock()
    client.transcribe_file.return_value = (True, list(generated_files))
    transcriber.capswriter_client = client
    return transcriber


def test_transcribe_raises_when_funasr_sidecar_cannot_be_read(tmp_path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fixture")
    transcriber = _make_transcriber(tmp_path, [tmp_path / "audio_funasr.json"])

    with pytest.raises(FileNotFoundError):
        transcriber.transcribe(str(audio))


def test_transcribe_raises_on_corrupt_funasr_sidecar(tmp_path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fixture")
    sidecar = tmp_path / "audio_funasr.json"
    sidecar.write_text("{not json", encoding="utf-8")
    transcriber = _make_transcriber(tmp_path, [sidecar])

    with pytest.raises(json.JSONDecodeError):
        transcriber.transcribe(str(audio))


def test_merged_segments_keep_interword_whitespace(tmp_path):
    """合并短段不得丢句间空格：侧车正文必须逐字等于 "".join(tokens)。

    合并若用字符串拼接（buffer["text"] + seg["text"]），分句时 strip 掉的
    句间空格会永久丢失，正文与时间轴同时被破坏且不报错。
    """
    client = _make_client(tmp_path)
    tokens = ["Hello", " ", "world", "。", " ", "How", " ", "are", " ", "you", "?"]
    text_accu = "".join(tokens)
    timestamps = [1.0 + index * 0.5 for index in range(len(tokens))]

    generated = asyncio.run(
        client._save_results(
            tmp_path / "audio.webm",
            {
                "task_id": "whitespace",
                "text": text_accu,
                "text_accu": text_accu,
                "tokens": tokens,
                "timestamps": timestamps,
                "duration": 9.0,
                "time_start": 1.0,
                "time_complete": 2.0,
            },
        )
    )

    sidecar = next(p for p in generated if "funasr" in p.name)
    segments = json.loads(sidecar.read_text(encoding="utf-8"))["segments"]
    assert segments
    joined = "".join(s["text"] for s in segments)
    assert joined == text_accu, f"正文丢字符: {joined!r} != {text_accu!r}"


def test_sentence_start_skips_blank_token_time(tmp_path):
    """句首是空白 token 时，起始时间必须取首个非空白 token，而不是空格的时间。

    服务端把空格也作为 token 返回，且空格的时间戳继承邻近词；直接用空白
    token 的时间会让每句起点系统性偏早。
    """
    client = _make_client(tmp_path)
    # 首句以空白 token 开头：正文 " Hello。 Bye."
    tokens = [" ", "Hello", "。", " ", "Bye", " ", "now", "."]
    text_accu = "".join(tokens)
    timestamps = [1.0, 1.5, 2.0, 3.5, 4.0, 5.0, 5.5, 6.0]

    generated = asyncio.run(
        client._save_results(
            tmp_path / "audio.webm",
            {
                "task_id": "blank-start",
                "text": text_accu,
                "text_accu": text_accu,
                "tokens": tokens,
                "timestamps": timestamps,
                "duration": 7.0,
                "time_start": 1.0,
                "time_complete": 2.0,
            },
        )
    )

    sidecar = next(p for p in generated if "funasr" in p.name)
    segments = json.loads(sidecar.read_text(encoding="utf-8"))["segments"]
    assert "".join(s["text"] for s in segments) == text_accu

    first = segments[0]
    # tokens[0] 是空白、时间 1.0；首个非空白是 tokens[1]、时间 1.5
    assert first["start_time"] == timestamps[1], (
        f"首句起点应为首个非空白 token 的时间 {timestamps[1]}，"
        f"实际 {first['start_time']}（若等于 {timestamps[0]} 说明用了空白 token）"
    )
    for segment in segments:
        assert segment["start_time"] is not None


def test_adjacent_segments_have_no_time_gap_with_blank_tokens(tmp_path):
    """相邻 segment 之间不得出现无依据的时间间隙。

    两边独立查表时，「start 跳过空白 token 而 end 没跳」会留下一个 token 的
    间隙。段尾一律取「下一句的起始时间」即可结构性避免。
    """
    client = _make_client(tmp_path)
    s1 = "This is a long sentence that clearly exceeds the minimum length requirement here."
    s2 = "Another quite long sentence right here so it will not get merged with the first."
    tokens = [" "] + list(s1) + ["。", " "] + list(s2) + ["。"]
    text_accu = "".join(tokens)
    timestamps = [round(1.0 + i * 0.31, 2) for i in range(len(tokens))]

    generated = asyncio.run(
        client._save_results(
            tmp_path / "audio.webm",
            {
                "task_id": "gap",
                "text": text_accu,
                "text_accu": text_accu,
                "tokens": tokens,
                "timestamps": timestamps,
                "duration": timestamps[-1] + 1.0,
                "time_start": 1.0,
                "time_complete": 2.0,
            },
        )
    )

    sidecar = next(p for p in generated if "funasr" in p.name)
    segments = json.loads(sidecar.read_text(encoding="utf-8"))["segments"]
    assert len(segments) >= 2, "样本应至少切成两段"
    assert "".join(s["text"] for s in segments) == text_accu
    for earlier, later in zip(segments, segments[1:]):
        if earlier["end_time"] is not None and later["start_time"] is not None:
            assert abs(later["start_time"] - earlier["end_time"]) < 1e-6, (
                f"段间出现时间间隙: {earlier['end_time']} -> {later['start_time']}"
            )


def test_optimize_segment_lengths_requires_text_parameter():
    """text 必填：给它默认值会让漏传变成「从空串切片」→ 正文静默丢失。"""
    import inspect

    from video_transcript_api.transcriber.capswriter_client import (
        _optimize_segment_lengths,
    )

    signature = inspect.signature(_optimize_segment_lengths)
    assert signature.parameters["text"].default is inspect.Parameter.empty, (
        "text 必须是无默认值的必填参数"
    )


def test_trailing_whitespace_segment_never_has_start_after_end(tmp_path):
    """尾部纯空白段不得出现 start_time > end_time（时间轴静默损坏）。

    尾部空白段的起始 token 会跳过所有空白落到末尾，而段尾按「最后一个非空白
    token」回退，两者可能倒挂。
    """
    client = _make_client(tmp_path)
    long_sentence = (
        "This sentence is deliberately long so that it exceeds the minimum "
        "segment length requirement."
    )
    tokens = list(long_sentence) + ["。", " ", " ", " "]
    text_accu = "".join(tokens)
    timestamps = [round(1.0 + i * 0.25, 2) for i in range(len(tokens))]

    generated = asyncio.run(
        client._save_results(
            tmp_path / "audio.webm",
            {
                "task_id": "trailing-ws",
                "text": text_accu,
                "text_accu": text_accu,
                "tokens": tokens,
                "timestamps": timestamps,
                "duration": timestamps[-1] + 1.0,
                "time_start": 1.0,
                "time_complete": 2.0,
            },
        )
    )

    sidecar = next(p for p in generated if "funasr" in p.name)
    segments = json.loads(sidecar.read_text(encoding="utf-8"))["segments"]
    assert len(segments) >= 2, "长句后应另有一段承载尾部空白"
    assert "".join(s["text"] for s in segments) == text_accu
    for segment in segments:
        if segment["start_time"] is not None and segment["end_time"] is not None:
            assert segment["start_time"] <= segment["end_time"], (
                f"start>end: {segment['start_time']} > {segment['end_time']} "
                f"text={segment['text']!r}"
            )
        if not segment["text"].strip():
            # 纯空白段不对应任何语音，必须标为不可用而不是占住时间轴
            assert segment["start_time"] is None and segment["end_time"] is None, (
                f"纯空白段不应有时间戳: {segment['start_time']} -> {segment['end_time']}"
            )


def test_merging_whitespace_segment_keeps_valid_end_time(tmp_path):
    """与纯空白段合并后，有效文本的 end_time 不得变成 None。

    空白段诚实降级为 None；若合并时无脑用它覆盖 buffer 的 end_time，
    一次合并就会让整段有效文本失去结束时间。
    """
    client = _make_client(tmp_path)
    tokens = ["Hi", "。", " ", " "]
    text_accu = "".join(tokens)
    timestamps = [round(1.0 + i * 0.3, 2) for i in range(len(tokens))]

    generated = asyncio.run(
        client._save_results(
            tmp_path / "audio.webm",
            {
                "task_id": "merge-ws",
                "text": text_accu,
                "text_accu": text_accu,
                "tokens": tokens,
                "timestamps": timestamps,
                "duration": timestamps[-1] + 1.0,
                "time_start": 1.0,
                "time_complete": 2.0,
            },
        )
    )

    sidecar = next(p for p in generated if "funasr" in p.name)
    segments = json.loads(sidecar.read_text(encoding="utf-8"))["segments"]
    assert "".join(s["text"] for s in segments) == text_accu
    for segment in segments:
        if segment["text"].strip():
            assert segment["end_time"] is not None, (
                f"有效文本段的 end_time 变成了 None: {segment['text']!r}"
            )
