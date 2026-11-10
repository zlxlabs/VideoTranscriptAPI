from unittest.mock import MagicMock

from src.video_transcript_api.api import context
from src.video_transcript_api.api.services import terminal_status


def test_persisted_targets_are_authoritative_without_user_or_audit_lookup(monkeypatch):
    runtime = MagicMock()
    monkeypatch.setattr(context, "get_runtime", lambda: runtime)

    result = terminal_status._task_notify_targets({
        "task_id": "task-persisted",
        "submitted_by": "changed-user",
        "notify_targets": {"feishu": "submit-time-feishu"},
    })

    assert result == {"feishu": "submit-time-feishu"}
    runtime.user_manager.get_user_by_id.assert_not_called()


def test_legacy_row_resolves_current_user_targets_and_null_submitter_is_silent(monkeypatch):
    from src.video_transcript_api.api.services import terminal_status

    runtime = MagicMock()
    runtime.user_manager.get_user_by_id.return_value = {
        "wechat_webhook": "current-wechat",
        "feishu_webhook": "current-feishu",
    }
    monkeypatch.setattr(context, "get_runtime", lambda: runtime)

    assert terminal_status._task_notify_targets({
        "task_id": "legacy-row", "submitted_by": "user-1", "notify_targets": None,
    }) == {"wechat": "current-wechat", "feishu": "current-feishu"}
    assert terminal_status._task_notify_targets({
        "task_id": "legacy-null-owner", "submitted_by": None, "notify_targets": None,
    }) == {}
    runtime.user_manager.get_user_by_id.assert_called_once_with("user-1")
