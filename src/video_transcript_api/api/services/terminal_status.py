"""Single exit for terminal (success/failed) status writes and status notifies.

Invariants (the spec this module is reviewed against; each is falsifiable):

I1 (atomic)   Any terminal CAS win <=> a `task_terminal_notifications` row for
              that task_id appears in the same transaction; a CAS loss => no row.
I2 (unique)   `task_terminal_notifications.task_id` is UNIQUE: at most one row
              per task, enforced by schema, not by code.
I3 (coherent) The row carries the status written by that CAS; the delivered
              text is rendered from that row, never by re-reading the task to
              decide "should this send".
I4 (no loss)  At process start and on every delivery cycle, every eligible
              row with `notified_at IS NULL` must be attempted. A steady-state
              age gate may temporarily omit a fresh row, but row age grows
              monotonically, so every row becomes eligible within the gate.
              NO condition may make a row permanently unlisted, and no
              ordering/limit may starve it: "no condition under which a row is
              never attempted" is the test.
              `attempts` is an observation counter and never filters anything;
              the listing is ordered by `attempts ASC` so a stuck head cannot
              starve fresh rows.
I5 (at-least-once) `notified_at` is written only after a send that returned a
              real success signal. A crash between send and mark => one
              duplicate is allowed (explicitly accepted window).
I6 (suppress at write) A terminal write that must not notify produces no
              deliverable row in that same transaction (see
              `suppress_terminal_notification` in CacheManager.update_task_status).

`sent` (= `notified_at`) means "handed to the notifier", NOT "delivered":
the current implementation enqueues into an in-process daemon queue.

Cross-cutting notification invariants N1-N4 (one logical terminal notice per
task, degraded-body visibility, parameter threading, no business-layer
splitting) are specified in `docs/sessions/261007-notify-slim/design.md`,
section "通知不变式（N1–N4）"; each entry names the code site and the test
that locks it.
"""

import threading
from typing import Any, Dict, Optional

from ..context import get_cache_manager, get_logger, lazy_resource
from ...utils.busy_lock import release_for_task
from ...utils.notifications import get_notification_router, slow_alert
from ...utils.notifications.targets import resolve_notify_targets
from ...utils.task_status import TaskStatus

logger = lazy_resource(get_logger)

TERMINAL_SUCCESS_STATUS = "【任务完成】"
TERMINAL_FAILED_STATUS = "【任务失败】"

# In-process mutual exclusion between the two deliverers of the same row: the
# inline helper path (called by the terminal writer) and the dispatcher thread.
# Both send immediately (fire-and-forget), so this lock is never held for a
# blocking wait. Cross-process exclusion is deliberately NOT attempted: the
# service is single-process (uvicorn without workers, one container).
_DELIVERY_LOCK = threading.Lock()


