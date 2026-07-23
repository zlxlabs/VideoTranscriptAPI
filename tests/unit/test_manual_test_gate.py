"""Regression tests for the tests/manual collection gate."""

import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
UNIT_TEST_NODE = (
    "tests/unit/test_errors.py::TestTranscriptAPIError::test_default_message"
)
MANUAL_TEST_FILE = "tests/manual/test_wechat_real.py"


def _run_mixed_pytest(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("VTAPI_TESTS_MANUAL", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", UNIT_TEST_NODE, MANUAL_TEST_FILE, *args],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_manual_gate_skips_only_manual_items_in_mixed_session():
    """A sibling unit test still runs while the real webhook tests are skipped."""
    result = _run_mixed_pytest("-rs")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "6 skipped" in result.stdout


def test_manual_network_marker_does_not_deselect_sibling_unit_items():
    """The manual network marker must not leak to sibling unit tests."""
    result = _run_mixed_pytest("-m", "not network", "--collect-only")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_default_message" in result.stdout
    assert MANUAL_TEST_FILE not in result.stdout
