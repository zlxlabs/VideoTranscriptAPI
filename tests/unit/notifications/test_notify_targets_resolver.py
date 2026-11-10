import pytest

from video_transcript_api.utils.notifications.targets import resolve_notify_targets


def test_request_overrides_user_and_channel_only_filters_resolved_targets():
    user = {"wechat_webhook": "user-wechat", "feishu_webhook": "user-feishu"}
    assert resolve_notify_targets(
        user,
        {
            "wechat_webhook": "request-wechat",
            "notification_config": {"channel": "feishu"},
        },
    ) == {"feishu": "user-feishu"}
    assert resolve_notify_targets(
        user,
        {
            "feishu_webhook": "request-feishu",
            "notification_config": {"channel": "feishu", "webhook": "custom-feishu"},
        },
    ) == {"feishu": "custom-feishu"}


def test_notify_none_overrides_every_source():
    assert resolve_notify_targets(
        {"wechat_webhook": "user-wechat", "feishu_webhook": "user-feishu"},
        {
            "notify": "none",
            "wechat_webhook": "request-wechat",
            "notification_config": {"channel": "feishu", "webhook": "custom-feishu"},
        },
    ) == {}


def test_notify_default_uses_user_targets_and_top_level_webhook_overrides_channel():
    assert resolve_notify_targets(
        {"wechat_webhook": "user-wechat", "feishu_webhook": "user-feishu"},
        {"feishu_webhook": "request-feishu"},
    ) == {"wechat": "user-wechat", "feishu": "request-feishu"}


@pytest.mark.parametrize(
    "request_data",
    [
        {"notification_config": {"webhook": "custom"}},
        {"notification_config": {"channel": "other"}},
    ],
)
def test_invalid_notification_config_is_rejected(request_data):
    with pytest.raises(ValueError):
        resolve_notify_targets({}, request_data)
