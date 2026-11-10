"""C1 stop-leak invariants: tenant notifications never reach global webhooks.

Invariant table comes from docs/sessions/notify-redesign-261110/design.md
("C1 stop-leak" + invariants 1-7). Every test asserts the real HTTP boundary:
the URL the producer actually POSTs, stubbed at the same place as
tests/unit/test_notification_e2e_delivery.py (the third-party sender's
``requests.post``). No test may assert only an internal dict.

Fake keys only; the domains match the two real sender modules so the stub can
route payloads by URL.
"""

import sqlite3
import threading

import pytest
from wecom_notifier import FeishuNotifier, WeComNotifier
from wecom_notifier.platforms.feishu import sender as feishu_sender
from wecom_notifier.platforms.wecom import sender as wecom_sender

from src.video_transcript_api.api.services.terminal_status import (
    deliver_pending_terminal_notifications,
    deliver_terminal_notification,
    finalize_terminal_status_and_notify,
)
from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.notifications.router import NotificationRouter
from src.video_transcript_api.utils.task_status import TaskStatus


CHANNEL_MODULE = "src.video_transcript_api.utils.notifications.channel"
ROUTER_MODULE = "src.video_transcript_api.utils.notifications.router"
WECHAT_MODULE = "src.video_transcript_api.utils.notifications.wechat"
GLOBAL_WECHAT = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=global-fake"
GLOBAL_FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/global-fake"
TENANT_FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/tenant-fake"
TENANT_WECHAT = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=tenant-fake"


@pytest.fixture
def http_boundary(monkeypatch):
    """Real router/channels/queues with a recording HTTP boundary.

    Global config declares both group webhooks; the tenant webhooks are
    distinct URLs so a leak to the global group is observable.
    """
    config = {
        "wechat": {"webhook": GLOBAL_WECHAT},
        "feishu": {"webhook": GLOBAL_FEISHU, "secret": None},
    }
    records = []
    condition = threading.Condition()

    class Response:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def post(url, *, json, **_kwargs):
        with condition:
            records.append(url)
            condition.notify_all()
        if "qyapi.weixin.qq.com" in url:
            return Response({"errcode": 0, "errmsg": "ok"})
        return Response({"code": 0, "msg": "ok"})

    def wait_for(count, timeout=5):
        with condition:
            return condition.wait_for(lambda: len(records) >= count, timeout=timeout)

    monkeypatch.setattr(wecom_sender.requests, "post", post)
    monkeypatch.setattr(feishu_sender.requests, "post", post)
    monkeypatch.setattr(f"{ROUTER_MODULE}.load_config", lambda: config)
    monkeypatch.setattr(f"{CHANNEL_MODULE}.load_config", lambda: config)
    monkeypatch.setattr(f"{WECHAT_MODULE}.load_config", lambda: config)
    monkeypatch.setattr(
        "src.video_transcript_api.utils.logging.load_config", lambda: config,
    )

    wecom_notifier = WeComNotifier(max_retries=0)
    feishu_notifier = FeishuNotifier(max_retries=0)
    monkeypatch.setattr(f"{WECHAT_MODULE}._get_global_notifier", lambda: wecom_notifier)
    monkeypatch.setattr(
        f"{CHANNEL_MODULE}._get_global_feishu_notifier", lambda: feishu_notifier,
    )

    yield {
        "router": NotificationRouter(),
        "records": records,
        "wait_for": wait_for,
        "config": config,
    }

    for manager in wecom_notifier.webhook_managers.values():
        manager._stop_flag.set()
        manager.worker_thread.join(timeout=5)
    feishu_notifier.stop_all()


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _persist_pending_terminal(
    cm, status=TaskStatus.SUCCESS, notify_targets=None, **create_kwargs,
):
    created = cm.create_task(
        url="https://example.test/clip", notify_targets=notify_targets, **create_kwargs
    )
    task_id = created["task_id"]
    cm.update_task_status(task_id, TaskStatus.PROCESSING)
    assert finalize_terminal_status_and_notify(
        task_id, status, cache_manager=cm, defer_delivery=True,
    ) is True
    return task_id


# ---------------------------------------------------------------------------
# Invariant 1 / 1b: tenant methods with empty vs declared webhooks.
# ---------------------------------------------------------------------------

