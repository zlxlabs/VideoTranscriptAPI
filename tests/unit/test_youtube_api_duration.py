"""#155 续：youtube-api 分支的转录时限预算一直拿不到时长（本卡的修复守卫）。

背景：#173 把准入探测出的时长接到了**通用分支**（``probed_media_duration`` →
``Transcriber.transcribe(..., media_duration=…)``）。youtube-api 分支当时按卡面
锁定刻意没接，于是这条路径至今仍是 ``duration=unknown fallback=sdk_auto``，
本仓日志看不到它的时长与预算，也拿不到「今后 SDK 再改公式时本仓显式值仍优先」
这个解耦。

本卡只做一件事：把**同一次**准入 ffprobe 已经解析出的 ``format.duration``
（``_ensure_audio_track`` 的返回值）接到该分支既有的 ``media_duration`` 实参上。
不新增任何探测、不改预算公式、不碰 FunASR 路径。

预算值不变的自证（两条路都是 ``duration*4+120``）：

* 传参 → 本仓 ``capswriter_client._transcription_deadline``：
  ``DEADLINE_REALTIME_FACTOR(4.0) * duration + DEADLINE_OVERHEAD_SECONDS(120.0)``；
* 不传 → SDK ``492fe191`` 的 ``_auto_budget(duration)`` 原文 ``return duration * 4 + 120``。

本文件锁的不变量（走真实入口 ``process_transcription``，只用 mock 造 ffprobe
stdout：不依赖预置 fixture、不需要 PATH 上的 ffmpeg）：

* 探测到时长时，``media_duration=<真实探测值>`` 真的传到 CapsWriter 层；
* 探测不到时长时，调用保持**两参**形状、``kwargs == {}``（不得无条件传 None）；
* **不新增探测**：整条链路只有准入那一次 ffprobe（执行性证明，见 ``SingleProbeRunner``）；
* FunASR 路径（``use_speaker_recognition=True``）不接时长（它不走 ``deadline_total``）；
* 拒绝语义逐字未变：4 条拒绝路径 + 结构畸形分支仍零引擎调用。
"""

from __future__ import annotations

import json
import subprocess

import pytest

import video_transcript_api.api.services.transcription as transcription
from video_transcript_api.transcriber.capswriter_client import _transcription_deadline

# 复用 #159 准入测试的接线与替身（同一套真实入口协作方，不另造会漂移的副本）：
# `wired` fixture、make_youtube_api_downloader（真实 YoutubeDownloader +
# use_api_server=True）、RecordingTranscriber / RecordingFunASR。
# 跨测试模块 import 在本仓已有先例（见 test_media_duration_probe.py）。
from tests.unit.test_transcription_audio_admission import (  # noqa: F401
    RecordingFunASR,
    RecordingTranscriber,
    make_youtube_api_downloader,
    wired,
)

YT_URL = "https://www.youtube.com/watch?v=vid159"
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

    第二次调用、或任何非 ffprobe 的二进制，当场 AssertionError：这就是「本卡不得
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


def _run(wired, monkeypatch, tmp_path, *, duration=NO_DURATION, proc=None,
         speaker=False, task_id="t-yt-dur"):
    """跑一次 youtube-api 真实入口，返回 (结果, 路线记录器, 子进程替身)。

    样本是**现场创建**的普通字节（不走 ffmpeg 真实编码）：准入探测已被 mock，
    生产代码此时只读自己 ffprobe 的 stdout，不解析媒体字节本身。
    """
    sample = tmp_path / "api_audio_bytes.m4a"
    sample.write_bytes(b"fake media bytes")
    downloader, rec = make_youtube_api_downloader(wired["tm"], sample)
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)
    runner = SingleProbeRunner(proc if proc is not None else _probe_completed(duration))
    monkeypatch.setattr(subprocess, "run", runner)
    result = transcription.process_transcription(
        task_id=task_id, url=YT_URL, use_speaker_recognition=speaker
    )
    return result, rec, runner


