"""CapsWriter SDK samples_total wire-contract tests."""

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
import websockets

from capswriter_asr import transcribe_file
from unittest.mock import AsyncMock, patch


FIXTURE_DIR = Path("/tmp/vta-preflight-fixtures")
FIXTURES = (
    ("gl.mp4", 72_653_824),
    ("bt.mp4", 19_662_507),
    ("normal.m4a", 9_600_000),
)

pytestmark = pytest.mark.skipif(
    not all((FIXTURE_DIR / name).is_file() for name, _ in FIXTURES),
    reason="real preflight fixtures are unavailable",
)


async def _count_decoded_samples(ffmpeg: str, media_path: Path) -> int:
    """Count mono 16 kHz samples decoded independently from the fixture."""
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-v",
        "error",
        "-nostdin",
        "-i",
        str(media_path),
        "-map",
        "0:a:0",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "s16le",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdout is not None
    sample_bytes = 0
    while chunk := await process.stdout.read(1024 * 1024):
        sample_bytes += len(chunk)
    return_code = await process.wait()
    if return_code != 0:
        raise RuntimeError(f"independent ffmpeg decode failed with exit code {return_code}")
    assert sample_bytes % 2 == 0
    return sample_bytes // 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "expected_samples"),
    FIXTURES,
    ids=("live-recording-gl", "live-recording-bt", "normal-aac"),
)
async def test_sdk_final_frame_samples_total_matches_independent_decode(
    fixture_name: str,
    expected_samples: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK's real final WS payload must use decoded samples, not duration."""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None, "ffmpeg is required for this cross-boundary contract"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ffprobe = fake_bin / "ffprobe"
    fake_ffprobe.write_text("#!/bin/sh\nexit 1\n", encoding="ascii")
    fake_ffprobe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{Path(ffmpeg).parent}")

    media_path = FIXTURE_DIR / fixture_name
    decoded_samples = await _count_decoded_samples(ffmpeg, media_path)
    assert decoded_samples == expected_samples

    received_final: dict[str, object] | None = None

    async def receive_frames(websocket) -> None:
        nonlocal received_final
        async for message in websocket:
            frame = json.loads(message)
            if frame.get("is_final"):
                received_final = frame
                await websocket.send(
                    json.dumps(
                        {
                            "type": "result",
                            "is_final": True,
                            "text": "",
                            "tokens": [],
                            "timestamps": [],
                            "duration": 0.0,
                        }
                    )
                )
                return

    with patch("capswriter_asr.client._check_server", new=AsyncMock()):
        async with websockets.serve(
            receive_frames,
            "127.0.0.1",
            0,
            max_size=None,
            ping_interval=None,
        ) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            await transcribe_file(
                media_path,
                f"ws://127.0.0.1:{port}",
                encoding="flac",
                idle_timeout=30.0,
            )

    assert received_final is not None, "WS stub did not receive a final frame"
    print(
        "WS_SERVER_RECEIVED_FINAL_JSON="
        + json.dumps(received_final, ensure_ascii=True, separators=(",", ":"))
    )
    assert received_final["samples_total"] == decoded_samples
