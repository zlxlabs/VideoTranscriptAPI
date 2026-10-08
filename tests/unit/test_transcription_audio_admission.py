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

import hashlib
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
from video_transcript_api.downloaders.youtube import YoutubeDownloader
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
    """CapsWriter 侧引擎替身：记录是否被调用，以及它**实际收到的文件字节**哈希。"""

    calls: list = []
    digests: list = []

    def __init__(self, *a, **k):
        pass

    def transcribe(self, local_file, output_base=None, **kwargs):
        digest = file_digest(local_file)
        RecordingTranscriber.calls.append((local_file, output_base, kwargs))
        RecordingTranscriber.digests.append((local_file, digest))
        EVENTS.append(("engine", local_file, digest))
        return {"transcript": "capswriter text"}


class RecordingFunASR:
    """FunASR 侧引擎替身：记录是否被调用，以及它**实际收到的文件字节**哈希。"""

    calls: list = []
    digests: list = []

    def __init__(self, *a, **k):
        pass

    def transcribe_sync(self, local_file):
        digest = file_digest(local_file)
        RecordingFunASR.calls.append(local_file)
        RecordingFunASR.digests.append((local_file, digest))
        EVENTS.append(("engine", local_file, digest))
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


def file_digest(path) -> str:
    """真实文件字节的 sha256（不给默认值：文件读不到就直接炸）。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# 全局事件序列：证明「准入前的哈希」确实取自下载落地那一刻，而不是准入之后。
EVENTS: list[tuple] = []


class RouteDownloader:
    """覆盖两条真实常规下载分支的下载器替身。

    route:
      * ``generic``   —— 常规下载：``download_file`` 落到当前任务目录
      * ``predownloaded`` —— 预下载就绪分支：``DownloadInfo.downloaded`` +
        ``local_file``（文件同样落在当前任务目录，归属可被清理断言覆盖）

    YouTube 优先下载分支**不归这里**：生产条件是
    ``hasattr(download_video_with_priority)`` 且 URL 含 youtube.com，见
    ``make_youtube_priority_downloader``。
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
            EVENTS.append(("download", str(target), file_digest(target)))
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
        EVENTS.append(("download", str(target), file_digest(target)))
        return str(target)


class YoutubeRouteRecorder:
    """记录真实分支上**到底调用了哪个 producer 方法**。

    只看 fixture 自己的变量名不算证据：断言读的是生产代码在这条路径上真实
    调用过的方法名。
    """

    def __init__(self):
        self.calls = []
        self.media_paths = []
        self.pre_admission_digests = {}

    def record(self, method, *args):
        self.calls.append((method, *args))

    @property
    def methods(self):
        return [c[0] for c in self.calls]


def _real_youtube_downloader(use_api_server: bool):
    """生产 ``YoutubeDownloader`` 的真实实例（绕过 ``__init__``，避免真连网）。

    用 ``__new__`` 而不是继承：生产分支判定的是**精确类名**
    ``__class__.__name__ == "YoutubeDownloader"``，子类会让这个判定静默失效
    （上一轮假阳性的根因）。用真实类 + 实例级桩，生产改类名时本测试会红，
    而不是跟着漂移。

    ``use_api_server`` 是生产类的只读 property（真值为 ``_youtube_api_client
    is not None``），所以这里设的是它真正的后端，而不是在替身上另开一个同名
    属性——否则判定的就不是生产形状了。
    """
    dl = YoutubeDownloader.__new__(YoutubeDownloader)
    dl._youtube_api_client = object() if use_api_server else None
    return dl


def _attach_common_stubs(dl, rec, temp_manager, sample, platform="youtube",
                         video_id="vid159"):
    """两条 YouTube 分支共用的替身：元数据 + 落地媒体 + 方法调用记录。"""

    def materialize(name):
        target = Path(temp_manager.get_current_task_dir()) / name
        _materialize(sample, target)
        rec.media_paths.append(target)
        rec.pre_admission_digests[str(target)] = file_digest(target)
        # 时间点证据：下载落地那一刻的字节，准入还没跑
        EVENTS.append(("download", str(target), rec.pre_admission_digests[str(target)]))
        return target

    dl.get_metadata = lambda url: VideoMetadata(
        video_id=video_id, platform=platform, title="yt title",
        author="yt author", description="",
    )
    dl.get_subtitle_result = lambda url: None
    dl.get_subtitle = lambda url: None

    def download_file(url, filename):
        rec.record("download_file", url, filename)
        return str(materialize(filename))

    dl.download_file = download_file

    def download_video_with_priority(url, video_info=None):
        rec.record("download_video_with_priority", url, dict(video_info or {}))
        return str(materialize("priority_download.mp4"))

    dl.download_video_with_priority = download_video_with_priority

    def get_download_info(url):
        rec.record("get_download_info", url)
        return DownloadInfo(
            download_url="http://example.invalid/yt.mp4", file_ext="mp4",
            filename="yt.mp4", downloaded=False, local_file=None,
        )

    dl.get_download_info = get_download_info
    return materialize