TENANT_METHODS = (
    lambda router, webhooks: router.send_text("hello", webhooks=webhooks),
    lambda router, webhooks: router.send_rich("## hello", webhooks=webhooks),
    lambda router, webhooks: router.notify_task_status(
        url="https://example.test/clip", status="started", webhooks=webhooks,
        task_id="task_abcdef0123456789",
    ),
    lambda router, webhooks: router.send_view_link(
        title="clip", view_token="tok", webhooks=webhooks,
        task_id="task_abcdef0123456789",
    ),
    lambda router, webhooks: router.send_long_text(
        title="clip", url="https://example.test/clip", text="body",
        webhooks=webhooks,
    ),
)


def test_router_tenant_methods_with_empty_webhooks_request_no_global_url(
    http_boundary,
):
    """Invariant 1: non-legacy tenant, webhooks={} -> zero global HTTP calls."""
    router = http_boundary["router"]
    for call in TENANT_METHODS:
        result = call(router, {})
        assert result == {}, result
    assert http_boundary["wait_for"](1, timeout=0.3) is False
    assert http_boundary["records"] == []


def test_router_tenant_methods_with_declared_feishu_request_only_that_url(
    http_boundary,
):
    """Invariant 1b: webhooks={"feishu": X} -> exactly X is requested, the
    global wechat group stays silent (positive control: no blanket muting)."""
    router = http_boundary["router"]
    for call in TENANT_METHODS:
        result = call(router, {"feishu": TENANT_FEISHU})
        assert result == {"feishu": True}, result
    assert http_boundary["wait_for"](len(TENANT_METHODS))
    assert set(http_boundary["records"]) == {TENANT_FEISHU}


def test_router_channels_initialized_without_global_webhooks(monkeypatch):
    """Channel enablement no longer depends on global config webhooks."""
    monkeypatch.setattr(
        f"{ROUTER_MODULE}.load_config", lambda: {"wechat": {}, "feishu": {}},
    )
    router = NotificationRouter()
    assert [channel.name for channel in router.channels] == ["wechat", "feishu"]


# ---------------------------------------------------------------------------
# Invariant 2: no global feishu config, user feishu still delivered.
# ---------------------------------------------------------------------------

def test_user_feishu_target_reaches_http_boundary_without_global_feishu_config(
    http_boundary, monkeypatch,
):
    config = {"wechat": {"webhook": GLOBAL_WECHAT}, "feishu": {}}
    monkeypatch.setattr(f"{ROUTER_MODULE}.load_config", lambda: config)
    monkeypatch.setattr(f"{CHANNEL_MODULE}.load_config", lambda: config)
    monkeypatch.setattr(f"{WECHAT_MODULE}.load_config", lambda: config)
    router = NotificationRouter()

    result = router.send_rich("## body", webhooks={"feishu": TENANT_FEISHU})

    assert result == {"feishu": True}
    assert http_boundary["wait_for"](1)
    assert http_boundary["records"] == [TENANT_FEISHU]


# ---------------------------------------------------------------------------
# Invariant 3: zero-target terminal notice processed once, never retried.
# ---------------------------------------------------------------------------

