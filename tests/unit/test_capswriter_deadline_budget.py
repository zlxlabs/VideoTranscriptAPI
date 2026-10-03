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
    _transcription_deadline,
)
from video_transcript_api.transcriber.transcriber import Transcriber


class _ProbeDownloader(BaseDownloader):
    def can_handle(self, url): return True
    def extract_video_id(self, url): return "fixture"
    def _fetch_metadata(self, url, video_id): raise NotImplementedError
    def _fetch_download_info(self, url, video_id): raise NotImplementedError
    def get_subtitle(self, url): return None


def _probe_ok(duration=None):
    fmt = {} if duration is None else {"duration": str(duration)}
    return MagicMock(returncode=0, stdout=json.dumps({"streams": [{"codec_type": "audio"}], "format": fmt}).encode(), stderr=b"")


def _make_client(output_dir: Path) -> CapsWriterClient:
    with patch.object(CapsWriterClient, "__init__", lambda self: None):
        client = CapsWriterClient()
    client.output_dir, client.max_retries, client.retry_delay, client.log = str(output_dir), 1, 0, MagicMock()
    return client


def _patch_sdk():
    raw = {"task_id": "task-1", "time_start": 10.0, "time_complete": 12.5, "text_accu": "hello!"}
    transcript = Transcript(text="hallo", tokens=list("hello!"), timestamps=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5], duration=1.5, raw=raw)
    return patch("video_transcript_api.transcriber.capswriter_client.transcribe_file_sync", return_value=transcript)


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


def test_probed_duration_reaches_sdk_deadline_kwarg(tmp_path, sdk_config, audio):
    out = tmp_path / "out"
    out.mkdir()
    downloader = _ProbeDownloader()
    seen = []
    with patch("subprocess.run", side_effect=lambda c, **k: (seen.append(c), _probe_ok(600))[1]):
        assert downloader._validate_media_file(str(audio)) is True
    assert downloader.last_media_duration == 600.0 and len(seen) == 1
    media_duration = getattr(downloader, "last_media_duration", None)
    assert media_duration == 600.0
    with _patch_sdk() as sdk_call:
        success, _ = _make_client(out).transcribe_file(str(audio), media_duration=media_duration)
    assert success is True and sdk_call.call_args.kwargs["deadline_total"] == 2520.0


def test_duration_flows_through_transcriber_layer(tmp_path, sdk_config, audio):
    out = tmp_path / "out"
    out.mkdir()
    transcriber = Transcriber.__new__(Transcriber)
    transcriber.output_dir, transcriber.capswriter_client = str(out), _make_client(out)
    with _patch_sdk() as sdk_call:
        transcriber.transcribe(str(audio), "out_base", media_duration=600.0)
    assert sdk_call.call_args.kwargs["deadline_total"] == 2520.0


@pytest.mark.parametrize("duration,expected", [
    (0.0, 120.0), (1.0, 124.0), (30.0, 240.0), (45.0, 300.0),
    (45.1, 300.4), (93.08898, 492.35592), (600.0, 2520.0), (3600.0, 14520.0),
])
def test_known_duration_formula(tmp_path, sdk_config, audio, duration, expected):
    with _patch_sdk() as sdk_call:
        _make_client(tmp_path).transcribe_file(str(audio), media_duration=duration)
    assert sdk_call.call_args.kwargs["deadline_total"] == pytest.approx(expected)


@pytest.mark.parametrize("media_duration", [None, -1.0, float("nan"), float("inf"), float("-inf")])
def test_unknown_duration_omits_deadline_kwarg(tmp_path, sdk_config, audio, media_duration):
    with _patch_sdk() as sdk_call:
        _make_client(tmp_path).transcribe_file(str(audio), media_duration=media_duration)
    assert "deadline_total" not in sdk_call.call_args.kwargs


def test_transcription_deadline_math_equivalence():
    """锁住链式比较与 math.isfinite 逐值等价，覆盖 nan, ±inf, 负数与正常值。"""
    assert _transcription_deadline(None) is None
    assert _transcription_deadline(float("nan")) is None
    assert _transcription_deadline(float("inf")) is None
    assert _transcription_deadline(float("-inf")) is None
    assert _transcription_deadline(-1.0) is None
    assert _transcription_deadline(93.08898) == pytest.approx(492.35592)
    assert _transcription_deadline(600.0) == 2520.0