def make_youtube_api_downloader(temp_manager, sample, filename="api_audio.m4a",
                              subtitle=None):
    """youtube-api 快速路径替身（真实 YoutubeDownloader 类 + ``use_api_server``）。

    生产条件：``metadata_downloader.__class__.__name__ == "YoutubeDownloader"``
    且 ``hasattr(use_api_server)`` 且为真值 —— 三者缺一就退回常规下载路线。
    给了 ``subtitle`` 就走字幕直命中（``need_transcription=False``、``audio_path=None``、
    **不落任何媒体文件**），对应生产 ``transcription.py:1913`` 起的字幕分支。
    """
    rec = YoutubeRouteRecorder()
    dl = _real_youtube_downloader(use_api_server=True)
    materialize = _attach_common_stubs(dl, rec, temp_manager, sample)

    def fetch_for_transcription(url, use_speaker_recognition):
        rec.record("fetch_for_transcription", url, use_speaker_recognition)
        api_result = {
            "video_id": "vid159",
            "video_title": "yt title",
            "author": "yt author",
            "description": "",
            "platform": "youtube",
            "transcript": None,
            "transcript_segments": None,
            "audio_path": None,
            "need_transcription": True,
        }
        if subtitle is not None:
            api_result.update({
                "transcript": subtitle,
                "transcript_segments": [
                    {"start": 0.0, "end": 1.0, "text": subtitle},
                ],
                "audio_path": None,
                "need_transcription": False,
            })
            return api_result
        api_result["audio_path"] = str(materialize(filename))
        return api_result

    dl.fetch_for_transcription = fetch_for_transcription
    dl._admission_rec = rec
    return dl, rec


def make_youtube_priority_downloader(temp_manager, sample, url):
    """YouTube 优先下载分支替身（真实 YoutubeDownloader 类 + 优先方法）。"""
    rec = YoutubeRouteRecorder()
    dl = _real_youtube_downloader(use_api_server=False)
    _attach_common_stubs(dl, rec, temp_manager, sample)
    dl._admission_rec = rec
    return dl, rec


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
        # 真实 ffprobe 采样点直接进 EVENTS：字节不变断言需要用**实际发生的探测**
        # 证明两次哈希取样之间确实夹着准入，而不是只靠源码顺序读。
        if isinstance(cmd, list) and cmd and cmd[0].endswith("ffprobe"):
            EVENTS.append(("probe", cmd[0], list(cmd)))
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
    monkeypatch.setattr(subprocess, "run", spy)

    RecordingTranscriber.calls = []
    RecordingTranscriber.digests = []
    RecordingFunASR.calls = []
    RecordingFunASR.digests = []
    EVENTS.clear()

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


def test_youtube_api_double_satisfies_production_trigger_condition(wired, tmp_path):
    """RED-first：生产 youtube-api 分支判定的是**精确类名** `YoutubeDownloader` +
    `use_api_server` 真值。替身不满足这两个条件就会静默退回常规下载路线，
    准入点也跟着换人——这正是上一轮「移掉 API 分支准入仍全绿」的成因。
    """
    sample = build_audio_only(tmp_path / "api_trigger.m4a")
    dl, _rec = make_youtube_api_downloader(wired["tm"], sample)
    assert dl.__class__.__name__ == "YoutubeDownloader"
    assert dl.use_api_server is True
    assert dl._youtube_api_client is not None


@requires_ffmpeg
def test_youtube_priority_double_declares_priority_entrypoint(wired, tmp_path):
    """RED-first：生产优先分支条件是 hasattr(download_video_with_priority) 且
    URL 含 youtube.com。替身缺这个方法就永远走不到优先下载分支。"""
    sample = build_audio_only(tmp_path / "priority_trigger.m4a")
    url = "https://www.youtube.com/watch?v=vid159"
    dl, _rec = make_youtube_priority_downloader(wired["tm"], sample, url)
    assert hasattr(dl, "download_video_with_priority")
    assert "youtube.com" in url