def _engine_kwargs():
    """CapsWriter 层实际收到的 kwargs（只有真正被调用时才有）。"""
    assert len(RecordingTranscriber.calls) == 1, RecordingTranscriber.calls
    return RecordingTranscriber.calls[0][2]


# ---------------------------------------------------------------------------
# 1. 修复本体：youtube-api 分支拿到真实时长
# ---------------------------------------------------------------------------

def test_youtube_api_duration_reaches_transcriber(wired, monkeypatch, tmp_path):
    """RED-first：准入探测到的 600.0 秒必须真的作为 kwarg 传下去。

    把本卡新增的 ``extra = {...}`` 删掉（恢复成两参调用）后，这条立刻转红。
    """
    result, rec, _runner = _run(wired, monkeypatch, tmp_path, duration="600.0")

    # 先确认生产 youtube-api 分支真的被走（否则断言可能在通用分支上假绿）。
    assert rec.methods == ["fetch_for_transcription"], rec.calls
    assert result["status"] == "success", result
    assert _engine_kwargs() == {"media_duration": 600.0}


def test_probe_duration_is_the_real_parsed_value(wired, monkeypatch, tmp_path):
    """透传的是**本次探测解析出的**真实值，不是硬编码/默认值（亚秒精度保真）。"""
    result, _rec, _runner = _run(wired, monkeypatch, tmp_path, duration="1286.78")

    assert result["status"] == "success", result
    kwargs = _engine_kwargs()
    assert kwargs == {"media_duration": 1286.78}
    # 与预算公式真正对齐：不是「传下去但预算算不出来」的哑弹。
    assert _transcription_deadline(kwargs["media_duration"]) == pytest.approx(1286.78 * 4 + 120)


def test_no_extra_media_probe(wired, monkeypatch, tmp_path):
    """时长是**复用**准入探测的结果，不是新探测：整条链路只有那一次 ffprobe。

    ``SingleProbeRunner`` 对第二次子进程调用直接 AssertionError，所以这条断言是
    执行性的。argv 里必须带 ``-show_format``：时长就是从它的输出里读出来的。
    """
    result, rec, runner = _run(wired, monkeypatch, tmp_path, duration="600.0")

    assert result["status"] == "success", result
    assert len(runner.argvs) == 1, runner.argvs
    argv = runner.argvs[0]
    assert argv[0] == "ffprobe"
    assert "-show_format" in argv and "-show_streams" in argv
    assert not any(a[0].endswith("ffmpeg") for a in runner.argvs), runner.argvs
    # 时长来自**这次**探测的那个文件（不是别处的残留值）
    assert argv[-1] == str(rec.media_paths[0])


# ---------------------------------------------------------------------------
# 2. 形状不变：探测不到时长时保持两参调用，不得无条件传 None
# ---------------------------------------------------------------------------

def test_no_duration_keeps_two_arg_call_shape(wired, monkeypatch, tmp_path):
    """容器没给 ``format.duration``：保持既有的两参调用形状，一个多余 kwarg 都不加。

    这是本卡最容易写坏的地方（无脑 ``transcribe(f, b, media_duration=None)``
    会让既有外部 mock 的调用形状漂移），所以单独锁一条。
    """
    result, _rec, _runner = _run(wired, monkeypatch, tmp_path, duration=NO_DURATION)

    assert result["status"] == "success", result
    assert _engine_kwargs() == {}
    _local_file, output_base, kwargs = RecordingTranscriber.calls[0]
    assert kwargs == {} and output_base is not None


@pytest.mark.parametrize(
    "raw, why", [("not-a-number", "非数字字符串"), ("", "空串"), ("NaN", "NaN"),
     ("Inf", "Inf"), ("-inf", "-Inf"), ("-12.5", "负数"), ("1e309", "溢出成 inf"),
     (None, "JSON null"), ("600.0.0", "畸形数字")]
)
def test_invalid_duration_never_fabricates_a_value(wired, monkeypatch, tmp_path, raw, why):
    """非法时长一律当「没探测到」：不填默认值、不猜（判据是**下游实参**）。

    读的是 CapsWriter 层真正收到的东西，避免恒真的中间断言挡不住「把 NaN 当 0.0
    传下去」这类瞎编实现。
    """
    result, _rec, _runner = _run(wired, monkeypatch, tmp_path, duration=raw)
    assert result["status"] == "success", result
    assert _engine_kwargs() == {}, f"{why} must not reach the deadline budget"