def test_unknown_duration_emits_greppable_log(tmp_path, sdk_config, audio):
    records = []
    sink_id = loguru_logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        with _patch_sdk():
            _make_client(tmp_path).transcribe_file(str(audio))
    finally:
        loguru_logger.remove(sink_id)
    hits = [line for line in records if "transcription_deadline" in line]
    assert hits and any("duration=unknown" in l and "fallback=sdk_auto" in l for l in hits)
    assert not any("value=300" in l for l in hits)


def test_probe_state_is_not_fabricated_or_leaked(tmp_path):
    first, second, third = (tmp_path / n for n in ("a.mp4", "b.mp4", "c.mp4"))
    for path in (first, second, third): path.write_bytes(b"x")
    downloader = _ProbeDownloader()
    with patch("subprocess.run", return_value=_probe_ok(600)):
        assert downloader._validate_media_file(str(first)) is True
    assert downloader.last_media_duration == 600.0
    with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout=b"", stderr=b"boom")):
        assert downloader._validate_media_file(str(second)) is False
    assert downloader.last_media_duration is None
    with patch("subprocess.run", return_value=_probe_ok(None)):
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


def test_generic_overridden_download_leaves_duration_none(tmp_path):
    """覆写 download_file 的 GenericDownloader 不调基类探测，last_media_duration 保持 None。"""
    from video_transcript_api.downloaders.generic import GenericDownloader
    downloader = GenericDownloader()
    assert downloader.last_media_duration is None
    out_file = tmp_path / "test.mp4"
    out_file.write_bytes(b"dummy")
    with patch.object(GenericDownloader, "download_file", return_value=str(out_file)):
        assert downloader.download_file("https://example.com/v.mp4", "test.mp4") == str(out_file)
    assert downloader.last_media_duration is None


def test_transcription_reads_duration_from_actual_downloader(monkeypatch):
    """当 download_downloader 为 None 时，media_duration 取自 create_downloader 新建的实例。"""
    from video_transcript_api.api.services import transcription as tx
    fake_dl = _ProbeDownloader.__new__(_ProbeDownloader)
    fake_dl._metadata_cache, fake_dl._download_info_cache, fake_dl.last_media_duration = {}, {}, 600.0
    fake_dl.get_download_info = lambda u: MagicMock(downloaded=False, local_file=None, download_url="http://x/v.mp4", filename="v.mp4")
    fake_dl.get_subtitle = lambda u: None
    fake_dl.download_file = MagicMock(return_value="/tmp/fake.mp4")

    captured = []
    class _FakeTranscriber:
        def transcribe(self, p, b=None, **k):
            captured.append(k.get("media_duration"))
            return {"transcript": "ok", "success": True}

    # #159: this test owns the duration budget only and its downloader double
    # returns a placeholder path ("/tmp/fake.mp4"). Declare an isolated probe
    # stand-in so real admission does not hijack the assertion; the admission
    # truth boundary lives in tests/unit/test_transcription_audio_admission.py.
    monkeypatch.setattr(tx, "_ensure_audio_track", lambda path: None)
    monkeypatch.setattr(tx, "get_temp_manager", lambda: MagicMock())
    monkeypatch.setattr(tx, "create_downloader", lambda u: fake_dl)
    monkeypatch.setattr(tx, "Transcriber", _FakeTranscriber)
    monkeypatch.setattr(tx, "cache_manager", MagicMock(get_cache=lambda *a, **k: None, save_cache=lambda *a, **k: True, update_task_status=lambda *a, **k: True, is_terminal_notification_pending=lambda *a: False, list_unattempted_terminal_notifications=lambda *a: [], get_task_by_id=lambda *a, **k: {}))
    monkeypatch.setattr(tx, "llm_task_queue", MagicMock())
    monkeypatch.setattr(tx, "get_notification_router", lambda: MagicMock())

    res = tx.process_transcription("t1", "https://example.com/test.mp4")
    assert res.get("status") == "success"
    assert captured == [600.0]


def test_base_download_file_does_not_add_probe_calls():
    """锁定决策 2：基类探测调用点数量不因本卡增加。"""
    src = inspect.getsource
    assert src(BaseDownloader.download_file).count("_validate_media_file") == 1
    assert src(BaseDownloader._validate_media_file).count('"ffprobe"') == 1
    assert _ProbeDownloader().last_media_duration is None