"""ASR 宕机/恢复告警的投递路径行为测试。

ASR 告警是全仓唯一不走 per-user webhook 的通知路径：
``ASRMonitor._send_notification`` 直接取全局 router 并调用
``send_text(message)``，不传 ``webhooks``，因此 router 的
``_resolve_webhook`` 回落到通道自身在 ``config.jsonc`` 里读到的全局
``wechat.webhook``。

本文件锁定的可观察事实：
1. 宕机告警最终落到全局企微通道，且 router 收到的 ``webhook``/``webhooks``
   参数表明走的是「config 全局回落」而非 per-user 覆盖；
2. 恢复通知同样落到全局企微通道；
3. 去抖窗口内的重复失败不再发送（注入时钟，不 sleep）。

全程零真实网络请求：底层企微 sender 被换成记录器，另有 autouse fixture
记录 ``socket.socket.connect`` / ``socket.getaddrinfo`` 的目标，测试结束
断言不存在非 loopback 连接。
"""

import socket
from unittest.mock import patch

import pytest

from video_transcript_api.utils.asr_monitor import ASRMonitor
from video_transcript_api.utils.notifications.router import NotificationRouter


ROUTER_MODULE = "video_transcript_api.utils.notifications.router"
NOTIFICATIONS_MODULE = "video_transcript_api.utils.notifications"
GLOBAL_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=GLOBAL-KEY"


class _RecordingSender:
    """替换 WechatNotifier：只记录被投递的内容，不发网络请求。"""

    def __init__(self, webhook):
        self.webhook = webhook
        self.calls = []

    def send_text(self, content, *args, **kwargs):
        self.calls.append(content)
        return True

    def send_markdown_v2(self, content, *args, **kwargs):
        self.calls.append(content)
        return True


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    """记录出站连接目标，测试结束断言没有指向非 loopback 的连接。"""
    targets = []
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def _record(address):
        if isinstance(address, tuple) and address:
            targets.append(str(address[0]))
        else:
            targets.append(str(address))

    def fake_connect(self, address, *args, **kwargs):
        _record(address)
        return real_connect(self, address, *args, **kwargs)

    def fake_getaddrinfo(host, *args, **kwargs):
        targets.append(str(host))
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", fake_connect)
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    try:
        yield
    finally:
        monkeypatch.undo()
        offenders = [
            t
            for t in targets
            if t not in ("127.0.0.1", "::1", "localhost", "0.0.0.0", "::", "")
        ]
        assert offenders == [], f"test made outbound network calls: {offenders}"


@pytest.fixture
def global_alert_env():
    """真实 NotificationRouter（只含全局企微通道），底层 sender 换成记录器。

    返回 (router, sender, router_send_calls)；``router_send_calls`` 记录
    ``send_text`` 收到的实参，用来观察 asr_monitor 是否传了 per-user 覆盖。
    """
    config = {"wechat": {"webhook": GLOBAL_WEBHOOK}}
    sender = _RecordingSender(GLOBAL_WEBHOOK)
    router_send_calls = []

    with patch(f"{ROUTER_MODULE}.load_config", return_value=config):
        router = NotificationRouter()
    # 用记录器替换真实企微 sender（WeComChannel 是 router 唯一的企微通道）
    router.channels[0]._notifier = sender

    real_send_text = router.send_text

    def spy_send_text(content, *args, **kwargs):
        router_send_calls.append((args, kwargs))
        return real_send_text(content, *args, **kwargs)

    router.send_text = spy_send_text

    with patch(f"{NOTIFICATIONS_MODULE}.get_notification_router", return_value=router):
        yield router, sender, router_send_calls


def _monitor(notifier=None, debounce_seconds=1800):
    return ASRMonitor(
        services={"CapsWriter": "ws://localhost:10001"},
        check_interval=1,
        failure_threshold=3,
        debounce_seconds=debounce_seconds,
        notifier=notifier,
    )


class TestAlertDeliveryTarget:
    """宕机告警 / 恢复通知的收件方 = config.jsonc 的全局企微 webhook。"""

    def test_down_alert_falls_back_to_global_wecom_webhook(self, global_alert_env):
        router, sender, router_send_calls = global_alert_env
        monitor = _monitor()

        for _ in range(3):
            monitor._handle_check_result("CapsWriter", "ws://localhost:10001", False)

        # 送到全局企微通道（router 真的 dispatch 到了该通道的 sender）
        assert len(sender.calls) == 1
        assert "CapsWriter" in sender.calls[0]

        # 走的是 config 全局回落路径：router 未收到任何 per-user 覆盖，
        # 通道自身持有的 webhook 正是 config.jsonc 的全局 key。
        assert len(router_send_calls) == 1
        args, kwargs = router_send_calls[0]
        assert args == ()
        assert kwargs.get("webhook") is None
        assert kwargs.get("webhooks") is None
        assert sender.webhook == GLOBAL_WEBHOOK

    def test_recovery_alert_falls_back_to_global_wecom_webhook(self, global_alert_env):
        router, sender, router_send_calls = global_alert_env
        monitor = _monitor()

        for _ in range(3):
            monitor._handle_check_result("CapsWriter", "ws://localhost:10001", False)
        monitor._handle_check_result("CapsWriter", "ws://localhost:10001", True)

        assert len(sender.calls) == 2
        assert "恢复" in sender.calls[1]

        args, kwargs = router_send_calls[1]
        assert args == ()
        assert kwargs.get("webhook") is None
        assert kwargs.get("webhooks") is None


class TestDebounce:
    """去抖窗口内的重复失败不再发送。"""

    def test_repeat_failure_within_debounce_is_not_sent(self, global_alert_env, monkeypatch):
        router, sender, router_send_calls = global_alert_env
        monitor = _monitor(debounce_seconds=1800)

        clock = {"now": 1_000_000.0}
        monkeypatch.setattr(
            "video_transcript_api.utils.asr_monitor.time.time", lambda: clock["now"]
        )

        for _ in range(3):
            monitor._handle_check_result("CapsWriter", "ws://localhost:10001", False)
        assert len(sender.calls) == 1

        # 去抖窗口内继续失败：不发送
        clock["now"] += 600
        monitor._handle_check_result("CapsWriter", "ws://localhost:10001", False)
        assert len(sender.calls) == 1

        # 越过窗口后再次达到阈值：恢复发送
        clock["now"] += 1800
        monitor._handle_check_result("CapsWriter", "ws://localhost:10001", False)
        assert len(sender.calls) == 2
