"""Dockerfile HEALTHCHECK must probe a route the app actually serves.

Regression: docker/Dockerfile's HEALTHCHECK curled `/api/health`, but
health.router is mounted with no prefix (app.py: `include_router(health.router)`)
so the real paths are `/livez` and `/health` -- `/api/health` 404s on every
candidate container, which Docker reports as "unhealthy" forever and the D3
rollout treats as a failed deploy: every candidate rolls back, permanently.

This does not need a Docker daemon -- it only parses the Dockerfile text and
checks the path against the FastAPI route table (equivalent to a TestClient
GET returning something other than 404, without paying for a real HTTP call
or triggering the app lifespan).
"""

import re
from pathlib import Path
from urllib.parse import urlparse

DOCKERFILE = Path(__file__).parents[2] / "docker" / "Dockerfile"


def _healthcheck_url() -> str:
    """Extract the URL curled by the Dockerfile's HEALTHCHECK CMD.

    The instruction spans two physical lines (`HEALTHCHECK ... \\` then the
    `CMD curl ...` continuation), so the whole file is searched rather than
    a single line.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"HEALTHCHECK\b.*?CMD\s+curl\b[^\n]*", text, re.DOTALL)
    assert match, "docker/Dockerfile has no HEALTHCHECK ... CMD curl instruction"
    url_match = re.search(r"https?://\S+", match.group(0))
    assert url_match, "could not find a URL in the HEALTHCHECK CMD"
    return url_match.group(0)


def test_dockerfile_healthcheck_probes_a_real_route():
    """The HEALTHCHECK path must exist in the app's real route table.

    Before the fix this asserted against `/api/health`, which is not
    registered anywhere (health.router has no prefix -- see app.py), so this
    test failed red. `/livez` is the lightweight liveness probe purpose-built
    for container health checks (it never depends on downstream ASR/LLM
    reachability, unlike `/health`'s deep check).
    """
    from video_transcript_api.api.app import create_app

    url = _healthcheck_url()
    path = urlparse(url).path

    # create_app() only registers routes; it does not run the lifespan (that
    # is deferred to ASGI startup), so this is safe to call without a real
    # config or RuntimeContext.
    app = create_app()
    registered_paths = {route.path for route in app.routes}

    assert path in registered_paths, (
        f"Dockerfile HEALTHCHECK probes {path!r}, which is not a route the "
        f"app registers ({sorted(registered_paths)}); every candidate "
        f"container would report unhealthy and the deploy would always "
        f"roll back"
    )


def test_pull_and_deploy_legacy_fallback_probe_matches_dockerfile_healthcheck():
    """docker/pull_and_deploy.sh's no-HEALTHCHECK fallback probe must target
    the same path as the Dockerfile's real HEALTHCHECK, or the two checks
    silently diverge (one green, one red) depending on which code path a
    given deployed image takes."""
    script = Path(__file__).parents[2] / "docker" / "pull_and_deploy.sh"
    script_text = script.read_text(encoding="utf-8")
    url_match = re.search(r"urlopen\('([^']+)'", script_text)
    assert url_match, "could not find the fallback urlopen(...) probe URL"
    fallback_path = urlparse(url_match.group(1)).path

    dockerfile_path = urlparse(_healthcheck_url()).path
    assert fallback_path == dockerfile_path
