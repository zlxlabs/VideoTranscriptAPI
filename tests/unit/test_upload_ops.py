"""Fail-closed contracts for local upload operations checks."""

import json
from pathlib import Path
import subprocess
import pytest

from scripts.ops import local_upload_check
from scripts.perf import local_upload_capacity


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


def test_sample_limit_check_compares_real_file_size_and_duration():
    config = {"storage": {"upload_limits": {
        "max_file_mib": 1,
        "max_media_hours": 1,
        "receive_concurrency": 1,
        "upload_temp_budget_mib": 2,
    }}}
    assert local_upload_check.sample_limit_status(
        {"bytes": 1024, "duration_seconds": 3600}, config
    ) == "within_declared_limits"
    assert local_upload_check.sample_limit_status(
        {"bytes": 1024 * 1024 + 1, "duration_seconds": 1}, config
    ) == "exceeds_file_limit"
    assert local_upload_check.sample_limit_status(
        {"bytes": 1, "duration_seconds": 3600.1}, config
    ) == "exceeds_duration_limit"
    assert local_upload_check.sample_limit_status(
        {"bytes": 1, "duration_seconds": 1}, {"storage": {}}
    ) == "unknown_limits"


def test_compatibility_uses_real_commit_ancestry_not_sha_text():
    repo = Path(__file__).resolve().parents[2]
    assert local_upload_check.compatibility_status(
        repo, "6af391d20edab8dfd8b320ce6b91d4d19e3260e2"
    ) == "safe-lineage"
    assert local_upload_check.compatibility_status(
        repo, "38b299468daaf309608f46ffe1c59886b0922c04"
    ) == "incompatible"


def test_missing_minimum_commit_query_is_unknown_from_real_git(tmp_path, monkeypatch):
    repo = tmp_path / "source-repository"
    repo.mkdir()
    isolated_gitconfig = tmp_path / "gitconfig"
    isolated_gitconfig.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(isolated_gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    minimum_sha = local_upload_check.MINIMUM_SAFE_SHA

    subprocess.run(["git", "init", "--quiet", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Upload Ops Fixture"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True)
    (repo / "producer.txt").write_bytes(b"real source commit fixture\\n")
    subprocess.run(["git", "-C", str(repo), "add", "--", "producer.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "fixture source"], check=True)

    producer = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    source_sha = producer.stdout.strip()
    assert len(source_sha) == 40
    int(source_sha, 16)

    absent_minimum = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{minimum_sha}^{{commit}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert absent_minimum.returncode == 128
    actual_query = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", minimum_sha, source_sha],
        check=False,
        capture_output=True,
        text=True,
    )
    assert actual_query.returncode == 128
    assert local_upload_check.compatibility_status(repo, source_sha) == "unknown"


def test_ffprobe_subprocess_receives_sample_path_and_json_contract(tmp_path, monkeypatch):
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
    monkeypatch.setenv("FFPROBE_CAPTURE", str(capture))
    monkeypatch.setenv("FFPROBE_OUTPUT", str(output))
    inspected = local_upload_check.inspect_sample(sample, str(fake_ffprobe))

    argv = json.loads(capture.read_text())
    assert argv == [
        "-v", "error", "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(sample),
    ]
    assert inspected == {"bytes": len(b"synthetic sample bytes"), "duration_seconds": 1.25}


def test_capacity_fixture_uses_only_sandbox_paths_and_loopback_peers(tmp_path):
    config_path, users_path = local_upload_capacity._write_synthetic_config(
        tmp_path, api_port=18080, asr_port=18081
    )
    config = json.loads(config_path.read_text())
    sandbox_data = tmp_path / "data"

    assert Path(config["storage"]["cache_dir"]).is_relative_to(sandbox_data)
    assert Path(config["storage"]["workspace_dir"]).is_relative_to(sandbox_data)
    assert Path(config["storage"]["temp_dir"]).is_relative_to(sandbox_data)
    assert config["api"]["host"] == "127.0.0.1"
    assert config["capswriter"]["server_url"] == "ws://127.0.0.1:18081"
    assert config["security"]["download_url_allowlist"] == ["127.0.0.1/32"]
    assert config["storage"]["upload_limits"]["receive_concurrency"] == 2
    assert "VTA_UPLOADS_ENABLED" not in config
    assert "sandbox-token-never-use-outside-this-run" in users_path.read_text()
    assert not (tmp_path / "data").exists()


def test_capacity_experiment_rejects_unbounded_sample_duration():
    with pytest.raises(local_upload_capacity.ExperimentFailure, match="1_and_30"):
        local_upload_capacity.run_experiment(31)


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
