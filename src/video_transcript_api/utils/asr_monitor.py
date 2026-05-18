"""ASR 服务监控模块

后台定时检查 CapsWriter/FunASR 服务状态，连续失败时发送企微告警，
恢复后发送恢复通知。内置防抖机制（告警后 30 分钟内不重复告警）。
"""

import asyncio
import time
import threading
from typing import Dict, Optional

import websockets

from .logging import setup_logger

logger = setup_logger("asr_monitor")

# 默认配置
DEFAULT_CHECK_INTERVAL = 300  # 5 分钟
DEFAULT_FAILURE_THRESHOLD = 3  # 连续 N 次失败触发告警
DEFAULT_DEBOUNCE_SECONDS = 1800  # 30 分钟防抖


class ASRMonitor:
    """ASR 服务监控器

    定期检查 ASR 服务状态，连续失败时发送企微告警。

    Attributes:
        services: 被监控的服务列表
        check_interval: 检查间隔（秒）
        failure_threshold: 连续失败次数阈值
    """

    def __init__(
        self,
        services: Optional[Dict[str, str]] = None,
        check_interval: int = DEFAULT_CHECK_INTERVAL,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
        notifier=None,
    ):
        """初始化 ASR 监控器

        Args:
            services: 服务名称到 WebSocket URL 的映射
            check_interval: 检查间隔（秒）
            failure_threshold: 连续失败次数阈值
            debounce_seconds: 告警防抖时间（秒）
            notifier: 通知器实例（WechatNotifier），为 None 时自动创建
        """
        self.services = services or {}
        self.check_interval = check_interval
        self.failure_threshold = failure_threshold
        self.debounce_seconds = debounce_seconds
        self.notifier = notifier

        # 每个服务的状态追踪
        self._failure_counts: Dict[str, int] = {}
        self._last_alert_time: Dict[str, float] = {}
        self._was_down: Dict[str, bool] = {}

        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """启动后台监控线程"""
        if self._running:
            logger.warning("ASR monitor is already running")
            return

        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info(
            f"ASR monitor started: interval={self.check_interval}s, "
            f"threshold={self.failure_threshold}, services={list(self.services.keys())}"
        )

    def stop(self):
        """停止监控"""
        self._running = False
        logger.info("ASR monitor stopped")

    def check_service(self, name: str, url: str, timeout: float = 5.0) -> bool:
        """检查单个服务的连通性（WebSocket 协议层握手）。

        建立一次真实的 WebSocket 握手，成功即视为健康，然后立即正常关闭。
        相比裸 TCP connect/close，本方法：
        - 把验活强度提升到协议层：覆盖 server 死锁、handler 注册失败、
          TLS 证书坏等场景（这些情况下 TCP 仍能 accept，裸 TCP 会假阳性）。
        - 不污染对端日志：对端 ``websockets`` server 见到的是一次正常握手 +
          关闭，不会抛 ``InvalidMessage: did not receive a valid HTTP request``。

        Args:
            name: 服务名称（用于日志）
            url: WebSocket URL（``ws://`` / ``wss://``）
            timeout: 握手超时（秒）

        Returns:
            bool: 服务是否可用
        """
        try:
            # ASR monitor 在独立线程（``_monitor_loop``）中运行，每次调用都创建
            # 独立的事件循环，不会与其他 loop 冲突。
            return asyncio.run(self._ws_probe(name, url, timeout))
        except Exception as e:
            # asyncio.run 自身极少抛错；这里是兜底，确保监控线程不会因为
            # 单次探测异常而中断。
            logger.info(f"service check failed for {name}: {e}")
            return False

    async def _ws_probe(self, name: str, url: str, timeout: float) -> bool:
        """执行单次 WebSocket 握手探测。

        握手成功立即返回 True 并触发正常关闭；任何异常都视为不健康并返回
        False。失败原因走 ``logger.info``（不是 debug），方便排障时直接
        看到端口为何被判定 down。
        """
        try:
            async def _go() -> bool:
                async with websockets.connect(
                    url,
                    open_timeout=timeout,
                    close_timeout=1,
                    ping_interval=None,  # 探活不需要心跳
                    max_size=None,
                ):
                    return True

            # 兜一层 wait_for，覆盖 DNS / 内核 connect 卡死等 open_timeout
            # 不一定能拦住的边角情况。
            return await asyncio.wait_for(_go(), timeout=timeout + 2)
        except Exception as e:
            logger.info(f"service check failed for {name}: {e}")
            return False

    def _monitor_loop(self):
        """监控循环（在后台线程中运行）"""
        logger.info("ASR monitor loop started")

        while self._running:
            for name, url in self.services.items():
                try:
                    is_healthy = self.check_service(name, url)
                    self._handle_check_result(name, url, is_healthy)
                except Exception as e:
                    logger.error(f"error checking service {name}: {e}")

            # 等待下一次检查
            for _ in range(self.check_interval):
                if not self._running:
                    break
                time.sleep(1)

    def _handle_check_result(self, name: str, url: str, is_healthy: bool):
        """处理检查结果，决定是否告警

        Args:
            name: 服务名称
            url: 服务 URL
            is_healthy: 是否健康
        """
        if is_healthy:
            # 服务恢复
            if self._was_down.get(name, False):
                self._send_recovery_alert(name, url)
                self._was_down[name] = False

            self._failure_counts[name] = 0
        else:
            # 服务失败
            self._failure_counts[name] = self._failure_counts.get(name, 0) + 1
            count = self._failure_counts[name]

            logger.warning(f"service {name} check failed ({count}/{self.failure_threshold})")

            if count >= self.failure_threshold:
                self._maybe_send_alert(name, url, count)
                self._was_down[name] = True

    def _get_local_time_str(self) -> str:
        """获取配置时区的当前时间字符串

        Returns:
            格式化的本地时间字符串 (YYYY-MM-DD HH:MM:SS)
        """
        try:
            from .timeutil.timezone_helper import get_configured_timezone
            import datetime
            tz = get_configured_timezone()
            return datetime.datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return time.strftime('%Y-%m-%d %H:%M:%S')

    def _maybe_send_alert(self, name: str, url: str, failure_count: int):
        """发送告警（带防抖）

        Args:
            name: 服务名称
            url: 服务 URL
            failure_count: 连续失败次数
        """
        now = time.time()
        last_alert = self._last_alert_time.get(name, 0)

        if now - last_alert < self.debounce_seconds:
            logger.debug(f"alert debounced for {name} (last alert {now - last_alert:.0f}s ago)")
            return

        self._last_alert_time[name] = now

        message = (
            f"**ASR 服务宕机告警**\n\n"
            f"- 服务: {name}\n"
            f"- 地址: {url}\n"
            f"- 连续失败: {failure_count} 次\n"
            f"- 时间: {self._get_local_time_str()}\n\n"
            f"请检查服务状态。"
        )

        self._send_notification(message)
        logger.error(f"ASR alert sent for {name}: {failure_count} consecutive failures")

    def _send_recovery_alert(self, name: str, url: str):
        """发送恢复通知

        Args:
            name: 服务名称
            url: 服务 URL
        """
        message = (
            f"**ASR 服务恢复通知**\n\n"
            f"- 服务: {name}\n"
            f"- 地址: {url}\n"
            f"- 时间: {self._get_local_time_str()}\n\n"
            f"服务已恢复正常。"
        )

        self._send_notification(message)
        logger.info(f"ASR recovery alert sent for {name}")

    def _send_notification(self, message: str):
        """Send alert notification via all configured channels.

        Args:
            message: notification content
        """
        try:
            if self.notifier is None:
                from .notifications import get_notification_router
                self.notifier = get_notification_router()

            if hasattr(self.notifier, 'send_text') and callable(self.notifier.send_text):
                self.notifier.send_text(message)
            else:
                logger.warning("notifier does not support send_text")
        except Exception as e:
            logger.error(f"failed to send ASR alert notification: {e}")


def start_asr_monitor(config: dict) -> Optional[ASRMonitor]:
    """从配置启动 ASR 监控器

    Args:
        config: 应用配置字典

    Returns:
        ASRMonitor: 监控器实例，配置不存在时返回 None
    """
    services = {}

    capswriter_url = config.get("capswriter", {}).get("server_url")
    if capswriter_url:
        services["CapsWriter"] = capswriter_url

    funasr_url = config.get("funasr_spk_server", {}).get("server_url")
    if funasr_url:
        services["FunASR"] = funasr_url

    if not services:
        logger.info("no ASR services configured, monitor not started")
        return None

    monitor = ASRMonitor(services=services)
    monitor.start()
    return monitor
