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
    assert f"{MANUAL_FILE}: 6" in result.stdout
    assert "skipped" not in result.stdout


@pytest.mark.parametrize("value", [None, "true", "yes", "1"], ids=lambda v: "unset" if v is None else v)
@pytest.mark.parametrize("config_present", [False, True], ids=["no-config", "config-present"])
def test_config_seeding_does_not_depend_on_the_switch(monkeypatch, value, config_present):
    """The placeholder config is injected in BOTH disk states, for every switch value.

    Both halves of the promise are parameterised here on purpose. An earlier
    version only ran the "config.jsonc missing" state while its docstring claimed
    both, so the regression it was meant to lock (seeding skipped whenever a
    config.jsonc exists) stayed green -- a promise wider than the coverage, the
    very defect this card exists to remove.

    What the probe asserts: the placeholder values win over anything on disk,
    in both states. That is the promise of 9371d52d (default suite never reads
    config.jsonc: no per-developer drift, no credential prefixes in logs) and it
    is the only assertion with teeth for the "config-present" state: a copy of
    config.example.jsonc is byte-identical to the injected placeholder, so it
    cannot distinguish "seeded" from "read the file". The on-disk config is
    therefore a sentinel file with a distinctive api_key -- no real credentials
    ever enter test code, and any implementation that lets the file win fails
    here.

    The probe has to live under tests/unit/ because that is the only way to make
    pytest load tests/conftest.py -- the very code under test here.
    """
    if value is None:
        monkeypatch.delenv("VTAPI_TESTS_MANUAL", raising=False)

    live_config = PROJECT_ROOT / "config" / "config.jsonc"
    example_config = PROJECT_ROOT / "config" / "config.example.jsonc"
    sentinel_api_key = "sentinel-on-disk-config-must-not-win"
    backup = None
    if config_present:
        if live_config.exists():
            backup = live_config.with_suffix(".jsonc.bak")
            shutil.move(live_config, backup)
        live_config.write_text(
            example_config.read_text(encoding="utf-8").replace(
                "your-tikhub-api-key-here", sentinel_api_key
            ),
            encoding="utf-8",
        )

    probe = PROJECT_ROOT / "tests" / "unit" / "_probe_config_seed_tmp.py"
    probe.write_text(
        "from video_transcript_api.utils.logging.logger import load_config\n"
        "\n"
        "def test_placeholder_config_was_injected():\n"
        "    api_key = load_config()['tikhub']['api_key']\n"
        "    assert api_key == 'your-tikhub-api-key-here', \\\n"
        "        'on-disk config.jsonc won over the injected placeholder: ' + str(api_key)\n",
        encoding="utf-8",
    )
    try:
        result = _run_pytest(value, "tests/unit/_probe_config_seed_tmp.py")
    finally:
        probe.unlink()
        live_config.unlink(missing_ok=True)
        if backup is not None:
            shutil.move(backup, live_config)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


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