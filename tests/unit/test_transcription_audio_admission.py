"""#159：真实共享准入边界上的音轨准入（strict admission）。

不变量（锁在两条真实共享入口上，不是锁在探测函数本身）：

* 无音轨媒体（0 条 ``codec_type == "audio"`` 流）在**两条共享入口**都被拒：
  任务终态 FAILED、失败通知 payload 带具名原因 ``no_audio_track``、
  FunASR 与 CapsWriter **零调用**、临时文件经既有清理路径回收。
* 探测失败（ffprobe 缺失 / 超时 / 非零退出 / 非法 JSON / 缺 streams /
  streams 畸形 / 文件不存在）与「确实无音轨」是**两种不同的失败**：
  原因标 ``media_probe_failed``，用户文案不得出现「不含音轨」。
* 有声样本与音视频混合样本正常放行进入所选引擎；音视频混合**不抽音轨**
  （一次额外 ffprobe 是允许的最小代价，不得出现 ffmpeg 转码调用）。
* 每次准入只探测一次。

真实 producer 证据：``audio_only`` / ``mixed`` / ``video_only`` 三个样本由
``ffmpeg -f lavfi`` 现场生成（无预置 /tmp fixture），准入探测跑的是仓库自己
启动的 **真实 ffprobe**，测试用 spy 记录真实 argv 与真实 JSON 输出，并落成
验证产物 ``data/temp/audio_admission_probe/``。只有「探测失败」分支用隔离
mock（真实 ffprobe 无法被构造成超时/缺失二进制）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger as loguru_logger

import video_transcript_api.api.services.transcription as transcription
from video_transcript_api.downloaders.models import DownloadInfo, VideoMetadata
from video_transcript_api.utils.tempfile_manager import TempFileManager

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

# 缺二进制不是"凑绿"的理由：下面的 skip 会带着完整原因出现在报告里，且本仓
# 维护者在本地/CI 都必须看到这批真实 CLI 用例真的跑过（见 PR 描述与报告）。
requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None, reason="需要真实 ffmpeg 以 lavfi 现场生成媒体样本"
)
requires_ffprobe = pytest.mark.skipif(
    FFPROBE is None, reason="需要真实 ffprobe 才能验证准入探测的真实 argv/JSON"
)

# 验证产物落在 data/temp/（.gitignore 已忽略）：真实 argv + 真实 ffprobe JSON
# + 样本大小 + 探测耗时，供事后核对，不用预置 fixture 冒充。
ARTIFACT_DIR = Path("data/temp/audio_admission_probe")

# 模块导入时抓一份真实实现：测试里的桩函数一律调它，否则经 monkeypatch 过的
# subprocess.run 会自调用成无限递归。
REAL_RUN = subprocess.run


# ---------------------------------------------------------------------------
# 真实媒体样本：ffmpeg lavfi 现场生成，不依赖任何预置 fixture
# ---------------------------------------------------------------------------

def _ffmpeg_build(args: list[str], out_path: Path) -> None:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args, str(out_path)]
    proc = REAL_RUN(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"ffmpeg build failed: {proc.stderr}"
    assert out_path.exists() and out_path.stat().st_size > 0


def build_video_only(path: Path) -> Path:
    """纯视频、无音轨（-an 显式丢弃音频）。"""
    _ffmpeg_build(
        [
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        ],
        path,
    )
    return path


def build_audio_only(path: Path) -> Path:
    """纯音频、无视频。"""
    _ffmpeg_build(
        [
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=2",
            "-vn", "-c:a", "aac",
        ],
        path,
    )
    return path


def build_mixed(path: Path) -> Path:
    """音视频混合。"""
    _ffmpeg_build(
        [
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        ],
        path,
    )
    return path


# ---------------------------------------------------------------------------
# Fake collaborators (真实入口 process_transcription 照常跑完整条业务链)
# ---------------------------------------------------------------------------

class FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class FakeCacheManager:
    """如实反映真实 CacheManager 的 CAS 语义（成功返回 True）。"""

    def __init__(self):
        self.saved = []
        self.status_updates = []
        self.tasks = {}
        self._outbox = {}

    def get_cache(self, *a, **k):
        return None

    def save_cache(self, **kwargs):
        self.saved.append(kwargs)
        return True

    def update_task_status(self, task_id, status, **kwargs):
        self.status_updates.append((task_id, status, kwargs))
        if status in ("success", "failed"):
            self._outbox.setdefault(task_id, {
                "task_id": task_id,
                "status": status,
                "error_message": kwargs.get("error_message"),
                "completed_at": "done",
                "notified_at": None,
                "attempts": 0,
            })
        return True

    def list_unattempted_terminal_notifications(self, limit=20, **kwargs):
        return [r for r in self._outbox.values() if r["notified_at"] is None][:limit]

    def is_terminal_notification_pending(self, task_id):
        row = self._outbox.get(task_id)
        return row is not None and row["notified_at"] is None

    def mark_terminal_notification_attempted(self, task_id):
        row = self._outbox.get(task_id)
        if row is not None:
            row["attempts"] += 1

    def mark_terminal_notification_sent(self, task_id):
        row = self._outbox.get(task_id)
        if row is not None:
            row["notified_at"] = "sent"

    def get_task_by_id(self, task_id):
        return self.tasks.get(task_id)

    def close(self):
        pass


class RecordingTranscriber:
    """CapsWriter 侧引擎替身：记录是否被调用。"""

    calls: list = []

    def __init__(self, *a, **k):
        pass

    def transcribe(self, local_file, output_base=None, **kwargs):
        RecordingTranscriber.calls.append((local_file, output_base, kwargs))
        return {"transcript": "capswriter text"}


class RecordingFunASR:
    """FunASR 侧引擎替身：记录是否被调用。"""

    calls: list = []

    def __init__(self, *a, **k):
        pass

    def transcribe_sync(self, local_file):
        RecordingFunASR.calls.append(local_file)
        return {
            "formatted_text": "funasr text",
            "transcription_result": [{"speaker": "spk_0", "text": "hello"}],
        }

    def format_transcript_with_speakers(self, data):
        return "funasr formatted"


class RecordingRouter:
    """通知路由替身：保留用户真正看到的 payload（status + error）。"""

    def __init__(self):
        self.calls = []

    def notify_task_status(self, *args, **kwargs):
        self.calls.append({
            "status": kwargs.get("status", args[1] if len(args) > 1 else ""),
            "error": kwargs.get("error"),
            "title": kwargs.get("title"),
        })
        return {"wechat": True}

    def send_text(self, *a, **k):
        return {"wechat": True}

    def send_long_text(self, *a, **k):
        return {"wechat": True}


def _materialize(sample, path: Path) -> Path:
    """把样本复制进目标路径（样本本身在 task 目录外，复制保证归属清晰）。"""
    shutil.copyfile(sample, path)
    return path


class RouteDownloader:
    """覆盖三条真实下载分支的下载器替身。

    route:
      * ``generic``   —— 常规下载：``download_file`` 落到当前任务目录
      * ``predownloaded`` —— 预下载就绪分支：``DownloadInfo.downloaded`` +
        ``local_file``（文件同样落在当前任务目录，归属可被清理断言覆盖）
      * ``youtube``   —— YouTube 选择路径（yt-dlp 主链路的 download_file 分支）
    """

    use_api_server = False

    def __init__(self, temp_manager, sample: Path, route: str, url: str):
        self._tm = temp_manager
        self._sample = sample
        self._route = route
        self._url = url
        self.written_paths = []

    # --- metadata / download info -------------------------------------
    def get_metadata(self, url):
        return VideoMetadata(
            video_id="vid159",
            platform="generic" if self._route == "generic" else "youtube",
            title="admission title",
            author="admission author",
            description="",
        )

    def get_download_info(self, url):
        if self._route == "predownloaded":
            target = Path(self._tm.get_current_task_dir()) / "predownloaded.mp4"
            _materialize(self._sample, target)
            self.written_paths.append(target)
            return DownloadInfo(
                download_url=None, file_ext="mp4", filename="predownloaded.mp4",
                downloaded=True, local_file=str(target),
            )
        return DownloadInfo(
            download_url=f"http://example.invalid/{self._route}.mp4",
            file_ext="mp4", filename=f"{self._route}.mp4",
            downloaded=False, local_file=None,
        )

    def get_subtitle(self, url):
        return None

    def download_file(self, url, filename):
        target = Path(self._tm.get_current_task_dir()) / filename
        _materialize(self._sample, target)
        self.written_paths.append(target)
        return str(target)


class YoutubeApiServerDownloader(RouteDownloader):
    """youtube-api 分支：音频由 API Server 下好后直调 ASR。"""

    use_api_server = True

    def __init__(self, temp_manager, sample: Path, **kwargs):
        super().__init__(temp_manager, sample, "youtube_api", "https://www.youtube.com/watch?v=vid159")
        self.audio_paths = []

    def get_metadata(self, url):
        return VideoMetadata(
            video_id="vid159", platform="youtube", title="api title",
            author="api author", description="",
        )

    def fetch_for_transcription(self, url, use_speaker_recognition):
        target = Path(self._tm.get_current_task_dir()) / "api_audio.m4a"
        _materialize(self._sample, target)
        self.audio_paths.append(target)
        return {
            "video_id": "vid159",
            "video_title": "api title",
            "author": "api author",
            "description": "",
            "platform": "youtube",
            "transcript": None,
            "transcript_segments": None,
            "audio_path": str(target),
            "need_transcription": True,
        }


class SubprocessSpy:
    """记录仓库真实启动的子进程 argv（并量探测耗时），再委托给真实实现。

    隔离的失败分支 mock 之外，这里只做「记录 + 真跑」，因此断言的是本仓实际
    下发的 ffprobe 命令行，而不是测试自造的字符串。
    """

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *args, **kwargs):
        started = time.monotonic()
        proc = REAL_RUN(cmd, *args, **kwargs)
        self.calls.append({
            "argv": list(cmd),
            "elapsed": time.monotonic() - started,
            "stdout": getattr(proc, "stdout", None),
            "returncode": getattr(proc, "returncode", None),
        })
        return proc

    def argvs_for(self, binary: str) -> list[list[str]]:
        return [c["argv"] for c in self.calls if c["argv"][0].endswith(binary)]

    def calls_for(self, binary: str) -> list[dict]:
        return [c for c in self.calls if c["argv"][0].endswith(binary)]


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """把 process_transcription 的协作者接到 tmp 目录上的真实业务链。"""
    tm = TempFileManager(str(tmp_path / "temp"), retention_hours=24)
    cache = FakeCacheManager()
    router = RecordingRouter()
    spy = SubprocessSpy()

    monkeypatch.setattr(transcription, "get_temp_manager", lambda: tm)
    monkeypatch.setattr(transcription, "cache_manager", cache)
    monkeypatch.setattr(transcription, "get_notification_router", lambda: router)
    monkeypatch.setattr(transcription, "llm_task_queue", FakeQueue())
    monkeypatch.setattr(transcription, "Transcriber", RecordingTranscriber)
    monkeypatch.setattr(transcription, "FunASRSpeakerClient", RecordingFunASR)
    monkeypatch.setattr(transcription, "get_base_url", lambda: "http://test")
    monkeypatch.setattr(subprocess, "run", spy)

    RecordingTranscriber.calls = []
    RecordingFunASR.calls = []

    return {
        "tm": tm,
        "cache": cache,
        "router": router,
        "spy": spy,
        "downloaders": [],
    }


def _engine_calls():
    return RecordingFunASR.calls + [c[0] for c in RecordingTranscriber.calls]


def _terminal_failure(router, cache, task_id):
    """失败通知与 FAILED 终态上真实承载给用户的文本。"""
    delivered = [c for c in router.calls if c["status"].startswith("【任务失败】")]
    assert delivered, f"no terminal failure notification: {router.calls}"
    assert (task_id, "failed") in [(t, s) for t, s, _ in cache.status_updates]
    return delivered[-1]["error"]


# ---------------------------------------------------------------------------
# 1. 无音轨 -> 两条共享入口都拒绝（终态 FAILED + 具名原因 + 引擎零调用）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["generic", "predownloaded", "youtube"])
@pytest.mark.parametrize("use_speaker_recognition", [True, False])
@requires_ffmpeg
def test_no_audio_track_is_rejected_at_regular_entry(
    tmp_path, monkeypatch, wired, route, use_speaker_recognition
):
    sample = build_video_only(tmp_path / "video_only.mp4")
    url = (
        "https://www.youtube.com/watch?v=vid159"
        if route == "youtube"
        else "https://example.invalid/clip"
    )
    downloader = RouteDownloader(wired["tm"], sample, route, url)
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    # 清理边界按文件所有权而不是「有 local_file 就删」：另一个并发任务的在途
    # 文件与既有缓存产物都必须原样存活。
    foreign_dir = wired["tm"].create_task_dir("other-task")
    foreign_file = Path(foreign_dir) / "in-flight.mp4"
    foreign_file.write_bytes(b"other task bytes")
    wired["tm"].mark_active("other-task")
    cache_artifact = tmp_path / "cache" / "already-transcribed.mp3"
    cache_artifact.parent.mkdir(parents=True, exist_ok=True)
    cache_artifact.write_bytes(b"existing cache bytes")

    task_id = f"t-noaudio-{route}-{int(use_speaker_recognition)}"
    result = transcription.process_transcription(
        task_id=task_id,
        url=url,
        use_speaker_recognition=use_speaker_recognition,
    )

    assert result["status"] == "failed", result
    assert "no_audio_track" in result["message"]
    assert "不含音轨" in result["message"]

    error = _terminal_failure(wired["router"], wired["cache"], task_id)
    assert "no_audio_track" in error
    assert "不含音轨" in error
    assert "media_probe_failed" not in error

    # 两个引擎都必须零调用（无空转录、无假成功）
    assert _engine_calls() == []
    assert wired["cache"].saved == []

    # 既有清理机制按文件归属回收：任务目录内的下载产物被删，别人的文件不动
    assert wired["tm"].get_task_dir(task_id) is None
    assert not wired["tm"].is_active(task_id)
    for path in downloader.written_paths:
        assert not path.exists()
    assert foreign_file.exists(), "cleanup must not touch another task's files"
    assert wired["tm"].is_active("other-task")
    assert cache_artifact.exists(), "cleanup must not touch existing cache artifacts"


@requires_ffmpeg
def test_no_audio_track_is_rejected_at_youtube_api_entry(tmp_path, monkeypatch, wired):
    sample = build_video_only(tmp_path / "video_only_api.mp4")
    downloader = YoutubeApiServerDownloader(wired["tm"], sample)
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    task_id = "t-noaudio-youtube-api"
    result = transcription.process_transcription(
        task_id=task_id,
        url="https://www.youtube.com/watch?v=vid159",
        use_speaker_recognition=True,
    )

    assert result["status"] == "failed", result
    assert "no_audio_track" in result["message"]
    assert "不含音轨" in result["message"]
    error = _terminal_failure(wired["router"], wired["cache"], task_id)
    assert "no_audio_track" in error and "media_probe_failed" not in error
    assert _engine_calls() == []
    for path in downloader.audio_paths:
        assert not path.exists()


# ---------------------------------------------------------------------------
# 2. 有声 / 混合样本放行，且不抽音轨
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "builder, suffix", [(build_audio_only, ".m4a"), (build_mixed, ".mp4")]
)
@requires_ffmpeg
def test_audio_bearing_media_reaches_selected_engine(
    tmp_path, monkeypatch, wired, builder, suffix
):
    sample = builder(tmp_path / f"sample{suffix}")
    downloader = RouteDownloader(wired["tm"], sample, "generic", "https://example.invalid/clip")
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    result = transcription.process_transcription(
        task_id="t-ok-capswriter",
        url="https://example.invalid/clip",
        use_speaker_recognition=False,
    )
    assert result["status"] == "success", result
    assert len(RecordingTranscriber.calls) == 1
    assert RecordingFunASR.calls == []


@requires_ffmpeg
def test_mixed_media_reaches_funasr_without_transcoding(tmp_path, monkeypatch, wired):
    """音视频混合样本走 FunASR 分支，准入只多一次 ffprobe，不出现 ffmpeg 转码。"""
    sample = build_mixed(tmp_path / "mixed.mp4")
    downloader = RouteDownloader(wired["tm"], sample, "generic", "https://example.invalid/clip")
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    result = transcription.process_transcription(
        task_id="t-ok-funasr",
        url="https://example.invalid/clip",
        use_speaker_recognition=True,
    )
    assert result["status"] == "success", result
    assert len(RecordingFunASR.calls) == 1
    assert RecordingTranscriber.calls == []

    probes = wired["spy"].argvs_for("ffprobe")
    transcodes = wired["spy"].argvs_for("ffmpeg")
    assert len(probes) == 1, probes
    assert transcodes == [], f"audio extraction is forbidden: {transcodes}"


@requires_ffmpeg
def test_youtube_api_audio_only_reaches_engine(tmp_path, monkeypatch, wired):
    sample = build_audio_only(tmp_path / "api_audio.m4a")
    downloader = YoutubeApiServerDownloader(wired["tm"], sample)
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    result = transcription.process_transcription(
        task_id="t-api-ok",
        url="https://www.youtube.com/watch?v=vid159",
        use_speaker_recognition=True,
    )
    assert result["status"] == "success", result
    assert len(RecordingFunASR.calls) == 1
    probes = wired["spy"].argvs_for("ffprobe")
    assert len(probes) == 1, probes


# ---------------------------------------------------------------------------
# 3. 真实 ffprobe 边界：argv、真实 JSON 结构、探测次数、耗时
# ---------------------------------------------------------------------------

@requires_ffmpeg
@requires_ffprobe
@pytest.mark.parametrize(
    "builder, suffix, expect_admitted",
    [
        (build_video_only, ".mp4", False),
        (build_audio_only, ".m4a", True),
        (build_mixed, ".mp4", True),
    ],
)
def test_real_ffprobe_argv_and_json_are_the_producer_evidence(
    tmp_path, monkeypatch, wired, builder, suffix, expect_admitted
):
    sample = builder(tmp_path / f"real{suffix}")
    downloader = RouteDownloader(wired["tm"], sample, "generic", "https://example.invalid/clip")
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    result = transcription.process_transcription(
        task_id="t-real-probe",
        url="https://example.invalid/clip",
        use_speaker_recognition=False,
    )

    probe_calls = wired["spy"].calls_for("ffprobe")
    assert len(probe_calls) == 1, probe_calls
    call = probe_calls[0]
    elapsed = call["elapsed"]
    probe_argv = call["argv"]

    # 本仓实际下发的 argv（不是测试自造的命令）
    assert probe_argv[0] == "ffprobe"
    assert probe_argv[1:] == [
        "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams",
        str(downloader.written_paths[0]),
    ]

    # 真实 ffprobe JSON 结构：root 对象 + streams 列表 + 有效 stream 对象
    payload = json.loads(call["stdout"])
    assert isinstance(payload, dict)
    assert isinstance(payload["streams"], list)
    assert payload["streams"] and all(
        isinstance(s, dict) and "codec_type" in s for s in payload["streams"]
    )
    audio_streams = [s for s in payload["streams"] if s["codec_type"] == "audio"]
    assert bool(audio_streams) is expect_admitted
    assert (result["status"] == "success") is expect_admitted

    # 验证产物：真实 argv + 真实 JSON + 样本大小 + 探测耗时
    artifact = {
        "sample": str(sample),
        "sample_bytes": sample.stat().st_size,
        "ffprobe_argv": probe_argv,
        "ffprobe_stdout": payload,
        "probe_seconds": round(elapsed, 4),
        "admitted": expect_admitted,
        "task_result": result,
    }
    (ARTIFACT_DIR / f"probe_{builder.__name__}.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2)
    )
    print(
        f"[audio-admission artifact] {builder.__name__} "
        f"bytes={sample.stat().st_size} probe={elapsed:.4f}s argv={probe_argv}"
    )


# ---------------------------------------------------------------------------
# 4. 探测失败 = 检查失败（与「不含音轨」严格区分）
# ---------------------------------------------------------------------------

_PROBE_FAILURES = {
    "missing_binary": FileNotFoundError(2, "No such file or directory", "ffprobe"),
    "timeout": subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=30),
    "nonzero_exit": subprocess.CompletedProcess(
        args=["ffprobe"], returncode=1, stdout=b"", stderr=b"moov atom not found"
    ),
    "bad_json": subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0, stdout=b"not-json-at-all", stderr=b""
    ),
    "missing_streams": subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0, stdout=b'{"format": {"duration": "2.0"}}', stderr=b""
    ),
    "streams_not_list": subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0, stdout=b'{"streams": {"codec_type": "audio"}}', stderr=b""
    ),
    "stream_entry_malformed": subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0, stdout=b'{"streams": ["audio"]}', stderr=b""
    ),
}


def _probe_returns(proc):
    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[0].endswith("ffprobe"):
            return proc
        return REAL_RUN(cmd, *args, **kwargs)
    return fake_run


def _probe_raises(exc):
    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[0].endswith("ffprobe"):
            raise exc
        return REAL_RUN(cmd, *args, **kwargs)
    return fake_run


@pytest.mark.parametrize("failure", sorted(_PROBE_FAILURES))
def test_probe_failures_are_attributed_to_check_not_to_input(
    tmp_path, monkeypatch, wired, failure
):
    """四种探测失败 + 缺 streams + 畸形 streams 全部任务失败，且用户可区分。"""
    outcome = _PROBE_FAILURES[failure]
    written_media = tmp_path / "video_only.mp4"
    build_video_only(written_media)

    monkeypatch.setattr(
        subprocess,
        "run",
        _probe_returns(outcome) if isinstance(outcome, subprocess.CompletedProcess)
        else _probe_raises(outcome),
    )

    downloader = RouteDownloader(wired["tm"], written_media, "generic", "https://example.invalid/clip")
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    task_id = f"t-probe-{failure}"
    result = transcription.process_transcription(
        task_id=task_id,
        url="https://example.invalid/clip",
        use_speaker_recognition=False,
    )

    assert result["status"] == "failed", result
    assert "media_probe_failed" in result["message"]
    # 关键区分：检查失败不得被表述成「不含音轨」
    assert "不含音轨" not in result["message"]
    assert "no_audio_track" not in result["message"]
    error = _terminal_failure(wired["router"], wired["cache"], task_id)
    assert "media_probe_failed" in error
    assert "不含音轨" not in error
    assert _engine_calls() == []
    for path in downloader.written_paths:
        assert not path.exists()


@requires_ffmpeg
def test_missing_media_file_is_check_failure_not_no_audio(tmp_path, monkeypatch, wired):
    """准入点拿到不存在的路径 = 检查失败，绝不谎称无音轨。"""
    ghost = tmp_path / "vanished.mp4"
    downloader = RouteDownloader(wired["tm"], ghost, "generic", "https://example.invalid/clip")
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    def ghost_download(url, filename):
        target = Path(wired["tm"].get_current_task_dir()) / filename
        downloader.written_paths.append(target)
        return str(target)

    monkeypatch.setattr(downloader, "download_file", ghost_download)

    result = transcription.process_transcription(
        task_id="t-ghost-media",
        url="https://example.invalid/clip",
        use_speaker_recognition=False,
    )
    assert result["status"] == "failed", result
    assert "media_probe_failed" in result["message"]
    assert "不含音轨" not in result["message"]
    assert _engine_calls() == []


def test_probe_failure_at_youtube_api_entry_is_check_failure(tmp_path, monkeypatch, wired):
    sample = build_audio_only(tmp_path / "audio.m4a")
    downloader = YoutubeApiServerDownloader(wired["tm"], sample)
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    def boom(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[0].endswith("ffprobe"):
            raise FileNotFoundError(2, "No such file or directory", "ffprobe")
        return subprocess.run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", boom)

    result = transcription.process_transcription(
        task_id="t-api-probe-fail",
        url="https://www.youtube.com/watch?v=vid159",
        use_speaker_recognition=True,
    )
    assert result["status"] == "failed", result
    assert "media_probe_failed" in result["message"]
    assert "不含音轨" not in result["message"]
    assert _engine_calls() == []


# ---------------------------------------------------------------------------
# 5. 诊断面：stdout 日志可 grep 英文字面量，且不带原始 stderr 全文
# ---------------------------------------------------------------------------

def _collect_log_lines(level="INFO"):
    """按仓库既有做法把 loguru 输出收进 list（caplog 收不到 loguru sink）。"""
    records: list[str] = []
    sink_id = loguru_logger.add(lambda m: records.append(str(m)), level=level)
    return records, sink_id


def test_diagnostics_log_grepable_literals(tmp_path, monkeypatch, wired):
    """准入诊断用可 grep 的英文字面量，且不把原始 stderr 全文塞给用户。"""
    written_media = tmp_path / "video_only.mp4"
    build_video_only(written_media)
    downloader = RouteDownloader(
        wired["tm"], written_media, "generic", "https://example.invalid/clip"
    )
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    monkeypatch.setattr(subprocess, "run", _probe_returns(subprocess.CompletedProcess(
        args=["ffprobe"], returncode=1,
        stdout=b"", stderr=b"SECRET_INTERNAL_DETAIL that must not leak to users",
    )))

    records, sink_id = _collect_log_lines()
    try:
        result = transcription.process_transcription(
            task_id="t-diagnostic",
            url="https://example.invalid/clip",
            use_speaker_recognition=False,
        )
    finally:
        loguru_logger.remove(sink_id)

    assert result["status"] == "failed"
    admission_lines = [line for line in records if "audio_track_admission" in line]
    assert admission_lines, "admission produced no greppable log line"
    assert any("media_probe_failed" in line for line in admission_lines)
    # 诊断里允许出现 ffprobe 的返回码/白名单 stderr，但用户文案不行
    assert "SECRET_INTERNAL_DETAIL" not in result["message"]
    assert "SECRET_INTERNAL_DETAIL" not in "\n".join(
        str(c["error"]) for c in wired["router"].calls if c["error"]
    )


def test_no_audio_track_log_line_is_ascii(tmp_path, monkeypatch, wired):
    """新加的 stdout 准入日志保持 ASCII-only（仓库 console 契约）。"""
    written_media = tmp_path / "video_only.mp4"
    build_video_only(written_media)
    downloader = RouteDownloader(
        wired["tm"], written_media, "generic", "https://example.invalid/clip"
    )
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    records, sink_id = _collect_log_lines()
    try:
        transcription.process_transcription(
            task_id="t-ascii-log",
            url="https://example.invalid/clip",
            use_speaker_recognition=False,
        )
    finally:
        loguru_logger.remove(sink_id)

    admission_lines = [line for line in records if "audio_track_admission" in line]
    assert admission_lines
    for line in admission_lines:
        assert all(ord(ch) < 128 for ch in line), line
