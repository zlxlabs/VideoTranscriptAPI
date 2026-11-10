"""
Notification router — dispatches notifications to declared channels.

Tenant notifications go ONLY to the channels with an address in the task's
declared ``webhooks`` dict; an unconfigured channel is skipped silently, and a
task with zero declared targets is logged once as NOTIFY_TARGET_MISSING
(C1 stop-leak, docs/sessions/notify-redesign-261110/design.md). Ops alerts
(ASR monitor) go through :meth:`send_ops_alert`, which explicitly targets the
global group webhooks.
"""

from typing import Dict, List, Optional

from ..logging import setup_logger, load_config
from .channel import FeishuChannel, WeComChannel, NotificationChannel

logger = setup_logger("notification_router")


class NotificationRouter:
    """Routes notifications: tenant methods to declared targets, ops alerts to
    the global group webhooks."""

    def __init__(self):
        self.channels: List[NotificationChannel] = []
        self._init_channels_from_config()

    def _init_channels_from_config(self):
        """Initialize every channel object regardless of global webhook config.

        Channel enablement for tenant sends is decided per task by the
        declared ``webhooks`` dict, not by the global config; the global
        webhook only feeds the ops-alert path (``send_ops_alert``).
        """
        config = load_config()

        for factory, name in ((WeComChannel, "wechat"), (FeishuChannel, "feishu")):
            try:
                ch = factory()
                self.channels.append(ch)
                logger.info(f"[router] {name} channel initialized")
            except Exception as e:
                logger.error(f"[router] Failed to init {name} channel: {e}")

        if not self.channels:
            logger.warning("[router] No notification channels available")
        else:
            names = [ch.name for ch in self.channels]
            logger.info(f"[router] Active channels: {names}")

    @property
    def is_enabled(self) -> bool:
        return len(self.channels) > 0

    def _tenant_targets(
        self,
        webhooks: Optional[Dict[str, str]] = None,
        channel_name: Optional[str] = None,
        *,
        method_name: str,
        task_id: str = None,
    ) -> List[NotificationChannel]:
        """Channels this tenant call may use: those with a declared address.

        A channel without an address in ``webhooks`` is skipped silently. When
        NO declared channel has an address (zero-target task), one
        NOTIFY_TARGET_MISSING line is logged and the caller gets no targets.
        """
        declared = webhooks or {}
        targets = [
            ch for ch in self.channels
            if declared.get(ch.name) and (channel_name is None or ch.name == channel_name)
        ]
        if not targets:
            details = f"method={method_name}"
            if task_id is not None:
                details += f" task_id={task_id}"
            logger.warning(f"NOTIFY_TARGET_MISSING {details}")
        return targets

    def send_ops_alert(self, message: str) -> Dict[str, bool]:
        """Send an ops alert (ASR monitor, service health) to the global group
        webhooks explicitly. This is the only path allowed to use the global
        channel-level webhooks; tenant notifications must never land here."""
        results = {}
        for ch in self.channels:
            if not ch.is_enabled:
                continue
            try:
                results[ch.name] = ch.send_text(message, webhook=ch.webhook)
            except Exception as e:
                logger.error(f"[router] [{ch.name}] send_ops_alert exception: {e}")
                results[ch.name] = False
        return results

    def send_text(
        self,
        content: str,
        channel_name: str = None,
        webhooks: Dict[str, str] = None,
    ) -> Dict[str, bool]:
        """Send text only to channels declared in ``webhooks``."""
        targets = self._tenant_targets(
            webhooks, channel_name, method_name="send_text",
        )
        results = {}

        for ch in targets:
            try:
                results[ch.name] = ch.send_text(content, webhook=webhooks[ch.name])
                logger.debug(f"[router] [{ch.name}] send_text: {results[ch.name]}")
            except Exception as e:
                logger.error(f"[router] [{ch.name}] send_text exception: {e}")
                results[ch.name] = False

        return results

    def send_rich(
        self,
        content: str,
        channel_name: str = None,
        webhooks: Dict[str, str] = None,
        _router_method: str = "send_rich",
        _task_id: str = None,
        **kwargs,
    ) -> Dict[str, bool]:
        """Send rich content only to channels declared in ``webhooks``."""
        task_id = _task_id if _task_id is not None else kwargs.get("task_id")
        targets = self._tenant_targets(
            webhooks, channel_name, method_name=_router_method, task_id=task_id,
        )
        results = {}

        for ch in targets:
            try:
                results[ch.name] = ch.send_rich(
                    content, webhook=webhooks[ch.name], **kwargs
                )
            except Exception as e:
                logger.error(f"[router] [{ch.name}] {_router_method} exception: {e}")
                results[ch.name] = False

        return results

    def notify_task_status(
        self,
        url: str,
        status: str,
        channel_name: str = None,
        webhooks: Dict[str, str] = None,
        **kwargs,
    ) -> Dict[str, bool]:
        """Send task status only to channels declared in ``webhooks``."""
        targets = self._tenant_targets(
            webhooks,
            channel_name,
            method_name="notify_task_status",
            task_id=kwargs.get("task_id"),
        )
        results = {}

        for ch in targets:
            try:
                results[ch.name] = ch.notify_task_status(
                    url=url, status=status, webhook=webhooks[ch.name], **kwargs
                )
            except Exception as e:
                logger.error(f"[router] [{ch.name}] notify_task_status exception: {e}")
                results[ch.name] = False

        return results

    def send_long_text(
        self,
        title: str,
        url: str,
        text: str,
        channel_name: str = None,
        webhooks: Dict[str, str] = None,
        is_summary: bool = False,
        has_speaker_recognition: bool = False,
        skip_content_type_header: bool = False,
    ) -> Dict[str, bool]:
        """Send long text to target channels (uses send_rich internally)."""
        from .channel import _clean_url, _apply_risk_control_safe

        if not text or not text.strip():
            return {}

        clean = _clean_url(url)
        safe_title = _apply_risk_control_safe(title, text_type="title") if title else ""
        text_type = "summary" if is_summary else "general"
        safe_text = _apply_risk_control_safe(text, text_type=text_type)

        if skip_content_type_header:
            message = f"## {safe_title}\n\n{clean}\n\n{safe_text}\n"
        else:
            content_type = '**总结文本**' if is_summary else '**校对文本**'
            speaker_info = '（含说话人识别）' if has_speaker_recognition else ''
            message = f"## {safe_title}\n\n{clean}\n\n{content_type}{speaker_info}\n\n{safe_text}\n"

        return self.send_rich(
            message,
            channel_name=channel_name,
            webhooks=webhooks,
            title=safe_title or "转录结果",
        )

    def send_view_link(
        self,
        title: str,
        view_token: str,
        channel_name: str = None,
        webhooks: Dict[str, str] = None,
        original_url: str = None,
        task_id: str = None,
        source_label: str = None,
    ) -> Dict[str, bool]:
        """Send the task-accepted message with the shared task heading."""
        from .channel import (
            _apply_risk_control_safe,
            _clean_url,
            build_task_notification_heading,
        )
        from ..rendering import get_base_url

        base_url = get_base_url()
        view_url = f"{base_url}/view/{view_token}"
        heading = build_task_notification_heading(
            task_id, title, original_url or "", icon="📥",
        )

        if source_label:
            safe_label = _apply_risk_control_safe(source_label, text_type="general")
            source_lines = [f"来源：{safe_label}"]
            if original_url:
                source_lines.append(f"原始地址：{_clean_url(original_url)}")
            message = (
                f"{heading}\n\n" + "\n".join(source_lines)
                + f"\n\n🔗 点击查看转录进度和结果：\n{view_url}"
            )
        elif original_url:
            message = (
                f"{heading}\n\n{_clean_url(original_url)}"
                f"\n\n🔗 点击查看转录进度和结果：\n{view_url}"
            )
        else:
            message = f"{heading}\n\n🔗 点击查看转录进度和结果：\n{view_url}"

        return self.send_rich(
            message,
            channel_name=channel_name,
            webhooks=webhooks,
            title=heading,
            _router_method="send_view_link",
            _task_id=task_id,
        )
