import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from video_transcript_api.transcriber import capswriter_client as caps_mod
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
    client = _make_client(tmp_path)
    result = _valid_result()
    real_write = caps_mod._write_temp
    calls = {"n": 0}

    def flaky_write(target: Path, content: str) -> Path:
        # 第一个产物（txt）的临时文件写成功，第二个（funasr 侧车）失败
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("disk full")
        return real_write(target, content)

    monkeypatch.setattr(caps_mod, "_write_temp", flaky_write)

    with pytest.raises(OSError):
        asyncio.run(client._save_results(tmp_path / "audio.webm", result))

    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == [], f"写盘失败后残留产物: {leftovers}"


def test_oversized_segments_are_split_without_string_concatenation():
    """超长段落必须被切开，且不得靠拼接字符串实现（会丢句间空格）。"""
    from video_transcript_api.transcriber.capswriter_client import (
        Config,
        _create_segments_from_capswriter,
    )

    sentence = "这是一个很长的句子用来测试超长段落是否会被切开"
    long_text = "。".join([sentence] * 8) + "。"
    tokens = list(long_text)
    timestamps = [index * 0.1 for index in range(len(tokens))]

    previous = Config.max_segment_length
    try:
        Config.max_segment_length = 40
        segments = _create_segments_from_capswriter(
            text=long_text, tokens=tokens, timestamps=timestamps
        )
    finally:
        Config.max_segment_length = previous

    assert len(segments) > 1, "超长段落未被切开"
    assert max(s["length"] for s in segments) <= 40, [s["length"] for s in segments]
    # 切分不得丢文本（若用字符串拼接会出现分隔符丢失）
    assert "".join(s["text"] for s in segments) == long_text
    for earlier, later in zip(segments, segments[1:]):
        assert earlier["end_time"] <= later["start_time"], "切分后时间轴出现空洞"


def test_failed_write_preserves_previous_successful_artifacts(tmp_path, monkeypatch):
    """写盘失败绝不能删除上一次成功转录的产物（真实数据丢失）。"""
    client = _make_client(tmp_path)
    result = _valid_result()
    asyncio.run(client._save_results(tmp_path / "audio.webm", result))

    before = {p.name: p.read_text() for p in tmp_path.iterdir()}
    assert before, "第一次转录应当产出文件"

    real_write = caps_mod._write_temp
    calls = {"n": 0}

    def flaky_write(target: Path, content: str) -> Path:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("disk full")
        return real_write(target, content)

    monkeypatch.setattr(caps_mod, "_write_temp", flaky_write)

    with pytest.raises(OSError):
        asyncio.run(client._save_results(tmp_path / "audio.webm", result))

    after = {p.name: p.read_text() for p in tmp_path.iterdir()}
    assert set(before) <= set(after), f"已有产物被删除: {set(before) - set(after)}"
    for name, content in before.items():
        assert after[name] == content, f"已有产物被破坏: {name}"
    assert not [k for k in after if k.endswith(".tmp")], f"残留临时文件: {after.keys()}"


def test_commit_stage_failure_never_deletes_existing_artifacts(tmp_path, monkeypatch):
    """就位阶段失败绝不能删除已存在的产物（数据丢失红线）。

    刻意不做跨文件回滚：备份+回滚在备份阶段中途失败时会把「尚未备份」的
    上一次成功产物删掉——风险大于跨文件一致性的收益。因此这里断言的是
    「不删除」，而不是「回滚到旧版本」。
    """
    client = _make_client(tmp_path)
    result = _valid_result()
    asyncio.run(client._save_results(tmp_path / "audio.webm", result))
    before = {p.name: p.read_text() for p in tmp_path.iterdir()}
    assert before

    original_replace = Path.replace
    calls = {"n": 0}

    def flaky_replace(self, target):
        if self.name.endswith(".tmp"):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("rename failed")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    with pytest.raises(OSError):
        asyncio.run(client._save_results(tmp_path / "audio.webm", result))

    after = {p.name: p.read_text() for p in tmp_path.iterdir()}
    assert set(before) <= set(after), f"已存在产物被删除: {set(before) - set(after)}"
    assert not [k for k in after if k.endswith((".tmp", ".bak"))], sorted(after)


def test_half_written_temp_file_is_cleaned_immediately(tmp_path, monkeypatch):
    """写 .tmp 过程中失败必须就地清理（它不会进入 pending 列表）。"""
    import builtins as _builtins

    client = _make_client(tmp_path)
    real_open = _builtins.open

    def flaky_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        if str(file).endswith(".tmp") and "w" in mode:
            handle.write("半截")
            handle.flush()
            handle.close()
            raise OSError("disk full during temp write")
        return handle

    monkeypatch.setattr(_builtins, "open", flaky_open)

    with pytest.raises(OSError):
        asyncio.run(client._save_results(tmp_path / "audio.webm", _valid_result()))

    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == [], f"写盘失败后残留: {leftovers}"


def test_oversized_segment_without_secondary_punctuation_is_still_split():
    """没有次级标点的超长段也必须受 max_len 约束（否则会输出超长片段）。"""
    from video_transcript_api.transcriber.capswriter_client import (
        Config,
        _create_segments_from_capswriter,
    )

    body = "词" * 800  # 无任何标点
    tokens = list(body)
    timestamps = [index * 0.1 for index in range(len(tokens))]

    previous = Config.max_segment_length
    try:
        Config.max_segment_length = 300
        segments = _create_segments_from_capswriter(
            text=body, tokens=tokens, timestamps=timestamps
        )
    finally:
        Config.max_segment_length = previous

    assert len(segments) > 1, "无标点超长段未被切开"
    assert max(s["length"] for s in segments) <= 300
    assert "".join(s["text"] for s in segments) == body


def test_each_artifact_is_reported_exactly_once(tmp_path, monkeypatch):
    """generate_json 打开时，产物列表不得出现重复项。"""
    from video_transcript_api.transcriber import capswriter_client as caps_mod

    monkeypatch.setattr(caps_mod.Config, "generate_json", True)
    client = _make_client(tmp_path)
    files = asyncio.run(client._save_results(tmp_path / "audio.webm", _valid_result()))
    names = [p.name for p in files]
    assert len(names) == len(set(names)), f"产物列表重复: {names}"
    for name in names:
        assert list(tmp_path.iterdir()).count(tmp_path / name) == 1


def test_segment_with_multiple_secondary_punct_is_further_split():
    """逗号间隔本身超过 max_len 时，仍必须切到上限以内。"""
    from video_transcript_api.transcriber.capswriter_client import (
        Config,
        _create_segments_from_capswriter,
    )

    body = ("词" * 500 + ",") * 3
    tokens = list(body)
    timestamps = [index * 0.1 for index in range(len(tokens))]

    previous = Config.max_segment_length
    try:
        Config.max_segment_length = 300
        segments = _create_segments_from_capswriter(
            text=body, tokens=tokens, timestamps=timestamps
        )
    finally:
        Config.max_segment_length = previous

    assert max(s["length"] for s in segments) <= 300, max(
        s["length"] for s in segments
    )
    assert "".join(s["text"] for s in segments) == body
