import os
import subprocess
import time
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "docker" / "pull_and_deploy.sh"
PUSH_SCRIPT = Path(__file__).parents[2] / "docker" / "push_to_ghcr.sh"


def _fake_docker(
    tmp_path,
    *,
    preflight_ok=True,
    health="healthy",
    compose_fail_once=False,
    state_commit_ok=True,
    running_container_image_ref="",
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "echo \"image=$VIDEO_TRANSCRIPT_IMAGE $*\" >> \"$DOCKER_LOG\"\n"
        "case \"$1 $2\" in\n"
        "  'info '*) exit 0 ;;\n"
        "  'image inspect') printf '%s\\n' 'docker.io/wrong/api@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' 'ghcr.io/example/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' 'ghcr.io/zj1123581321/video-transcript-api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'; exit 0 ;;\n"
        "  'compose -f')\n"
        # Real `docker compose` refuses to load its model for ANY subcommand
        # (up/stop/ps/...) when a mandatory `${VIDEO_TRANSCRIPT_IMAGE:?...}`
        # interpolation has no value -- it's not just `config` that needs it.
        # `config` is exempted below since it independently fakes rendering
        # by inspecting the compose file directly.
        "    case \" $* \" in\n"
        "      *' up '*|*' stop '*|*' ps '*)\n"
        "        if [ -z \"$VIDEO_TRANSCRIPT_IMAGE\" ]; then\n"
        "          echo 'error while interpolating services.video-transcript-api.image: required variable VIDEO_TRANSCRIPT_IMAGE is missing a value' >&2\n"
        "          exit 1\n"
        "        fi\n"
        "        ;;\n"
        "    esac\n"
        "    case \" $* \" in\n"
        "      *' config '*) if grep -q VIDEO_TRANSCRIPT_IMAGE \"$3\"; then IMAGE=\"$VIDEO_TRANSCRIPT_IMAGE\"; else IMAGE=\"${RENDERED_IMAGE:-legacy-static:latest}\"; fi; printf 'services:\\n  unrelated:\\n    image: %s\\n  video-transcript-api:\\n    image: %s\\n' \"$VIDEO_TRANSCRIPT_IMAGE\" \"$IMAGE\"; exit 0 ;;\n"
        "      *' ps -q '*) echo \"$COMPOSE_CONTAINER_ID\"; exit 0 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  'run --rm') [ \"$PREFLIGHT_OK\" = 1 ]; exit $? ;;\n"
        "  'inspect --format={{.State.Health.Status}}') cat \"$HEALTH_STATUS_FILE\"; exit 0 ;;\n"
        "  'inspect --format={{.Config.Image}}') printf '%s\\n' \"$OLD_CONTAINER_IMAGE_REF\"; exit 0 ;;\n"
        "  'inspect --format={{.Image}}') printf '%s\\n' \"$OLD_CONTAINER_IMAGE_ID\"; exit 0 ;;\n"
        "  'exec compose-managed-id') [ \"$LEGACY_HTTP_OK\" = 1 ]; exit $? ;;\n"
        "esac\n"
        "if [ \"$1\" = compose ] && case \" $* \" in *' up -d '*) true;; *) false;; esac; then sleep \"$COMPOSE_UP_DELAY\"; fi\n"
        "if [ \"$1\" = compose ] && [ \"$COMPOSE_FAIL_ONCE\" = 1 ] && [ ! -f \"$COMPOSE_MARKER\" ]; then touch \"$COMPOSE_MARKER\"; exit 1; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    if not state_commit_ok:
        mv = bin_dir / "mv"
        mv.write_text(
            "#!/bin/sh\n"
            "case \"$1 $2\" in\n"
            "  *'.deploy-image.tmp '*'/.deploy-image) exit 1 ;;\n"
            "esac\n"
            "exec /bin/mv \"$@\"\n",
            encoding="utf-8",
        )
        mv.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "PREFLIGHT_OK": "1" if preflight_ok else "0",
        "HEALTH_STATUS": health,
        "HEALTH_STATUS_FILE": str(tmp_path / "health.status"),
        "COMPOSE_FAIL_ONCE": "1" if compose_fail_once else "0",
        "COMPOSE_MARKER": str(tmp_path / "compose.failed"),
        "COMPOSE_UP_DELAY": "0",
        "RENDERED_IMAGE": "",
        "COMPOSE_CONTAINER_ID": "compose-managed-id",
        "LEGACY_HTTP_OK": "1",
        "PROJECT_ROOT_OVERRIDE": str(tmp_path / "project"),
        "HEALTH_ATTEMPTS": "1",
        "HEALTH_INTERVAL_SECONDS": "0",
        # Simulates `docker inspect --format='{{.Config.Image}}'` /
        # `{{.Image}}` on the currently-running container, used by the
        # no-.deploy-image discovery branch that resolves a tag-launched
        # container to a rollback-able digest. Empty by default so tests
        # that don't care about this path see the pre-existing no-op
        # behaviour (empty output, no case match).
        "OLD_CONTAINER_IMAGE_REF": running_container_image_ref,
        "OLD_CONTAINER_IMAGE_ID": (
            "sha256:" + "d" * 64 if running_container_image_ref else ""
        ),
    })
    project = Path(env["PROJECT_ROOT_OVERRIDE"])
    Path(env["HEALTH_STATUS_FILE"]).write_text(health, encoding="utf-8")
    (project / "config").mkdir(parents=True)
    (project / "config" / "config.jsonc").write_text("{}", encoding="utf-8")
    return env


