from unittest.mock import MagicMock

import pytest

from src.video_transcript_api.api import context
from src.video_transcript_api.api.services import terminal_status


@pytest.mark.parametrize(
    ("audit_webhook", "user", "expected"),
    [
        (
            "audit-wechat",
            {"wechat_webhook": "user-wechat", "feishu_webhook": "user-feishu"},
            {"wechat": "audit-wechat", "feishu": "user-feishu"},
        ),
        (
            None,
            {"wechat_webhook": "user-wechat", "feishu_webhook": "user-feishu"},
            {"wechat": "user-wechat", "feishu": "user-feishu"},
        ),
        (None, {"wechat_webhook": None, "feishu_webhook": None}, None),
    ],
)
def test_resolve_delivery_webhooks_merges_channels_and_preserves_empty_result(
    monkeypatch, audit_webhook, user, expected,
):
    runtime = MagicMock()
    runtime.user_manager.get_user_by_id.return_value = user
    monkeypatch.setattr(context, "get_runtime", lambda: runtime)
    monkeypatch.setattr(
        terminal_status,
        "_lookup_audit_wechat_webhook",
        lambda task_id: audit_webhook,
    )

    result = terminal_status._resolve_delivery_webhooks(
        {"task_id": "task-1", "submitted_by": "user-1"}
    )

    assert result == expected
    runtime.user_manager.get_user_by_id.assert_called_once_with("user-1")
