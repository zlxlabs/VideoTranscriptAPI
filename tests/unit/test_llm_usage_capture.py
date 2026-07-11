"""
LLMClient token usage capture tests.

Covers:
- LLMClient.call() extracts usage (prompt/completion/total tokens) from the
  llm-compat ChatResult and records it via UsageRecorder, tagged with the
  current task_id/stage from usage_context.
- usage=None (provider did not report usage) -> recorded with usage_missing=1
  and zeroed token counts, never silently dropped.
- Recording-layer exceptions never affect call()'s return value or raised
  exceptions (fail-open).

These tests exercise the REAL call_llm_api()/llm.py code path (not mocking
call_llm_api directly) by patching llm.get_sync_client() to return a fake
SyncLLMClient whose .chat() returns a controllable llm_compat ChatResult.
This is necessary because call_llm_api() intentionally does not expose
ChatResult.usage in its public str/StructuredResult return types -- the
usage bridge in llm/core/usage_context.py is what carries it across.

All console output must be in English only (no emoji, no Chinese).
"""

from unittest.mock import MagicMock, patch

import pytest
from llm_compat import ChatResult, TokenUsage

from video_transcript_api.llm.core.llm_client import LLMClient
from video_transcript_api.llm.core import usage_context
from video_transcript_api.utils.logging.audit_logger import AuditLogger
from video_transcript_api.utils.logging.usage_recorder import UsageRecorder


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_audit.db")


@pytest.fixture
def audit_logger(tmp_db):
    return AuditLogger(db_path=tmp_db)


@pytest.fixture
def recorder(audit_logger):
    return UsageRecorder(audit_logger=audit_logger)


@pytest.fixture
def client():
    return LLMClient(
        api_key="test-key",
        base_url="https://api.test.com",
    )


@pytest.fixture(autouse=True)
def _reset_usage_bridge():
    """Ensure the usage_context bridge slot never leaks between tests."""
    usage_context.pop_chat_result_usage()
    yield
    usage_context.pop_chat_result_usage()


def _fake_sync_client(chat_result: ChatResult) -> MagicMock:
    """Build a MagicMock standing in for llm_compat.SyncLLMClient."""
    fake = MagicMock()
    fake.chat.return_value = chat_result
    return fake


class TestUsageExtractionAndRecording:
    """Verify call() extracts ChatResult.usage and records it via UsageRecorder."""

    def test_text_mode_records_usage(self, client, recorder):
        chat_result = ChatResult(
            content="calibrated text",
            usage=TokenUsage(prompt_tokens=120, completion_tokens=40, total_tokens=160),
            model="actual-model-used",
        )

        with patch(
            "video_transcript_api.llm.llm.get_sync_client",
            return_value=_fake_sync_client(chat_result),
        ), patch(
            "video_transcript_api.llm.core.llm_client.get_usage_recorder",
            return_value=recorder,
        ), usage_context.set_context(task_id="task-abc", stage="calibration"):
            result = client.call(
                model="requested-model",
                system_prompt="system",
                user_prompt="user",
            )

        assert result.text == "calibrated text"

        with recorder._audit_logger._get_cursor() as cursor:
            cursor.execute(
                "SELECT task_id, stage, model, prompt_tokens, completion_tokens, "
                "total_tokens, usage_missing FROM llm_usage"
            )
            row = cursor.fetchone()

        # model should reflect ChatResult.model (actual model used, e.g. after fallback),
        # not the originally requested model
        assert row == ("task-abc", "calibration", "actual-model-used", 120, 40, 160, 0)

    def test_records_duration_ms_greater_or_equal_zero(self, client, recorder):
        chat_result = ChatResult(
            content="x",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="m",
        )
        with patch(
            "video_transcript_api.llm.llm.get_sync_client",
            return_value=_fake_sync_client(chat_result),
        ), patch(
            "video_transcript_api.llm.core.llm_client.get_usage_recorder",
            return_value=recorder,
        ):
            client.call(model="m", system_prompt="s", user_prompt="u")

        with recorder._audit_logger._get_cursor() as cursor:
            cursor.execute("SELECT duration_ms FROM llm_usage")
            duration_ms = cursor.fetchone()[0]

        assert duration_ms >= 0

    def test_falls_back_to_requested_model_when_chat_result_model_empty(self, client, recorder):
        chat_result = ChatResult(
            content="x",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="",  # provider did not echo back a model name
        )
        with patch(
            "video_transcript_api.llm.llm.get_sync_client",
            return_value=_fake_sync_client(chat_result),
        ), patch(
            "video_transcript_api.llm.core.llm_client.get_usage_recorder",
            return_value=recorder,
        ):
            client.call(model="requested-model", system_prompt="s", user_prompt="u")

        with recorder._audit_logger._get_cursor() as cursor:
            cursor.execute("SELECT model FROM llm_usage")
            model = cursor.fetchone()[0]

        assert model == "requested-model"


