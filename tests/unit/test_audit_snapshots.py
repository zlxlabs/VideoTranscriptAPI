import sqlite3

import pytest

from video_transcript_api.utils.logging.audit_logger import AuditLogger


def test_history_status_rejects_non_terminal_values():
    from fastapi import HTTPException
    from video_transcript_api.api.routes.audit import _normalize_history_status

    assert _normalize_history_status(None) == "success"
    assert _normalize_history_status("all") == "all"
    with pytest.raises(HTTPException) as exc_info:
        _normalize_history_status("processing")
    assert exc_info.value.status_code == 422


def _task(task_id="task-1", **overrides):
    value = {
        "task_id": task_id,
        "view_token": "view-live",
        "title": "Title",
        "author": "Author",
        "platform": "youtube",
        "status": "success",
        "calibration_status": "full",
        "summary_status": "generated",
        "submitted_by": "user-1",
        "processing_options": {"calibrate": True},
        "completed_at": "2026-07-15 10:00:00",
    }
    value.update(overrides)
    return value


def test_schema_migration_is_idempotent(tmp_path):
    from video_transcript_api.utils.logging.audit_logger import CURRENT_SCHEMA_VERSION

    path = tmp_path / "audit.db"
    first = AuditLogger(str(path))
    first.close()
    second = AuditLogger(str(path))
    with second._get_cursor() as cursor:
        assert (
            cursor.execute("SELECT version FROM schema_version").fetchone()[0]
            == CURRENT_SCHEMA_VERSION
        )
        columns = {
            row[1] for row in cursor.execute("PRAGMA table_info(task_audit_snapshots)")
        }
    assert {
        "task_id",
        "view_token",
        "content_expired",
        "processing_options",
        "chapters_status",
    } <= columns


def test_archive_upsert_and_expire_are_idempotent(tmp_path):
    logger = AuditLogger(str(tmp_path / "audit.db"))
    logger.archive_task_snapshot(_task(title="First"))
    logger.archive_task_snapshot(_task(title="Updated"))
    snapshot = logger.get_task_snapshot("task-1")
    assert snapshot["title"] == "Updated"
    assert snapshot["view_token"] == "view-live"

    logger.expire_task_snapshot("task-1")
    logger.expire_task_snapshot("task-1")
    snapshot = logger.get_task_snapshot("task-1")
    assert snapshot["view_token"] is None
    assert snapshot["content_expired"] is True


def test_repair_does_not_revive_expired_capability(tmp_path):
    logger = AuditLogger(str(tmp_path / "audit.db"))
    logger.archive_task_snapshot(_task())
    logger.expire_task_snapshot("task-1")

    class Cache:
        def list_terminal_tasks(self, *, limit, after=None):
            return [_task()][:limit]

        def get_task_by_id(self, task_id):
            return _task() if task_id == "task-1" else None

    assert logger.repair_task_snapshots(Cache()) == 0
    snapshot = logger.get_task_snapshot("task-1")
    assert snapshot["content_expired"] is True
    assert snapshot["view_token"] is None


def test_repair_skips_stale_task_deleted_between_list_and_archive(tmp_path):
    """Codex-reported跨进程复活窗口: repair_task_snapshots 用
    list_terminal_tasks 早先拉取的 task dict 直接归档，若在遍历到这条任务
    之前，任务已经被另一个进程/协程的 cleanup_task_status 完整处理过（归档
    -> expire -> 从 cache.db 删除任务行）*并且* 它在 audit.db 里的墓碑也已
    被 cleanup_old_logs 彻底删除（墓碑不是永久保留，只是多一层
    task_exists 门槛——见 AuditLogger.cleanup_old_logs），get_task_snapshot
    会看到 None（而非一条 content_expired=1 的墓碑），旧代码会用这份过时
    的 dict（仍带着已吊销的 view_token）重新 INSERT 出一条
    content_expired=0 的快照，复活一个本该保持吊销的 view_token。

    修复后 repair_task_snapshots 归档前用 cache_manager.get_task_by_id
    重新确认任务是否仍然存在；get_task_by_id 返回 None（任务已被删除，
    这里用它模拟"任务已被另一进程删除"）时必须跳过，不归档、不复活。
    """
    logger = AuditLogger(str(tmp_path / "audit.db"))

    class Cache:
        def list_terminal_tasks(self, *, limit, after=None):
            # 模拟稍早前的一次列表拉取：那一刻任务仍然存在，view_token
            # 仍是活的。
            return [_task(view_token="view-stale-live")][:limit]

        def get_task_by_id(self, task_id):
            # 模拟另一个进程/协程在 list_terminal_tasks 之后、
            # repair_task_snapshots 遍历到这条任务之前，已经完整地
            # 归档 -> expire -> 删除了这个任务：cache.db 里已经查不到它了。
            return None

    assert logger.repair_task_snapshots(Cache()) == 0
    # 任务从未在 audit.db 里出现过任何快照（既没有活的，也没有墓碑）——
    # 修复前会在这里错误地插入一条 content_expired=0、view_token
    # ="view-stale-live" 的快照，复活一个不该存在的能力。
    assert logger.get_task_snapshot("task-1") is None


