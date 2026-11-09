"""Deploy busy lock: each in-flight transcription task holds a shared flock.

The deploy script takes an exclusive flock on the same bind-mounted file.
This module only does non-blocking shared admission against an EXISTING
lock file: the service never creates the lock file or its directory. A
self-created file would land on the container layer with a different inode
than the host deploy lock, silently voiding mutual exclusion — equivalent
to fail-open. Provisioning is the deployment side's job (host deploy
script / first-manual-deploy ``touch busy.lock``).

Fail-closed: when the lock file is missing, not a regular file, or flock
itself fails (permission problems), the instance disables itself, logs one
grep-able BUSY LOCK WARN, and admission DENIES new tasks — a broken lock
must never degrade into an unprotected admission path.
"""
from __future__ import annotations

import errno
import fcntl
import os
import threading
from typing import Optional

from .logging import setup_logger

logger = setup_logger("busy_lock")

# 覆盖口仅给无 /app 挂载的沙箱/开发环境（集成实验 local_upload_capacity.py、本地调试）
# 显式 opt-in；生产不设置，走默认路径，deploy.yml 双闸对账的就是这个真实路径。
# 契约：锁文件由部署侧预建（部署脚本/首次人工部署 touch busy.lock），服务侧永不自建——
# 自建文件落在容器层时与宿主部署锁不同 inode，互斥静默失效，等价 fail-open。
BUSY_LOCK_PATH = os.environ.get("VTA_BUSY_LOCK_PATH") or "/app/.deploy-state/busy.lock"


class BusyLock:
    """One shared flock fd covering a single task lifetime.

    ``acquire()`` is the production admission entry: if an exclusive holder
    exists it returns False immediately. Init or flock infrastructure errors
    also mark the instance disabled and return False — admission fails
    closed (the caller answers 503) instead of serving without mutual
    exclusion.
    """

    _warned_paths: set[str] = set()

    def __init__(self, path: str = BUSY_LOCK_PATH):
        self.path = path
        self._fd: Optional[int] = None
        self._disabled = False

    @property
    def disabled(self) -> bool:
        """True once a lock-infrastructure failure denied this instance."""
        return self._disabled

    def _warn(self, message: str) -> None:
        if self.path in self._warned_paths:
            return
        self._warned_paths.add(self.path)
        logger.warning("BUSY LOCK WARN: " + message)

    @staticmethod
    def _os_error_reason(exc: OSError) -> str:
        """Coarse mount/permission bucket for the one-shot warn line."""
        if exc.errno in (errno.EACCES, errno.EPERM):
            return "permission denied"
        return "deploy-state mount/filesystem problem"

    def acquire(self) -> bool:
        """Non-blocking shared flock admission.

        False means denied: either the deploy holds the exclusive lock, or
        the lock infrastructure is unavailable (fail-closed).

        Reentrant calls on an instance that already holds the shared flock
        raise RuntimeError instead of silently opening a second flock and
        overwriting ``self._fd`` (which would leak the first fd and leave
        the original lock undiscoverable for release()).
        """
        if self._fd is not None:
            raise RuntimeError(
                f"reentrant acquire on {self.path}: this BusyLock already "
                "holds a shared flock; call acquire() once per instance"
            )
        if self._disabled:
            return False
        # 只读打开既有文件：不 makedirs、不带 O_CREAT（契约见模块注释）。
        # 目录不存在与文件不存在同为 ENOENT，同走「缺失」分支。
        try:
            fd = os.open(self.path, os.O_RDONLY)
            if not os.path.isfile(self.path):
                os.close(fd)
                self._disabled = True
                self._warn(
                    f"busy lock path {self.path} is not a regular file "
                    "(deploy-state mount misconfigured); "
                    "admission now denies new tasks until the mount is fixed"
                )
                return False
        except OSError as exc:
            self._disabled = True
            if exc.errno == errno.ENOENT:
                self._warn(
                    f"lock file missing — deployment mount likely broken: "
                    f"{self.path} ({exc}); "
                    "admission now denies new tasks until the mount is fixed"
                )
            else:
                self._warn(
                    f"cannot initialize {self.path} "
                    f"({self._os_error_reason(exc)}: {exc}); "
                    "admission now denies new tasks until it is fixed"
                )
            return False

        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                self._fd = fd
                # Fail-closed admission: the shared flock above is the only
                # allow path; a disabled instance can never admit a task.
                return not self._disabled
            except BlockingIOError:
                os.close(fd)
                return False
            except OSError as exc:
                self._disabled = True
                self._warn(
                    f"cannot acquire {self.path} "
                    f"({self._os_error_reason(exc)}: {exc}); "
                    "admission now denies new tasks until it is fixed"
                )
                os.close(fd)
                return False
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


_held_locks: dict[str, BusyLock] = {}
_held_locks_guard = threading.Lock()


def acquire_for_task(task_id: str, path: str | None = None) -> bool:
    """Open a dedicated fd and hold shared flock until release_for_task."""
    lock = BusyLock(path or BUSY_LOCK_PATH)
    if not lock.acquire():
        return False
    with _held_locks_guard:
        previous = _held_locks.pop(task_id, None)
        _held_locks[task_id] = lock
    if previous is not None:
        previous.release()
    # Held shared flock on a healthy lock instance is the only allow path.
    return not lock.disabled


def release_for_task(task_id: str) -> None:
    """Close the fd held for task_id. Unknown ids are a no-op."""
    with _held_locks_guard:
        lock = _held_locks.pop(task_id, None)
    if lock is not None:
        lock.release()
