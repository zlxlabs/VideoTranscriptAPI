"""#155：转录时限预算的时长在 generic / recorder:// 路径上拿不到（修复守卫）。

缺陷：``media_duration`` 只有一个来源 ``downloader.last_media_duration``，而它
只在 ``BaseDownloader._validate_media_file`` 里赋值。generic 下载器自实现
``download_file``（自实现下载 + 断点续传），从不调用那个函数，于是 recorder://
与一切落到 generic 的 URL 上该属性恒为 ``None``，预算恒走 SDK 自动分支（生产
日志里那条 ``transcription_deadline duration=unknown fallback=sdk_auto``）。
修复：把音轨准入（issue #159）**同一次** ffprobe 探测已经解析出的
``format.duration`` 交出来（``_ensure_audio_track`` 的返回值），不新增任何探测。

本文件锁的不变量（走真实入口 ``process_transcription``，只用 mock 造 ffprobe
stdout：不依赖预置 fixture、不需要真实 ffmpeg）：

* generic 路径上 ``media_duration`` 从 ``None`` 变成探测到的真实值，并真的
  作为 kwarg 传到 CapsWriter 层；
* 修复**不增加**媒体探测：整条链路上只有准入那一次 ffprobe，且 argv 里必须带
  ``-show_format``（时长就是从它的输出里读的），零 ffmpeg 调用；
* 时长拿不到（容器无 ``format.duration``）时**回退**既有来源，调用形状与今天一致；
* 非法时长（负数 / NaN / Inf / 非数字）永远不会被编造成一个值流到预算里；
* 拒绝语义未变（引擎零调用 + 具名原因）。拒绝路径的完整矩阵由
  ``test_transcription_audio_admission.py`` 用真实 ffprobe 覆盖。
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

import video_transcript_api.api.services.transcription as transcription
from video_transcript_api.errors import InvalidMediaError
from video_transcript_api.transcriber.capswriter_client import _transcription_deadline

# 复用 #159 准入测试的接线与替身（同一套真实入口协作方，不另造会漂移的副本）：
# `wired` fixture、RouteDownloader（generic 路线替身，**从不**写
# last_media_duration —— 缺陷现场）、RecordingTranscriber / RecordingFunASR 等。
from tests.unit.test_transcription_audio_admission import (  # noqa: F401
    RecordingFunASR,
    RecordingTranscriber,
    RouteDownloader,
    wired,
)

URL = "https://example.invalid/clip"
NO_DURATION = ...  # 哨兵：format 里根本没有 duration 键


# ---------------------------------------------------------------------------
# ffprobe stdout 构造（mock 掉子进程，理由见模块 docstring）
# ---------------------------------------------------------------------------

def _probe_stdout(duration=NO_DURATION, *, streams=None):
    """一次真实准入探测会打印的 stdout：streams 判音轨，format.duration 给时长。"""
    payload = {"streams": streams if streams is not None else [{"codec_type": "audio"}],
               "format": {} if duration is NO_DURATION else {"duration": duration}}
    return json.dumps(payload).encode("utf-8")


def _probe_completed(duration=NO_DURATION, **kw):
    return subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0, stdout=_probe_stdout(duration, **kw), stderr=b""
    )


class SingleProbeRunner:
    """只放行一次准入 ffprobe 的子进程替身。

    第二次调用、或任何非 ffprobe 的二进制，当场 AssertionError：这就是「修复不得
    新增媒体探测」的执行方式——不是事后数日志里的次数，而是让多余的那一次无处可藏。
    """

    def __init__(self, proc):
        self.proc = proc
        self.argvs = []

    def __call__(self, cmd, *args, **kwargs):
        self.argvs.append(list(cmd))
        assert len(self.argvs) == 1, f"extra subprocess call: {self.argvs}"
        assert cmd[0].endswith("ffprobe"), f"unexpected binary: {cmd}"
        return self.proc


def _run(wired, monkeypatch, tmp_path, *, duration=NO_DURATION, last_media_duration=None,
         proc=None, task_id="t-dur"):
    """跑一次真实入口，返回 (结果, 下载器, 子进程替身)。"""
    sample = tmp_path / "sample.mp4"
    sample.write_bytes(b"fake media bytes")
    downloader = RouteDownloader(wired["tm"], sample, "generic", URL)
    # 复现缺陷现场：generic 路线从不给该属性赋值（值缺失即 getattr 读出 None）。
    if last_media_duration is not None:
        downloader.last_media_duration = last_media_duration
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)
    runner = SingleProbeRunner(proc if proc is not None else _probe_completed(duration))
    monkeypatch.setattr(subprocess, "run", runner)
    result = transcription.process_transcription(
        task_id=task_id, url=URL, use_speaker_recognition=False
    )
    return result, downloader, runner


def _engine_kwargs():
    """CapsWriter 层实际收到的 kwargs（只有真正被调用时才有）。"""
    assert len(RecordingTranscriber.calls) == 1, RecordingTranscriber.calls
    return RecordingTranscriber.calls[0][2]


# ---------------------------------------------------------------------------
# 1. 缺陷修复本体：generic 路径拿到真实时长
# ---------------------------------------------------------------------------

def test_generic_path_duration_reaches_transcriber(wired, monkeypatch, tmp_path):
    """RED-first：``last_media_duration`` 拿不到（generic 现场）时，准入探测到的
    600.0 秒必须真的作为 kwarg 传下去。删掉新返回路径这条立刻转红。"""
    result, downloader, _runner = _run(wired, monkeypatch, tmp_path, duration="600.0")

    assert result["status"] == "success", result
    assert getattr(downloader, "last_media_duration", None) is None, "缺陷现场前提"
    assert _engine_kwargs() == {"media_duration": 600.0}


def test_no_extra_media_probe(wired, monkeypatch, tmp_path):
    """时长是**复用**准入探测的结果，不是新探测：整条链路只有那一次 ffprobe。

    ``SingleProbeRunner`` 对第二次子进程调用直接 AssertionError，所以这条断言是
    执行性的。argv 里必须带 ``-show_format``：时长就是从它的输出里读出来的。
    """
    _result, downloader, runner = _run(wired, monkeypatch, tmp_path, duration="600.0")

    assert len(runner.argvs) == 1, runner.argvs
    argv = runner.argvs[0]
    assert argv[0] == "ffprobe"
    assert "-show_format" in argv and "-show_streams" in argv
    assert not any(a[0].endswith("ffmpeg") for a in runner.argvs), runner.argvs
    # 时长来自**这次**探测的那个文件（不是别处的残留值）
    assert argv[-1] == str(downloader.written_paths[0])


# ---------------------------------------------------------------------------
# 2. 时长拿不到：回退既有来源，调用形状与今天一致
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("probe_duration, fallback, expected", [
    (NO_DURATION, 123.0, 123.0),   # 容器没给 format.duration
    ("NaN", 45.0, 45.0),           # 探测给了非法值
    ("600.0.0", 45.0, 45.0),       # 畸形数字
])
def test_unusable_probe_duration_falls_back_to_downloader_duration(
    wired, monkeypatch, tmp_path, probe_duration, fallback, expected
):
    """探测拿不到合法时长：回退 ``last_media_duration``（非 generic 路径行为不变）。"""
    result, _dl, _runner = _run(
        wired, monkeypatch, tmp_path, duration=probe_duration, last_media_duration=fallback
    )
    assert result["status"] == "success", result
    assert _engine_kwargs() == {"media_duration": expected}


def test_no_duration_anywhere_keeps_two_arg_call_shape(wired, monkeypatch, tmp_path):
    """两边都拿不到时长：保持既有的两参调用形状，一个多余的 kwarg 都不加。"""
    result, _dl, _runner = _run(wired, monkeypatch, tmp_path, duration=NO_DURATION)
    assert result["status"] == "success", result
    assert _engine_kwargs() == {}
    _local_file, output_base, kwargs = RecordingTranscriber.calls[0]
    assert kwargs == {} and output_base is not None


@pytest.mark.parametrize(
    "duration, why", [("not-a-number", "非数字字符串"), ("", "空串"), ("NaN", "NaN"),
     ("Inf", "Inf"), ("-inf", "-Inf"), ("-12.5", "负数"), ("1e309", "溢出成 inf"),
     (None, "JSON null"), ("600.0.0", "畸形数字")]
)
def test_invalid_duration_never_fabricates_a_value(wired, monkeypatch, tmp_path, duration, why):
    """非法时长一律当「没探测到」：不填默认值、不猜（判据是**下游实参**）。

    一条恒真的中间断言挡不住实现被改成瞎编（例如把 NaN 当 0.0 传下去），所以断言
    读的是 CapsWriter 层真正收到的东西。
    """
    result, _dl, _runner = _run(wired, monkeypatch, tmp_path, duration=duration)
    assert result["status"] == "success", result
    assert _engine_kwargs() == {}, f"{why} must not reach the deadline budget"




def test_format_without_dict_is_not_duration(wired, monkeypatch, tmp_path):
    """``format`` 不是对象（如 ``"format": "600.0"``）：结构畸形按无时长处理。"""
    proc = subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0,
        stdout=json.dumps({"streams": [{"codec_type": "audio"}], "format": "600.0"}).encode(),
        stderr=b"",
    )
    result, _dl, _runner = _run(wired, monkeypatch, tmp_path, proc=proc)
    assert result["status"] == "success", result
    assert _engine_kwargs() == {}


# ---------------------------------------------------------------------------
# 3. 拒绝语义不变（#159），且拒绝时不会带出时长
# ---------------------------------------------------------------------------

def test_no_audio_track_is_still_rejected_without_engine_call(wired, monkeypatch, tmp_path):
    """0 条音轨：仍是 no_audio_track、引擎零调用，绝不因为「顺手交了时长」而放行。"""
    proc = _probe_completed("600.0", streams=[{"codec_type": "video"}])
    result, _dl, runner = _run(wired, monkeypatch, tmp_path, proc=proc)

    assert result["status"] == "failed", result
    assert "no_audio_track" in result["message"]
    assert RecordingTranscriber.calls == [] and RecordingFunASR.calls == []
    assert len(runner.argvs) == 1


def test_probe_failure_is_still_check_failure(wired, monkeypatch, tmp_path):
    """探测非零退出：仍是 media_probe_failed（不得谎称无音轨）、引擎零调用。"""
    proc = subprocess.CompletedProcess(args=["ffprobe"], returncode=1, stdout=b"", stderr=b"x")
    result, _dl, runner = _run(wired, monkeypatch, tmp_path, proc=proc)

    assert result["status"] == "failed", result
    assert "media_probe_failed" in result["message"] and "不含音轨" not in result["message"]
    assert RecordingTranscriber.calls == [] and RecordingFunASR.calls == []
    assert len(runner.argvs) == 1


def test_admission_rejection_still_raises_invalid_media(tmp_path):
    """拒绝路径的异常类型与文案未变（时长返回值不得把异常路径变成放行）。"""
    media = tmp_path / "broken.mp3"
    media.write_bytes(b"fixture")
    proc = subprocess.CompletedProcess(args=["ffprobe"], returncode=1, stdout=b"", stderr=b"x")
    with patch("subprocess.run", return_value=proc):
        with pytest.raises(InvalidMediaError) as excinfo:
            transcription._ensure_audio_track(str(media))
    assert "media_probe_failed" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. 准入返回的时长与预算公式接受的时长不会分叉
# ---------------------------------------------------------------------------

def _admission_duration(media_path, duration=NO_DURATION, **kw):
    """直接问准入探测本身（文件在 tmp_path 现场创建，子进程被 mock）。"""
    media_path.write_bytes(b"fixture")
    with patch("subprocess.run", return_value=_probe_completed(duration, **kw)):
        return transcription._ensure_audio_track(str(media_path))


@pytest.mark.parametrize("raw, expected", [
    ("600.0", 600.0), ("1286.78", 1286.78), ("17501.67", 17501.67),
    ("0", 0.0), (600, 600.0), (1286.78, 1286.78),
])
def test_admission_returns_the_probed_duration(tmp_path, raw, expected):
    """准入放行时交出探测到的时长（秒，float）。"""
    assert _admission_duration(tmp_path / "a.mp3", raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw", ["not-a-number", "", "NaN", "Inf", "-inf", -12.5, "1e309", None]
)
def test_admission_returns_none_for_unusable_duration(tmp_path, raw):
    """容器没给 / 给了非法时长：诚实返回 None，不填默认值（预算侧同样拿不到 deadline_total）。"""
    parsed = _admission_duration(tmp_path / "a.mp3", raw)
    assert parsed is None
    assert _transcription_deadline(parsed) is None


@pytest.mark.parametrize("raw", ["600.0", "1286.78", "17501.67"])
def test_every_admitted_duration_is_accepted_by_the_deadline_budget(tmp_path, raw):
    """准入认可的时长必须被预算公式真正采纳（不能是「算出来是 None」的哑弹）。

    锁的是副作用的可观测面：时长一旦流向预算，``deadline_total`` 就是
    ``duration*4+120``，而不是悄悄退回 SDK 自动预算。容器头部写 0 的直播录制
    不在此列：0 按未知时长处理，由 ``test_capswriter_deadline_budget.py`` 覆盖
    （issue #190）。
    """
    parsed = _admission_duration(tmp_path / "a.mp3", raw)
    assert parsed is not None
    assert _transcription_deadline(parsed) == pytest.approx(parsed * 4 + 120)