# ---------------------------------------------------------------------------
# 1. 无音轨 -> 两条共享入口都拒绝（终态 FAILED + 具名原因 + 引擎零调用）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["generic", "predownloaded"])
@pytest.mark.parametrize("use_speaker_recognition", [True, False])
@requires_ffmpeg
def test_no_audio_track_is_rejected_at_regular_entry(
    tmp_path, monkeypatch, wired, route, use_speaker_recognition
):
    sample = build_video_only(tmp_path / "video_only.mp4")
    url = "https://example.invalid/clip"
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
    """youtube-api 分支真实分支：断言 API producer 真的被调用、常规下载路线未被走。"""
    sample = build_video_only(tmp_path / "video_only_api.mp4")
    url = "https://www.youtube.com/watch?v=vid159"
    downloader, rec = make_youtube_api_downloader(wired["tm"], sample)
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    task_id = "t-noaudio-youtube-api"
    result = transcription.process_transcription(
        task_id=task_id,
        url=url,
        use_speaker_recognition=True,
    )

    # 生产 API 分支真的被走（否则「移掉 API 准入仍全绿」的假阳性会复现）
    assert rec.methods == ["fetch_for_transcription"], rec.calls
    assert "download_file" not in rec.methods and "get_download_info" not in rec.methods
    assert downloader.__class__.__name__ == "YoutubeDownloader"
    assert downloader.use_api_server is True
    # 引擎零调用是这条分支最硬的不变式，先于终态断言（移除 API 分支准入的
    # 变异下，这一条最先转红）。
    assert _engine_calls() == []

    assert result["status"] == "failed", result
    assert "no_audio_track" in result["message"]
    assert "不含音轨" in result["message"]
    error = _terminal_failure(wired["router"], wired["cache"], task_id)
    assert "no_audio_track" in error and "media_probe_failed" not in error
    assert wired["cache"].saved == []
    for path in rec.media_paths:
        assert not path.exists()
    assert wired["tm"].get_task_dir(task_id) is None


@requires_ffmpeg
def test_youtube_api_audio_bearing_reaches_engine(tmp_path, monkeypatch, wired):
    """API 分支放行对照：audio_path 真的到达 FunASR，字节未被改写。"""
    sample = build_mixed(tmp_path / "api_mixed.mp4")
    url = "https://www.youtube.com/watch?v=vid159"
    downloader, rec = make_youtube_api_downloader(wired["tm"], sample)
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    result = transcription.process_transcription(
        task_id="t-api-ok",
        url=url,
        use_speaker_recognition=True,
    )
    assert rec.methods == ["fetch_for_transcription"], rec.calls
    assert result["status"] == "success", result
    assert len(RecordingFunASR.calls) == 1
    assert RecordingTranscriber.calls == []
    assert wired["spy"].argvs_for("ffmpeg") == []
    assert len(RecordingFunASR.digests) == 1
    _assert_bytes_unchanged(
        rec.pre_admission_digests[RecordingFunASR.calls[0]],
        RecordingFunASR.digests, sample, RecordingFunASR.calls[0],
    )


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

    # 常规 download_file 入口的字节不变：下载落盘 vs 引擎入口，真实文件现算，
    # 且真实 ffprobe 必须发生在两次取样之间。
    media_path = RecordingFunASR.calls[0]
    assert probes[0][-1] == media_path
    _assert_bytes_unchanged(
        _download_event(media_path), RecordingFunASR.digests, sample, media_path
    )


def _assert_bytes_unchanged(pre_digest, engine_digests, sample, media_path):
    """准入不得改写输入媒体字节：下载落盘时的 sha256 == 引擎入口读到的 sha256。

    两个值都来自**真实文件字节**（不是常量、不是同一个值自比）：
      * ``pre_digest`` 是下载器把媒体落到任务目录那一刻现算的；
      * ``engine_digest`` 由引擎替身在真正被调用时现算。
    另外用样本源文件的独立哈希做第三个锥点，并用 ``EVENTS`` 断言取样时序为
    ``download -> probe -> engine``：实际发生的真实 ffprobe 必须夹在两次取样
    之间，否则「没改字节」可能只是「压根没探测」。
    """
    assert len(engine_digests) == 1, engine_digests
    engine_path, engine_digest = engine_digests[0]
    assert engine_path == media_path

    assert len(engine_digest) == 64 and int(engine_digest, 16) >= 0
    assert pre_digest == engine_digest, "admission must not rewrite media bytes"
    assert engine_digest == file_digest(sample), "bytes must still match the lavfi sample"

    stages = [e[0] for e in EVENTS if e[1] == media_path or (e[0] == "probe" and e[2][-1] == media_path)]
    assert stages == ["download", "probe", "engine"], stages
    probe_events = [e for e in EVENTS if e[0] == "probe" and e[2][-1] == media_path]
    assert len(probe_events) == 1, probe_events
    return engine_digest