def finalize_terminal_status_and_notify(
    task_id: str,
    status: str,
    *,
    error_message: Optional[str] = None,
    url: Optional[str] = None,
    title: Optional[str] = None,
    author: Optional[str] = None,
    notify_status: Optional[str] = None,
    notify_error: Optional[str] = None,
    channel_name: Optional[str] = None,
    webhooks: Optional[Dict[str, str]] = None,
    suppress_terminal_notification: bool = False,
    defer_delivery: bool = False,
    cache_manager=None,
    router=None,
    notify_via=None,
    **update_kwargs: Any,
) -> bool:
    """Write terminal status via CAS; notify only if this call won.

    Returns True iff the row was updated. Notify errors are logged, not raised.
    Pass suppress_terminal_notification=True to persist without producing an
    outbox row at all (I6, HTTP cleanup path).
    Pass defer_delivery=True to persist the outbox row without inline delivery;
    the caller must deliver it after any content notification.
    """
    try:
        # 先取消计时再写终态，且不论 CAS 成败都执行。到点回调持有同一把锁
        # 完成 ⏳ 入队，因此若回调先拿锁，⏳ 必先于终态写入及终态通知入队。
        slow_alert.cancel_all(task_id)

        if cache_manager is None:
            cache_manager = get_cache_manager()

        written = cache_manager.update_task_status(
            task_id,
            status,
            error_message=error_message,
            title=title,
            author=author,
            suppress_terminal_notification=suppress_terminal_notification,
            **update_kwargs,
        )
    except BaseException:
        # 终态未能写入的异常路径兜底：忙锁不得跨过本函数泄漏，原样上抛。
        release_for_task(task_id)
        raise

    if not written:
        current_task = cache_manager.get_task_by_id(task_id)
        current_status = current_task.get("status") if current_task else "unknown"
        logger.warning(
            f"terminal CAS lost (task already {current_status}), "
            f"skip status notify: {task_id}"
        )
        release_for_task(task_id)
        return False

    try:
        logger.info(f"terminal CAS won: {task_id} -> {status}")

        # 终态已落库：忙锁在通知投递前释放（gate 第 9 轮）——投递及其重试队列
        # 不该占着忙锁拉长部署侧排他锁的等待。此点之后任何路径都不再持有该锁；
        # 投递失败由 deliver_terminal_notification 自行记录，不吞、不回滚。
        release_for_task(task_id)

        if suppress_terminal_notification:
            logger.debug(
                f"caller suppressed terminal status notification at write time: {task_id}"
            )
            return True

        if not defer_delivery:
            deliver_terminal_notification(
                task_id,
                status,
                error_message=error_message,
                url=url,
                title=title,
                author=author,
                notify_status=notify_status,
                notify_error=notify_error,
                channel_name=channel_name,
                webhooks=webhooks,
                cache_manager=cache_manager,
                router=router,
                notify_via=notify_via,
            )
        return True
    finally:
        # 终态 CAS 成功后异常路径幂等兜底（gate 第 11 轮）：
        # 任何 post-CAS 异常都不可能永久持锁；首释放生效后此兜底为 no-op。
        release_for_task(task_id)


def deliver_terminal_notification(
    task_id: str,
    status: str,
    *,
    error_message: Optional[str] = None,
    url: Optional[str] = None,
    title: Optional[str] = None,
    author: Optional[str] = None,
    notify_status: Optional[str] = None,
    notify_error: Optional[str] = None,
    channel_name: Optional[str] = None,
    webhooks: Optional[Dict[str, str]] = None,
    cache_manager=None,
    router=None,
    notify_via=None,
) -> None:
    """Deliver an already-persisted terminal notification inline."""
    if cache_manager is None:
        cache_manager = get_cache_manager()

    if notify_status is None:
        if status == TaskStatus.SUCCESS:
            notify_status = TERMINAL_SUCCESS_STATUS
        else:
            notify_status = TERMINAL_FAILED_STATUS

    error_for_notify = notify_error if notify_error is not None else error_message

    task = _prepare_local_upload_notification_task(
        cache_manager, cache_manager.get_task_by_id(task_id) or {}
    )
    webhooks = _task_notify_targets(task)
    channel_name = None
    display_url = url or task.get("url") or ""
    if title is None:
        title = task.get("title")
    if author is None:
        author = task.get("author")
    view_url = _resolve_view_url(cache_manager, task_id, task)

    accepted = False
    with _DELIVERY_LOCK:
        if not cache_manager.is_terminal_notification_pending(task_id):
            logger.debug(
                f"terminal notification already sent by another deliverer: {task_id}"
            )
            return
        cache_manager.mark_terminal_notification_attempted(task_id)
        try:
            completion_body = _render_success_body(task, view_url, status)
            completion_receipt = _render_success_receipt(task, view_url, status)
            result = _emit_status_notification(
                display_url=display_url or "",
                notify_status=notify_status,
                error_for_notify=error_for_notify,
                title=title,
                author=author,
                channel_name=channel_name,
                webhooks=webhooks,
                router=router,
                notify_via=notify_via,
                view_url=view_url,
                task_id=task_id,
                completion_body=completion_body,
                completion_receipt=completion_receipt,
            )
            zero_target = isinstance(result, dict) and result == {}
            if zero_target:
                logger.info(
                    f"NOTIFY_TARGET_SKIPPED task_id={task_id}: "
                    "no declared notify target, outbox row processed"
                )
            accepted = _notification_accepted(result, zero_target=zero_target)
        except Exception:
            logger.exception(
                f"status notification failed (terminal already persisted): {task_id}"
            )
        else:
            if accepted:
                cache_manager.mark_terminal_notification_sent(task_id)


