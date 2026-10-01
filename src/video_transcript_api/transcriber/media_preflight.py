"""Preflight media duration checks for CapsWriter transcription."""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from ..utils.logging import setup_logger


MEDIA_DURATION_TOLERANCE_SECONDS = 1.0
AUDIO_SAMPLES_PER_FRAME = {
    "aac": 1024,
    "aac_latm": 1024,
    "mp3": 1152,
}

logger = setup_logger("media_preflight")


def resolve_transcription_source(media_path: Path, work_dir: Path) -> Path:
    """Return the source path that should be sent to CapsWriter.

    Declared container duration is compared with the decoder-facing sample
    count estimated from audio frame metadata. Packet timestamps are not used:
    ffprobe demux is truncated by the same container duration that CapsWriter
    reads, so a packet-tail probe cannot observe the mismatch.

    Missing frame-count metadata is not a command failure. The original path
    is returned and a greppable warning is logged. Command crashes and
    unparseable ffprobe output still fail fast.
    """
    media_path = Path(media_path)
    work_dir = Path(work_dir)
    try:
        declared_duration, audio_duration = _probe_media_duration(media_path)
        if audio_duration is None:
            logger.warning(
                "Media preflight sample count unavailable; using original path: "
                f"{media_path}"
            )
            return media_path
        if abs(declared_duration - audio_duration) <= MEDIA_DURATION_TOLERANCE_SECONDS:
            return media_path
        logger.info(
            "Media preflight duration mismatch; normalizing: "
            f"{media_path} declared={declared_duration:.6f}s "
            f"estimated={audio_duration:.6f}s"
        )
        return _normalize_media_to_flac(media_path, work_dir)
    except RuntimeError as exc:
        raise RuntimeError(f"Media preflight failed for {media_path}: {exc}") from exc


def _probe_media_duration(media_path: Path) -> tuple[float, Optional[float]]:
    """Read declared duration and estimated audio duration from metadata."""
    metadata = _run_ffprobe_metadata(media_path)
    format_data = metadata.get("format")
    streams = metadata.get("streams")
    if not isinstance(format_data, dict):
        raise RuntimeError("ffprobe output is missing media metadata")
    if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
        return (
            _parse_finite_number(format_data.get("duration"), "format duration"),
            None,
        )

    declared_duration = _parse_finite_number(
        format_data.get("duration"), "format duration"
    )
    return declared_duration, _estimate_audio_duration(streams[0])


def _estimate_audio_duration(stream: dict[str, Any]) -> Optional[float]:
    """Estimate decoded duration from frame count without decoding audio."""
    codec_name = stream.get("codec_name")
    samples_per_frame = AUDIO_SAMPLES_PER_FRAME.get(codec_name)
    frame_count = stream.get("nb_frames")
    sample_rate = stream.get("sample_rate")
    if samples_per_frame is None or frame_count in (None, "N/A") or sample_rate in (
        None,
        "N/A",
    ):
        return None
    try:
        frame_count_value = int(frame_count)
        sample_rate_value = float(sample_rate)
    except (TypeError, ValueError):
        return None
    if frame_count_value < 0 or not math.isfinite(sample_rate_value) or sample_rate_value <= 0:
        return None
    return frame_count_value * samples_per_frame / sample_rate_value


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
