"""Safety gate for tests that require real external services."""

import os
from pathlib import Path
import sys

import pytest


MANUAL_TESTS_DIR = Path(__file__).resolve().parent


def _tests_manual_env_enabled() -> bool:
    """复用 tests/conftest.py 里的唯一定义，避免两处判定漂移。

    fail fast：根 conftest 已被 pytest 先行加载，找不到说明这次调用没有走到
    仓内入口（例如直接 import tests.manual.conftest 而没有经过 pytest 收集，
    或从 src/ 下的路径进入），此时给出可操作的指引而不是裸崩溃。
    """
    root_conftest = sys.modules.get("tests.conftest")
    if root_conftest is None:
        raise RuntimeError(
            "VTAPI_TESTS_MANUAL gate has no single source of truth: "
            "tests/conftest.py was not loaded. Run the manual tests from the "
            "repository root so pytest collects tests/conftest.py first, e.g.\\n"
            "  uv run pytest tests/manual/<file>.py\\n"
            "  uv run pytest --pyargs tests.manual.<module>\\n"
            "  cd tests/manual && uv run pytest <file>.py\\n"
            "An absolute path to tests/manual/<file>.py also works."
        )
    return root_conftest._tests_manual_env_enabled()


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

    if _tests_manual_env_enabled():
        return

    skip_manual = pytest.mark.skip(
        reason="manual tests require VTAPI_TESTS_MANUAL=1"
    )
    for item in manual_items:
        item.add_marker(skip_manual)
