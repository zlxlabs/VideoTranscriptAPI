"""
Multi-channel notification abstraction.

Defines NotificationChannel protocol and concrete implementations
for WeChat Work (WeComChannel) and Feishu/Lark (FeishuChannel).
"""

import datetime
import re
from typing import Optional, Protocol, runtime_checkable

from wecom_notifier import FeishuNotifier

from ..logging import setup_logger, load_config

logger = setup_logger("notification_channel")

# Global FeishuNotifier singleton
_global_feishu_notifier: Optional[FeishuNotifier] = None


def init_global_feishu_notifier():
    """Initialize global FeishuNotifier singleton."""
    global _global_feishu_notifier
    if _global_feishu_notifier is None:
        config = load_config()
        secret = config.get("feishu", {}).get("secret")
        _global_feishu_notifier = FeishuNotifier(secret=secret)
        logger.info("Global FeishuNotifier initialized")


def shutdown_global_feishu_notifier():
    """Shutdown global FeishuNotifier singleton."""
    global _global_feishu_notifier
    if _global_feishu_notifier is not None:
        _global_feishu_notifier.stop_all()
        _global_feishu_notifier = None
        logger.info("Global FeishuNotifier shut down")


def _get_global_feishu_notifier() -> FeishuNotifier:
    """Get or lazily initialize the global FeishuNotifier."""
    global _global_feishu_notifier
    if _global_feishu_notifier is None:
        logger.warning("Global FeishuNotifier not initialized, auto-initializing")
        init_global_feishu_notifier()
    return _global_feishu_notifier


@runtime_checkable
class NotificationChannel(Protocol):
    """Protocol for notification channels."""

    @property
    def name(self) -> str: ...

    @property
    def is_enabled(self) -> bool: ...

    def send_text(self, content: str, webhook: str = None) -> bool: ...

    def send_rich(self, content: str, webhook: str = None, **kwargs) -> bool: ...

    def notify_task_status(self, url: str, status: str, **kwargs) -> bool: ...


def _clean_url(url: str) -> str:
    """Clean tracking parameters from URL (shared across channels)."""
    if "xiaohongshu.com" in url or "xhslink.com" in url or "xhslink.cn" in url:
        if "?" in url:
            base, query = url.split("?", 1)
            kept = [p for p in query.split("&") if p.startswith("xsec_token=")]
            return f"{base}?{'&'.join(kept)}" if kept else base
        return url
    elif "youtube.com" in url or "youtu.be" in url:
        if "?" in url:
            base, query = url.split("?", 1)
            kept = [p for p in query.split("&") if p.startswith("v=")]
            return f"{base}?{'&'.join(kept)}" if kept else base
        return url
    else:
        return url.split("?")[0] if "?" in url else url


def _get_status_emoji(status: str, error: str = None) -> str:
    """Get emoji for task status (shared across channels)."""
    if error or "失败" in status or "异常" in status or "错误" in status:
        return "❌"
    if "开始" in status or "处理" in status:
        return "🔄"
    if "下载" in status:
        if "正在下载" in status:
            return "⬇️"
        if "下载完成" in status or "下载成功" in status:
            return "✅"
        return "📥"
    if "转录" in status:
        if "正在转录" in status:
            return "🎤"
        if "转录完成" in status or "转录成功" in status:
            return "✅"
        return "📝"
    if "完成" in status or "成功" in status:
        return "✅"
    if "等待" in status or "队列" in status:
        return "⏳"
    if "缓存" in status:
        return "💾"
    if "平台字幕" in status:
        return "📄"
    return "🔄"


def _get_risk_control():
    """Get risk control module (shared across channels)."""
    try:
        from video_transcript_api.risk_control import is_enabled, sanitize_text

        class RiskControlWrapper:
            @staticmethod
            def is_enabled():
                return is_enabled()

            @staticmethod
            def sanitize_text(text, text_type="general"):
                return sanitize_text(text, text_type)

        return RiskControlWrapper()
    except ImportError:
        return None


def _protect_urls(content: str) -> tuple:
    """Extract and protect URLs from risk control processing."""
    if not content:
        return content, {}
    url_pattern = r'https?://[^\s一-鿿]+'
    urls = re.findall(url_pattern, content)
    if not urls:
        return content, {}
    url_map = {}
    protected = content
    for i, url in enumerate(urls):
        placeholder = f"__URL_PLACEHOLDER_{i}__"
        url_map[placeholder] = url
        protected = protected.replace(url, placeholder, 1)
    return protected, url_map


def _restore_urls(content: str, url_map: dict) -> str:
    """Restore protected URLs."""
    if not url_map:
        return content
    for placeholder, url in url_map.items():
        content = content.replace(placeholder, url)
    return content