class TestUsageMissing:
    """Verify usage=None (provider did not report token usage) is flagged, not dropped."""

    def test_usage_none_records_zeroed_row_with_missing_flag(self, client, recorder):
        chat_result = ChatResult(
            content="calibrated text",
            usage=None,
            model="some-model",
        )
        with patch(
            "video_transcript_api.llm.llm.get_sync_client",
            return_value=_fake_sync_client(chat_result),
        ), patch(
            "video_transcript_api.llm.core.llm_client.get_usage_recorder",
            return_value=recorder,
        ):
            result = client.call(model="some-model", system_prompt="s", user_prompt="u")

        assert result.text == "calibrated text"

        with recorder._audit_logger._get_cursor() as cursor:
            cursor.execute(
                "SELECT prompt_tokens, completion_tokens, total_tokens, usage_missing "
                "FROM llm_usage"
            )
            row = cursor.fetchone()

        assert row == (0, 0, 0, 1)


class TestRecordingFailOpen:
    """Verify recording-layer failures never affect call()'s outcome."""

    def test_broken_usage_recorder_does_not_break_successful_call(self, client):
        chat_result = ChatResult(
            content="ok",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="m",
        )

        broken_recorder = MagicMock()
        broken_recorder.record.side_effect = RuntimeError("DB is on fire")

        with patch(
            "video_transcript_api.llm.llm.get_sync_client",
            return_value=_fake_sync_client(chat_result),
        ), patch(
            "video_transcript_api.llm.core.llm_client.get_usage_recorder",
            return_value=broken_recorder,
        ):
            # Should not raise despite the recorder blowing up internally
            result = client.call(model="m", system_prompt="s", user_prompt="u")

        assert result.text == "ok"

    def test_broken_usage_context_does_not_break_call(self, client):
        chat_result = ChatResult(
            content="ok",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="m",
        )

        with patch(
            "video_transcript_api.llm.llm.get_sync_client",
            return_value=_fake_sync_client(chat_result),
        ), patch(
            "video_transcript_api.llm.core.llm_client.pop_chat_result_usage",
            side_effect=RuntimeError("context blew up"),
        ):
            result = client.call(model="m", system_prompt="s", user_prompt="u")

        assert result.text == "ok"

    def test_broken_recorder_does_not_swallow_original_exception(self, client):
        """When call_llm_api itself raises, the original error must still propagate
        even if the usage-recording hook (in the finally block) also fails."""
        from video_transcript_api.llm.core.errors import FatalError

        broken_recorder = MagicMock()
        broken_recorder.record.side_effect = RuntimeError("DB is on fire")

        with patch(
            "video_transcript_api.llm.core.llm_client.call_llm_api",
            side_effect=FatalError("401 Unauthorized"),
        ), patch(
            "video_transcript_api.llm.core.llm_client.get_usage_recorder",
            return_value=broken_recorder,
        ):
            with pytest.raises(FatalError):
                client.call(model="m", system_prompt="s", user_prompt="u")
