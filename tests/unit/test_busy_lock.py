"""Real-fcntl tests for the service-side deploy busy lock.

Console output is English-only. MagicMock is not used to fake flock.
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import os
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger as loguru_logger

from video_transcript_api.api.services import terminal_status as ts
from video_transcript_api.api.services.terminal_status import (
    finalize_terminal_status_and_notify,
)
from video_transcript_api.utils.busy_lock import (
    BUSY_LOCK_PATH,
    BusyLock,
    acquire_for_task,
    release_for_task,
)
from video_transcript_api.utils.task_status import TaskStatus


def _host_ex_blocked(path: str) -> bool:
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _hold_host_ex(path: str) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _release_host_ex(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def _provision(path: str) -> None:
    """Pre-create the lock file the way the deployment side does.

    The service itself never creates the lock file or its directory (gate
    round 7: self-provisioning lands on the container layer with a foreign
    inode, silently voiding mutual exclusion); tests stand in for the deploy
    side here.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(path):
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o666))


# ---------------------------------------------------------------------------
# BusyLock + mapping: real flock
# ---------------------------------------------------------------------------


def test_shared_lock_blocks_host_ex_and_release_allows_it(tmp_path):
    path = str(tmp_path / ".deploy-state" / "busy.lock")
    _provision(path)
    shared = BusyLock(path)
    assert shared.acquire() is True
    assert shared.disabled is False
    assert _host_ex_blocked(path)
    shared.release()
    assert not _host_ex_blocked(path)


def test_exclusive_holder_makes_acquire_return_false(tmp_path):
    path = str(tmp_path / ".deploy-state" / "busy.lock")
    _provision(path)
    seed = BusyLock(path)
    assert seed.acquire() is True
    seed.release()
    holder = _hold_host_ex(path)
    try:
        denied = BusyLock(path)
        assert denied.acquire() is False
        assert denied.disabled is False
    finally:
        _release_host_ex(holder)
    allowed = BusyLock(path)
    assert allowed.acquire() is True
    allowed.release()


def test_two_shared_holders_block_exclusive_until_both_release(tmp_path):
    path = str(tmp_path / ".deploy-state" / "busy.lock")
    _provision(path)
    first = BusyLock(path)
    second = BusyLock(path)
    assert first.acquire() is True
    assert second.acquire() is True
    assert _host_ex_blocked(path)
    first.release()
    assert _host_ex_blocked(path)
    second.release()
    assert not _host_ex_blocked(path)


def test_uncreatable_path_disables_and_denies_once(tmp_path):
    """Fail-closed: init infrastructure failure denies admission, warns once."""
    BusyLock._warned_paths.clear()
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    path = str(blocker / "busy.lock")
    seen: list[str] = []
    sink_id = loguru_logger.add(lambda message: seen.append(message.record["message"]))
    try:
        first = BusyLock(path)
        assert first.acquire() is False
        assert first.disabled is True
        # Repeat call hits the disabled short-circuit; it must also deny.
        assert first.acquire() is False
        second = BusyLock(path)
        assert second.acquire() is False
        assert second.disabled is True
    finally:
        loguru_logger.remove(sink_id)
    warns = [item for item in seen if "BUSY LOCK WARN" in item]
    assert len(warns) == 1


def test_flock_os_error_denies_admission_and_warns_reason(monkeypatch, tmp_path):
    """Fail-closed: non-BlockingIOError flock OSError denies, warns mount/permission."""
    path = str(tmp_path / "busy.lock")
    _provision(path)
    seed = BusyLock(path)
    assert seed.acquire() is True
    seed.release()
    # Seed creation already consumed this path's once-only warn; reset so the
    # flock-failure warn below is the one under observation.
    BusyLock._warned_paths.clear()

    def refuse(fd, flags):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(fcntl, "flock", refuse)
    seen: list[str] = []
    sink_id = loguru_logger.add(lambda message: seen.append(message.record["message"]))
    try:
        denied = BusyLock(path)
        assert denied.acquire() is False
        assert denied.disabled is True
    finally:
        loguru_logger.remove(sink_id)
    monkeypatch.undo()  # restore real flock before exercising the lock file again
    # No shared fd leaked: host exclusive lock is still acquirable.
    assert not _host_ex_blocked(path)
    warns = [item for item in seen if "BUSY LOCK WARN" in item]
    assert len(warns) == 1
    assert any("permission denied" in item for item in warns)