def test_rearchive_does_not_revive_expired_capability(tmp_path):
    logger = AuditLogger(str(tmp_path / "audit.db"))
    logger.archive_task_snapshot(_task(title="Before"))
    logger.expire_task_snapshot("task-1")

    logger.archive_task_snapshot(_task(title="After"))

    snapshot = logger.get_task_snapshot("task-1")
    assert snapshot["title"] == "After"
    assert snapshot["content_expired"] is True
    assert snapshot["view_token"] is None


def test_cleanup_restores_snapshot_if_expiry_succeeds_but_delete_fails(tmp_path):
    from video_transcript_api.cache.cache_manager import CacheManager

    cache = CacheManager(str(tmp_path / "cache"))
    audit = AuditLogger(str(tmp_path / "audit.db"))
    cache.audit_logger = audit
    task_id = cache.create_task("https://example.com/delete-failure")["task_id"]
    with cache._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET status='success', completed_at='2020-01-01 00:00:00' WHERE task_id=?",
            (task_id,),
        )

    with cache._get_cursor() as cursor:
        cursor.execute('''
            CREATE TRIGGER fail_task_delete
            BEFORE DELETE ON task_status
            BEGIN
                SELECT RAISE(ABORT, 'cache delete failed');
            END
        ''')

    with pytest.raises(Exception, match="cache delete failed"):
        cache.cleanup_task_status(30)

    task = cache.get_task_by_id(task_id)
    assert task is not None
    snapshot = audit.get_task_snapshot(task_id)
    assert snapshot["content_expired"] is False
    assert snapshot["view_token"] == task["view_token"]


def test_cleanup_does_not_revive_old_expiry_when_current_expire_fails(tmp_path):
    from video_transcript_api.cache.cache_manager import CacheManager

    cache = CacheManager(str(tmp_path / "cache"))
    audit = AuditLogger(str(tmp_path / "audit.db"))
    cache.audit_logger = audit
    task_id = cache.create_task("https://example.com/old-expiry")["task_id"]
    with cache._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET status='success', completed_at='2020-01-01 00:00:00' WHERE task_id=?",
            (task_id,),
        )
    audit.archive_task_snapshot(cache.get_task_by_id(task_id))
    audit.expire_task_snapshot(task_id)
    audit.expire_task_snapshot = lambda ignored: (_ for _ in ()).throw(
        OSError("expire unavailable")
    )

    with pytest.raises(OSError, match="expire unavailable"):
        cache.cleanup_task_status(30)

    snapshot = audit.get_task_snapshot(task_id)
    assert snapshot["content_expired"] is True
    assert snapshot["view_token"] is None