def _run(tmp_path, image, **kwargs):
    env = _fake_docker(tmp_path, **kwargs)
    result = subprocess.run(
        ["bash", str(SCRIPT), image],
        env=env,
        text=True,
        capture_output=True,
    )
    log_path = Path(env["DOCKER_LOG"])
    return result, log_path.read_text(encoding="utf-8") if log_path.exists() else ""


def test_rejects_latest_tag_before_touching_docker(tmp_path):
    result, log = _run(
        tmp_path, "ghcr.io/zj1123581321/video-transcript-api:latest"
    )
    assert result.returncode != 0
    assert "unique non-latest tag" in result.stderr
    assert log == ""


def test_preflight_failure_keeps_current_service_running(tmp_path):
    result, log = _run(
        tmp_path,
        "ghcr.io/zj1123581321/video-transcript-api:abc123def456",
        preflight_ok=False,
    )
    assert result.returncode != 0
    assert "run --rm" in log
    assert "compose" not in log


def test_unhealthy_candidate_restores_previous_digest(tmp_path):
    env = _fake_docker(tmp_path, health="unhealthy")
    project = Path(env["PROJECT_ROOT_OVERRIDE"])
    old = "ghcr.io/zj1123581321/video-transcript-api@sha256:" + "b" * 64
    (project / ".deploy-image").write_text(old + "\n", encoding="utf-8")
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "ghcr.io/zj1123581321/video-transcript-api:abc123def456",
        ],
        env=env,
        text=True,
        capture_output=True,
    )
    log = Path(env["DOCKER_LOG"]).read_text(encoding="utf-8")
    compose_lines = [line for line in log.splitlines() if "compose" in line and "up -d" in line]
    assert result.returncode != 0
    assert len(compose_lines) == 2
    assert old in compose_lines[1]
    assert (project / ".deploy-image").read_text(encoding="utf-8").strip() == old


def test_state_commit_failure_rolls_back_candidate(tmp_path):
    env = _fake_docker(tmp_path, state_commit_ok=False)
    project = Path(env["PROJECT_ROOT_OVERRIDE"])
    old = "ghcr.io/zj1123581321/video-transcript-api@sha256:" + "b" * 64
    (project / ".deploy-image").write_text(old + "\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), "ghcr.io/example/api:abc123def456"],
        env=env,
        text=True,
        capture_output=True,
    )
    log = Path(env["DOCKER_LOG"]).read_text(encoding="utf-8")
    compose_lines = [
        line for line in log.splitlines() if "compose" in line and "up -d" in line
    ]

    assert result.returncode != 0
    assert len(compose_lines) == 2
    assert old in compose_lines[1]
    assert (project / ".deploy-image").read_text(encoding="utf-8").strip() == old


