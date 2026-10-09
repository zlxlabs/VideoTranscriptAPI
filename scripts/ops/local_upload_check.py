#!/usr/bin/env python3
"""Read-only local upload preflight; it never enables, deploys, or restores data."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MINIMUM_SAFE_SHA = "6af391d20edab8dfd8b320ce6b91d4d19e3260e2"
UPLOAD_LIMIT_FIELDS = (
    "max_file_mib",
    "max_media_hours",
    "receive_concurrency",
    "upload_temp_budget_mib",
)


class CheckFailure(RuntimeError):
    """A local check could not produce an evidence-backed result."""


def valid_upload_limits(config: dict[str, Any]) -> tuple[bool, str]:
    storage = config.get("storage")
    limits = storage.get("upload_limits") if isinstance(storage, dict) else None
    if not isinstance(limits, dict):
        return False, "missing"
    values = [limits.get(field) for field in UPLOAD_LIMIT_FIELDS]
    for field, value in zip(UPLOAD_LIMIT_FIELDS, values, strict=True):
        if field == "receive_concurrency":
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                return False, "invalid"
        elif (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            return False, "invalid"
    return True, "valid"


def inspect_sample(path: Path, ffprobe: str = "ffprobe") -> dict[str, float | int]:
    if not path.is_file():
        raise CheckFailure("sample_not_file")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise CheckFailure(f"ffprobe_failed_rc_{result.returncode}")
    probe = json.loads(result.stdout)
    streams = probe.get("streams")
    duration = probe.get("format", {}).get("duration")
    if not isinstance(streams, list) or not any(
        isinstance(item, dict) and item.get("codec_type") in {"audio", "video"}
        for item in streams
    ):
        raise CheckFailure("sample_has_no_media_stream")
    try:
        duration_seconds = float(duration)
    except (TypeError, ValueError) as exc:
        raise CheckFailure("sample_duration_unknown") from exc
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise CheckFailure("sample_duration_invalid")
    return {"bytes": path.stat().st_size, "duration_seconds": duration_seconds}


def compatibility_status(repo: Path, source_sha: str | None) -> str:
    if source_sha is None:
        return "unknown"
    if not re.fullmatch(r"[0-9a-fA-F]{40}", source_sha):
        return "invalid"
    resolved = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{source_sha}^{{commit}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if resolved.returncode != 0 or resolved.stdout.strip().lower() != source_sha.lower():
        return "unknown"
    ancestor = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "merge-base",
            "--is-ancestor",
            MINIMUM_SAFE_SHA,
            source_sha,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return "safe-lineage" if ancestor.returncode == 0 else "incompatible"


def deployment_env_source(compose_path: Path) -> str:
    """Report the tracked env_file declaration without reading any dotenv file."""
    if not compose_path.is_file():
        return "unknown"
    lines = compose_path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "env_file:" and index + 1 < len(lines):
            candidate = lines[index + 1].strip()
            if candidate.startswith("- "):
                return candidate[2:].split()[0]
    return "unknown"


def run_check(args: argparse.Namespace) -> int:
    from video_transcript_api.api.context import load_and_validate_config

    config = load_and_validate_config(args.config)
    limits_ok, limits_state = valid_upload_limits(config)
    print(f"upload_limits={limits_state}")

    data_path = Path(args.data_dir)
    if not data_path.is_dir():
        raise CheckFailure("data_dir_not_directory")
    disk = shutil.disk_usage(data_path)
    print(f"data_disk_free_bytes={disk.free}")

    sample_state = "not_supplied"
    if args.sample:
        sample = inspect_sample(Path(args.sample), args.ffprobe)
        print(f"sample_bytes={sample['bytes']}")
        print(f"sample_duration_seconds={sample['duration_seconds']:.3f}")
        sample_state = "measured"

    compose_path = Path(args.compose)
    env_source = deployment_env_source(compose_path)
    print(f"deployment_env_file={env_source}")
    configured_gate = os.environ.get("VTA_UPLOADS_ENABLED")
    print(f"process_upload_gate={'exact_true' if configured_gate == 'true' else 'off_or_unset'}")
    print(f"source_compatibility={compatibility_status(Path(args.repo), args.source_sha)}")
    print("production_capacity_evidence=unknown")
    print("restore_domain_evidence=unknown")
    print("production_ingress_evidence=unknown")

    # Local checks describe inputs only. They cannot certify a production domain,
    # guarantee rollback-control independence, or authorize changing the gate.
    blocked_reasons = [
        "production capacity has not been measured",
        "independent restore domain has not been proved",
        "production ingress and runtime image have not been verified",
    ]
    if not limits_ok:
        blocked_reasons.append("four limits are not positive finite values")
    if sample_state == "not_supplied":
        blocked_reasons.append("no local media sample was measured")
    print("UPLOAD_ENABLE_BLOCKED: " + "; ".join(blocked_reasons))
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="actual consumer config path")
    parser.add_argument("--data-dir", required=True, help="actual data mount path")
    parser.add_argument("--compose", default=str(PROJECT_ROOT / "docker/docker-compose.deploy.yml"))
    parser.add_argument("--repo", default=str(PROJECT_ROOT), help="repository containing source commits")
    parser.add_argument("--source-sha", help="source revision proven for the runtime image")
    parser.add_argument("--sample", help="optional local media file inspected by ffprobe")
    parser.add_argument("--ffprobe", default="ffprobe")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run_check(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    raise SystemExit(main())
