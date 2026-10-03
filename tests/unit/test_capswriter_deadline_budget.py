"""转录时限预算按实测吞吐计算（issue #155）。

SDK 自动预算是 ``max(120, duration + 60)``，本仓在拿得到时长时显式传 ``deadline_total = duration*4 + 120``；
拿不到时长时不传该键回退 SDK 自动预算。每个用例都断言真正传给 ``transcribe_file_sync`` 的实参。
"""

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from capswriter_asr import Transcript
from loguru import logger as loguru_logger

from video_transcript_api.downloaders.base import BaseDownloader
from video_transcript_api.transcriber.capswriter_client import (
    CapsWriterClient,
    Config,
)
from video_transcript_api.transcriber.transcriber import Transcriber


class _ProbeDownloader(BaseDownloader):
    def can_handle(self, url):
        return True

    def extract_video_id(self, url):
        return "fixture"

    def _fetch_metadata(self, url, video_id):
        raise NotImplementedError

    def _fetch_download_info(self, url, video_id):
        raise NotImplementedError

    def get_subtitle(self, url):
        return None


def _probe_ok(duration=None):
    """造一次成功探测的返回值；``duration=None`` 表示没有 duration 字段。"""
    fmt = {} if duration is None else {"duration": str(duration)}
    payload = {"streams": [{"codec_type": "audio"}], "format": fmt}
    return MagicMock(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")


def _make_client(output_dir: Path) -> CapsWriterClient:
    with patch.object(CapsWriterClient, "__init__", lambda self: None):
        client = CapsWriterClient()
    client.output_dir = str(output_dir)
    client.max_retries = 1
    client.retry_delay = 0
    client.log = MagicMock()
    return client


def _patch_sdk():
    raw = {"task_id": "task-1", "time_start": 10.0, "time_complete": 12.5, "text_accu": "hello!"}
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
    """探测与 SDK 调用相关的配置，其余走默认值。"""
    monkeypatch.setattr(Config, "server_addr", "test-server")
    monkeypatch.setattr(Config, "server_port", 6010)
    monkeypatch.setattr(Config, "generate_funasr_compat", False)


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"fixture")
    return path


def test_probed_duration_reaches_sdk_deadline_kwarg(tmp_path, sdk_config, audio):
    """真实链路：探测到的 600 秒 → SDK 收到 600*4+120=2520。"""
    out = tmp_path / "out"
    out.mkdir()
    downloader = _ProbeDownloader()
    seen = []

    def _run(cmd, **kwargs):
        seen.append(cmd)
        return _probe_ok(600)

    with patch("subprocess.run", side_effect=_run):
        assert downloader._validate_media_file(str(audio)) is True

    assert downloader.last_media_duration == 600.0
    assert len(seen) == 1, "探测次数不得因本卡增加"

    # 服务层就是这个读取动作
    media_duration = getattr(downloader, "last_media_duration", None)
    assert media_duration == 600.0

    with _patch_sdk() as sdk_call:
        success, _ = _make_client(out).transcribe_file(
            str(audio), media_duration=media_duration
        )

    assert success is True
    assert sdk_call.call_args.kwargs["deadline_total"] == 2520.0


def test_duration_flows_through_transcriber_layer(tmp_path, sdk_config, audio):
    """时长穿过 Transcriber 层后实参仍按公式算出，不是恒定值。"""
    out = tmp_path / "out"
    out.mkdir()
    transcriber = Transcriber.__new__(Transcriber)
    transcriber.output_dir = str(out)
    transcriber.capswriter_client = _make_client(out)

    with _patch_sdk() as sdk_call:
        transcriber.transcribe(str(audio), "out_base", media_duration=600.0)

    assert sdk_call.call_args.kwargs["deadline_total"] == 2520.0


@pytest.mark.parametrize(
    "duration,expected",
    [
        (0.0, 120.0), (1.0, 124.0), (30.0, 240.0), (45.0, 300.0),
        (45.1, 300.4), (93.08898, 492.35592), (600.0, 2520.0), (3600.0, 14520.0),
    ],
)
def test_known_duration_formula(tmp_path, sdk_config, audio, duration, expected):
    with _patch_sdk() as sdk_call:
        _make_client(tmp_path).transcribe_file(str(audio), media_duration=duration)

    assert sdk_call.call_args.kwargs["deadline_total"] == pytest.approx(expected)


@pytest.mark.parametrize(
    "media_duration",
    [None, -1.0, float("nan"), float("inf")],
    ids=["none", "negative", "nan", "inf"],
)
def test_unknown_duration_omits_deadline_kwarg(
    tmp_path, sdk_config, audio, media_duration
):
    """时长不可用时 kwargs 不含 deadline_total 键（不是 300，也不是 None）。"""
    with _patch_sdk() as sdk_call:
        _make_client(tmp_path).transcribe_file(str(audio), media_duration=media_duration)

    assert "deadline_total" not in sdk_call.call_args.kwargs


def test_unknown_duration_emits_greppable_log(tmp_path, sdk_config, audio):
    """缺失路径必须留一行可 grep 的日志，且不得写成 value=300。"""
    records = []
    sink_id = loguru_logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        with _patch_sdk():
            _make_client(tmp_path).transcribe_file(str(audio))
    finally:
        loguru_logger.remove(sink_id)

    hits = [line for line in records if "transcription_deadline" in line]
    assert hits, f"缺少 transcription_deadline 日志，实际记录: {records}"
    assert any("duration=unknown" in l and "fallback=sdk_auto" in l for l in hits)
    assert not any("value=300" in l for l in hits)


def test_probe_state_is_not_fabricated_or_leaked(tmp_path):
    """上一次文件的时长不得泄漏；探测不到 duration 时也不得编造小值。"""
    first, second, third = (tmp_path / n for n in ("a.mp4", "b.mp4", "c.mp4"))
    for path in (first, second, third):
        path.write_bytes(b"x")
    downloader = _ProbeDownloader()

    with patch("subprocess.run", side_effect=lambda *a, **k: _probe_ok(600)):
        assert downloader._validate_media_file(str(first)) is True
    assert downloader.last_media_duration == 600.0

    # 第二个文件探测失败，时长必须回到 None
    failed = MagicMock(returncode=1, stdout=b"", stderr=b"boom")
    with patch("subprocess.run", side_effect=lambda *a, **k: failed):
        assert downloader._validate_media_file(str(second)) is False
    assert downloader.last_media_duration is None

    # 第三个文件探测成功但 format 里没有 duration，同样保持 None
    with patch("subprocess.run", side_effect=lambda *a, **k: _probe_ok(None)):
        assert downloader._validate_media_file(str(third)) is True
    assert downloader.last_media_duration is None


def test_unprobed_download_clears_stale_duration(audio):
    """同一个实例先走探测拿到 duration，再走未探测的下载路径，必须被清零为 None。"""
    downloader = _ProbeDownloader()
    with patch("subprocess.run", return_value=_probe_ok(600)):
        assert downloader._validate_media_file(str(audio)) is True
    assert downloader.last_media_duration == 600.0

    mock_resp = MagicMock(status_code=403)
    mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_resp)
    with patch("requests.get", return_value=mock_resp):
        assert downloader.download_file("https://example.com/v", "v.mp4") is None
    assert downloader.last_media_duration is None


def test_base_download_file_does_not_add_probe_calls():
    """锁定决策 2：基类探测调用点数量不因本卡增加。"""
    src = inspect.getsource
    assert src(BaseDownloader.download_file).count("_validate_media_file") == 1
    assert src(BaseDownloader._validate_media_file).count('"ffprobe"') == 1
    assert _ProbeDownloader().last_media_duration is None