def test_signal_after_candidate_start_rolls_back(tmp_path):
    env = _fake_docker(tmp_path, health="starting")
    env["HEALTH_INTERVAL_SECONDS"] = "1"
    project = Path(env["PROJECT_ROOT_OVERRIDE"])
    old = "ghcr.io/zj1123581321/video-transcript-api@sha256:" + "b" * 64
    (project / ".deploy-image").write_text(old + "\n", encoding="utf-8")
    log_path = Path(env["DOCKER_LOG"])

    process = subprocess.Popen(
        ["bash", str(SCRIPT), "ghcr.io/example/api:abc123def456"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if log_path.exists() and "up -d" in log_path.read_text(encoding="utf-8"):
            break
        time.sleep(0.02)
    else:
        process.kill()
        process.communicate()
        raise AssertionError("candidate did not start")

    Path(env["HEALTH_STATUS_FILE"]).write_text("healthy", encoding="utf-8")
    process.terminate()
    process.communicate(timeout=5)
    log = log_path.read_text(encoding="utf-8")
    compose_lines = [
        line for line in log.splitlines() if "compose" in line and "up -d" in line
    ]

    assert process.returncode != 0
    assert len(compose_lines) == 2
    assert old in compose_lines[1]
    assert (project / ".deploy-image").read_text(encoding="utf-8").strip() == old


def test_signal_while_compose_start_is_running_rolls_back(tmp_path):
    env = _fake_docker(tmp_path, health="healthy")
    env["COMPOSE_UP_DELAY"] = "1"
    project = Path(env["PROJECT_ROOT_OVERRIDE"])
    old = "ghcr.io/zj1123581321/video-transcript-api@sha256:" + "b" * 64
    (project / ".deploy-image").write_text(old + "\n", encoding="utf-8")
    log_path = Path(env["DOCKER_LOG"])

    process = subprocess.Popen(
        ["bash", str(SCRIPT), "ghcr.io/example/api:abc123def456"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if log_path.exists() and "up -d" in log_path.read_text(encoding="utf-8"):
            break
        time.sleep(0.02)
    else:
        process.kill()
        process.communicate()
        raise AssertionError("compose start did not run")

    process.terminate()
    process.communicate(timeout=5)
    log = log_path.read_text(encoding="utf-8")
    compose_lines = [
        line for line in log.splitlines() if "compose" in line and "up -d" in line
    ]
    assert process.returncode != 0
    assert len(compose_lines) == 2
    assert old in compose_lines[1]


def test_unhealthy_first_deployment_stops_failed_candidate(tmp_path):
    result, log = _run(
        tmp_path,
        "ghcr.io/example/api:abc123def456",
        health="unhealthy",
    )
    assert result.returncode != 0
    assert "stop video-transcript-api" in log
    # The log line above only proves the stop command was *attempted*, not
    # that it actually stopped anything: `docker.write_text` logs `$*`
    # unconditionally before dispatching, so it can't tell a real stop apart
    # from one that failed and was swallowed by `|| true`. Compose refuses to
    # load its model for `stop` (same as any other subcommand) when
    # VIDEO_TRANSCRIPT_IMAGE has no value -- and at this call site it never
    # does, because deploy_digest() only ever sets it as a per-command
    # prefix. A `stop` that dies on interpolation and gets swallowed leaves
    # the unhealthy candidate running under `restart: unless-stopped`.
    assert "error while interpolating" not in result.stderr


def test_candidate_start_failure_restores_and_health_checks_previous_digest(tmp_path):
    env = _fake_docker(tmp_path, compose_fail_once=True)
    project = Path(env["PROJECT_ROOT_OVERRIDE"])
    old = "ghcr.io/zj1123581321/video-transcript-api@sha256:" + "b" * 64
    (project / ".deploy-image").write_text(old + "\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), "ghcr.io/example/api:abc123def456"],
        env=env, text=True, capture_output=True,
    )
    log = Path(env["DOCKER_LOG"]).read_text(encoding="utf-8")

    assert result.returncode != 0
    compose_lines = [line for line in log.splitlines() if "compose" in line and "up -d" in line]
    assert len(compose_lines) == 2
    assert old in compose_lines[1]
    assert "State.Health.Status" in log


def test_deployment_refuses_concurrent_project_lock(tmp_path):
    env = _fake_docker(tmp_path)
    project = Path(env["PROJECT_ROOT_OVERRIDE"])
    lock_path = project / ".deploy.lock"
    holder = subprocess.Popen(
        ["bash", "-c", 'exec 9>"$1"; flock 9; sleep 2', "holder", str(lock_path)],
        env=env,
    )
    try:
        import time
        time.sleep(0.1)
        result = subprocess.run(
            ["bash", str(SCRIPT), "ghcr.io/example/api:abc123def456"],
            env=env, text=True, capture_output=True,
        )
    finally:
        holder.terminate()
        holder.wait()

    assert result.returncode != 0
    assert "deployment is already running" in result.stderr


def test_incompatible_existing_compose_is_migrated_before_restart(tmp_path):
    env = _fake_docker(tmp_path)
    project = Path(env["PROJECT_ROOT_OVERRIDE"])
    compose = project / "docker-compose.yml"
    compose.write_text("services:\n  video-transcript-api:\n    image: legacy-static:latest\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(SCRIPT), "ghcr.io/example/api:abc123def456"],
        env=env, text=True, capture_output=True,
    )
    log = Path(env["DOCKER_LOG"]).read_text(encoding="utf-8")

    assert result.returncode == 0
    assert "up -d" in log
    assert (project / "docker-compose.yml.pre-digest.bak").exists()


def test_candidate_digest_matches_requested_repository(tmp_path):
    result, log = _run(tmp_path, "ghcr.io/example/api:abc123def456")
    assert result.returncode == 0
    assert "docker.io/wrong/api@sha256" not in result.stdout
    assert "ghcr.io/example/api@sha256" in result.stdout


def test_preflight_uses_same_env_file_as_deployment(tmp_path):
    env = _fake_docker(tmp_path)
    project = Path(env["PROJECT_ROOT_OVERRIDE"])
    (project / ".env").write_text("TOKEN=test\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), "ghcr.io/example/api:abc123def456"],
        env=env, text=True, capture_output=True,
    )
    log = Path(env["DOCKER_LOG"]).read_text(encoding="utf-8")

    assert result.returncode == 0
    assert f"--env-file {project / '.env'}" in log
    compose_lines = [line for line in log.splitlines() if " compose " in line]
    assert compose_lines
    assert all(f"--env-file {project / '.env'}" in line for line in compose_lines)
    assert all(f"--project-directory {project}" in line for line in compose_lines)


def test_failed_candidate_restores_legacy_compose_file(tmp_path):
    env = _fake_docker(tmp_path, health="unhealthy")
    project = Path(env["PROJECT_ROOT_OVERRIDE"])
    legacy_source = "services:\n  video-transcript-api:\n    image: legacy-static:latest\n"
    compose = project / "docker-compose.yml"
    compose.write_text(legacy_source, encoding="utf-8")
    old = "ghcr.io/zj1123581321/video-transcript-api@sha256:" + "b" * 64
    (project / ".deploy-image").write_text(old + "\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), "ghcr.io/example/api:abc123def456"],
        env=env, text=True, capture_output=True,
    )

    assert result.returncode != 0
    assert compose.read_text(encoding="utf-8") == legacy_source
    log = Path(env["DOCKER_LOG"]).read_text(encoding="utf-8")
    rollback_lines = [line for line in log.splitlines() if f"image={old}" in line]
    assert any("docker-compose.image-override.yml" in line for line in rollback_lines)


def test_tagged_running_container_is_resolved_to_old_digest(tmp_path):
    """No .deploy-image state file exists (e.g. the first run of this
    hardened script against a container an older tool started from a
    mutable tag). The script must still recover a pinned digest to roll
    back to: it reads the running container's Config.Image; since that is
    a tag rather than a digest, it falls back to the container's
    underlying image ID and searches that image's RepoDigests for the
    entry matching the old repository. This drives that whole discovery
    chain through fake docker and confirms a failed candidate deployment
    rolls back to the digest resolved this way, rather than giving up with
    "no previous digest is available for rollback".
    """
    old_tag_ref = "ghcr.io/zj1123581321/video-transcript-api:v0.9.0"
    resolved_old_digest = (
        "ghcr.io/zj1123581321/video-transcript-api@sha256:" + "a" * 64
    )
    env = _fake_docker(
        tmp_path, health="unhealthy", running_container_image_ref=old_tag_ref
    )
    project = Path(env["PROJECT_ROOT_OVERRIDE"])
    # Deliberately do NOT seed .deploy-image: this exercises the "discover
    # from the running container" branch instead of the state-file branch
    # covered by test_unhealthy_candidate_restores_previous_digest.
    result = subprocess.run(
        ["bash", str(SCRIPT), "ghcr.io/example/api:abc123def456"],
        env=env,
        text=True,
        capture_output=True,
    )
    log = Path(env["DOCKER_LOG"]).read_text(encoding="utf-8")
    compose_lines = [
        line for line in log.splitlines() if "compose" in line and "up -d" in line
    ]

    assert result.returncode != 0
    # Sanity: the discovery chain actually ran (Config.Image, then Image),
    # rather than the assertions below passing for an unrelated reason.
    assert "inspect --format={{.Config.Image}}" in log
    assert "inspect --format={{.Image}}" in log
    assert len(compose_lines) == 2
    assert resolved_old_digest in compose_lines[1]
    assert not (project / ".deploy-image").exists()


def test_health_checks_compose_managed_container_id(tmp_path):
    result, log = _run(tmp_path, "ghcr.io/example/api:abc123def456")
    assert result.returncode == 0
    assert "State.Health.Status}} compose-managed-id" in log


def test_legacy_image_without_docker_health_uses_api_probe(tmp_path):
    result, log = _run(
        tmp_path, "ghcr.io/example/api:abc123def456", health=""
    )
    assert result.returncode == 0
    assert "exec compose-managed-id python -c" in log


def test_compose_template_uses_legacy_compatible_env_file_syntax():
    source = (SCRIPT.parent / "docker-compose.deploy.yml").read_text(encoding="utf-8")
    assert "required:" not in source
    assert "- .env" in source


def test_build_script_uses_only_git_sha_tag():
    source = PUSH_SCRIPT.read_text(encoding="utf-8")
    assert 'IMAGE_REF="${REGISTRY_IMAGE}:${GIT_SHA}"' in source
    assert ":latest" not in source
    assert '--build-arg "GIT_SHA=${GIT_SHA}"' in source
    assert "status --porcelain" in source
    assert "dirty worktree" in source