def test_cleanup_does_not_revive_already_expired_snapshot_when_delete_fails_again(tmp_path):
    """Z2 (PR3 review hardening, this round): the failure-compensation
    branch in cleanup_task_status used to set `expired_this_attempt = True`
    unconditionally right after calling expire_task_snapshot(), regardless
    of whether that call actually performed a live -> expired transition
    *in this attempt*. This reproduces the exact gap that left open:
    simulate a crash-interrupted earlier cleanup run where archive + expire
    already completed successfully (content_expired=1, view_token cleared)
    but the DELETE FROM task_status step never ran, so the task_status row
    is still live and gets re-selected on retry. Unlike the sibling test
    above (test_cleanup_does_not_revive_old_expiry_when_current_expire_
    fails), expire_task_snapshot() itself does NOT raise here -- it's a
    silent, successful no-op on the already-expired row (the realistic
    shape of a retry, not a fresh failure). If DELETE fails again on this
    retry for an unrelated reason, the old unconditional flag would still
    trigger restore_live_task_snapshot(revive_expired=True) and resurrect
    an already-dead capability, breaking revocation monotonicity. The fix
    (expire_task_snapshot now returns whether *this* call performed the
    transition) must leave the tombstone alone."""
    from video_transcript_api.cache.cache_manager import CacheManager

    cache = CacheManager(str(tmp_path / "cache"))
    audit = AuditLogger(str(tmp_path / "audit.db"))
    cache.audit_logger = audit
    task_id = cache.create_task("https://example.com/already-expired-retry")["task_id"]
    with cache._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET status='success', completed_at='2020-01-01 00:00:00' WHERE task_id=?",
            (task_id,),
        )

    # Simulate the crash-interrupted earlier run: archive + expire already
    # completed for real (a genuine live -> expired transition happened
    # then), but DELETE never ran -- task_status is untouched, still
    # selectable by cleanup_task_status on this retry.
    audit.archive_task_snapshot(cache.get_task_by_id(task_id))
    audit.expire_task_snapshot(task_id)
    assert audit.get_task_snapshot(task_id)["content_expired"] is True

    # This retry's DELETE fails for an unrelated, fresh reason.
    with cache._get_cursor() as cursor:
        cursor.execute('''
            CREATE TRIGGER fail_task_delete_retry
            BEFORE DELETE ON task_status
            BEGIN
                SELECT RAISE(ABORT, 'cache delete failed again');
            END
        ''')

    with pytest.raises(Exception, match="cache delete failed again"):
        cache.cleanup_task_status(30)

    snapshot = audit.get_task_snapshot(task_id)
    assert snapshot["content_expired"] is True, (
        "already-revoked snapshot must not be revived by this round's "
        "unrelated delete failure (red: old code revives it)"
    )
    assert snapshot["view_token"] is None


def test_archive_failure_prevents_task_deletion(tmp_path, monkeypatch):
    from video_transcript_api.cache.cache_manager import CacheManager

    cache = CacheManager(str(tmp_path / "cache"))
    audit = AuditLogger(str(tmp_path / "audit.db"))
    cache.audit_logger = audit
    task_id = cache.create_task("https://example.com/archive")["task_id"]
    with cache._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET status='success', completed_at='2020-01-01 00:00:00' WHERE task_id=?",
            (task_id,),
        )
    monkeypatch.setattr(audit, "archive_task_snapshot", lambda task: (_ for _ in ()).throw(OSError("full")))

    with pytest.raises(OSError, match="full"):
        cache.cleanup_task_status(30)
    assert cache.get_task_by_id(task_id) is not None


def test_missing_audit_logger_prevents_task_deletion(tmp_path):
    from video_transcript_api.cache.cache_manager import CacheManager

    cache = CacheManager(str(tmp_path / "cache"))
    task_id = cache.create_task("https://example.com/no-audit")['task_id']
    with cache._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET status='success', completed_at='2020-01-01 00:00:00' WHERE task_id=?",
            (task_id,),
        )

    with pytest.raises(RuntimeError, match="audit logger"):
        cache.cleanup_task_status(30)
    assert cache.get_task_by_id(task_id) is not None


def test_repair_reads_at_most_500_candidates(tmp_path):
    logger = AuditLogger(str(tmp_path / "audit.db"))

    class Cache:
        def __init__(self):
            self.limit = None

        def list_terminal_tasks(self, *, limit, after=None):
            self.limit = limit
            return [_task("task-a"), _task("task-b")][:limit]

        def get_task_by_id(self, task_id):
            return {"task-a": _task("task-a"), "task-b": _task("task-b")}.get(task_id)

    cache = Cache()
    assert logger.repair_task_snapshots(cache, limit=9999) == 2
    assert cache.limit == 500
    assert logger.get_task_snapshot("task-a") is not None


