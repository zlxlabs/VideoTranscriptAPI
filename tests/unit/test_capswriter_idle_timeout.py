"""上传阶段单帧空闲上限按媒体时长计算（issue #181）。

生产实测：17502 秒 / 494 MB 的直播录制在 ASR 上传阶段两次同因失败，
``AsrError("timeout", "发送音频帧超过 idle_timeout")``——SDK 默认
``idle_timeout=300.0`` 与媒体长度无关。本仓在拿得到时长时显式传
``idle_timeout = duration/3 + 300``（可证上界：单次发送停顿不超过服务端读完
剩余积压所需的时间），拿不到时长时不传该键，逐字回退 SDK 默认。

每个用例都断言真正传给 ``transcribe_file_sync`` 的实参（跨 SDK 入口的边界，
不是只测本仓计算函数的返回值）。
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from capswriter_asr import Transcript
from loguru import logger as loguru_logger

from video_transcript_api.transcriber.capswriter_client import (
    IDLE_OVERHEAD_SECONDS,
    IDLE_REALTIME_FACTOR,
    CapsWriterClient,
    Config,
    _asr_idle_timeout,
)

# SDK 侧写死的默认值，生产两次失败就是撞在它上面（capswriter_asr/client.py:498）。
SDK_DEFAULT_IDLE_TIMEOUT = 300.0


def _make_client(output_dir: Path) -> CapsWriterClient:
    with patch.object(CapsWriterClient, "__init__", lambda self: None):
        client = CapsWriterClient()
    client.output_dir = str(output_dir)
    client.max_retries = 1
    client.retry_delay = 0
    client.log = MagicMock()
    return client


def _patch_sdk():
    raw = {
        "task_id": "task-1",
        "time_start": 10.0,
        "time_complete": 12.5,
        "text_accu": "hello!",
    }
    transcript = Transcript(
        text="hallo",
        tokens=list("hello!"),
        timestamps=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5],
        duration=1.5,
        raw=raw,
    )
    return patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        return_value=transcript,
    )


@pytest.fixture
def sdk_config(monkeypatch):
    monkeypatch.setattr(Config, "server_addr", "test-server")
    monkeypatch.setattr(Config, "server_port", 6010)
    monkeypatch.setattr(Config, "generate_funasr_compat", False)


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"fixture")
    return path


def test_known_duration_passes_scaled_idle_timeout(tmp_path, sdk_config, audio):
    """有时长时：idle_timeout 键出现在 SDK 实参里，取值按公式算。"""
    with _patch_sdk() as sdk_call:
        success, _ = _make_client(tmp_path).transcribe_file(
            str(audio), media_duration=600.0
        )
    assert success is True
    keywords = sdk_call.call_args.kwargs
    assert "idle_timeout" in keywords, sorted(keywords)
    assert keywords["idle_timeout"] == pytest.approx(600.0 / 3 + 300.0)
    # 与 deadline_total 同属一次预算，两个键互不覆盖。
    assert keywords["deadline_total"] == pytest.approx(600.0 * 4 + 120.0)


def test_production_failure_size_clears_sdk_default(tmp_path, sdk_config, audio):
    """生产失败的那个尺寸：17502 秒必须拿到远大于 SDK 默认 300 秒的上限。"""
    with _patch_sdk() as sdk_call:
        _make_client(tmp_path).transcribe_file(str(audio), media_duration=17502.0)
    keywords = sdk_call.call_args.kwargs
    assert "idle_timeout" in keywords, sorted(keywords)
    idle_timeout = keywords["idle_timeout"]
    assert idle_timeout == pytest.approx(17502.0 / 3 + 300.0)
    assert idle_timeout > SDK_DEFAULT_IDLE_TIMEOUT


@pytest.mark.parametrize("media_duration", [None, -1.0, float("nan"), float("inf"), float("-inf")])
def test_unknown_duration_omits_idle_timeout_kwarg(
    tmp_path, sdk_config, audio, media_duration
):
    """无时长时：不传该键，SDK 用自己的默认 300.0 秒，行为与修复前逐字一致。"""
    with _patch_sdk() as sdk_call:
        _make_client(tmp_path).transcribe_file(str(audio), media_duration=media_duration)
    assert "idle_timeout" not in sdk_call.call_args.kwargs


@pytest.mark.parametrize(
    "duration,expected",
    [
        (0.0, 300.0),
        (1.0, 300.3333333),
        (30.0, 310.0),
        (600.0, 500.0),
        (3600.0, 1500.0),
        (17502.0, 6134.0),
    ],
)
def test_known_duration_formula(duration, expected):
    assert _asr_idle_timeout(duration) == pytest.approx(expected)


def test_constants_match_documented_formula():
    assert (IDLE_REALTIME_FACTOR, IDLE_OVERHEAD_SECONDS) == (3.0, 300.0)


@pytest.mark.parametrize(
    "media_duration", [None, -1.0, float("nan"), float("inf"), float("-inf")]
)
def test_unknown_duration_returns_none(media_duration):
    assert _asr_idle_timeout(media_duration) is None


def test_unknown_duration_emits_greppable_log(tmp_path, sdk_config, audio):
    records = []
    sink_id = loguru_logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        with _patch_sdk():
            _make_client(tmp_path).transcribe_file(str(audio))
    finally:
        loguru_logger.remove(sink_id)
    hits = [line for line in records if "asr_idle_timeout" in line]
    assert hits and any(
        "duration=unknown" in line and "fallback=sdk_default_300" in line
        for line in hits
    )


def test_upload_timeout_still_surfaces_code_and_reason(tmp_path, sdk_config, audio):
    """#171 的行为不许回归：上传超时时通知侧仍能看到 code=timeout 与原因。"""
    import capswriter_asr

    boom = capswriter_asr.AsrError("timeout", "发送音频帧超过 idle_timeout")
    client = _make_client(tmp_path)
    with patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        side_effect=boom,
    ):
        success, _ = client.transcribe_file(str(audio), media_duration=17502.0)
    assert success is False
    assert "code=timeout" in client.last_failure_detail
    assert "发送音频帧超过 idle_timeout" in client.last_failure_detail