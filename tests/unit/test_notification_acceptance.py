"""Outbox acceptance semantics: which router results mark a row sent.

Split out of test_terminal_notification_outbox.py (file-size ratchet).
Invariant 4 lives here: one rejected declared channel keeps the whole
notice unaccepted; only all-declared-accepted marks the row sent, and a
mixed result stays pending for the next retry round.
"""

from unittest.mock import MagicMock

import pytest

from src.video_transcript_api.api.services.terminal_status import (
    _notification_accepted,
    deliver_pending_terminal_notifications,
    finalize_terminal_status_and_notify,
)
from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.task_status import TaskStatus

from tests.unit.test_terminal_notification_outbox import _new_task, _outbox_state


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


class TestNotificationAcceptance:
    @pytest.mark.parametrize(
        ("result", "accepted"),
        [
            (None, False),
            ({"wechat": False}, False),
            ({"wechat": True}, True),
            # Invariant 4: one rejected declared channel keeps the whole
            # notice unaccepted; the retry re-sends to accepted channels.
            ({"wechat": False, "feishu": True}, False),
            ({"wechat": object()}, False),
            ({"wechat": "ok"}, False),
            ({"wechat": 1}, False),
            ({"wechat": []}, False),
            ({"wechat": {}}, False),
            ({}, False),
            ("True", False),
            ({"wechat": False, "feishu": False}, False),
        ],
    )
    def test_only_dict_with_accepted_channel_is_accepted(self, result, accepted):
        assert _notification_accepted(result) is accepted

    def test_mixed_channel_acceptance_keeps_row_pending_for_retry(self, cm):
        """Invariant 4: feishu rejected + wechat accepted => the row is NOT
        marked sent; the next round retries the whole declared set."""
        task_id = _new_task(cm)
        cm.update_task_status(task_id, TaskStatus.PROCESSING)
        router = MagicMock()
        router.notify_task_status.return_value = {"wechat": False, "feishu": True}

        assert finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.FAILED,
            error_message="boom",
            cache_manager=cm,
            router=router,
        ) is True
        assert _outbox_state(cm, task_id)["notified_at"] is None
        assert cm.is_terminal_notification_pending(task_id) is True

        router.notify_task_status.return_value = {"wechat": True, "feishu": True}
        assert deliver_pending_terminal_notifications(cm, router=router) == 1
        assert _outbox_state(cm, task_id)["notified_at"] is not None