def _keyset_cache(tasks: dict):
    """Fake cache_manager backing store mirroring the real
    list_terminal_tasks keyset (seek) pagination contract: ordered by
    (completed_at, task_id) ascending, `after` filters strictly greater than
    the given (completed_at, task_id) pair -- exactly the semantics of the
    real SQLite row-value comparison `WHERE (completed_at, task_id) > (?, ?)`
    (Python tuple comparison has the same lexicographic paired semantics).
    `tasks` is a mutable dict so tests can simulate concurrent deletion
    (e.g. cleanup_task_status removing a batch) between two repair calls."""

    class Cache:
        def list_terminal_tasks(self, *, limit, after=None):
            ordered = sorted(
                tasks.values(), key=lambda t: (t["completed_at"], t["task_id"])
            )
            if after is not None:
                ordered = [
                    t for t in ordered if (t["completed_at"], t["task_id"]) > after
                ]
            return ordered[:limit]

        def get_task_by_id(self, task_id):
            return tasks.get(task_id)

    return Cache()


def test_repair_pages_past_existing_snapshots(tmp_path):
    logger = AuditLogger(str(tmp_path / "audit.db"))
    tasks = {f"task-{index:03d}": _task(f"task-{index:03d}") for index in range(502)}
    for task_id in list(tasks)[:500]:
        logger.archive_task_snapshot(tasks[task_id])

    cache = _keyset_cache(tasks)
    assert logger.repair_task_snapshots(cache, limit=500) == 0
    assert logger.repair_task_snapshots(cache, limit=500) == 2
    assert logger.get_task_snapshot("task-501") is not None


def test_repair_survives_deletion_of_earlier_page_during_scan(tmp_path):
    """Codex-reported OFFSET pagination bug (local review round 4, T3):
    repair_task_snapshots used to persist a raw OFFSET across periodic
    calls. cleanup_task_status deletes terminal tasks oldest-first -- the
    exact same order list_terminal_tasks scans in. If cleanup removes a
    whole batch's worth of rows between two repair cycles, the remaining
    rows shift left in the OFFSET-numbered sequence: resuming at the old
    OFFSET then skips a full batch of tasks that were never actually
    scanned, potentially forever (until the cursor wraps back to zero, if
    it ever does).

    Reproduced directly: 1000 terminal tasks, first repair cycle scans and
    archives the first 500; before the second cycle runs, all 500 of those
    tasks are deleted (mirroring what cleanup_task_status would do to the
    oldest batch); the second cycle must still find and archive all 500
    remaining tasks. Keyset (seek) pagination is immune to this because the
    resume cursor is a (completed_at, task_id) *value*, not a row count --
    deleting rows before the cursor can only shrink the already-scanned
    side, it can never make rows after the cursor disappear from view."""
    logger = AuditLogger(str(tmp_path / "audit.db"))
    tasks = {f"task-{index:03d}": _task(f"task-{index:03d}") for index in range(1000)}
    cache = _keyset_cache(tasks)

    assert logger.repair_task_snapshots(cache, limit=500) == 500
    assert logger.repair_scan_complete is False

    # Simulate cleanup_task_status deleting the oldest 500 (the batch just
    # scanned) before the next periodic maintenance cycle runs.
    for index in range(500):
        del tasks[f"task-{index:03d}"]

    assert logger.repair_task_snapshots(cache, limit=500) == 500
    assert logger.get_task_snapshot("task-500") is not None
    assert logger.get_task_snapshot("task-999") is not None


