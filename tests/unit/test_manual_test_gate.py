"""Regression tests for the tests/manual collection gate."""

import os
from pathlib import Path
import subprocess
import sys


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
