"""Unit tests for `_periodic_maintenance` in
src/video_transcript_api/api/app.py (codex-review R10, two P2 findings).

Both findings are about the same invariant: task_status rows must not be
reclaimed before every downstream consumer that depends on them (the
/view/{view_token} resolver and the /api/audit/history LEFT JOIN) is done
needing them.

#1 (shared now): cleanup_old_cache() walks and deletes files, which can
take seconds; if cleanup_task_status() then independently calls now()
again, the two cutoffs drift apart and open a race window where a record
is judged "not yet expired" by one cleanup and "expired" by the other.
_periodic_maintenance must compute one UTC now per maintenance pass and
hand the identical value to both cleanup_old_cache and cleanup_task_status.

#2 (retention floor covers audit too): the existing clamp (added in an
earlier review round) only protected cache_retention_days. But
/api/audit/history's LEFT JOIN also depends on task_status surviving at
least as long as audit_log_retention_days -- a real config like "keep
audit logs a year, cache files only a month" would otherwise let
task_status rows disappear out from under still-in-window audit records
(NULL title/platform/view_token, and silently dropped entirely by the
default `status='success'` filter). The floor passed to
cleanup_task_status must be max(cache_retention_days,
audit_log_retention_days), and must fall back to the "retained forever"
sentinel (0) if either side is configured as permanent (0), since a
finite max() would otherwise let a permanently-retained consumer's data
outlive the task_status rows it needs.

These tests mock CacheManager/AuditLogger entirely (no real DB I/O) and
only assert on the call arguments _periodic_maintenance passes them, since
the cleanup functions' own retention/clamp math is already covered by
tests/cache/test_task_status_cleanup.py and
tests/cache/test_cleanup_clock_consistency.py.

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


# ---------------------------------------------------------------------------
# Problem 1: cleanup_old_cache and cleanup_task_status must share one now
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Problem 2: task_status retention floor = max(cache, audit) retention
# ---------------------------------------------------------------------------

class TestPeriodicMaintenanceRetentionFloor:
    def test_floor_clamped_to_audit_when_longer_than_cache(self, monkeypatch):
        """audit_log_retention_days(365) > cache_retention_days(30): the
        floor handed to cleanup_task_status must be 365, not 30, otherwise
        /api/audit/history loses title/platform/view_token for records
        still inside their audit retention window."""
        cache_manager, audit_logger = _build_fake_managers()
        config = {
            "storage": {
                "cache_retention_days": 30,
                "task_status_retention_days": 5,
                "audit_log_retention_days": 365,
            }
        }

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        call = cache_manager.cleanup_task_status.call_args
        assert call.args[0] == 5, "retention_days positional arg must be untouched"
        assert call.args[1] == 365, "floor must be the larger of cache(30) and audit(365)"

    def test_floor_clamped_to_cache_when_longer_than_audit(self, monkeypatch):
        """Reverse config: cache_retention_days(200) > audit_log_retention_days(10).
        The floor must still be the larger value (200), confirming the fix
        takes max(cache, audit) rather than unconditionally preferring audit."""
        cache_manager, audit_logger = _build_fake_managers()
        config = {
            "storage": {
                "cache_retention_days": 200,
                "task_status_retention_days": 5,
                "audit_log_retention_days": 10,
            }
        }

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        call = cache_manager.cleanup_task_status.call_args
        assert call.args[1] == 200, "must take the max, not unconditionally the audit value"

    def test_floor_forced_to_zero_when_cache_retained_forever(self, monkeypatch):
        """cache_retention_days=0 means the cache is kept forever; the floor
        must be forced to the 'retained forever' sentinel (0) rather than
        max(0, audit_days), otherwise a finite audit_log_retention_days
        would let task_status (and its view links) expire even though the
        underlying cache never does."""
        cache_manager, audit_logger = _build_fake_managers()
        config = {
            "storage": {
                "cache_retention_days": 0,
                "task_status_retention_days": 50,
                "audit_log_retention_days": 100,
            }
        }

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        call = cache_manager.cleanup_task_status.call_args
        assert call.args[1] == 0

    def test_floor_forced_to_zero_when_audit_retained_forever(self, monkeypatch):
        """audit_log_retention_days=0 means audit logs are kept forever; the
        floor must likewise be forced to 0 so task_status rows never expire
        out from under permanently-retained audit records."""
        cache_manager, audit_logger = _build_fake_managers()
        config = {
            "storage": {
                "cache_retention_days": 100,
                "task_status_retention_days": 50,
                "audit_log_retention_days": 0,
            }
        }

        _run_one_maintenance_pass(monkeypatch, config, cache_manager, audit_logger)

        call = cache_manager.cleanup_task_status.call_args
        assert call.args[1] == 0