def test_repair_keyset_cursor_survives_null_completed_at(tmp_path):
    """本地 codex review 第 5 轮 F1: keyset 游标假设 completed_at 永不为
    NULL，但 task_status.completed_at 列本身允许 NULL（历史遗留行/迁移
    边缘场景，`cleanup_task_status` 早已用 COALESCE(completed_at,
    created_at) 兼容这一事实——见其 SQL）。SQLite 行值比较遇到 NULL 时
    结果恒为 unknown（在 WHERE 中视为 false）：一旦某一页最后一行
    completed_at 为 NULL，游标存下 (NULL, task_id)，下一页的
    `(completed_at, task_id) > (?, ?)` 对任何行都不成立——返回空集，
    repair_task_snapshots 误判"整轮扫描完成"，之后所有尚未归档的终态
    任务永久饿死，不会再被扫到。

    用真实 CacheManager + AuditLogger 复现：5 个终态任务，其中 2 个是
    completed_at IS NULL 的历史遗留行——一个排在扫描顺序最前（对应"游标
    本身取值为 NULL"的场景），一个夹在中间（对应"游标已经前进、但后续
    仍有 NULL 行需要参与比较"的场景）。limit=1 强制逐行分页，把每一步
    的游标传递都暴露出来。修复前：只有第一条（NULL 行）能被归档，
    第二次调用因空集被误判为扫描完成，其余 4 条永远拿不到快照。"""
    from video_transcript_api.cache.cache_manager import CacheManager

    cache = CacheManager(str(tmp_path / "cache"))
    audit = AuditLogger(str(tmp_path / "audit.db"))
    cache.audit_logger = audit

    task_ids = [
        cache.create_task(f"https://example.com/null-completed-{i}")["task_id"]
        for i in range(5)
    ]
    # task_ids[0]、task_ids[2] 模拟历史遗留的 completed_at IS NULL 终态行；
    # created_at 显式赋值以固定排序顺序，避免依赖 CURRENT_TIMESTAMP 的
    # 时间精度导致测试抖动。
    seed = [
        (task_ids[0], None, "2020-01-01 00:00:00"),
        (task_ids[1], "2020-01-01 00:00:01", "2020-01-01 00:00:01"),
        (task_ids[2], None, "2020-01-01 00:00:02"),
        (task_ids[3], "2020-01-01 00:00:03", "2020-01-01 00:00:03"),
        (task_ids[4], "2020-01-01 00:00:04", "2020-01-01 00:00:04"),
    ]
    with cache._get_cursor() as cursor:
        for task_id, completed_at, created_at in seed:
            cursor.execute(
                "UPDATE task_status SET status='success', completed_at=?, created_at=? "
                "WHERE task_id=?",
                (completed_at, created_at, task_id),
            )

    archived_total = 0
    for _ in range(len(task_ids) + 1):
        archived_total += audit.repair_task_snapshots(cache, limit=1)
        if audit.repair_scan_complete:
            break

    assert audit.repair_scan_complete is True
    assert archived_total == len(task_ids)
    for task_id in task_ids:
        assert audit.get_task_snapshot(task_id) is not None


def test_startup_backfill_runs_bounded_batches_until_complete():
    from video_transcript_api.api.app import _repair_all_task_snapshots

    audit = type("Audit", (), {})()
    audit.results = [500, 500, 2]
    audit.repair_scan_complete = False

    def repair(cache, limit):
        value = audit.results.pop(0)
        audit.repair_scan_complete = not audit.results
        return value

    audit.repair_task_snapshots = repair

    assert _repair_all_task_snapshots(audit, object()) == 1002
    assert audit.results == []


def test_cleanup_write_lock_serializes_cutoff_refresh(tmp_path):
    import threading
    from video_transcript_api.cache.cache_manager import CacheManager

    cache = CacheManager(str(tmp_path / "cache"))
    audit = AuditLogger(str(tmp_path / "audit.db"))
    cache.audit_logger = audit
    task_id = cache.create_task("https://example.com/race")['task_id']
    with cache._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET status='success', completed_at='2020-01-01 00:00:00' WHERE task_id=?",
            (task_id,),
        )

    original_archive = audit.archive_task_snapshot
    refresh_started = threading.Event()
    refresh_threads = []

    def refresh_during_archive(task):
        original_archive(task)

        def refresh():
            refresh_started.set()
            with cache._get_cursor() as cursor:
                cursor.execute(
                    "UPDATE task_status SET completed_at=CURRENT_TIMESTAMP WHERE task_id=?",
                    (task_id,),
                )

        thread = threading.Thread(target=refresh)
        thread.start()
        refresh_threads.append(thread)
        assert refresh_started.wait(timeout=1)

    audit.archive_task_snapshot = refresh_during_archive
    assert cache.cleanup_task_status(30) == 1
    for thread in refresh_threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert cache.get_task_by_id(task_id) is None
    snapshot = audit.get_task_snapshot(task_id)
    assert snapshot["content_expired"] is True
    assert snapshot["view_token"] is None


def test_cleanup_acquires_cache_write_lock_before_archiving():
    import inspect
    from video_transcript_api.cache.cache_manager import CacheManager

    source = inspect.getsource(CacheManager.cleanup_task_status)
    assert source.index('cursor.execute("BEGIN IMMEDIATE")') < source.index(
        "self.audit_logger.archive_task_snapshot(task)"
    )


