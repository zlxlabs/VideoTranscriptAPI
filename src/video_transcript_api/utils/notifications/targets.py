"""Resolve task notification destinations at submission time."""

_CHANNEL_FIELDS = {
    "wechat": "wechat_webhook",
    "feishu": "feishu_webhook",
}


def resolve_notify_targets(user_info, request) -> dict[str, str]:
    """Return the effective per-channel webhook targets for one task.

    Precedence is notification_config, request-level webhook fields, then
    stored user configuration. A channel-only config filters the merged
    destinations; ``notify=none`` suppresses every source.
    """
    if hasattr(user_info, "model_dump"):
        user = user_info.model_dump(exclude_none=True)
    elif isinstance(user_info, dict):
        user = user_info
    elif user_info is None:
        user = {}
    else:
        raise TypeError("user_info must be a mapping or Pydantic model")

    if hasattr(request, "model_dump"):
        requested = request.model_dump(exclude_none=True)
    elif isinstance(request, dict):
        requested = request
    elif request is None:
        requested = {}
    else:
        raise TypeError("request must be a mapping or Pydantic model")
    notify = requested.get("notify", "default")
    if not isinstance(notify, str) or notify not in ("default", "none"):
        raise ValueError("notify must be 'default' or 'none'")
    if notify == "none":
        return {}

    targets = {
        channel: user[field]
        for channel, field in _CHANNEL_FIELDS.items()
        if user.get(field)
    }
    for channel, field in _CHANNEL_FIELDS.items():
        if requested.get(field):
            targets[channel] = requested[field]

    notification_config = requested.get("notification_config")
    if notification_config is None:
        notification_config = {}
    if hasattr(notification_config, "model_dump"):
        notification_config = notification_config.model_dump(exclude_none=True)
    elif not isinstance(notification_config, dict):
        raise TypeError("notification_config must be a mapping or Pydantic model")
    channel = notification_config.get("channel")
    webhook = notification_config.get("webhook")
    if channel is not None and (
        not isinstance(channel, str) or channel not in _CHANNEL_FIELDS
    ):
        raise ValueError("notification_config.channel must be 'wechat' or 'feishu'")
    if webhook and channel is None:
        raise ValueError("notification_config.webhook requires channel")
    if webhook:
        targets[channel] = webhook
    if channel is not None:
        targets = {channel: targets[channel]} if targets.get(channel) else {}
    return targets