def test_missing_lock_file_denies_and_warns(monkeypatch, tmp_path):
    """Round 7: the service never self-provisions the lock file.

    File missing (dir exists) and parent dir missing are both ENOENT ->
    disabled + admission False + one-shot "lock file missing" warn.
    """
    for path in (
        str(tmp_path / "busy.lock"),
        str(tmp_path / "no-such-dir" / "busy.lock"),
    ):
        BusyLock._warned_paths.clear()
        seen: list[str] = []
        sink_id = loguru_logger.add(
            lambda message: seen.append(message.record["message"])
        )
        try:
            denied = BusyLock(path)
            assert denied.acquire() is False
            assert denied.disabled is True
        finally:
            loguru_logger.remove(sink_id)
        warns = [item for item in seen if "BUSY LOCK WARN" in item]
        assert len(warns) == 1
        assert any("lock file missing" in item for item in warns)


def test_acquire_for_task_blocks_ex_until_release_for_task(tmp_path):
    path = str(tmp_path / "busy.lock")
    _provision(path)
    assert acquire_for_task("task-a", path=path) is True
    assert _host_ex_blocked(path)
    release_for_task("task-a")
    assert not _host_ex_blocked(path)
    release_for_task("task-missing")


def test_exclusive_holder_rejects_acquire_for_task_without_storing(tmp_path):
    path = str(tmp_path / "busy.lock")
    _provision(path)
    seed = BusyLock(path)
    assert seed.acquire() is True
    seed.release()
    holder = _hold_host_ex(path)
    try:
        assert acquire_for_task("task-blocked", path=path) is False
    finally:
        _release_host_ex(holder)
    release_for_task("task-blocked")
    assert acquire_for_task("task-blocked", path=path) is True
    assert _host_ex_blocked(path)
    release_for_task("task-blocked")
    assert not _host_ex_blocked(path)


def test_finalize_releases_held_shared_lock(tmp_path):
    path = str(tmp_path / "busy.lock")
    _provision(path)
    assert acquire_for_task("task-term", path=path) is True
    assert _host_ex_blocked(path)
    cache_manager = MagicMock()
    cache_manager.update_task_status.return_value = True
    written = finalize_terminal_status_and_notify(
        "task-term",
        TaskStatus.SUCCESS,
        cache_manager=cache_manager,
        suppress_terminal_notification=True,
    )
    assert written is True
    assert not _host_ex_blocked(path)


def test_finalize_releases_lock_before_notification_delivery(monkeypatch, tmp_path):
    """Gate round 9: lock released right after terminal CAS wins, BEFORE
    delivery runs; moving release back behind delivery turns this red.
    """
    path = str(tmp_path / "busy.lock")
    _provision(path)
    assert acquire_for_task("task-pre-notify", path=path) is True
    observed: list[bool] = []

    def probe_delivery(*args, **kwargs):
        observed.append(not _host_ex_blocked(path))

    monkeypatch.setattr(ts, "deliver_terminal_notification", probe_delivery)
    cache_manager = MagicMock()
    cache_manager.update_task_status.return_value = True
    written = finalize_terminal_status_and_notify(
        "task-pre-notify", TaskStatus.SUCCESS, cache_manager=cache_manager,
    )
    assert written is True
    assert observed == [True]
    assert not _host_ex_blocked(path)


def test_finalize_releases_lock_on_cas_loss_and_defer(monkeypatch, tmp_path):
    """Gate round 9: CAS-loss and defer-delivery early exits drop the lock too."""
    path = str(tmp_path / "busy.lock")
    _provision(path)
    calls: list[str] = []
    monkeypatch.setattr(
        ts, "deliver_terminal_notification", lambda *a, **k: calls.append("called")
    )
    cache_manager = MagicMock()
    cache_manager.update_task_status.return_value = False
    assert acquire_for_task("task-cas-loss", path=path) is True
    assert finalize_terminal_status_and_notify(
        "task-cas-loss", TaskStatus.SUCCESS, cache_manager=cache_manager,
    ) is False
    assert not _host_ex_blocked(path)

    cache_manager.update_task_status.return_value = True
    assert acquire_for_task("task-defer", path=path) is True
    assert finalize_terminal_status_and_notify(
        "task-defer", TaskStatus.SUCCESS, cache_manager=cache_manager, defer_delivery=True,
    ) is True
    assert calls == []
    assert not _host_ex_blocked(path)


def test_finalize_releases_lock_on_post_cas_exception(monkeypatch, tmp_path):
    """Gate round 11: post-CAS exception path releases busy lock via idempotent finally."""
    path = str(tmp_path / "busy.lock")
    _provision(path)
    assert acquire_for_task("task-post-cas-exc", path=path) is True
    assert _host_ex_blocked(path)

    cache_manager = MagicMock()
    cache_manager.update_task_status.return_value = True

    monkeypatch.setattr(
        ts.logger, "info", MagicMock(side_effect=RuntimeError("post-cas crash"))
    )

    with pytest.raises(RuntimeError, match="post-cas crash"):
        finalize_terminal_status_and_notify(
            "task-post-cas-exc", TaskStatus.SUCCESS, cache_manager=cache_manager,
        )

    assert not _host_ex_blocked(path)


