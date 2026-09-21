"""Single exit for terminal (success/failed) status writes and status notifies."""

from typing import Any, Dict, Optional

from ..context import get_cache_manager, get_logger, lazy_resource
from ...utils.notifications import get_notification_router
from ...utils.task_status import TaskStatus

logger = lazy_resource(get_logger)

TERMINAL_SUCCESS_STATUS = "【任务完成】"
TERMINAL_FAILED_STATUS = "【任务失败】"


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
    send_status_notification: bool = True,
    cache_manager=None,
    router=None,
    notify_via=None,
    **update_kwargs: Any,
) -> bool:
    """Write terminal status via CAS; notify only if this call won.

    Returns True iff the row was updated. Notify errors are logged, not raised.
    Pass send_status_notification=False to persist without sending (HTTP cleanup).
    """
    if cache_manager is None:
        cache_manager = get_cache_manager()

    written = cache_manager.update_task_status(
        task_id,
        status,
        error_message=error_message,
        title=title,
        author=author,
        **update_kwargs,
    )
    if not written:
        current_task = cache_manager.get_task_by_id(task_id)
        current_status = current_task.get("status") if current_task else "unknown"
        logger.warning(
            f"terminal CAS lost (task already {current_status}), "
            f"skip status notify: {task_id}"
        )
        return False

    logger.info(f"terminal CAS won: {task_id} -> {status}")

    if not send_status_notification:
        cache_manager.mark_terminal_notification_sent(task_id)
        logger.debug(
            f"caller suppressed terminal status notification: {task_id}"
        )
        return True

    if not cache_manager.claim_pending_terminal_notification(task_id):
        return True

    if notify_status is None:
        if status == TaskStatus.SUCCESS:
            notify_status = TERMINAL_SUCCESS_STATUS
        else:
            notify_status = TERMINAL_FAILED_STATUS

    error_for_notify = notify_error if notify_error is not None else error_message

    display_url = url
    task = None
    if not display_url or title is None or author is None:
        task = cache_manager.get_task_by_id(task_id) or {}
        if not display_url:
            display_url = task.get("url") or ""
        if title is None:
            title = task.get("title")
        if author is None:
            author = task.get("author")
    view_url = _resolve_view_url(cache_manager, task_id, task)

    accepted = False
    try:
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
        )
        accepted = _notification_accepted(result)
    except Exception:
        logger.exception(
            f"status notification failed (terminal already persisted): {task_id}"
        )
        cache_manager.release_terminal_notification_claim(task_id)
    else:
        if accepted:
            cache_manager.mark_terminal_notification_sent(task_id)
        else:
            cache_manager.release_terminal_notification_claim(task_id)
    return True


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
    )


def _notification_accepted(result) -> bool:
    """True only when a real channel result explicitly reports acceptance."""
    if not isinstance(result, dict):
        logger.warning(
            "terminal notification result rejected: type=%s value=%r",
            type(result).__name__,
            result,
        )
        return False
    return any(bool(value) for value in result.values())


DISPATCH_POLL_SECONDS = 0.5
DISPATCH_BATCH_SIZE = 20
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
) -> int:
    """Claim and send never-attempted pending rows. Used by the dispatcher and tests."""
    from ..context import get_cache_manager as _get_cm

    if cache_manager is None:
        cache_manager = _get_cm()
    if router is None:
        router = get_notification_router()

    rows = cache_manager.list_unattempted_terminal_notifications(limit=limit)
    sent = 0
    for row in rows:
        task_id = row["task_id"]
        if not cache_manager.claim_pending_terminal_notification(task_id):
            continue
        task = cache_manager.get_task_by_id(task_id) or {}
        notify_status = (
            TERMINAL_SUCCESS_STATUS
            if row["status"] == TaskStatus.SUCCESS
            else TERMINAL_FAILED_STATUS
        )
        error_for_notify = _compose_dispatcher_error(row, task)
        webhooks = _resolve_delivery_webhooks(task)
        view_url = _resolve_view_url(cache_manager, task_id, task)
        accepted = False
        try:
            result = router.notify_task_status(
                url=task.get("url") or "",
                status=notify_status,
                error=error_for_notify,
                title=task.get("title"),
                author=task.get("author"),
                webhooks=webhooks,
                view_url=view_url,
            )
            accepted = _notification_accepted(result)
        except Exception:
            logger.exception(
                f"dispatcher status notification failed: {task_id}"
            )
            cache_manager.release_terminal_notification_claim(task_id)
        else:
            if accepted:
                cache_manager.mark_terminal_notification_sent(task_id)
                sent += 1
            else:
                cache_manager.release_terminal_notification_claim(task_id)
    return sent


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
            deliver_pending_terminal_notifications(cache_manager)
            if stop_event is None:
                break
            stop_event.wait(DISPATCH_POLL_SECONDS)
    finally:
        cache_manager.close()
        logger.info("terminal notification dispatcher stopped")


def _compose_dispatcher_error(row: dict, task: dict) -> str:
    """Build the dispatcher error/body: original error, completed_at, recovery note."""
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


def _resolve_view_url(cache_manager, task_id: str, task: Optional[dict] = None) -> Optional[str]:
    """Build `{base_url}/view/{view_token}` from the task row."""
    if task is None:
        task = cache_manager.get_task_by_id(task_id) or {}
    token = task.get("view_token")
    if not token:
        return None
    from ...utils.rendering import get_base_url
    return f"{get_base_url()}/view/{token}"


def _resolve_delivery_webhooks(task: dict) -> Optional[Dict[str, str]]:
    """Resolve notify targets without request context: audit log, user, then global."""
    task_id = task.get("task_id")
    audit_webhook = _lookup_audit_wechat_webhook(task_id)
    if audit_webhook:
        return {"wechat": audit_webhook}

    submitted_by = task.get("submitted_by")
    if submitted_by:
        try:
            from ..context import get_runtime
            user = get_runtime().user_manager.get_user_by_id(submitted_by)
        except RuntimeError:
            user = None
        except Exception:
            logger.exception("failed to resolve user webhook for terminal notify")
            user = None
        if user:
            webhooks = {}
            if user.get("wechat_webhook"):
                webhooks["wechat"] = user["wechat_webhook"]
            if user.get("feishu_webhook"):
                webhooks["feishu"] = user["feishu_webhook"]
            if webhooks:
                return webhooks
    return None


def _lookup_audit_wechat_webhook(task_id: Optional[str]) -> Optional[str]:
    """Read api_audit_logs.wechat_webhook by task_id. Audit schema is not ours to extend."""
    if not task_id:
        return None
    try:
        from ..context import get_runtime
        audit = get_runtime().audit_logger
    except RuntimeError:
        return None
    except Exception:
        logger.exception("failed to open audit logger for terminal notify webhook")
        return None
    try:
        with audit._get_cursor() as cursor:
            cursor.execute(
                '''SELECT wechat_webhook FROM api_audit_logs
                   WHERE task_id = ? AND wechat_webhook IS NOT NULL
                         AND wechat_webhook != ''
                   ORDER BY request_time DESC LIMIT 1''',
                (task_id,),
            )
            row = cursor.fetchone()
        if row:
            return row[0]
    except Exception:
        logger.exception("failed to lookup audit wechat_webhook for terminal notify")
    return None