def _download_event(media_path):
    """下载落盘那一刻的真实字节哈希（真实文件现算，不是常量）。"""
    events = [e for e in EVENTS if e[0] == "download" and e[1] == media_path]
    assert len(events) == 1, events
    return events[0][2]


@requires_ffmpeg
def test_mixed_media_bytes_are_unchanged_across_admission(tmp_path, monkeypatch, wired):
    """mixed 实际 task 入口路径：准入前后 sha256 完全相等，且零转码调用。"""
    sample = build_mixed(tmp_path / "mixed_hash.mp4")
    url = "https://example.invalid/clip"
    downloader, rec = make_youtube_api_downloader(wired["tm"], sample)
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    result = transcription.process_transcription(
        task_id="t-mixed-hash",
        url=url,
        use_speaker_recognition=True,
    )
    assert result["status"] == "success", result
    assert len(wired["spy"].argvs_for("ffprobe")) == 1
    assert wired["spy"].argvs_for("ffmpeg") == []
    _assert_bytes_unchanged(
        rec.pre_admission_digests[RecordingFunASR.calls[0]],
        RecordingFunASR.digests, sample, RecordingFunASR.calls[0],
    )


@requires_ffmpeg
def test_youtube_api_audio_only_reaches_engine(tmp_path, monkeypatch, wired):
    sample = build_audio_only(tmp_path / "api_audio.m4a")
    downloader, rec = make_youtube_api_downloader(wired["tm"], sample)
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    result = transcription.process_transcription(
        task_id="t-api-ok",
        url="https://www.youtube.com/watch?v=vid159",
        use_speaker_recognition=True,
    )
    assert rec.methods == ["fetch_for_transcription"], rec.calls
    assert result["status"] == "success", result
    assert len(RecordingFunASR.calls) == 1
    probes = wired["spy"].argvs_for("ffprobe")
    assert len(probes) == 1, probes


# ---------------------------------------------------------------------------
# 2b. YouTube 优先下载分支（download_video_with_priority）
# ---------------------------------------------------------------------------

YT_PRIORITY_URL = "https://www.youtube.com/watch?v=vid159"


@requires_ffmpeg
def test_youtube_priority_no_audio_track_is_rejected(tmp_path, monkeypatch, wired):
    """优先下载分支真实被走：无音轨 -> no_audio_track、引擎零调用。"""
    sample = build_video_only(tmp_path / "priority_video_only.mp4")
    downloader, rec = make_youtube_priority_downloader(wired["tm"], sample, YT_PRIORITY_URL)
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    task_id = "t-priority-noaudio"
    result = transcription.process_transcription(
        task_id=task_id,
        url=YT_PRIORITY_URL,
        use_speaker_recognition=True,
    )

    assert rec.methods == ["get_download_info", "download_video_with_priority"], rec.calls
    assert "download_file" not in rec.methods, "priority route must not fall back"
    assert downloader.use_api_server is False

    assert result["status"] == "failed", result
    assert "no_audio_track" in result["message"]
    error = _terminal_failure(wired["router"], wired["cache"], task_id)
    assert "no_audio_track" in error and "media_probe_failed" not in error
    assert _engine_calls() == []
    for path in rec.media_paths:
        assert not path.exists()
    assert wired["tm"].get_task_dir(task_id) is None


@requires_ffmpeg
def test_youtube_priority_probe_failure_is_check_failure(tmp_path, monkeypatch, wired):
    """优先下载分支的探测失败归因：media_probe_failed，不得写成无音轨。"""
    sample = build_video_only(tmp_path / "priority_probe_fail.mp4")
    downloader, rec = make_youtube_priority_downloader(wired["tm"], sample, YT_PRIORITY_URL)
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)
    monkeypatch.setattr(subprocess, "run", _probe_raises(FileNotFoundError(
        2, "No such file or directory", "ffprobe")))

    task_id = "t-priority-probe-fail"
    result = transcription.process_transcription(
        task_id=task_id,
        url=YT_PRIORITY_URL,
        use_speaker_recognition=True,
    )
    assert rec.methods == ["get_download_info", "download_video_with_priority"], rec.calls
    assert result["status"] == "failed", result
    assert "media_probe_failed" in result["message"]
    assert "不含音轨" not in result["message"]
    error = _terminal_failure(wired["router"], wired["cache"], task_id)
    assert "media_probe_failed" in error and "不含音轨" not in error
    assert _engine_calls() == []


