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

4. ``asr_monitor`` 保持延迟导入 ``get_notification_router``（本文件的 patch
   目标依赖这一点，模块级绑定会让 patch 静默失效）。

全程零真实网络请求：底层企微 sender 被换成记录器；另有 autouse fixture 在
socket 层**阻断**指向非 loopback 的连接（``socket.socket.connect`` 与
``socket.getaddrinfo`` 均直接抛异常），保证「零外网」不只停留在事后断言。
"""

import inspect
import ipaddress
import socket
from unittest.mock import patch

import pytest

import video_transcript_api.utils.asr_monitor as asr_monitor_module
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


def _is_loopback(host) -> bool:
    """判断出站目标是否属于 loopback（字面 IP 或 localhost）。"""
    if isinstance(host, tuple) and host:
        host = host[0]
    host = str(host)
    if host in ("localhost", "", "0.0.0.0", "::"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NetworkGuard:
    """出站网络守卫：记录阻断事件，并允许测试显式声明预期阻断次数。"""

    def __init__(self):
        self.blocked = []
        self.allowed = []
        self.expected_blocks = 0

    def expect_blocks(self, count: int) -> None:
        self.expected_blocks += count

    def connected_to(self, host: str) -> bool:
        """记录里是否存在指向 ``host`` 的 connect（无论来自哪个线程）。

        守卫是进程级的 monkeypatch，``allowed``/``blocked`` 里混有本进程
        其它用例的流量（例如 ``test_asr_monitor_ws_probe.py`` 的 monitor
        线程连本地临时 WS server 时留下的 loopback 连接）。因此查询必须
        按目标限定，不要对整个列表做相等断言。
        """
        return f"connect:{host}" in self.allowed


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    """在 socket 层阻断一切非 loopback 出站（DNS 解析与 connect 都拦）。

    yield ``_NetworkGuard``；测试若故意触发出站（如锁死阻断行为的那条），
    需用 ``guard.expect_blocks(n)`` 登记预期，否则 teardown 判红。
    """
    guard = _NetworkGuard()
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def fake_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else address
        if not _is_loopback(host):
            guard.blocked.append(f"connect:{host}")
            raise AssertionError(f"outbound connect blocked by test guard: {host}")
        guard.allowed.append(f"connect:{host}")
        return real_connect(self, address, *args, **kwargs)

    def fake_getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            guard.blocked.append(f"getaddrinfo:{host}")
            raise AssertionError(f"outbound DNS blocked by test guard: {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", fake_connect)
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    try:
        yield guard
    finally:
        monkeypatch.undo()
        assert len(guard.blocked) == guard.expected_blocks, (
            f"unexpected outbound attempts: {guard.blocked} "
            f"(expected {guard.expected_blocks})"
        )


class TestNetworkGuard:
    """守卫本身的行为：非 loopback 出站必须在 socket 层被阻断。"""

    def test_dns_to_non_loopback_host_is_blocked(self, no_outbound_network):
        guard = no_outbound_network
        guard.expect_blocks(1)
        with pytest.raises(AssertionError, match="outbound DNS blocked"):
            socket.getaddrinfo("qyapi.weixin.qq.com", 443)
        assert guard.blocked == ["getaddrinfo:qyapi.weixin.qq.com"]

    def test_connect_to_non_loopback_ip_is_blocked_before_socket_connects(
        self, no_outbound_network
    ):
        guard = no_outbound_network
        guard.expect_blocks(1)

        with pytest.raises(AssertionError, match="outbound connect blocked"):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(("183.47.100.66", 80))
        assert guard.blocked == ["connect:183.47.100.66"]
        # 守卫在真实 connect 之前抛错：这个非 loopback 目标没有被放过。
        # 按目标限定，不对 allowed 做整体相等断言（守卫是进程级的）。
        assert guard.connected_to("183.47.100.66") is False

        # 反向对照（已知会走到真实 connect 的输入）：loopback 放行，
        # 报的是内核层 ECONNREFUSED 而不是守卫的 AssertionError，
        # 证明上面那条确实是被守卫拦下、而不是别的原因失败。
        with pytest.raises(OSError) as excinfo:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(("127.0.0.1", 1))
        assert "outbound connect blocked" not in str(excinfo.value)
        assert guard.connected_to("127.0.0.1") is True


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


class TestPatchTargetReliability:
    """本文件的 patch 目标依赖 asr_monitor 的延迟导入。

    ``_send_notification`` 在函数体内 ``from .notifications import
    get_notification_router``，所以 patch
    ``video_transcript_api.utils.notifications.get_notification_router``
    能生效。若有人改成模块级绑定，本文件的 patch 会静默失效退化为假绿，
    这条测试负责把它变红。
    """

    def test_no_module_level_get_notification_router_binding(self):
        assert "get_notification_router" not in vars(asr_monitor_module)

    def test_delayed_import_is_what_makes_the_patch_effective(self):
        # 延迟导入的函数源码里能看到实际取 router 的那一行
        src = inspect.getsource(ASRMonitor._send_notification)
        assert "from .notifications import get_notification_router" in src
        assert "self.notifier.send_text(message)" in src


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