def test_zero_target_terminal_notice_is_processed_once_across_replay_and_restart(
    cm, http_boundary,
):
    """An explicit empty target object is permanently silent across replays."""
    task_id = _persist_pending_terminal(cm, notify_targets={})

    assert deliver_pending_terminal_notifications(
        cm, router=http_boundary["router"],
    ) == 0
    assert cm.is_terminal_notification_pending(task_id) is False
    assert http_boundary["wait_for"](1, timeout=0.3) is False

    # More replay rounds and a simulated restart (fresh router) stay silent:
    # the row is gone from the eligible list, so no retry loop spins.
    for _ in range(3):
        assert deliver_pending_terminal_notifications(
            cm, router=http_boundary["router"],
        ) == 0
    assert deliver_pending_terminal_notifications(
        cm, router=NotificationRouter(),
    ) == 0
    with sqlite3.connect(cm.db_path) as connection:
        notified_at, attempts = connection.execute(
            "SELECT notified_at, attempts FROM task_terminal_notifications"
            " WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert notified_at is not None
    assert attempts == 1
    assert http_boundary["records"] == []


# ---------------------------------------------------------------------------
# Invariant 4: one rejected declared channel keeps the whole notice unaccepted.
# ---------------------------------------------------------------------------

def test_rejected_declared_channel_keeps_outbox_row_pending(cm, http_boundary):
    task_id = _persist_pending_terminal(
        cm, notify_targets={"wechat": TENANT_WECHAT, "feishu": TENANT_FEISHU}
    )
    feishu_channel = next(
        channel for channel in http_boundary["router"].channels
        if channel.name == "feishu"
    )
    # Channel-level rejection: send_rich returning False is exactly the
    # "not accepted" signal FeishuChannel produces when the notifier refuses.
    feishu_channel.send_rich = lambda *args, **kwargs: False

    deliver_terminal_notification(
        task_id,
        TaskStatus.SUCCESS,
        cache_manager=cm,
        router=http_boundary["router"],
    )

    assert cm.is_terminal_notification_pending(task_id) is True
    with sqlite3.connect(cm.db_path) as connection:
        notified_at = connection.execute(
            "SELECT notified_at FROM task_terminal_notifications WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert notified_at is None


# ---------------------------------------------------------------------------
# Invariant 5: ASR monitor ops alerts explicitly request the global webhook.
# ---------------------------------------------------------------------------

def test_asr_monitor_alert_requests_global_webhook_url(http_boundary, monkeypatch):
    from src.video_transcript_api.utils import notifications as notifications_module
    from src.video_transcript_api.utils.asr_monitor import ASRMonitor

    # ASRMonitor resolves its notifier lazily via get_notification_router; bind
    # it to this fixture's router so the test does not depend on the module
    # singleton's prior initialization state.
    monkeypatch.setattr(
        notifications_module, "get_notification_router",
        lambda: http_boundary["router"],
    )
    monitor = ASRMonitor(
        services={"CapsWriter": "ws://localhost:10001"},
        check_interval=1,
        failure_threshold=3,
        debounce_seconds=1800,
    )
    for _ in range(3):
        monitor._handle_check_result("CapsWriter", "ws://localhost:10001", False)

    assert http_boundary["wait_for"](2)
    assert GLOBAL_WECHAT in http_boundary["records"]
    assert GLOBAL_FEISHU in http_boundary["records"]
    assert TENANT_WECHAT not in http_boundary["records"]
    assert TENANT_FEISHU not in http_boundary["records"]


# ---------------------------------------------------------------------------
# Invariant 6: replay uses the submission-time destination snapshot.
# ---------------------------------------------------------------------------

def test_replay_ignores_later_user_configuration_changes(
    cm, http_boundary, monkeypatch,
):
    from src.video_transcript_api.api import context

    task_id = _persist_pending_terminal(
        cm, submitted_by="user-9", notify_targets={"feishu": TENANT_FEISHU},
    )
    user_manager = type("UM", (), {})()
    user_manager.get_user_by_id = lambda _user_id: {
        "user_id": "user-9", "is_legacy": False,
        "wechat_webhook": GLOBAL_WECHAT, "feishu_webhook": GLOBAL_FEISHU,
    }
    runtime = type("RT", (), {})()
    runtime.user_manager = user_manager
    monkeypatch.setattr(context, "get_runtime", lambda: runtime)

    assert deliver_pending_terminal_notifications(
        cm, router=http_boundary["router"],
    ) == 1
    assert http_boundary["wait_for"](2)
    assert set(http_boundary["records"]) == {TENANT_FEISHU}
    assert cm.is_terminal_notification_pending(task_id) is False


# ---------------------------------------------------------------------------
# Invariant 7: legacy_user keeps the global targets.
# ---------------------------------------------------------------------------

def test_legacy_user_replay_keeps_global_webhook_targets(
    cm, http_boundary, monkeypatch,
):
    from src.video_transcript_api.api import context
    from src.video_transcript_api.api.services import terminal_status

    task_id = _persist_pending_terminal(
        cm, submitted_by="legacy_user", notify_targets={},
    )
    with cm._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET notify_targets = NULL WHERE task_id = ?",
            (task_id,),
        )
    user_manager = type("UM", (), {})()
    user_manager.get_user_by_id = lambda _user_id: {
        "user_id": "legacy_user", "is_legacy": True,
        "wechat_webhook": GLOBAL_WECHAT, "feishu_webhook": GLOBAL_FEISHU,
    }
    runtime = type("RT", (), {})()
    runtime.user_manager = user_manager
    monkeypatch.setattr(context, "get_runtime", lambda: runtime)

    webhooks = terminal_status._task_notify_targets(
        cm.get_task_by_id(task_id),
    )
    assert webhooks == {"wechat": GLOBAL_WECHAT, "feishu": GLOBAL_FEISHU}

    def deliver():
        return deliver_pending_terminal_notifications(
            cm, router=http_boundary["router"],
        )

    deliver()
    assert http_boundary["wait_for"](2)
    assert set(http_boundary["records"]) == {GLOBAL_WECHAT, GLOBAL_FEISHU}
    assert cm.is_terminal_notification_pending(task_id) is False