@requires_ffmpeg
def test_youtube_priority_mixed_media_is_admitted_with_intact_bytes(
    tmp_path, monkeypatch, wired
):
    """含音轨对照：优先下载分支放行进 FunASR，真实 ffprobe 边界 + 字节不变。"""
    sample = build_mixed(tmp_path / "priority_mixed.mp4")
    downloader, rec = make_youtube_priority_downloader(wired["tm"], sample, YT_PRIORITY_URL)
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    result = transcription.process_transcription(
        task_id="t-priority-ok",
        url=YT_PRIORITY_URL,
        use_speaker_recognition=True,
    )
    assert rec.methods == ["get_download_info", "download_video_with_priority"], rec.calls
    assert result["status"] == "success", result
    assert len(RecordingFunASR.calls) == 1
    probes = wired["spy"].argvs_for("ffprobe")
    assert len(probes) == 1, probes
    assert probes[0][0] == "ffprobe" and probes[0][-1] == RecordingFunASR.calls[0]
    assert wired["spy"].argvs_for("ffmpeg") == []
    _assert_bytes_unchanged(
        rec.pre_admission_digests[RecordingFunASR.calls[0]],
        RecordingFunASR.digests, sample, RecordingFunASR.calls[0],
    )


@requires_ffmpeg
def test_youtube_api_subtitle_hit_skips_asr_and_probe(tmp_path, monkeypatch, wired):
    """API 字幕直命中：producer 返回字幕、不落音频路径 → 零准入探测、零 ASR 调用。

    字幕分支在 ``transcription.py:1913`` 就收口，位置早于 API 准入点（:2027）与两处
    ASR 调用；本用例把「不探测、不调引擎」钉成回归断言。
    """
    sample = build_audio_only(tmp_path / "unused_sample.m4a")  # 字幕路径根本不会碰它
    url = "https://www.youtube.com/watch?v=vid159"
    downloader, rec = make_youtube_api_downloader(wired["tm"], sample, subtitle="api subtitle text")
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)

    result = transcription.process_transcription(
        task_id="t-api-subtitle",
        url=url,
        use_speaker_recognition=False,
    )

    # 生产 API 字幕快路径真的被走
    assert rec.methods == ["fetch_for_transcription"], rec.calls
    assert "download_file" not in rec.methods and "get_download_info" not in rec.methods
    assert rec.media_paths == [], "subtitle hit must not materialize any media"
    assert downloader.use_api_server is True

    assert result["status"] == "success", result
    assert result["data"]["transcript"] == "api subtitle text"
    assert wired["cache"].saved, "subtitle hit must still save the transcript"
    assert wired["cache"].saved[0]["transcript_data"] == "api subtitle text"

    # 零探测 + 零引擎
    assert wired["spy"].argvs_for("ffprobe") == []
    assert wired["spy"].calls == []
    assert _engine_calls() == []

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
    downloader, rec = make_youtube_api_downloader(wired["tm"], sample)
    monkeypatch.setattr(transcription, "create_downloader", lambda u: downloader)
    monkeypatch.setattr(
        subprocess, "run",
        _probe_raises(FileNotFoundError(2, "No such file or directory", "ffprobe")),
    )

    result = transcription.process_transcription(
        task_id="t-api-probe-fail",
        url="https://www.youtube.com/watch?v=vid159",
        use_speaker_recognition=True,
    )
    assert rec.methods == ["fetch_for_transcription"], rec.calls
    assert result["status"] == "failed", result
    assert "media_probe_failed" in result["message"]
    assert "不含音轨" not in result["message"]
    # 失败通知与 FAILED 终态：探测失败也必须落到用户看得见的那条通道，
    # 且归因是「检查失败」而不是「不含音轨」。
    error = _terminal_failure(wired["router"], wired["cache"], "t-api-probe-fail")
    assert "media_probe_failed" in error
    assert "不含音轨" not in error and "no_audio_track" not in error
    assert wired["cache"].saved == []
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
