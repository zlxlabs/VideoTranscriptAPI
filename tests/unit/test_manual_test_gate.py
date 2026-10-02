"""Regression tests for the tests/manual collection gate and the
VTAPI_TESTS_MANUAL switch behind it.

The switch must do exactly one thing: decide whether tests/manual items run.
It must not influence the placeholder-config seeding in tests/conftest.py, and
its truth test must be defined in exactly one place. The behaviour tests below
assert real subprocess exit codes and real collection results.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
UNIT_TEST_NODE = (
    "tests/unit/test_errors.py::TestTranscriptAPIError::test_default_message"
)
SAFE_MANUAL_TEST_NODE = "tests/manual/test_loguru_migration.py::test_logger"
HIGH_RISK_MANUAL_TEST_FILE = "tests/manual/test_wechat_real.py"


def _collected_count(stdout: str, target: str) -> int:
    """Number of items pytest reports for `target` (quiet collect prints "file: N")."""
    for line in stdout.splitlines():
        if line.startswith(f"{target}:"):
            return int(line.split(":", 1)[1].strip())
    return 0


def _run_mixed_pytest(
    manual_target: str, *args: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("VTAPI_TESTS_MANUAL", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", UNIT_TEST_NODE, manual_target, *args],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_manual_gate_skips_only_manual_items_in_mixed_session():
    """A sibling unit test runs while a safe manual test is skipped."""
    result = _run_mixed_pytest(SAFE_MANUAL_TEST_NODE, "-rs")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "1 skipped" in result.stdout


def test_manual_network_marker_does_not_deselect_sibling_unit_items():
    """A high-risk manual test is only collected when checking marker scope."""
    result = _run_mixed_pytest(
        HIGH_RISK_MANUAL_TEST_FILE, "-m", "not network", "--collect-only"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_default_message" in result.stdout
    assert HIGH_RISK_MANUAL_TEST_FILE not in result.stdout


# ---------------------------------------------------------------------------
# Behaviour of the VTAPI_TESTS_MANUAL switch (issue #116)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANUAL_FILE = "tests/manual/test_wechat_real.py"
# This node fails outright when the placeholder config was not seeded.
CONFIG_SENSITIVE_NODE = (
    "tests/unit/web/test_transcript_summary_share.py"
    "::TestSummaryShareButton::test_share_text_format_documents_optional_original_url_line"
)

# Values the switch must ignore (everything except "1").
NON_TRUTHY_VALUES = [None, "true", "yes", "TRUE", "0", ""]
ALL_VALUES = ["1", "true", "yes", "TRUE", None]


def _run_pytest(value, *args):
    """Run pytest in a subprocess with VTAPI_TESTS_MANUAL set to `value`."""
    environment = os.environ.copy()
    if value is None:
        environment.pop("VTAPI_TESTS_MANUAL", None)
    else:
        environment["VTAPI_TESTS_MANUAL"] = value
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("value", ALL_VALUES, ids=lambda v: "unset" if v is None else v)
def test_default_suite_passes_regardless_of_switch_value(value):
    """`make test` semantics are identical for every spelling of the switch.

    Regression: VTAPI_TESTS_MANUAL=true used to disable the placeholder-config
    seeding and turn the main gate red while running zero manual tests.
    """
    result = _run_pytest(value, "-q", CONFIG_SENSITIVE_NODE)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "failed" not in result.stdout


@pytest.mark.parametrize("value", NON_TRUTHY_VALUES, ids=lambda v: "unset" if v is None else repr(v))
def test_manual_file_is_skipped_without_truthy_value(value):
    result = _run_pytest(value, MANUAL_FILE, "-rs")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no tests ran" not in result.stdout.lower()
    assert "SKIPPED" in result.stdout
    assert "manual tests require VTAPI_TESTS_MANUAL=1" in result.stdout


def test_manual_file_is_collected_when_switch_is_one():
    result = _run_pytest("1", MANUAL_FILE, "--collect-only", "-q")

    assert result.returncode == 0, result.stdout + result.stderr
    collected = _collected_count(result.stdout, MANUAL_FILE)
    assert collected >= 1, result.stdout
    assert "skipped" not in result.stdout


SENTINEL_API_KEY = "sentinel-on-disk-config-must-not-win"
PROBE_BODY = (
    "from video_transcript_api.utils.logging.logger import load_config\n"
    "\n"
    "def test_placeholder_config_was_injected():\n"
    "    api_key = load_config()['tikhub']['api_key']\n"
    "    assert api_key == 'your-tikhub-api-key-here', \\\n"
    "        'on-disk config.jsonc won over the injected placeholder: ' + str(api_key)\n"
)


def _build_sandbox(root: Path, config_present: bool) -> Path:
    """Build a throw-away project root that exercises the real tests/conftest.py.

    Nothing in the real worktree is written: tests/conftest.py is *copied* (read
    only) and src/ is *symlinked*, so the seeding logic under test is the shipped
    one while config/ lives entirely under tmp. An earlier version of this test
    created and deleted PROJECT_ROOT/config/config.jsonc, which destroyed the
    developer's real config on the no-config branch -- the one thing a test must
    never do.
    """
    (root / "tests").mkdir(parents=True)
    (root / "config").mkdir()
    shutil.copyfile(PROJECT_ROOT / "tests" / "conftest.py", root / "tests" / "conftest.py")
    shutil.copyfile(PROJECT_ROOT / "tests" / "__init__.py", root / "tests" / "__init__.py")
    (root / "src").symlink_to(PROJECT_ROOT / "src", target_is_directory=True)

    example_text = (PROJECT_ROOT / "config" / "config.example.jsonc").read_text(
        encoding="utf-8"
    )
    (root / "config" / "config.example.jsonc").write_text(example_text, encoding="utf-8")
    if config_present:
        # Sentinel, not the example verbatim: a byte-identical copy of the example
        # cannot distinguish "placeholder injected" from "file read", so an
        # implementation that lets the file win would still pass.
        (root / "config" / "config.jsonc").write_text(
            example_text.replace("your-tikhub-api-key-here", SENTINEL_API_KEY),
            encoding="utf-8",
        )
    return root


def _run_sandbox_pytest(root: Path, value, *args) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if value is None:
        environment.pop("VTAPI_TESTS_MANUAL", None)
    else:
        environment["VTAPI_TESTS_MANUAL"] = value
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("value", [None, "true", "yes", "1"], ids=lambda v: "unset" if v is None else v)
@pytest.mark.parametrize("config_present", [False, True], ids=["no-config", "config-present"])
def test_placeholder_config_is_injected_in_both_disk_states(tmp_path, value, config_present):
    """The injected placeholder wins over disk, for every switch value and both disk states.

    Both halves of the promise are parameterised on purpose. An earlier version
    only ran the "config.jsonc missing" state while its docstring claimed both,
    so the regression it was meant to lock stayed green -- a promise wider than
    the coverage, the very defect this card exists to remove.

    The run happens in a tmp sandbox holding a copy of tests/conftest.py, so the
    real worktree is never written to; see _build_sandbox.
    """
    root = _build_sandbox(tmp_path / "sandbox", config_present)
    (root / "tests" / "test_placeholder_seed_probe.py").write_text(
        PROBE_BODY, encoding="utf-8"
    )

    result = _run_sandbox_pytest(root, value, "-q", "tests/test_placeholder_seed_probe.py")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


MANUAL_ENTRYPOINTS = {
    "relative-path-from-root": (PROJECT_ROOT, ["tests/manual/test_wechat_real.py"]),
    "cwd-inside-manual-dir": (
        PROJECT_ROOT / "tests" / "manual",
        ["test_wechat_real.py"],
    ),
    "pyargs-dotted-path": (PROJECT_ROOT, ["--pyargs", "tests.manual.test_wechat_real"]),
    "absolute-path": (
        PROJECT_ROOT,
        [str(PROJECT_ROOT / "tests" / "manual" / "test_wechat_real.py")],
    ),
}


@pytest.mark.parametrize("name", sorted(MANUAL_ENTRYPOINTS))
def test_manual_gate_resolves_from_every_common_entrypoint(name):
    """The gate must find the single truth definition from any entrypoint.

    tests/manual/conftest.py looks the definition up in sys.modules["tests.conftest"]
    and raises when it is missing. That lookup is a contract, not an accident:
    it only works while tests/ and tests/manual/ are packages, so an entrypoint
    that imports them differently could crash during collection instead of
    skipping. Four entrypoints cannot prove "every entrypoint", so this locks the
    ones people actually use, and the error message names them.
    """
    cwd, argv = MANUAL_ENTRYPOINTS[name]
    result = _run_pytest("1", "--collect-only", "-q", *argv) if cwd == PROJECT_ROOT else None
    if result is None:
        environment = os.environ.copy()
        environment["VTAPI_TESTS_MANUAL"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", *argv],
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RuntimeError" not in result.stdout + result.stderr
    assert "does not have a single source of truth" not in result.stdout + result.stderr
    assert "tests.manual.test_wechat_real" in result.stdout.replace("\n", " ").replace(
        "::", "."
    ) or "test_wechat_real.py" in result.stdout


SWITCH_MATRIX = ["1", "true", "yes", "TRUE", "0", "", " 1 ", None]


@pytest.mark.parametrize("value", SWITCH_MATRIX, ids=lambda v: "unset" if v is None else repr(v))
def test_switch_truth_test_is_defined_once_and_only_accepts_one(monkeypatch, value):
    """tests/conftest.py and tests/manual/conftest.py must not drift apart.

    Two divergent copies were the second half of issue #116: the root conftest
    accepted four spellings while the manual gate accepted only "1". Asserting
    the two agree over the whole matrix catches any re-split; a value matrix
    where every entry is "1"-only pins the conservative semantics.
    """
    import tests.conftest as root_conftest
    import tests.manual.conftest as manual_conftest

    if value is None:
        monkeypatch.delenv("VTAPI_TESTS_MANUAL", raising=False)
    else:
        monkeypatch.setenv("VTAPI_TESTS_MANUAL", value)

    expected = value.strip() == "1" if value is not None else False

    assert root_conftest._tests_manual_env_enabled() is expected
    assert manual_conftest._tests_manual_env_enabled() is expected