# ---------------------------------------------------------------------------
# HTTP admission: real flock, mocked task persistence
# ---------------------------------------------------------------------------


_FAKE_USER_INFO = {
    "user_id": "test-user",
    "api_key": "sk-test-key-123456",
    "wechat_webhook": None,
}


async def _fake_verify_token():
    return _FAKE_USER_INFO


def _transcribe_client(monkeypatch, tmp_path, mock_cache_manager, queue=None):
    monkeypatch.setattr(
        "video_transcript_api.utils.busy_lock.BUSY_LOCK_PATH",
        str(tmp_path / "busy.lock"),
    )
    from video_transcript_api.api.routes import tasks as tasks_route
    from video_transcript_api.api.services.transcription import verify_token

    monkeypatch.setattr(tasks_route, "cache_manager", mock_cache_manager)
    monkeypatch.setattr(tasks_route, "audit_logger", MagicMock())
    if queue is None:
        queue = asyncio.Queue(maxsize=10)
    monkeypatch.setattr(tasks_route, "get_task_queue", lambda: queue)

    app = FastAPI()
    app.include_router(tasks_route.router)
    app.dependency_overrides[verify_token] = _fake_verify_token
    return TestClient(app)


@pytest.fixture()
def mock_cache_manager():
    mock = MagicMock()
    mock.generate_task_id.return_value = "task-busy-1"
    mock.create_task.return_value = {
        "task_id": "task-busy-1",
        "view_token": "vt-busy-1",
    }
    mock.update_task_status.return_value = True
    return mock


def test_http_ex_holder_returns_503_then_release_restores_202(
    monkeypatch, tmp_path, mock_cache_manager,
):
    """E2E: deploy-side exclusive flock yields HTTP 503; release restores 202.

    Also locks the no-gap invariant: 503 must not create a task row, and a
    successful 202 must keep shared flock until terminal write.
    """
    path = str(tmp_path / "busy.lock")
    _provision(path)
    client = _transcribe_client(monkeypatch, tmp_path, mock_cache_manager)
    seed = BusyLock(path)
    assert seed.acquire() is True
    seed.release()

    holder = _hold_host_ex(path)
    try:
        denied = client.post(
            "/api/transcribe",
            json={"url": "https://www.youtube.com/watch?v=abc123"},
        )
        assert denied.status_code == 503
        assert "lock unavailable" in denied.json()["detail"]
        mock_cache_manager.create_task.assert_not_called()
    finally:
        _release_host_ex(holder)

    accepted = client.post(
        "/api/transcribe",
        json={"url": "https://www.youtube.com/watch?v=abc123"},
    )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["code"] == 202
    mock_cache_manager.create_task.assert_called_once()
    assert _host_ex_blocked(path)

    finalize_terminal_status_and_notify(
        "task-busy-1",
        TaskStatus.SUCCESS,
        cache_manager=mock_cache_manager,
        suppress_terminal_notification=True,
    )
    assert not _host_ex_blocked(path)


def test_reentrant_acquire_raises_and_keeps_original_fd(tmp_path):
    """Reentrant acquire() must raise RuntimeError instead of silently opening
    a second flock and overwriting self._fd. The original fd must survive the
    failed call: the shared lock stays held (host exclusive probe stays
    blocked) and release() unlocks via the original fd.
    """
    path = str(tmp_path / "busy.lock")
    _provision(path)
    lock = BusyLock(path)
    assert lock.acquire() is True
    original_fd = lock._fd
    with pytest.raises(RuntimeError, match="reentrant"):
        lock.acquire()
    assert lock._fd is original_fd
    assert _host_ex_blocked(path)
    lock.release()
    assert not _host_ex_blocked(path)


def test_default_path_constant_matches_compose_mount():
    assert BUSY_LOCK_PATH == "/app/.deploy-state/busy.lock"


def test_compose_seed_mounts_deploy_state():
    compose = os.path.join(
        os.path.dirname(__file__), "..", "..", "docker", "docker-compose.deploy.yml"
    )
    with open(os.path.normpath(compose), encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    matching = [line for line in lines if "deploy-state" in line]
    assert len(matching) == 1
    assert "./.deploy-state:/app/.deploy-state" in matching[0]
