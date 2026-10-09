"""Fail-closed contracts for local upload operations checks."""

import json
from pathlib import Path
import pytest

from scripts.ops import local_upload_check


def test_upload_limits_must_all_be_positive_finite_and_concurrency_integral():
    limits = {
        "max_file_mib": 1.5,
        "max_media_hours": 2,
        "receive_concurrency": 1,
        "upload_temp_budget_mib": 4,
    }
    assert local_upload_check.valid_upload_limits({"storage": {"upload_limits": limits}}) == (True, "valid")

    for field, value in (
        ("max_file_mib", None),
        ("max_media_hours", float("inf")),
        ("upload_temp_budget_mib", float("nan")),
        ("receive_concurrency", True),
        ("receive_concurrency", 1.5),
    ):
        invalid = limits | {field: value}
        assert local_upload_check.valid_upload_limits(
            {"storage": {"upload_limits": invalid}}
        ) == (False, "invalid")

    assert local_upload_check.valid_upload_limits({"storage": {}}) == (False, "missing")


def test_ffprobe_subprocess_receives_sample_path_and_json_contract(tmp_path):
    capture = tmp_path / "argv.json"
    output = tmp_path / "probe.json"
    sample = tmp_path / "synthetic.wav"
    sample.write_bytes(b"synthetic sample bytes")
    output.write_text(json.dumps({"streams": [{"codec_type": "audio"}], "format": {"duration": "1.25"}}))
    fake_ffprobe = tmp_path / "ffprobe"
    fake_ffprobe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "open(os.environ['FFPROBE_CAPTURE'], 'w').write(json.dumps(sys.argv[1:]))\n"
        "print(open(os.environ['FFPROBE_OUTPUT']).read())\n"
    )
    fake_ffprobe.chmod(0o755)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("FFPROBE_CAPTURE", str(capture))
    monkeypatch.setenv("FFPROBE_OUTPUT", str(output))
    try:
        inspected = local_upload_check.inspect_sample(sample, str(fake_ffprobe))
    finally:
        monkeypatch.undo()

    argv = json.loads(capture.read_text())
    assert argv == [
        "-v", "error", "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(sample),
    ]
    assert inspected == {"bytes": len(b"synthetic sample bytes"), "duration_seconds": 1.25}


def test_preflight_logs_upload_enable_blocked_without_reading_dotenv(tmp_path, monkeypatch, capsys):
    secret = "do-not-print-this-dotenv-secret"
    env_file = tmp_path / ".env"
    env_file.write_text(f"VTA_UPLOADS_ENABLED=true\nTOKEN={secret}\n")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = {
        "storage": {
            "upload_limits": {
                "max_file_mib": None,
                "max_media_hours": None,
                "receive_concurrency": None,
                "upload_temp_budget_mib": None,
            }
        }
    }
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "false")
    monkeypatch.setattr(
        "video_transcript_api.api.context.load_and_validate_config",
        lambda _path: config,
        raising=False,
    )

    args = local_upload_check.build_parser().parse_args(
        ["--config", str(tmp_path / "config.jsonc"), "--data-dir", str(data_dir),
         "--compose", str(Path(__file__).resolve().parents[2] / "docker/docker-compose.deploy.yml"),
         "--repo", str(Path(__file__).resolve().parents[2])]
    )
    assert local_upload_check.run_check(args) == 2
    output = capsys.readouterr().out

    assert "upload_limits=invalid" in output
    assert "deployment_env_file=.env" in output
    assert "process_upload_gate=off_or_unset" in output
    assert "UPLOAD_ENABLE_BLOCKED:" in output
    assert "restore domain has not been proved" in output
    assert secret not in output


def test_process_gate_true_does_not_certify_enablement(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = {"storage": {"upload_limits": {
        "max_file_mib": 1,
        "max_media_hours": 1,
        "receive_concurrency": 1,
        "upload_temp_budget_mib": 2,
    }}}
    monkeypatch.setenv("VTA_UPLOADS_ENABLED", "true")
    monkeypatch.setattr(
        "video_transcript_api.api.context.load_and_validate_config",
        lambda _path: config,
        raising=False,
    )
    args = local_upload_check.build_parser().parse_args(
        ["--config", str(tmp_path / "config.jsonc"), "--data-dir", str(data_dir),
         "--compose", str(Path(__file__).resolve().parents[2] / "docker/docker-compose.deploy.yml"),
         "--repo", str(Path(__file__).resolve().parents[2]), "--source-sha", "not-a-sha"]
    )

    assert local_upload_check.run_check(args) == 2
    output = capsys.readouterr().out
    assert "upload_limits=valid" in output
    assert "process_upload_gate=exact_true" in output
    assert "source_compatibility=invalid" in output
    assert "UPLOAD_ENABLE_BLOCKED:" in output
