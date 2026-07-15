"""Unit tests for `_periodic_maintenance` in
src/video_transcript_api/api/app.py (codex-review R10 #1).

cleanup_old_cache() walks and deletes files, which can take seconds; if
cleanup_task_status() then independently calls now() again, the two
cutoffs drift apart and open a race window where a record is judged "not
yet expired" by one cleanup and "expired" by the other, breaking the
"task_status lives at least as long as cache" invariant. Fix:
_periodic_maintenance computes one UTC now per maintenance pass and hands
the identical value to both cleanup_old_cache and cleanup_task_status.

This test mocks CacheManager/AuditLogger entirely (no real DB I/O) and
only asserts on the call arguments _periodic_maintenance passes them,
since the cleanup functions' own now= handling is covered at the unit
level by tests/cache/test_cleanup_clock_consistency.py.

Console output: English only, no emoji.
"""
import asyncio
import importlib
from unittest.mock import MagicMock

import pytest

# NOTE: `import src.video_transcript_api.api.app as app_module` (or the
# equivalent `from ... import app as app_module`) would silently bind
# app_module to a FastAPI *instance* instead of this module: `import a.b.c
# as x` resolves via attribute-walk (a -> a.b -> a.b.c) after ensuring
# sys.modules contains the submodule, and
# src/video_transcript_api/api/__init__.py does `from .server import app`,
# which rebinds the `app` *attribute* on the `api` package to
# server.py's `app = create_app()` FastAPI instance -- shadowing the
# `api.app` submodule reference that Python's import machinery placed
# there. importlib.import_module() looks the submodule up directly in
# sys.modules by its dotted name, so it is immune to that attribute
# shadowing and returns the real app.py module.
app_module = importlib.import_module("src.video_transcript_api.api.app")


class _StopLoop(Exception):
    """Raised by the patched asyncio.sleep to break out of
    _periodic_maintenance's `while True` loop after exactly one pass, so
    tests can assert on a single maintenance cycle without waiting 24h or
    running forever."""


async def _raise_stop_loop(*_args, **_kwargs):
    raise _StopLoop()


def _build_fake_managers():
    """CacheManager/AuditLogger doubles: no real DB, just call recording."""
    cache_manager = MagicMock(name="cache_manager")
    cache_manager.cleanup_old_cache.return_value = 0
    cache_manager.cleanup_task_status.return_value = 0
    audit_logger = MagicMock(name="audit_logger")
    audit_logger.cleanup_old_logs.return_value = 0
    return cache_manager, audit_logger


def _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger):
    """Run _periodic_maintenance(config) for exactly one loop iteration.

    get_cache_manager/get_audit_logger/get_logger are module-level names
    imported into app_module via `from .context import (...)`, so they are
    patched directly on app_module. asyncio.sleep is patched on the shared
    `asyncio` module object (app_module.asyncio is that same module) to
    raise _StopLoop, which stands in for "the 24h wait between passes" and
    lets the test observe exactly one iteration's call arguments.
    """
    monkeypatch.setattr(app_module, "get_cache_manager", lambda: cache_manager)
    monkeypatch.setattr(app_module, "get_audit_logger", lambda: audit_logger)
    monkeypatch.setattr(app_module, "get_logger", lambda: MagicMock())
    monkeypatch.setattr(app_module.asyncio, "sleep", _raise_stop_loop)

    with pytest.raises(_StopLoop):
        asyncio.run(app_module._periodic_maintenance(config))


class TestPeriodicMaintenanceSharedNow:
    def test_cleanup_old_cache_and_cleanup_task_status_receive_the_same_now(self, monkeypatch):
        cache_manager, audit_logger = _build_fake_managers()
        config = {
            "storage": {
                "cache_retention_days": 30,
                "task_status_retention_days": 180,
                "audit_log_retention_days": 90,
            }
        }

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        cache_now = cache_manager.cleanup_old_cache.call_args.kwargs.get("now")
        task_now = cache_manager.cleanup_task_status.call_args.kwargs.get("now")

        assert cache_now is not None, "cleanup_old_cache must receive an explicit now="
        assert task_now is not None, "cleanup_task_status must receive an explicit now="
        assert cache_now.tzinfo is not None, "shared now must be UTC tz-aware"
        assert cache_now == task_now, (
            "cleanup_old_cache and cleanup_task_status must be handed the exact "
            "same now within one maintenance pass -- independent now() calls "
            "would open a race window between the two cleanups (codex-review R10 #1)"
        )