def _emit_status_notification(
    *,
    display_url: str,
    notify_status: str,
    error_for_notify: Optional[str],
    title: Optional[str],
    author: Optional[str],
    channel_name: Optional[str],
    webhooks: Optional[Dict[str, str]],
    router=None,
    notify_via=None,
    view_url: Optional[str] = None,
    task_id: str,
    completion_body: Optional[str] = None,
    completion_receipt: Optional[str] = None,
) -> None:
    """Dispatch the status line through a bound notifier or the router."""
    if notify_via is not None:
        return notify_via.notify_task_status(
            display_url,
            notify_status,
            error_for_notify,
            title,
            author,
            view_url=view_url,
            task_id=task_id,
            completion_body=completion_body,
            completion_receipt=completion_receipt,
        )

    sender = router if router is not None else get_notification_router()
    return sender.notify_task_status(
        url=display_url,
        status=notify_status,
        error=error_for_notify,
        title=title,
        author=author,
        channel_name=channel_name,
        webhooks=webhooks,
        view_url=view_url,
        task_id=task_id,
        completion_body=completion_body,
        completion_receipt=completion_receipt,
    )


def _render_success_receipt(task, view_url, status):
    """Render a copyable success receipt from the persisted terminal snapshot."""
    if status != TaskStatus.SUCCESS:
        return None
    from ...utils.notifications.completion_share import build_completion_share_receipt

    return build_completion_share_receipt(task, view_url or "")


def _render_success_body(task, view_url, status):
    """Render success content from the persisted terminal snapshot.

    A success row whose snapshot lacks a persisted ``result`` (missing or not
    a dict) degrades to a visible notice plus the view link and is sent
    normally: a raise here would fail this row on every replay and never let
    it be marked sent (I4 no-loss), and silently dropping the body would
    leave the user with no hint that the summary is missing. An empty dict
    counts as missing too (N2): it still gets the COMPLETION-BODY-MISSING
    warning here, then renders through ``_render_completion_body``'s
    empty-input guard, which returns the same degraded notice. The missing
    result is deterministic, so the row is not retried for the body; any
    other render error still propagates and keeps the row pending for retry.
    """
    if status != TaskStatus.SUCCESS:
        return None
    snapshot = task.get("terminal_snapshot") or {}
    result = snapshot.get("result")
    if not isinstance(result, dict) or not result:
        logger.warning(
            f"COMPLETION-BODY-MISSING task_id={task.get('task_id')}: "
            "success terminal notification lacks persisted result, "
            "sending header + degraded notice + view link"
        )
    if not isinstance(result, dict):
        degraded = "⚠️ 总结未能载入，请在网页查看"
        if view_url:
            degraded += f"\n\n🔗 查看：{view_url}"
        return degraded
    from .llm_ops import _render_completion_body

    return _render_completion_body(
        result,
        view_url or "",
        use_speaker_recognition=snapshot.get("use_speaker_recognition", False),
        calibrate_only=snapshot.get("calibrate_only", False),
    )


def _notification_accepted(result, *, zero_target: bool = False) -> bool:
    """Decide the outbox sent/skip disposition for a router result.

    Returns True only when EVERY declared channel explicitly accepted the
    enqueue (any single non-True value keeps the row pending for retry; the
    retry re-sends to already-accepted channels, an accepted semantic).
    Truthy objects or strings can represent queued or other intermediate
    states; treating them as success would prevent the outbox from retrying
    the row. A zero-target task (no declared channel had an address) is
    permanently skipped, not retried (C1)."""
    if zero_target:
        return True
    if not isinstance(result, dict):
        logger.warning(
            f"terminal notification result rejected: "
            f"type={type(result).__name__} value={result!r}"
        )
        return False
    if not result:
        return False
    return all(value is True for value in result.values())


DISPATCH_POLL_SECONDS = 0.5
DISPATCH_MIN_AGE_SECONDS = 5.0
DISPATCH_BATCH_SIZE = 20
# Observation only (I4): rows past this many attempts get a warning, never a filter.
STUCK_ATTEMPT_WARN_THRESHOLD = 3
_RECOVERY_REASONS = frozenset({
    "orphaned_on_startup",
    "shutdown_drain",
    "runtime_reconcile",
})