def test_cleanup_reacquires_write_lock_before_failure_compensation():
    import inspect
    from video_transcript_api.cache.cache_manager import CacheManager

    source = inspect.getsource(CacheManager.cleanup_task_status)
    restore_at = source.index("self.audit_logger.restore_live_task_snapshot(task)")
    assert source.rfind('cursor.execute("BEGIN IMMEDIATE")', 0, restore_at) > source.index(
        "except Exception:"
    )


def test_expired_snapshot_revokes_view_even_if_task_delete_was_interrupted(tmp_path):
    from video_transcript_api.cache.cache_manager import CacheManager

    cache = CacheManager(str(tmp_path / "cache"))
    audit = AuditLogger(str(tmp_path / "audit.db"))
    cache.audit_logger = audit
    created = cache.create_task("https://example.com/interrupted-delete")
    task = cache.get_task_by_id(created["task_id"])
    audit.archive_task_snapshot(task)
    audit.expire_task_snapshot(created["task_id"])

    assert cache.get_task_by_view_token(created["view_token"]) is None


def test_terminal_update_archives_snapshot(tmp_path):
    from video_transcript_api.cache.cache_manager import CacheManager

    cache = CacheManager(str(tmp_path / "cache"))
    audit = AuditLogger(str(tmp_path / "audit.db"))
    cache.audit_logger = audit
    task_id = cache.create_task(
        "https://example.com/live",
        processing_options={"summarize": False},
        submitted_by="user-1",
    )["task_id"]

    assert cache.update_task_status(
        task_id, "success", title="Live", platform="youtube"
    )
    snapshot = audit.get_task_snapshot(task_id)
    assert snapshot["title"] == "Live"
    assert snapshot["submitted_by"] == "user-1"
    assert snapshot["processing_options"] == {
        "calibrate": True,
        "infer_speaker_names": True,
        "summarize": False,
        "chapters": False,  # omitted -> follows summarize
    }


def test_terminal_update_skip_archive_leaves_no_snapshot_but_stays_repairable(tmp_path):
    """H3 (local codex review round 7): update_task_status(skip_archive=True)
    -- used by the shutdown-drain path -- must skip the synchronous
    archive_task_snapshot call entirely (avoiding the _terminal_archive_lock
    contention that motivated this flag: the shutdown path shares that lock
    with an in-flight maintenance call like repair_task_snapshots). The
    skipped snapshot must still be backfillable by the next startup's
    repair_task_snapshots -- skip_archive only defers the write, it must
    not create a permanently-missing snapshot."""
    from video_transcript_api.cache.cache_manager import CacheManager

    cache = CacheManager(str(tmp_path / "cache"))
    audit = AuditLogger(str(tmp_path / "audit.db"))
    cache.audit_logger = audit
    task_id = cache.create_task(
        "https://example.com/skip-archive",
        submitted_by="user-1",
    )["task_id"]

    assert cache.update_task_status(
        task_id, "success", title="Skip Archive", platform="youtube",
        skip_archive=True,
    )
    assert audit.get_task_snapshot(task_id) is None, (
        "skip_archive=True must not write a synchronous snapshot"
    )

    # Backfillable: the next startup's repair sweep picks up exactly the
    # rows skip_archive left un-archived.
    repaired = audit.repair_task_snapshots(cache)
    assert repaired == 1
    snapshot = audit.get_task_snapshot(task_id)
    assert snapshot is not None
    assert snapshot["title"] == "Skip Archive"


def test_shutdown_drain_uses_skip_archive_for_its_terminal_writes(tmp_path):
    """H3 companion: exercised through the real call chain
    (drain_non_terminal_tasks_on_shutdown -> _fail_non_terminal_tasks ->
    update_task_status), not just update_task_status directly -- pins down
    that the shutdown-drain path actually passes skip_archive=True, not
    just that the flag works in isolation."""
    from video_transcript_api.cache.cache_manager import CacheManager

    cache = CacheManager(str(tmp_path / "cache"))
    audit = AuditLogger(str(tmp_path / "audit.db"))
    cache.audit_logger = audit
    task_id = cache.create_task(
        "https://example.com/stuck-at-shutdown",
        submitted_by="user-1",
    )["task_id"]

    drained = cache.drain_non_terminal_tasks_on_shutdown()

    assert drained == 1
    assert cache.get_task_by_id(task_id)["status"] == "failed"
    assert audit.get_task_snapshot(task_id) is None

    repaired = audit.repair_task_snapshots(cache)
    assert repaired == 1
    assert audit.get_task_snapshot(task_id) is not None


