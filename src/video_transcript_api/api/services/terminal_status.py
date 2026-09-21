"""Single exit for terminal task status writes and status notifications.

Milestone 1: callers write success/failed through
``finalize_terminal_status_and_notify``. Status notification is sent only
when the CAS write wins. Content bodies (summary / notes / calibrated
text) stay on the worker side.

Milestone 2 replaces the in-process notify with an outbox row inserted
inside ``CacheManager.update_task_status`` plus a dispatcher thread.
"""

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
    """Write a terminal status and notify only if this call won the CAS.

    Args:
        task_id: Task id to finalize.
        status: ``success`` or ``failed``.
        error_message: Persisted on the task row when status is failed.
        url: Video URL used in the status notification.
        title: Optional title shown in the status notification.
        author: Optional author shown in the status notification.
        notify_status: Override the user-visible status string. Defaults to
            ``【任务完成】`` / ``【任务失败】``.
        notify_error: Override the error string sent with the notification.
            Defaults to ``error_message``.
        channel_name: Per-request channel pin, forwarded to the router.
        webhooks: Per-request webhook map, forwarded to the router.
        send_status_notification: When False, only the CAS write happens
            (used by the HTTP create-then-cleanup path that already has a
            response body).
        cache_manager: Cache manager instance; defaults to the runtime one.
        router: Notification router; defaults to the global router.
        notify_via: Optional bound notifier (``notify_task_status``). When
            set, channel/webhook context is already bound on that object.
        **update_kwargs: Passed through to ``update_task_status``.

    Returns:
        True if this call won the CAS (row updated). False if the task was
        already terminal or the row does not exist.

    Raises:
        Whatever ``update_task_status`` raises. Notification errors are
        logged and swallowed so they cannot roll back a landed terminal
        write.
    """
    if cache_manager is None:
        cache_manager = get_cache_manager()

    written = cache_manager.update_task_status(
        task_id,
        status,
        error_message=error_message,
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
        return True

    if notify_status is None:
        if status == TaskStatus.SUCCESS:
            notify_status = TERMINAL_SUCCESS_STATUS
        else:
            notify_status = TERMINAL_FAILED_STATUS

    error_for_notify = notify_error if notify_error is not None else error_message

    display_url = url
    if not display_url or title is None or author is None:
        task = cache_manager.get_task_by_id(task_id) or {}
        if not display_url:
            display_url = task.get("url") or ""
        if title is None:
            title = task.get("title")
        if author is None:
            author = task.get("author")

    try:
        _emit_status_notification(
            display_url=display_url or "",
            notify_status=notify_status,
            error_for_notify=error_for_notify,
            title=title,
            author=author,
            channel_name=channel_name,
            webhooks=webhooks,
            router=router,
            notify_via=notify_via,
        )
    except Exception:
        logger.exception(
            f"status notification failed (terminal already persisted): {task_id}"
        )
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
) -> None:
    """Dispatch the status line through a bound notifier or the router."""
    if notify_via is not None:
        notify_via.notify_task_status(
            display_url,
            notify_status,
            error_for_notify,
            title,
            author,
        )
        return

    sender = router if router is not None else get_notification_router()
    sender.notify_task_status(
        url=display_url,
        status=notify_status,
        error=error_for_notify,
        title=title,
        author=author,
        channel_name=channel_name,
        webhooks=webhooks,
    )