def _apply_risk_control_safe(content: str, text_type: str = "general") -> str:
    """Apply risk control with URL protection (shared across channels)."""
    if not content or not content.strip():
        return content
    protected_content, url_map = _protect_urls(content)
    rc = _get_risk_control()
    if rc and rc.is_enabled():
        try:
            result = rc.sanitize_text(protected_content, text_type=text_type)
            sanitized = result["sanitized_text"]
        except Exception:
            sanitized = protected_content
    else:
        sanitized = protected_content
    return _restore_urls(sanitized, url_map)


def _default_task_title(url: str) -> str:
    """Return the established platform title, or the cleaned URL."""
    for domain, title in (
        (("youtube.com", "youtu.be"), "YouTube视频转录"),
        (("bilibili.com", "b23.tv"), "Bilibili视频转录"),
        (("xiaoyuzhoufm.com",), "小宇宙播客转录"),
        (("podcasts.apple.com",), "Apple播客转录"),
        (("xiaohongshu.com", "xhslink.com", "xhslink.cn"), "小红书内容转录"),
        (("douyin.com",), "抖音视频转录"),
        (("weixin.qq.com",), "视频号视频转录"),
        (("twitter.com", "x.com"), "X视频转录"),
    ):
        if any(host in (url or "") for host in domain):
            return title
    return _clean_url(url or "")


def build_task_notification_heading(
    task_id: str, title: str = None, url: str = "", icon: str = "📥",
) -> str:
    """Build the shared task-notification heading for both channels."""
    short_id = (task_id or "000000").removeprefix("task_")[:6]
    display_title = title or _default_task_title(url)
    display_title = _apply_risk_control_safe(display_title, text_type="title")
    return f"{icon} [#{short_id}] {display_title}"


def build_task_status_content(
    url: str,
    status: str,
    error: str = None,
    title: str = None,
    author: str = None,
    transcript: str = None,
    view_url: str = None,
    task_id: str = None,
    completion_body: str = None,
) -> str:
    """Build task status notification content (shared across channels)."""
    clean = _clean_url(url)
    emoji = _get_status_emoji(status, error)
    if "已接收" in status:
        emoji = "📥"

    heading = build_task_notification_heading(task_id, title, url, emoji)
    if author:
        author = _apply_risk_control_safe(author, text_type="author")

    content = f"{heading}\n\n{clean}\n\n**状态：** {status}"
    if author:
        content += f"\n\n**作者：** {author}"
    if error:
        content += f"\n\n**错误：** {error}"
    if transcript and "转录完成" in status:
        preview = transcript[:100] + ("..." if len(transcript) > 100 else "")
        content += f"\n\n**转录预览：**\n```\n{preview}\n```"
    if view_url and not completion_body:
        content += f"\n\n🔗 查看：{view_url}"
    # 本层不截断 completion_body：企业微信由 wecom_notifier 库按
    # MAX_BYTES_PER_MESSAGE=3800 分段；飞书由 FeishuChannel.send_rich 按
    # FEISHU_CARD_MAX_BYTES 拆成多张卡片（库对 interactive 类型不分段）。
    # 旧的 send_long_text 内部也只是调用 send_rich，分段能力一直在库里。
    if completion_body:
        safe_body = _apply_risk_control_safe(completion_body, text_type="summary")
        content += f"\n\n{safe_body}"
    return content


# Feishu interactive cards get no library-side segmentation, so this layer
# enforces its own UTF-8 byte budget with headroom below the library's
# 19000-byte and Feishu's 30KB limits.
FEISHU_CARD_MAX_BYTES = 18000


def _split_feishu_card_content(content: str, max_bytes: int = FEISHU_CARD_MAX_BYTES) -> list:
    """Split content into chunks each within the UTF-8 byte budget.

    Cuts at blank lines first with greedy packing; a paragraph that alone
    exceeds the budget is hard-cut at UTF-8 character boundaries.
    Joining the returned chunks reproduces the original text exactly.
    """
    if len(content.encode("utf-8")) <= max_bytes:
        return [content]

    def byte_len(text: str) -> int:
        return len(text.encode("utf-8"))

    def hard_cut(text: str) -> list:
        pieces = []
        start = 0
        while start < len(text):
            lo, hi = start + 1, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if byte_len(text[start:mid]) <= max_bytes:
                    lo = mid
                else:
                    hi = mid - 1
            pieces.append(text[start:lo])
            start = lo
        return pieces

    chunks = []
    current = ""
    for i, paragraph in enumerate(content.split("\n\n")):
        piece = paragraph if i == 0 else "\n\n" + paragraph
        if byte_len(current + piece) <= max_bytes:
            current += piece
            continue
        if current:
            chunks.append(current)
            current = ""
        if byte_len(piece) <= max_bytes:
            current = piece
        else:
            pieces = hard_cut(piece)
            chunks.extend(pieces[:-1])
            current = pieces[-1]
    if current:
        chunks.append(current)
    return chunks


