"""Safety gate for tests that require real external services."""

import os
from pathlib import Path

import pytest


MANUAL_TESTS_DIR = Path(__file__).resolve().parent


def _is_manual_test_item(item) -> bool:
    """Return whether a collected pytest item lives under tests/manual."""
    item_path = getattr(item, "path", getattr(item, "fspath", None))
    if item_path is None:
        return False

    try:
        candidate = Path(os.fspath(item_path)).resolve()
    except TypeError:
        candidate = Path(str(item_path)).resolve()

    try:
        candidate.relative_to(MANUAL_TESTS_DIR)
    except ValueError:
        return False
    return True


def pytest_collection_modifyitems(items):
    """Mark manual tests as slow/network and require explicit opt-in to run."""
    manual_items = [item for item in items if _is_manual_test_item(item)]

    for item in manual_items:
        item.add_marker(pytest.mark.slow)
        item.add_marker(pytest.mark.network)

    if os.environ.get("VTAPI_TESTS_MANUAL") == "1":
        return

    skip_manual = pytest.mark.skip(
        reason="manual tests require VTAPI_TESTS_MANUAL=1"
    )
    for item in manual_items:
        item.add_marker(skip_manual)