def test_cleanup_removes_snapshot_only_after_last_audit_reference(tmp_path, monkeypatch):
    """M1 (local codex review round 10) changed the snapshot deletion gate
    to be purely age-based (archived_at < cutoff, same as the api_audit_logs/
    llm_usage tables) -- the tombstone (content_expired=1 + no remaining
    api_audit_logs reference) no longer bypasses the retention cutoff, so
    archived_at must also be backdated here for the snapshot to become
    eligible, otherwise it would now be kept (see
    test_cleanup_removes_tombstone_past_retention_without_audit_reference /
    test_cleanup_keeps_tombstone_within_retention_even_without_audit_reference
    below for the two sides of that gate in isolation)."""
    logger = AuditLogger(str(tmp_path / "audit.db"))
    logger.archive_task_snapshot(_task())
    logger.log_api_call(
        api_key="secret-key",
        user_id="user-1",
        endpoint="/api/transcribe",
        status_code=202,
        task_id="task-1",
    )
    with logger._get_cursor() as cursor:
        cursor.execute(
            "UPDATE api_audit_logs SET request_time='2020-01-01 00:00:00'"
        )
    logger.expire_task_snapshot("task-1")
    _backdate_snapshot_archived_at(logger, "task-1", "2020-01-01 00:00:00")

    assert logger.cleanup_old_logs(days=1, task_exists=lambda task_id: False) == 2
    assert logger.get_task_snapshot("task-1") is None


def test_cleanup_keeps_unexpired_snapshot_without_current_audit_reference(tmp_path):
    logger = AuditLogger(str(tmp_path / "audit.db"))
    logger.archive_task_snapshot(_task())

    assert logger.cleanup_old_logs(days=1) == 0
    assert logger.get_task_snapshot("task-1") is not None


def test_cleanup_keeps_expired_tombstone_while_task_row_exists(tmp_path):
    logger = AuditLogger(str(tmp_path / "audit.db"))
    logger.archive_task_snapshot(_task())
    logger.expire_task_snapshot("task-1")

    assert logger.cleanup_old_logs(days=1, task_exists=lambda task_id: True) == 0
    snapshot = logger.get_task_snapshot("task-1")
    assert snapshot["content_expired"] is True


# ---------------------------------------------------------------------------
# H5 (local codex review round 7): cleanup_old_logs previously only ever
# deleted a task_audit_snapshots row when content_expired=1 (the tombstone
# path above) -- a snapshot that never got expired (content_expired=0,
# e.g. the task row was deleted through some path other than
# cleanup_task_status's proper archive->expire->delete sequence, or any
# other legacy/edge inconsistency) stayed in the table forever regardless
# of age, even after its task_status row is long gone. That makes
# storage.audit_log_retention_days a lie for this table: title/author/
# platform/submitted_by/view_token can outlive the configured retention
# period indefinitely. The fix ages out ANY snapshot (content_expired 0 or
# 1) whose archived_at predates the retention cutoff, provided its task no
# longer exists in cache.db -- the exact same task_exists() gate the
# existing content_expired=1 path already uses, so a task that's still
# alive is never touched (avoiding a repair_task_snapshots
# recreate-then-immediately-eligible-for-deletion loop).
# ---------------------------------------------------------------------------

def _backdate_snapshot_archived_at(logger, task_id, archived_at):
    with logger._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_audit_snapshots SET archived_at=? WHERE task_id=?",
            (archived_at, task_id),
        )


def test_cleanup_removes_aged_out_snapshot_even_when_content_not_expired(tmp_path):
    """The scenario H5 fixes: content_expired is still 0 (never tombstoned)
    but the snapshot is older than the retention cutoff and its task is
    gone -- must be deleted, not kept forever."""
    logger = AuditLogger(str(tmp_path / "audit.db"))
    logger.archive_task_snapshot(_task())
    _backdate_snapshot_archived_at(logger, "task-1", "2020-01-01 00:00:00")

    deleted = logger.cleanup_old_logs(days=1, task_exists=lambda task_id: False)

    assert deleted == 1
    assert logger.get_task_snapshot("task-1") is None


