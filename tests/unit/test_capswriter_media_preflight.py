"""Tests for CapsWriter media duration preflight."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from video_transcript_api.transcriber.capswriter_client import CapsWriterClient
from video_transcript_api.transcriber.media_preflight import (
    resolve_transcription_source,
)


PRODUCTION_FIXTURE_DIR = Path("/tmp/vta-preflight-fixtures")
GL_MP4 = PRODUCTION_FIXTURE_DIR / "gl.mp4"
BT_MP4 = PRODUCTION_FIXTURE_DIR / "bt.mp4"
NORMAL_M4A = PRODUCTION_FIXTURE_DIR / "normal.m4a"


def _completed(stdout: str = "", returncode: int = 0):
    """Build a subprocess result without invoking ffprobe or ffmpeg."""
    return type(
        "CompletedProcessStub",
        (),
        {"stdout": stdout, "stderr": "", "returncode": returncode},
    )()


def _metadata(
    duration: float,
    *,
    codec_name: str = "aac",
    sample_rate: str = "48000",
    nb_frames: str | None = "100",
) -> str:
    """Build the metadata-only ffprobe response."""
    stream = {
        "codec_name": codec_name,
        "sample_rate": sample_rate,
        "nb_frames": nb_frames,
    }
    return json.dumps({"format": {"duration": str(duration)}, "streams": [stream]})


def _make_client() -> CapsWriterClient:
    """Build a client without loading project configuration."""
    with patch.object(CapsWriterClient, "__init__", lambda self: None):
        client = CapsWriterClient()
    client.max_retries = 1
    client.retry_delay = 0
    client.log = MagicMock()
    return client


def test_consistent_container_uses_original_path_without_ffmpeg(tmp_path):
    media_path = tmp_path / "download.mp4"
    media_path.write_bytes(b"fixture")

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"FLAC")
        return _completed(_metadata(10.005333, nb_frames="469"))

    with patch(
        "video_transcript_api.transcriber.media_preflight.subprocess.run",
        side_effect=fake_run,
    ) as run:
        resolved = resolve_transcription_source(media_path, tmp_path)

    assert resolved == media_path
    commands = [call.args[0] for call in run.call_args_list]
    assert commands
    assert all(command[0] == "ffprobe" for command in commands)
    assert not any(command[0] == "ffmpeg" for command in commands)


def test_mismatched_aac_is_normalized_to_16khz_mono_flac(tmp_path):
    media_path = tmp_path / "recording.mp4"
    media_path.write_bytes(b"fixture")
    responses = iter(
        [
            _completed(
                _metadata(
                    4356.138625,
                    sample_rate="48000",
                    nb_frames="212853",
                )
            ),
            _completed(),
        ]
    )

    def fake_run(command, **kwargs):
        result = next(responses)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"FLAC")
        return result

    with patch(
        "video_transcript_api.transcriber.media_preflight.subprocess.run",
        side_effect=fake_run,
    ) as run:
        resolved = resolve_transcription_source(media_path, tmp_path)

    assert resolved != media_path
    assert resolved.suffix == ".flac"
    assert resolved.read_bytes() == b"FLAC"
    ffmpeg_commands = [
        call.args[0] for call in run.call_args_list if call.args[0][0] == "ffmpeg"
    ]
    assert len(ffmpeg_commands) == 1
    ffmpeg_command = ffmpeg_commands[0]
    assert ffmpeg_command[ffmpeg_command.index("-map") + 1] == "0:a:0"
    assert ffmpeg_command[ffmpeg_command.index("-ar") + 1] == "16000"
    assert ffmpeg_command[ffmpeg_command.index("-ac") + 1] == "1"
    assert ffmpeg_command[ffmpeg_command.index("-c:a") + 1] == "flac"

    resolved.unlink()


def test_small_duration_difference_does_not_normalize(tmp_path):
    media_path = tmp_path / "short.mp4"
    media_path.write_bytes(b"fixture")

    with patch(
        "video_transcript_api.transcriber.media_preflight.subprocess.run",
        return_value=_completed(_metadata(10.0, nb_frames="478")),
    ) as run:
        resolved = resolve_transcription_source(media_path, tmp_path)

    assert resolved == media_path
    assert all(call.args[0][0] == "ffprobe" for call in run.call_args_list)


def test_missing_frame_count_uses_original_path(tmp_path):
    media_path = tmp_path / "unknown.mp4"
    media_path.write_bytes(b"fixture")

    with patch(
        "video_transcript_api.transcriber.media_preflight.subprocess.run",
        return_value=_completed(_metadata(10.0, nb_frames="N/A")),
    ) as run, patch(
        "video_transcript_api.transcriber.media_preflight.logger.warning"
    ) as warning:
        resolved = resolve_transcription_source(media_path, tmp_path)

    assert resolved == media_path
    assert all(call.args[0][0] == "ffprobe" for call in run.call_args_list)
    warning.assert_called_once()
    assert "Media preflight sample count unavailable" in warning.call_args.args[0]


def test_ffprobe_failure_is_fail_fast(tmp_path):
    media_path = tmp_path / "broken.mp4"
    media_path.write_bytes(b"fixture")

    with patch(
        "video_transcript_api.transcriber.media_preflight.subprocess.run",
        return_value=_completed(returncode=1),
    ) as run:
        with pytest.raises(RuntimeError, match="ffprobe metadata failed"):
            resolve_transcription_source(media_path, tmp_path)

    assert run.call_count == 1


def test_invalid_ffprobe_output_is_fail_fast(tmp_path):
    media_path = tmp_path / "invalid.mp4"
    media_path.write_bytes(b"fixture")

    with patch(
        "video_transcript_api.transcriber.media_preflight.subprocess.run",
        return_value=_completed("not-json"),
    ):
        with pytest.raises(RuntimeError, match="not valid JSON"):
            resolve_transcription_source(media_path, tmp_path)


def test_ffmpeg_failure_removes_partial_flac(tmp_path):
    media_path = tmp_path / "recording.mp4"
    media_path.write_bytes(b"fixture")
    responses = iter(
        [
            _completed(
                _metadata(
                    4356.138625,
                    sample_rate="48000",
                    nb_frames="212853",
                )
            ),
            _completed(returncode=1),
        ]
    )

    with patch(
        "video_transcript_api.transcriber.media_preflight.subprocess.run",
        side_effect=lambda command, **kwargs: next(responses),
    ):
        with pytest.raises(RuntimeError, match="normalization failed"):
            resolve_transcription_source(media_path, tmp_path)

    assert list(tmp_path.glob("vta-preflight-*.flac")) == []


def test_preflight_failure_does_not_call_sdk(tmp_path):
    client = _make_client()
    media_path = tmp_path / "recording.mp4"
    media_path.write_bytes(b"fixture")

    with patch(
        "video_transcript_api.transcriber.capswriter_client.resolve_transcription_source",
        side_effect=RuntimeError("preflight failed"),
    ), patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync"
    ) as sdk_call:
        with pytest.raises(RuntimeError, match="preflight failed"):
            client.transcribe_file(str(media_path))

    sdk_call.assert_not_called()


def test_preflight_error_includes_media_path(tmp_path):
    media_path = tmp_path / "broken.mp4"
    media_path.write_bytes(b"fixture")

    with patch(
        "video_transcript_api.transcriber.media_preflight.subprocess.run",
        return_value=_completed(returncode=1),
    ):
        with pytest.raises(RuntimeError, match=str(media_path)):
            resolve_transcription_source(media_path, tmp_path)


@pytest.mark.parametrize(
    "sdk_side_effect",
    [
        None,
        RuntimeError("sdk failed"),
        KeyboardInterrupt(),
    ],
)
def test_normalized_source_is_removed_on_success_failure_and_exception(
    tmp_path, sdk_side_effect
):
    client = _make_client()
    client._save_results = AsyncMock(return_value=[tmp_path / "result.txt"])
    media_path = tmp_path / "recording.mp4"
    media_path.write_bytes(b"fixture")
    normalized_path = tmp_path / "vta-preflight-test.flac"
    normalized_path.write_bytes(b"FLAC")

    with patch(
        "video_transcript_api.transcriber.capswriter_client.resolve_transcription_source",
        return_value=normalized_path,
    ), patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        side_effect=sdk_side_effect,
    ):
        if isinstance(sdk_side_effect, KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                client.transcribe_file(str(media_path))
        else:
            success, files = client.transcribe_file(str(media_path))
            if sdk_side_effect is None:
                assert (success, files) == (
                    True,
                    [tmp_path / "result.txt"],
                )
            else:
                assert (success, files) == (False, [])

    assert not normalized_path.exists()


@pytest.mark.skipif(
    not GL_MP4.exists() or not BT_MP4.exists(),
    reason="production recorder fixtures not present at /tmp/vta-preflight-fixtures",
)
def test_production_recorder_files_are_normalized(tmp_path):
    from video_transcript_api.transcriber.media_preflight import (
        _probe_media_duration,
    )

    cases = (
        (GL_MP4, 4356.138625, 4540.864),
        (BT_MP4, 1220.437146, 1228.907),
    )
    for media_path, expected_declared, expected_audio in cases:
        declared, estimated = _probe_media_duration(media_path)
        assert estimated is not None
        assert abs(declared - expected_declared) < 0.001
        assert abs(estimated - expected_audio) < 0.001
        assert abs(declared - estimated) > 1.0

        real_run = subprocess.run

        def fake_run(command, **kwargs):
            if command[0] == "ffmpeg":
                Path(command[-1]).write_bytes(b"FLAC")
                return _completed()
            return real_run(command, **kwargs)

        with patch(
            "video_transcript_api.transcriber.media_preflight.subprocess.run",
            side_effect=fake_run,
        ):
            resolved = resolve_transcription_source(media_path, tmp_path)
        try:
            assert resolved != media_path
            assert resolved.suffix == ".flac"
            assert resolved.read_bytes() == b"FLAC"
        finally:
            if resolved != media_path:
                resolved.unlink(missing_ok=True)


@pytest.mark.skipif(
    not NORMAL_M4A.exists(),
    reason="normal.m4a fixture not present at /tmp/vta-preflight-fixtures",
)
def test_normal_m4a_uses_original_path_without_ffmpeg():
    with patch(
        "video_transcript_api.transcriber.media_preflight.subprocess.run",
        wraps=subprocess.run,
    ) as run:
        resolved = resolve_transcription_source(NORMAL_M4A, NORMAL_M4A.parent)

    assert resolved == NORMAL_M4A
    commands = [call.args[0] for call in run.call_args_list]
    assert commands
    assert all(command[0] == "ffprobe" for command in commands)
    assert not any(command[0] == "ffmpeg" for command in commands)
