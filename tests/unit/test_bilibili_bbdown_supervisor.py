"""BBDown 受控重试监督器的单元测试。

这些测试不启动真实 BBDown、不会访问网络，也不会调用 ffprobe；进程、时钟和
临时目录均由可控 fake 提供，以锁定共享预算和空闲检测的边界。
"""

from __future__ import annotations

import io
import signal
import subprocess
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

from video_transcript_api.downloaders.bilibili import BilibiliDownloader


BV_ID = "BV1AoEg6SEW4"
CANONICAL_URL = f"https://www.bilibili.com/video/{BV_ID}"


class FakeClock:
    """由测试主动推进的单调时钟。"""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeProcess:
    """poll 驱动的受控进程，可在指定轮次写入下载文件。"""

    _next_pid = 10000

    def __init__(self, poll_results, on_poll=None, stdout=b"", stderr=b""):
        self.poll_results = iter(poll_results)
        self.on_poll = on_poll
        self.poll_count = 0
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1

    def poll(self):
        self.poll_count += 1
        if self.on_poll:
            self.on_poll(self.poll_count)
        try:
            result = next(self.poll_results)
        except StopIteration:
            result = self.returncode
        if result is not None:
            self.returncode = result
        return result

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode if self.returncode is not None else -15


class TerminationProcess:
    """用于隔离验证进程树终止顺序的 Popen 替身。"""

    def __init__(self, pid=4321, poll_result=None, wait_results=()):
        self.pid = pid
        self.poll_result = poll_result
        self.wait = Mock(side_effect=wait_results)

    def poll(self):
        return self.poll_result


class FakeTempManager:
    """为每次 BBDown 尝试分配可观察的独立目录。"""

    def __init__(self, root: Path):
        self.root = root
        self.attempt_dirs = []
        self.target_index = 0

    def create_temp_dir(self, prefix="tmp_"):
        path = self.root / f"{prefix}{len(self.attempt_dirs)}"
        path.mkdir()
        self.attempt_dirs.append(path)
        return str(path)

    def create_temp_file(self, suffix=""):
        path = self.root / f"final_{self.target_index}{suffix}"
        self.target_index += 1
        return str(path)


@pytest.fixture
def supervisor(tmp_path):
    cfg = {
        "bbdown": {
            "executable_linux": "fake-bbdown",
            "timeout": 90,
            "audio_only": True,
        }
    }
    with patch("video_transcript_api.downloaders.base.load_config", return_value=cfg):
        downloader = BilibiliDownloader()
    downloader.config = cfg
    downloader.temp_manager = FakeTempManager(tmp_path)
    return downloader


def _run_with_fakes(downloader, processes, clock, url=CANONICAL_URL):
    """调用受测入口，同时替换进程、时间和可执行文件检查。"""
    def signal_process_group(pgid, sig):
        for process in processes:
            if process.pid != pgid:
                continue
            if sig == signal.SIGTERM:
                process.terminated = True
                process.returncode = -15
            elif sig == signal.SIGKILL:
                process.killed = True
                process.returncode = -9

    with patch("video_transcript_api.downloaders.bilibili.os.path.exists", return_value=True), patch(
        "video_transcript_api.downloaders.bilibili.subprocess.Popen",
        side_effect=processes,
    ) as popen, patch(
        "video_transcript_api.downloaders.bilibili.time.monotonic",
        side_effect=clock.monotonic,
    ), patch(
        "video_transcript_api.downloaders.bilibili.time.sleep",
        side_effect=clock.sleep,
    ), patch(
        "video_transcript_api.downloaders.bilibili.os.killpg",
        side_effect=signal_process_group,
    ):
        return downloader._get_video_info_bbdown(url), popen


