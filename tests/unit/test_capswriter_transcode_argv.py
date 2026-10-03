"""CapsWriter SDK 转码 argv 的音轨限定守卫。

背景（#155）：`capswriter_asr.client._transcode` 若不显式选轨，ffmpeg 的自动选流
会把视频轨拉进解码图，转码耗时高出约两个数量级（60 秒 720p 样本：2.60s vs 0.14s；
生产实测 214s vs 1.04s）。上游在 PR #53 修复（`client.py:_transcode` 加 `-map 0:a:0`），
契约同时写入 `sdk/README.md` 与 `docs/reference/protocol.md`。

本文件把该契约钉在 argv 上。断言只落在 argv，不落在产物字节或耗时上：

* 产物字节数在有无 `-map` 时完全一致——慢在解码，不在编码，也不在产物，写产物
  断言等于写恒真判据；
* 耗时倍率与样本大小强相关，写进 CI 必然 flaky。

本文件**不依赖 ffmpeg 是否存在**：`_transcode` 的两处环境依赖（`shutil.which`
与 `_run_process`）都被替换掉，因此在任何环境（含 PATH 为空）下都能观察到 argv，
也不会退化成恒 skip。
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from capswriter_asr import client as sdk_client


ENCODINGS = ("flac", "s16le", "f32le", "ogg_opus")

#: 假的 ffmpeg 路径：测试全程不执行它，只用它占住 argv[0]。
FAKE_FFMPEG = "/nonexistent/bin/ffmpeg"


class _RecordingRunner:
    """替换 `_run_process`：记录 argv 并返回空输出，不启动任何子进程。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, *args: str) -> tuple[int, bytes, bytes]:
        self.calls.append(args)
        return 0, b"", b""


def _capture_transcode_argv(monkeypatch, encoding: str) -> tuple[str, ...]:
    """跑一次 `_transcode`，返回本次交给 ffmpeg 的 argv。"""
    monkeypatch.setattr(
        sdk_client,
        "shutil",
        SimpleNamespace(
            which=lambda name: FAKE_FFMPEG if name == "ffmpeg" else None
        ),
    )
    runner = _RecordingRunner()
    monkeypatch.setattr(sdk_client, "_run_process", runner)

    # 空 stdout 对 s16le/f32le 分支都算样本对齐，不会走到截断分支。
    asyncio.run(sdk_client._transcode(Path("/nonexistent/sample.mp4"), encoding))

    assert len(runner.calls) == 1, "期望恰好启动一次 ffmpeg"
    return runner.calls[0]


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_transcode_argv_maps_first_audio_track(monkeypatch, encoding):
    """退化写法一：`-map` 后面必须紧跟 `0:a:0`（四种编码共用同一条规则）。"""
    argv = _capture_transcode_argv(monkeypatch, encoding)

    assert "-map" in argv, f"{encoding}: argv 缺少显式选轨（回到 #155 的缺陷状态）"
    map_index = argv.index("-map")
    assert argv[map_index + 1 : map_index + 2] == (
        "0:a:0",
    ), f"{encoding}: `-map` 之后必须紧跟 0:a:0，实际 argv={list(argv)}"


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_transcode_argv_does_not_fall_back_to_vn(monkeypatch, encoding):
    """退化写法二：用 `-vn` 顶替。它只说「不要视频」，没写明选哪条音轨。"""
    argv = _capture_transcode_argv(monkeypatch, encoding)

    assert (
        "-vn" not in argv
    ), f"{encoding}: 不得用 -vn 顶替显式选轨，实际 argv={list(argv)}"


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_transcode_argv_map_is_not_optional(monkeypatch, encoding):
    """退化写法三：`0:a:0?` 会让无音轨文件静默产出空音频。

    本仓的下载校验目前放行无音轨文件，硬失败（ffmpeg 报错）才是期望行为，
    所以这个 `?` 不能出现。
    """
    argv = _capture_transcode_argv(monkeypatch, encoding)

    map_index = argv.index("-map")
    map_spec = argv[map_index + 1]
    assert not map_spec.endswith("?"), (
        f"{encoding}: `-map` 的值不得带可选标记 ?（无音轨文件必须硬失败），"
        f"实际 map_spec={map_spec!r}"
    )


def test_transcode_argv_keeps_input_before_output_options(monkeypatch):
    """`-map` 只出现一次且排在 `-i` 之后——放在 `-i` 之前对 ffmpeg 无意义。"""
    argv = _capture_transcode_argv(monkeypatch, "flac")

    assert argv.count("-i") == 1
    assert argv.count("-map") == 1
    assert argv.index("-i") < argv.index("-map")