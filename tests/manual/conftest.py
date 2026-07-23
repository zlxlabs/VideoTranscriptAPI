"""Safety gate for tests that require real external services."""

import os

import pytest


def pytest_collection_modifyitems(items):
    """Mark manual tests as slow/network and require explicit opt-in to run."""
    for item in items:
        item.add_marker(pytest.mark.slow)
        item.add_marker(pytest.mark.network)

    if os.environ.get("VTAPI_TESTS_MANUAL") == "1":
        return

    skip_manual = pytest.mark.skip(
        reason="manual tests require VTAPI_TESTS_MANUAL=1"
    )
    for item in items:
        item.add_marker(skip_manual)