def test_format_without_dict_is_not_duration(wired, monkeypatch, tmp_path):
    """``format`` 不是对象（如 ``"format": "600.0"``）：结构畸形按无时长处理。"""
    proc = subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0,
        stdout=json.dumps({"streams": [{"codec_type": "audio"}], "format": "600.0"}).encode(),
        stderr=b"",
    )
    result, _rec, _runner = _run(wired, monkeypatch, tmp_path, proc=proc)
    assert result["status"] == "success", result
    assert _engine_kwargs() == {}


# ---------------------------------------------------------------------------
# 3. FunASR 路径不受影响（非目标：它不经过 deadline_total）
# ---------------------------------------------------------------------------

def test_funasr_path_takes_no_duration(wired, monkeypatch, tmp_path):
    """``use_speaker_recognition=True``：FunASR 直连，不接时长、不经 CapsWriter。

    本卡非目标之一。守住它，防止「顺手也给 FunASR 塞个时长」把非目标变成副作用。
    """
    result, rec, runner = _run(wired, monkeypatch, tmp_path, duration="600.0", speaker=True)

    assert rec.methods == ["fetch_for_transcription"], rec.calls
    assert result["status"] == "success", result
    assert len(RecordingFunASR.calls) == 1
    assert RecordingTranscriber.calls == []
    assert len(runner.argvs) == 1


# ---------------------------------------------------------------------------
# 4. 拒绝语义未变（#159）：带时长也不改判级，引擎零调用
# ---------------------------------------------------------------------------

def test_no_audio_track_still_rejected_with_duration_present(wired, monkeypatch, tmp_path):
    """0 条音轨但容器给了时长：仍是 no_audio_track、引擎零调用，绝不放行。"""
    proc = _probe_completed("600.0", streams=[{"codec_type": "video"}])
    result, _rec, runner = _run(wired, monkeypatch, tmp_path, proc=proc)

    assert result["status"] == "failed", result
    assert "no_audio_track" in result["message"]
    assert RecordingTranscriber.calls == [] and RecordingFunASR.calls == []
    assert len(runner.argvs) == 1


def test_probe_failure_still_check_failure(wired, monkeypatch, tmp_path):
    """探测非零退出：仍是 media_probe_failed（不得谎称无音轨）、引擎零调用。"""
    proc = subprocess.CompletedProcess(args=["ffprobe"], returncode=1, stdout=b"", stderr=b"x")
    result, _rec, runner = _run(wired, monkeypatch, tmp_path, proc=proc)

    assert result["status"] == "failed", result
    assert "media_probe_failed" in result["message"] and "不含音轨" not in result["message"]
    assert RecordingTranscriber.calls == [] and RecordingFunASR.calls == []
    assert len(runner.argvs) == 1


# ---------------------------------------------------------------------------
# 5. 预算值不变：本仓显式公式 == SDK 自动预算公式
# ---------------------------------------------------------------------------

def test_repo_budget_formula_matches_sdk_auto_budget():
    """本卡「不改任何任务预算值」的自证：两条路给的是同一个数。

    本仓显式预算：``capswriter_client._transcription_deadline``。
    SDK 自动预算：``capswriter_asr.client._auto_budget``（pyproject pin 的
    492fe191 = 上游 PR #71 之后的公式）。

    若哪天 SDK 再改公式、两者不再相等，本卡的前提失效——这条会先红，提醒重新决策，
    而不是让线上预算在无人察觉的情况下被悄悄改掉。
    """
    from capswriter_asr.client import _auto_budget

    for duration in (0.0, 1.0, 93.0, 600.0, 1286.78, 17501.67):
        explicit = _transcription_deadline(duration)
        assert explicit == pytest.approx(_auto_budget(duration)), duration
        assert explicit == pytest.approx(duration * 4 + 120), duration