def deliver_pending_terminal_notifications(
    cache_manager=None,
    *,
    router=None,
    limit: int = DISPATCH_BATCH_SIZE,
    min_age_seconds: float = 0.0,
) -> int:
    """Send every eligible unsent row, one at a time.

    ``min_age_seconds=0`` keeps direct callers and startup replay immediate.
    The steady-state dispatcher supplies the age gate so a row written by an
    in-flight inline success path cannot be sent before its content message.
    Fresh rows are only temporarily omitted; their age grows monotonically and
    they become eligible after the gate, preserving I4's no-loss guarantee.

    In-process mutual exclusion comes from `_DELIVERY_LOCK`, shared with the
    inline helper path: both re-check `notified_at IS NULL` while holding it, so
    the same row is sent once. Cross-process exclusion is NOT assumed (the
    service is single-process: uvicorn without workers, one container).
    """
    from ..context import get_cache_manager as _get_cm

    if cache_manager is None:
        cache_manager = _get_cm()
    if router is None:
        router = get_notification_router()

    rows = cache_manager.list_unattempted_terminal_notifications(
        limit=limit,
        min_age_seconds=min_age_seconds,
    )
    sent = 0
    for row in rows:
        task_id = row["task_id"]
        try:
            task = _prepare_local_upload_notification_task(
                cache_manager, cache_manager.get_task_by_id(task_id) or {}
            )
            notify_status = (
                TERMINAL_SUCCESS_STATUS
                if row["status"] == TaskStatus.SUCCESS
                else TERMINAL_FAILED_STATUS
            )
            error_for_notify = _compose_dispatcher_error(row, task)
            webhooks = _task_notify_targets(task)
            view_url = _resolve_view_url(cache_manager, task_id, task)
        except Exception:
            # Poison-row isolation: one unreadable row (corrupt notify_targets
            # JSON, missing local-upload mapping, ...) must not kill the whole
            # replay round for every other user's pending rows. Mark the
            # attempt so _warn_repeatedly_failing_rows keeps it visible and
            # move on; the row itself stays pending (no fake "sent", no
            # fallback target) and is retried next round.
            logger.exception(
                f"NOTIFY_TARGETS_RESOLVE_FAILED task_id={task_id}"
            )
            cache_manager.mark_terminal_notification_attempted(task_id)
            continue
        accepted = False
        with _DELIVERY_LOCK:
            if not cache_manager.is_terminal_notification_pending(task_id):
                continue
            cache_manager.mark_terminal_notification_attempted(task_id)
            try:
                completion_body = _render_success_body(task, view_url, row["status"])
                completion_receipt = _render_success_receipt(task, view_url, row["status"])
                result = router.notify_task_status(
                    url=task.get("url") or "",
                    status=notify_status,
                    error=error_for_notify,
                    title=task.get("title"),
                    author=task.get("author"),
                    webhooks=webhooks,
                    view_url=view_url,
                    task_id=task_id,
                    completion_body=completion_body,
                    completion_receipt=completion_receipt,
                )
                zero_target = isinstance(result, dict) and result == {}
                if zero_target:
                    logger.info(
                        f"NOTIFY_TARGET_SKIPPED task_id={task_id}: "
                        "no declared notify target, outbox row processed"
                    )
                accepted = _notification_accepted(result, zero_target=zero_target)
            except Exception:
                logger.exception(
                    f"dispatcher status notification failed: {task_id}"
                )
            else:
                if accepted:
                    cache_manager.mark_terminal_notification_sent(task_id)
                    if not zero_target:
                        sent += 1
    _warn_repeatedly_failing_rows(cache_manager)
    return sent


def deliver_terminal_notification_dispatcher_round(
    cache_manager=None,
    *,
    router=None,
    limit: int = DISPATCH_BATCH_SIZE,
) -> int:
    """Run one steady-state dispatcher round with the minimum age gate."""
    return deliver_pending_terminal_notifications(
        cache_manager,
        router=router,
        limit=limit,
        min_age_seconds=DISPATCH_MIN_AGE_SECONDS,
    )


def _warn_repeatedly_failing_rows(cache_manager) -> None:
    """I4 guardrail: keep retrying, but make a stuck row visible in the log."""
    stuck = cache_manager.count_attempted_terminal_notifications(
        STUCK_ATTEMPT_WARN_THRESHOLD
    )
    if stuck:
        logger.warning(
            f"{stuck} terminal notification row(s) still unsent after "
            f"{STUCK_ATTEMPT_WARN_THRESHOLD}+ attempts; they stay listed and "
            "keep being retried"
        )


