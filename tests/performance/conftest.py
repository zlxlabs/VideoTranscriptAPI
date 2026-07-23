"""Markers for manual performance tests that use real external services."""

import pytest


def pytest_collection_modifyitems(items):
    """Mark all performance tests as slow and dependent on the network."""
    for item in items:
        item.add_marker(pytest.mark.slow)
        item.add_marker(pytest.mark.network)
