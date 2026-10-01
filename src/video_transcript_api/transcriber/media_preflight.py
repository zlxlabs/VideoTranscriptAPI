"""Preflight media duration checks for CapsWriter transcription."""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


MEDIA_DURATION_TOLERANCE_SECONDS = 2.0
FFPROBE_TAIL_SECONDS = 1.0
AAC_SAMPLES_PER_FRAME = 1024


def resolve_transcription_source(media_path: Path, work_dir: Path) -> Path:
    """Return the source path that should be sent to CapsWriter.

    The initial probe reads container and stream metadata only. AAC files with
    a frame count use that metadata as the fast path; other codecs use a
    packet-tail probe. Only a duration mismatch beyond the tolerance causes a
    full normalization pass.
    """
    media_path = Path(media_path)
    work_dir = Path(work_dir)
    declared_duration, audio_duration = _probe_media_duration(media_path)

    if (
        math.isclose(
            declared_duration,
            audio_duration,
            abs_tol=MEDIA_DURATION_TOLERANCE_SECONDS,
        )
        or abs(declared_duration - audio_duration)
        <= MEDIA_DURATION_TOLERANCE_SECONDS
    ):
        return media_path

    return _normalize_media_to_flac(media_path, work_dir)


def _probe_media_duration(media_path: Path) -> tuple[float, float]:
    """Read the declared and metadata-derived audio durations."""
    metadata = _run_ffprobe_metadata(media_path)
    format_data = metadata.get("format")
    streams = metadata.get("streams")
    if not isinstance(format_data, dict) or not isinstance(streams, list):
        raise RuntimeError("ffprobe output is missing media metadata")
    if not streams or not isinstance(streams[0], dict):
        raise RuntimeError("ffprobe output has no audio stream")

    declared_duration = _parse_finite_number(
        format_data.get("duration"), "format duration"
    )
    stream = streams[0]
    codec_name = stream.get("codec_name")
    frame_count = stream.get("nb_frames")
    sample_rate = stream.get("sample_rate")

    if (
        codec_name == "aac"
        and frame_count not in (None, "N/A")
        and sample_rate not in (None, "N/A")
    ):
        frame_count_value = _parse_non_negative_integer(frame_count, "AAC frame count")
        sample_rate_value = _parse_finite_number(sample_rate, "audio sample rate")
        if sample_rate_value <= 0:
            raise RuntimeError("ffprobe returned an invalid audio sample rate")
        audio_duration = frame_count_value * AAC_SAMPLES_PER_FRAME / sample_rate_value
    else:
        audio_duration = _probe_audio_packet_tail(media_path, declared_duration)

    return declared_duration, audio_duration


def _run_ffprobe_metadata(media_path: Path) -> dict[str, Any]:
    """Run the metadata-only ffprobe command."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "format=duration:stream=codec_name,sample_rate,nb_frames",
        "-of",
        "json",
        str(media_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe metadata failed with exit code {result.returncode}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe metadata output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ffprobe metadata output is not an object")
    return payload


def _probe_audio_packet_tail(media_path: Path, declared_duration: float) -> float:
    """Read packet timestamps from the declared tail without decoding audio."""
    tail_start = max(declared_duration - FFPROBE_TAIL_SECONDS, 0.0)
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-read_intervals",
        f"{tail_start:.6f}%",
        "-show_entries",
        "packet=pts_time,duration_time",
        "-of",
        "csv=p=0",
        str(media_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe packet probe failed with exit code {result.returncode}")

    last_packet_end: float | None = None
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 2:
            raise RuntimeError("ffprobe packet output is not parseable")
        if "N/A" in fields:
            continue
        packet_pts = _parse_finite_number(fields[0], "packet PTS")
        packet_duration = _parse_finite_number(fields[1], "packet duration")
        if packet_duration < 0:
            raise RuntimeError("ffprobe returned a negative packet duration")
        packet_end = packet_pts + packet_duration
        if last_packet_end is None or packet_end > last_packet_end:
            last_packet_end = packet_end

    if last_packet_end is None:
        raise RuntimeError("ffprobe packet probe returned no usable audio packets")
    return last_packet_end


def _normalize_media_to_flac(media_path: Path, work_dir: Path) -> Path:
    """Normalize a mismatched media file and remove partial output on failure."""
    work_dir.mkdir(parents=True, exist_ok=True)
    output_fd, output_name = tempfile.mkstemp(
        dir=work_dir,
        prefix="vta-preflight-",
        suffix=".flac",
    )
    os.close(output_fd)
    output_path = Path(output_name)
    completed = False
    try:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media_path),
            "-map",
            "0:a:0",
            "-vn",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "flac",
            str(output_path),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=media_path.stat().st_size / 1024 / 1024 + 300,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg media normalization failed with exit code {result.returncode}"
            )
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg media normalization produced no output")
        completed = True
        return output_path
    finally:
        if not completed:
            output_path.unlink(missing_ok=True)


def _parse_finite_number(value: Any, field_name: str) -> float:
    """Parse a finite non-negative numeric ffprobe field."""
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"ffprobe returned an invalid {field_name}") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise RuntimeError(f"ffprobe returned an invalid {field_name}")
    return parsed


def _parse_non_negative_integer(value: Any, field_name: str) -> int:
    """Parse a non-negative integer ffprobe field."""
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"ffprobe returned an invalid {field_name}") from exc
    if parsed < 0:
        raise RuntimeError(f"ffprobe returned an invalid {field_name}")
    return parsed
