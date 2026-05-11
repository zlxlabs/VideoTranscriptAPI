from .wechat import (
    WechatNotifier,
    wechat_notify,
    send_long_text_wechat,
    send_markdown_wechat,
    send_view_link_wechat,
    send_wechat_notification,
    init_global_notifier,
    shutdown_global_notifier,
    format_llm_config_markdown,
)
from .channel import (
    NotificationChannel,
    FeishuChannel,
    WeComChannel,
    init_global_feishu_notifier,
    shutdown_global_feishu_notifier,
)
from .router import NotificationRouter

# Global router singleton
_global_router = None


def init_all_notifiers():
    """Initialize all notification subsystems (call at app startup)."""
    global _global_router
    init_global_notifier()
    init_global_feishu_notifier()
    _global_router = NotificationRouter()


def shutdown_all_notifiers():
    """Shutdown all notification subsystems (call at app shutdown)."""
    global _global_router
    _global_router = None
    shutdown_global_notifier()
    shutdown_global_feishu_notifier()


def get_notification_router() -> NotificationRouter:
    """Get the global notification router (lazy init if needed)."""
    global _global_router
    if _global_router is None:
        init_all_notifiers()
    return _global_router


__all__ = [
    # Legacy WeCom-specific (backward compatible)
    "WechatNotifier",
    "wechat_notify",
    "send_long_text_wechat",
    "send_markdown_wechat",
    "send_view_link_wechat",
    "send_wechat_notification",
    "format_llm_config_markdown",
    # Lifecycle
    "init_global_notifier",
    "shutdown_global_notifier",
    "init_all_notifiers",
    "shutdown_all_notifiers",
    "get_notification_router",
    # Multi-channel
    "NotificationChannel",
    "FeishuChannel",
    "WeComChannel",
    "NotificationRouter",
]