def test_b23_input_uses_canonical_bv_url_and_skip_flags(supervisor):
    """短链只在本地解析一次，BBDown 接收 BV 长链及跳过无关资源的参数。"""
    clock = FakeClock()
    process = FakeProcess([0], stdout=b"done\n")
    supervisor.resolve_short_url = lambda _: CANONICAL_URL + "?p=2"
    supervisor._validate_media_file = lambda _: True

    def write_media(_):
        attempt_dir = supervisor.temp_manager.attempt_dirs[0]
        (attempt_dir / "[BV1AoEg6SEW4]title.m4a").write_bytes(b"media")

    process.on_poll = lambda count: write_media(count) if count == 1 else None
    result, popen = _run_with_fakes(supervisor, [process], clock, "https://b23.tv/abc")

    args = popen.call_args.args[0]
    assert CANONICAL_URL in args
    assert "https://b23.tv/abc" not in args
    assert args[args.index("-p") + 1] == "2"
    assert {"--skip-subtitle", "--skip-cover", "--skip-ai"} <= set(args)
    assert Path(result["local_file"]).exists()


def test_preflight_failure_retries_and_returns_second_valid_file(supervisor):
    """第一次预取停滞后会清理并重试，第二次有效文件成功即停止。"""
    clock = FakeClock()
    stalled = FakeProcess([None] * 30)
    success = FakeProcess([0])
    supervisor._validate_media_file = lambda _: True

    def write_media(count):
        if count == 1:
            attempt_dir = supervisor.temp_manager.attempt_dirs[1]
            (attempt_dir / "[BV1AoEg6SEW4]ok.m4a").write_bytes(b"media")

    success.on_poll = write_media
    result, popen = _run_with_fakes(supervisor, [stalled, success], clock)

    assert popen.call_count == 2
    assert stalled.terminated is True
    assert Path(result["local_file"]).exists()
    assert not supervisor.temp_manager.attempt_dirs[0].exists()


def test_continuing_file_growth_uses_download_idle_threshold_not_preflight(supervisor):
    """已有文件持续增长时，即使总时长超过预取阈值也不应被误杀。"""
    clock = FakeClock()
    process = FakeProcess([None] * 25 + [0])
    supervisor._validate_media_file = lambda _: True

    def grow_media(count):
        attempt_dir = supervisor.temp_manager.attempt_dirs[0]
        media = attempt_dir / "[BV1AoEg6SEW4]growing.m4a"
        media.write_bytes(b"x" * count)

    process.on_poll = grow_media
    result, popen = _run_with_fakes(supervisor, [process], clock)

    assert popen.call_count == 1
    assert process.terminated is False
    assert Path(result["local_file"]).exists()


def test_all_failures_share_total_budget_and_report_attempt_and_stage(supervisor):
    """连续预取停滞不会把 90 秒配置放大为三倍，错误含 attempt/stage。"""
    clock = FakeClock()
    processes = [FakeProcess([None] * 100) for _ in range(3)]

    with pytest.raises(ValueError, match=r"attempt.*stage=preflight_stalled"):
        _run_with_fakes(supervisor, processes, clock)

    assert clock.now <= 90
    assert all(not path.exists() for path in supervisor.temp_manager.attempt_dirs)


@pytest.mark.parametrize("filename,valid", [("empty.m4a", True), ("invalid.m4a", False)])
def test_exit_zero_invalid_media_retries(supervisor, filename, valid):
    """exit 0 的空文件或 ffprobe 无效文件不是成功，且会在预算内重试。"""
    clock = FakeClock()
    first = FakeProcess([0])
    second = FakeProcess([0])
    validations = iter([valid, True])
    supervisor._validate_media_file = lambda _: next(validations)

    def write_first(count):
        if count == 1:
            (supervisor.temp_manager.attempt_dirs[0] / filename).write_bytes(
                b"bad" if filename == "invalid.m4a" else b""
            )

    def write_second(count):
        if count == 1:
            (supervisor.temp_manager.attempt_dirs[1] / "good.m4a").write_bytes(b"good")

    first.on_poll = write_first
    second.on_poll = write_second
    result, popen = _run_with_fakes(supervisor, [first, second], clock)

    assert popen.call_count == 2
    assert Path(result["local_file"]).exists()
    assert not supervisor.temp_manager.attempt_dirs[0].exists()