def run_terminal_notification_dispatcher() -> None:
    """Replay pending rows, then poll. Closes this thread's sqlite connection on exit."""
    from ..context import get_cache_manager, get_runtime

    logger.info("terminal notification dispatcher starting")
    cache_manager = get_cache_manager()
    runtime = get_runtime()
    stop_event = getattr(runtime, "terminal_notify_stop_event", None)
    try:
        delivered = deliver_pending_terminal_notifications(cache_manager)
        if delivered:
            logger.info(f"terminal notification replay sent {delivered} pending row(s)")
        while stop_event is None or not stop_event.is_set():
            deliver_terminal_notification_dispatcher_round(cache_manager)
            if stop_event is None:
                break
            stop_event.wait(DISPATCH_POLL_SECONDS)
    finally:
        cache_manager.close()
        logger.info("terminal notification dispatcher stopped")


def _compose_dispatcher_error(row: dict, task: dict) -> str:
    """Build the dispatcher error/body: original error, completed_at, recovery note.

    Success rows compose no note: completed_at/recovery lines are failure
    diagnostics, and a non-None error would flip the channel-side status
    emoji from ✅ to ❌.
    """
    if row["status"] == TaskStatus.SUCCESS:
        return None
    parts = []
    error_message = row.get("error_message") or task.get("error_message")
    if error_message:
        parts.append(str(error_message))
    completed_at = row.get("completed_at")
    if completed_at:
        parts.append(f"completed_at={completed_at}")
    snapshot = task.get("terminal_snapshot") or {}
    if isinstance(snapshot, dict) and snapshot.get("reason") in _RECOVERY_REASONS:
        parts.append("由服务重启恢复判定")
    return "\n".join(parts) if parts else None


def _prepare_local_upload_notification_task(cache_manager, task: dict) -> dict:
    """Enrich an ephemeral delivery copy; never write aliases back to task/audit."""
    task = dict(task)
    if task.get("platform") != "local_upload":
        return task
    upload = cache_manager.get_local_upload_for_task(task["task_id"])
    if upload is None:
        raise ValueError(f"local upload delivery mapping missing: {task['task_id']}")
    from ...utils.notifications.task_formatter import local_upload_notification_fields

    fields = local_upload_notification_fields(upload)
    task["title"] = fields["title"]
    task["url"] = fields["source_url"] or fields["source_label"]
    task["source_label"] = fields["source_label"]
    task["view_token"] = upload["view_token"]
    return task


def _resolve_view_url(cache_manager, task_id: str, task: Optional[dict] = None) -> Optional[str]:
    """Build `{base_url}/view/{view_token}` from task or upload identity."""
    if task is None:
        task = cache_manager.get_task_by_id(task_id) or {}
    token = task.get("view_token")
    if not token and task.get("platform") == "local_upload":
        upload = cache_manager.get_local_upload_for_task(task_id)
        if upload is None:
            raise ValueError(f"local upload delivery mapping missing: {task_id}")
        token = upload["view_token"]
    if not token:
        return None
    from ...utils.rendering import get_base_url
    return f"{get_base_url()}/view/{token}"


def load_task_targets(cache_manager, task_id: str) -> Dict[str, str]:
    """Load a task's immutable targets, resolving only pre-migration rows."""
    task = cache_manager.get_task_by_id(task_id)
    if task is None:
        logger.warning("NOTIFY_TARGETS_TASK_ROW_MISSING task_id={}", task_id)
        return {}
    return _task_notify_targets(task)


def _task_notify_targets(task: dict) -> Dict[str, str]:
    """Read persisted destinations, resolving only pre-migration rows live."""
    stored = task.get("notify_targets")
    if stored is not None:
        return stored

    task_id = task.get("task_id")
    logger.warning("NOTIFY_TARGETS_LEGACY_ROW task_id={}", task_id)
    submitted_by = task.get("submitted_by")
    if not submitted_by:
        return {}

    from ..context import get_runtime

    user = get_runtime().user_manager.get_user_by_id(submitted_by)
    if not user:
        return {}
    return resolve_notify_targets(user, {})