class FeishuChannel:
    """Feishu notification channel using FeishuNotifier from wecom-notifier."""

    def __init__(self, webhook: str = None, secret: str = None):
        config = load_config()
        feishu_config = config.get("feishu", {})
        self.webhook = webhook or feishu_config.get("webhook")
        self.notifier = _get_global_feishu_notifier()

        if not self.webhook:
            logger.debug("Feishu webhook not configured, channel disabled")
        else:
            logger.debug("FeishuChannel initialized")

    @property
    def name(self) -> str:
        return "feishu"

    @property
    def is_enabled(self) -> bool:
        return bool(self.webhook)

    def send_text(self, content: str, webhook: str = None) -> bool:
        """Send plain text via Feishu webhook bot."""
        target = webhook or self.webhook
        if not target:
            logger.warning("[feishu] No webhook configured, skipping send_text")
            return False
        if not content or not content.strip():
            logger.warning("[feishu] Empty content, skipping send_text")
            return False
        try:
            self.notifier.send_text(
                webhook_url=target,
                content=content,
                async_send=True,
            )
            logger.debug(f"[feishu] Text submitted: {content[:50]}...")
            return True
        except Exception as e:
            logger.exception(f"[feishu] send_text failed: {e}")
            return False

    def send_rich(self, content: str, webhook: str = None, title: str = "通知", **kwargs) -> bool:
        """Send rich content as Feishu card (accepts markdown).

        Content over FEISHU_CARD_MAX_BYTES is split into multiple cards;
        chunk i (i >= 2) gets the title suffix " (i/n)". The heading line
        always stays on the first card. Every chunk must be submitted
        successfully for this to return True.
        """
        target = webhook or self.webhook
        if not target:
            logger.warning("[feishu] No webhook configured, skipping send_rich")
            return False
        if not content or not content.strip():
            logger.warning("[feishu] Empty content, skipping send_rich")
            return False
        try:
            cards = _split_feishu_card_content(content)
            for i, card_content in enumerate(cards, start=1):
                card_title = title if i == 1 else f"{title} ({i}/{len(cards)})"
                self.notifier.send_card(
                    webhook_url=target,
                    content=card_content,
                    title=card_title,
                    async_send=True,
                )
            logger.debug(f"[feishu] Cards submitted ({len(cards)}): {content[:50]}...")
            return True
        except Exception as e:
            logger.exception(f"[feishu] send_rich failed: {e}")
            return False

    def notify_task_status(
        self,
        url: str,
        status: str,
        error: str = None,
        title: str = None,
        author: str = None,
        transcript: str = None,
        webhook: str = None,
        view_url: str = None,
        task_id: str = None,
        completion_body: str = None,
    ) -> bool:
        """Send task status notification via Feishu card."""
        content = build_task_status_content(
            url, status, error, title, author, transcript,
            view_url=view_url, task_id=task_id, completion_body=completion_body,
        )
        heading = content.splitlines()[0]
        return self.send_rich(content, webhook=webhook, title=heading)


class WeComChannel:
    """WeChat Work notification channel — wraps existing WechatNotifier."""

    def __init__(self, webhook: str = None):
        from .wechat import WechatNotifier
        self._notifier = WechatNotifier(webhook)

    @property
    def name(self) -> str:
        return "wechat"

    @property
    def is_enabled(self) -> bool:
        return bool(self._notifier.webhook)

    def send_text(self, content: str, webhook: str = None, **kwargs) -> bool:
        """Send text via WeCom (delegates to WechatNotifier)."""
        if webhook and webhook != self._notifier.webhook:
            from .wechat import WechatNotifier
            notifier = WechatNotifier(webhook)
            return notifier.send_text(content)
        return self._notifier.send_text(content)

    def send_rich(self, content: str, webhook: str = None, **kwargs) -> bool:
        """Send rich content via WeCom markdown (delegates to WechatNotifier)."""
        if webhook and webhook != self._notifier.webhook:
            from .wechat import WechatNotifier
            notifier = WechatNotifier(webhook)
            return notifier.send_markdown_v2(content, skip_risk_control=True)
        return self._notifier.send_markdown_v2(content, skip_risk_control=True)

    def notify_task_status(
        self,
        url: str,
        status: str,
        error: str = None,
        title: str = None,
        author: str = None,
        transcript: str = None,
        webhook: str = None,
        view_url: str = None,
        task_id: str = None,
        completion_body: str = None,
    ) -> bool:
        """Send task status notification via WeCom."""
        if webhook and webhook != self._notifier.webhook:
            from .wechat import WechatNotifier
            notifier = WechatNotifier(webhook)
            return notifier.notify_task_status(
                url, status, error, title, author, transcript,
                view_url=view_url, task_id=task_id, completion_body=completion_body,
            )
        return self._notifier.notify_task_status(
            url, status, error, title, author, transcript,
            view_url=view_url, task_id=task_id, completion_body=completion_body,
        )