def test_success_does_not_start_later_attempt_and_cleans_attempt_dir(supervisor):
    """首轮有效结果返回后不启动后续进程，并清理已搬空的尝试目录。"""
    clock = FakeClock()
    success = FakeProcess([0])
    supervisor._validate_media_file = lambda _: True

    def write_media(count):
        if count == 1:
            (supervisor.temp_manager.attempt_dirs[0] / "good.mp3").write_bytes(b"good")

    success.on_poll = write_media
    result, popen = _run_with_fakes(supervisor, [success], clock)

    assert popen.call_count == 1
    assert Path(result["local_file"]).exists()
    assert not supervisor.temp_manager.attempt_dirs[0].exists()


def test_terminate_bbdown_process_posix_terminates_group_then_kills_and_reaps_parent():
    """POSIX 超时后按 TERM、KILL 顺序处理整个会话组并等待父进程回收。"""
    process = TerminationProcess(
        wait_results=[subprocess.TimeoutExpired("BBDown", 1.5), -9]
    )

    with patch("video_transcript_api.downloaders.bilibili.os.name", "posix"), patch(
        "video_transcript_api.downloaders.bilibili.os.killpg"
    ) as killpg:
        BilibiliDownloader._terminate_bbdown_process(process, grace_seconds=1.5)

    assert killpg.call_args_list == [
        call(process.pid, signal.SIGTERM),
        call(process.pid, signal.SIGKILL),
    ]
    assert process.wait.call_args_list == [call(timeout=1.5), call()]


def test_terminate_bbdown_process_posix_does_not_signal_reused_group_after_exit():
    """已退出的父进程已被 poll 回收，不能再对可能复用的 pgid 发信号。"""
    process = TerminationProcess(poll_result=0)

    with patch("video_transcript_api.downloaders.bilibili.os.name", "posix"), patch(
        "video_transcript_api.downloaders.bilibili.os.killpg"
    ) as killpg:
        BilibiliDownloader._terminate_bbdown_process(process)

    killpg.assert_not_called()
    process.wait.assert_not_called()


def test_terminate_bbdown_process_windows_terminates_tree_and_reaps_parent():
    """Windows 使用 taskkill /T 处理创建的新进程组，必要时强杀整个树。"""
    process = TerminationProcess(
        wait_results=[subprocess.TimeoutExpired("BBDown", 1.5), -9]
    )

    with patch("video_transcript_api.downloaders.bilibili.os.name", "nt"), patch(
        "video_transcript_api.downloaders.bilibili.subprocess.run"
    ) as run:
        BilibiliDownloader._terminate_bbdown_process(process, grace_seconds=1.5)

    assert run.call_args_list == [
        call(
            ["taskkill", "/PID", str(process.pid), "/T"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        ),
        call(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ),
    ]
    assert process.wait.call_args_list == [call(timeout=1.5), call()]


@pytest.mark.parametrize("os_name", ["posix", "nt"])
def test_termination_failures_do_not_mask_supervisor_download_error(supervisor, os_name):
    """发送终止信号或 wait 出错时，调用方仍报告原始的下载监督失败。"""
    clock = FakeClock()
    processes = [FakeProcess([None] * 30) for _ in range(3)]
    for process in processes:
        process.wait = Mock(side_effect=OSError("termination failed"))

    with patch("video_transcript_api.downloaders.bilibili.os.path.exists", return_value=True), patch(
        "video_transcript_api.downloaders.bilibili.subprocess.Popen", side_effect=processes
    ), patch(
        "video_transcript_api.downloaders.bilibili.time.monotonic", side_effect=clock.monotonic
    ), patch(
        "video_transcript_api.downloaders.bilibili.time.sleep", side_effect=clock.sleep
    ), patch("video_transcript_api.downloaders.bilibili.os.name", os_name):
        if os_name == "nt":
            termination = patch(
                "video_transcript_api.downloaders.bilibili.subprocess.run",
                side_effect=OSError("taskkill failed"),
            )
        else:
            termination = patch(
                "video_transcript_api.downloaders.bilibili.os.killpg",
                side_effect=OSError("killpg failed"),
            )
        with termination, pytest.raises(
            ValueError, match=r"stage=preflight_stalled"
        ):
            supervisor._get_video_info_bbdown(CANONICAL_URL)