def test_cleanup_keeps_aged_out_snapshot_when_task_still_exists(tmp_path):
    """Same aged-out snapshot as above, but its task is still alive in
    cache.db -- must be kept, otherwise the next repair_task_snapshots pass
    would just recreate it (get_task_snapshot returns None -> repair sees
    "never archived" -> re-archives from the still-live task), and cleanup
    would delete it again next cycle: a pointless recreate/delete loop this
    gate exists specifically to avoid."""
    logger = AuditLogger(str(tmp_path / "audit.db"))
    logger.archive_task_snapshot(_task())
    _backdate_snapshot_archived_at(logger, "task-1", "2020-01-01 00:00:00")

    deleted = logger.cleanup_old_logs(days=1, task_exists=lambda task_id: True)

    assert deleted == 0
    snapshot = logger.get_task_snapshot("task-1")
    assert snapshot is not None
    assert snapshot["content_expired"] is False


def test_cleanup_keeps_snapshot_within_retention_even_when_task_deleted(tmp_path):
    """The flip side of the age check: a snapshot archived_at just now (well
    within the retention window) must be kept even if its task is already
    gone -- H5 only ages out snapshots past the configured retention
    period, it must not turn "task deleted" alone into an immediate
    snapshot purge (that would defeat the whole point of an audit-owned,
    task-row-independent history record)."""
    logger = AuditLogger(str(tmp_path / "audit.db"))
    logger.archive_task_snapshot(_task())
    # archived_at defaults to CURRENT_TIMESTAMP at archive time -- freshly
    # archived, well inside the 1-day retention window below.

    deleted = logger.cleanup_old_logs(days=1, task_exists=lambda task_id: False)

    assert deleted == 0
    assert logger.get_task_snapshot("task-1") is not None


# ---------------------------------------------------------------------------
# M1 (local codex review round 10): the tombstone branch (content_expired=1
# + no remaining api_audit_logs reference) used to be independent of
# archived_at, unbounded by age -- a task whose audit-log write failed
# (G1's protected scenario: log_api_call never ran, so there's never a
# reference to begin with) could have its snapshot tombstoned and become an
# immediate deletion candidate moments after being archived, long before
# storage.audit_log_retention_days elapses. The fix requires the tombstone
# path to also respect archived_at < cutoff, the same gate the aging path
# (H5, above) already uses -- both delete paths now honor the retention
# period.
# ---------------------------------------------------------------------------


def test_cleanup_keeps_tombstone_within_retention_even_without_audit_reference(tmp_path):
    """The bug M1 fixes: content_expired=1, no api_audit_logs reference, and
    the task row is already gone -- but archived_at is fresh (well within
    the 1-day retention window below). Before the fix this was deleted
    immediately; it must now be kept until the retention cutoff passes."""
    logger = AuditLogger(str(tmp_path / "audit.db"))
    logger.archive_task_snapshot(_task())
    logger.expire_task_snapshot("task-1")
    # archived_at defaults to CURRENT_TIMESTAMP at archive time -- freshly
    # archived. No api_audit_logs reference was ever created (simulates a
    # failed log_api_call write) and the task row is already gone.

    deleted = logger.cleanup_old_logs(days=1, task_exists=lambda task_id: False)

    assert deleted == 0
    assert logger.get_task_snapshot("task-1") is not None


def test_cleanup_removes_tombstone_past_retention_without_audit_reference(tmp_path):
    """Flip side of the fix above: once the same tombstone ages past the
    retention cutoff it must still be deleted -- the fix only adds a floor,
    it must not turn the tombstone path into a permanent keep."""
    logger = AuditLogger(str(tmp_path / "audit.db"))
    logger.archive_task_snapshot(_task())
    logger.expire_task_snapshot("task-1")
    _backdate_snapshot_archived_at(logger, "task-1", "2020-01-01 00:00:00")

    deleted = logger.cleanup_old_logs(days=1, task_exists=lambda task_id: False)

    assert deleted == 1
    assert logger.get_task_snapshot("task-1") is None
