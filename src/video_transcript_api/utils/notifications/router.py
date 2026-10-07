"""
Notification router — dispatches notifications to configured channels.

Supports multi-channel parallel dispatch, per-request channel selection,
and fallback on failure.
"""

from typing import Dict, List, Optional

from ..logging import setup_logger, load_config
from .channel import FeishuChannel, WeComChannel, NotificationChannel

logger = setup_logger("notification_router")


class NotificationRouter:
    """Routes notifications to configured channels with fallback support."""

    def __init__(self):
        self.channels: List[NotificationChannel] = []
        self._init_channels_from_config()

    def _init_channels_from_config(self):
        """Detect and initialize channels based on config."""
        config = load_config()

        if config.get("wechat", {}).get("webhook"):
            try:
                ch = WeComChannel()
                self.channels.append(ch)
                logger.info("[router] WeChat channel initialized")
            except Exception as e:
                logger.error(f"[router] Failed to init WeChat channel: {e}")

        if config.get("feishu", {}).get("webhook"):
            try:
                ch = FeishuChannel()
                self.channels.append(ch)
                logger.info("[router] Feishu channel initialized")
            except Exception as e:
                logger.error(f"[router] Failed to init Feishu channel: {e}")

        if not self.channels:
            logger.warning("[router] No notification channels configured")
        else:
            names = [ch.name for ch in self.channels]
            logger.info(f"[router] Active channels: {names}")

    @property
    def is_enabled(self) -> bool:
        return len(self.channels) > 0

    def _resolve_targets(
        self, channel_name: Optional[str] = None
    ) -> List[NotificationChannel]:
        """Resolve which channels to send to."""
        if channel_name:
            return [ch for ch in self.channels if ch.name == channel_name]
        return list(self.channels)

    def _get_fallback_channels(
        self, excluded: List[NotificationChannel]
    ) -> List[NotificationChannel]:
        """Get channels not in the excluded list (for fallback)."""
        excluded_names = {ch.name for ch in excluded}
        return [ch for ch in self.channels if ch.name not in excluded_names]

    def _resolve_webhook(
        self, ch_name: str, webhooks: Optional[Dict[str, str]] = None, webhook: str = None,
    ) -> Optional[str]:
        """Resolve webhook for a specific channel: webhooks dict > single webhook > None."""
        if webhooks:
            return webhooks.get(ch_name)
        return webhook

    def send_text(
        self,
        content: str,
        channel_name: str = None,
        webhook: str = None,
        webhooks: Dict[str, str] = None,
    ) -> Dict[str, bool]:
        """Send text to target channels with fallback."""
        targets = self._resolve_targets(channel_name)
        results = {}

        for ch in targets:
            try:
                ch_webhook = self._resolve_webhook(ch.name, webhooks, webhook)
                results[ch.name] = ch.send_text(content, webhook=ch_webhook)
                logger.debug(f"[router] [{ch.name}] send_text: {results[ch.name]}")
            except Exception as e:
                logger.error(f"[router] [{ch.name}] send_text exception: {e}")
                results[ch.name] = False

        # Fallback: if targeted channel(s) were found but all failed, try others
        if channel_name and targets and not any(results.values()):
            fallbacks = self._get_fallback_channels(targets)
            for ch in fallbacks:
                try:
                    ch_webhook = self._resolve_webhook(ch.name, webhooks)
                    ok = ch.send_text(content, webhook=ch_webhook)
                    results[f"{ch.name}_fallback"] = ok
                    if ok:
                        logger.info(f"[router] Fallback to {ch.name} succeeded")
                        break
                except Exception as e:
                    logger.error(f"[router] Fallback [{ch.name}] failed: {e}")
                    results[f"{ch.name}_fallback"] = False

        return results

    def send_rich(
        self,
        content: str,
        channel_name: str = None,
        webhook: str = None,
        webhooks: Dict[str, str] = None,
        **kwargs,
    ) -> Dict[str, bool]:
        """Send rich content to target channels with fallback."""
        targets = self._resolve_targets(channel_name)
        results = {}

        for ch in targets:
            try:
                ch_webhook = self._resolve_webhook(ch.name, webhooks, webhook)
                results[ch.name] = ch.send_rich(content, webhook=ch_webhook, **kwargs)
            except Exception as e:
                logger.error(f"[router] [{ch.name}] send_rich exception: {e}")
                results[ch.name] = False

        if channel_name and targets and not any(results.values()):
            for ch in self._get_fallback_channels(targets):
                try:
                    ch_webhook = self._resolve_webhook(ch.name, webhooks)
                    ok = ch.send_rich(content, webhook=ch_webhook, **kwargs)
                    results[f"{ch.name}_fallback"] = ok
                    if ok:
                        logger.info(f"[router] Fallback to {ch.name} succeeded")
                        break
                except Exception as e:
                    results[f"{ch.name}_fallback"] = False

        return results

    def notify_task_status(
        self,
        url: str,
        status: str,
        channel_name: str = None,
        webhook: str = None,
        webhooks: Dict[str, str] = None,
        **kwargs,
    ) -> Dict[str, bool]:
        """Send task status notification to target channels with fallback."""
        targets = self._resolve_targets(channel_name)
        results = {}

        for ch in targets:
            try:
                ch_webhook = self._resolve_webhook(ch.name, webhooks, webhook)
                results[ch.name] = ch.notify_task_status(
                    url=url, status=status, webhook=ch_webhook, **kwargs
                )
            except Exception as e:
                logger.error(f"[router] [{ch.name}] notify_task_status exception: {e}")
                results[ch.name] = False

        if channel_name and targets and not any(results.values()):
            for ch in self._get_fallback_channels(targets):
                try:
                    ch_webhook = self._resolve_webhook(ch.name, webhooks)
                    ok = ch.notify_task_status(
                        url=url, status=status, webhook=ch_webhook, **kwargs
                    )
                    results[f"{ch.name}_fallback"] = ok
                    if ok:
                        logger.info(f"[router] Fallback to {ch.name} succeeded")
                        break
                except Exception as e:
                    results[f"{ch.name}_fallback"] = False

        return results

    def send_long_text(
        self,
        title: str,
        url: str,
        text: str,
        channel_name: str = None,
        webhook: str = None,
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
            webhook=webhook,
            webhooks=webhooks,
            title=safe_title or "转录结果",
        )

    def send_view_link(
        self,
        title: str,
        view_token: str,
        channel_name: str = None,
        webhook: str = None,
        webhooks: Dict[str, str] = None,
        original_url: str = None,
        task_id: str = None,
    ) -> Dict[str, bool]:
        """Send the task-accepted message with the shared task heading."""
        from .channel import _clean_url, build_task_notification_heading
        from ..rendering import get_base_url

        base_url = get_base_url()
        view_url = f"{base_url}/view/{view_token}"
        heading = build_task_notification_heading(
            task_id, title, original_url or "", icon="📥",
        )

        if original_url:
            message = (
                f"{heading}\n\n{_clean_url(original_url)}"
                f"\n\n🔗 点击查看转录进度和结果：\n{view_url}"
            )
        else:
            message = f"{heading}\n\n🔗 点击查看转录进度和结果：\n{view_url}"

        return self.send_rich(
            message,
            channel_name=channel_name,
            webhook=webhook,
            webhooks=webhooks,
            title=heading,
        )
