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


class TestStaleUsageSlotNotMisattributed:
    """Regression coverage for codex-review R1 item 4.

    The usage bridge slot (usage_context._chat_usage_log) is written by
    llm/llm.py's internal call helpers whenever they get a real ChatResult
    back from llm-compat, and is only ever POPPED by LLMClient.call()'s
    finally block. Call sites that invoke call_llm_api() directly -- bypassing
    LLMClient.call() entirely, e.g. llm_ops._generate_title_if_needed -- write
    the slot but never pop it. If the NEXT LLMClient.call() then fails before
    ever reaching a real ChatResult (so record_chat_result_usage() is never
    called again), its finally-block pop would read that stale, unrelated
    snapshot and misattribute its tokens to the current task/stage.
    """

    def test_stale_slot_from_earlier_direct_call_not_misattributed_on_failure(
        self, client, recorder
    ):
        """Simulate the exact leak: a stale snapshot sits in the bridge slot
        (as if left behind by a direct call_llm_api() call elsewhere), and
        THIS call() fails before any real ChatResult is produced. The
        recorded row must reflect THIS call's (missing) usage, not the stale
        snapshot's tokens."""
        from video_transcript_api.llm.core.errors import FatalError

        usage_context.record_chat_result_usage(
            model="stale-model-from-earlier-call",
            usage=TokenUsage(prompt_tokens=999, completion_tokens=999, total_tokens=1998),
        )

        with patch(
            "video_transcript_api.llm.core.llm_client.call_llm_api",
            side_effect=FatalError("401 Unauthorized"),
        ), patch(
            "video_transcript_api.llm.core.llm_client.get_usage_recorder",
            return_value=recorder,
        ):
            with pytest.raises(FatalError):
                client.call(model="m", system_prompt="s", user_prompt="u")

        with recorder._audit_logger._get_cursor() as cursor:
            cursor.execute(
                "SELECT model, prompt_tokens, completion_tokens, total_tokens, usage_missing "
                "FROM llm_usage"
            )
            row = cursor.fetchone()

        # Must NOT be the stale snapshot (would be
        # ("stale-model-from-earlier-call", 999, 999, 1998, 0) if the bug were
        # still present) -- this call never reached a real ChatResult, so it
        # must be recorded as usage_missing with the requested model as
        # fallback, exactly like the "call_llm_api never made a real API
        # round-trip" case already covered elsewhere (None tokens are stored
        # as 0 by UsageRecorder, matching test_usage_none_records_zeroed_row).
        assert row == ("m", 0, 0, 0, 1)

    def test_successful_call_still_pops_its_own_usage_normally(self, client, recorder):
        """Sanity check: the pre-call clear must not break the happy path --
        a call that DOES produce a real ChatResult still records its own
        (correct) usage, not a missing/empty one."""
        chat_result = ChatResult(
            content="ok",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            model="actual-model",
        )
        with patch(
            "video_transcript_api.llm.llm.get_sync_client",
            return_value=_fake_sync_client(chat_result),
        ), patch(
            "video_transcript_api.llm.core.llm_client.get_usage_recorder",
            return_value=recorder,
        ):
            result = client.call(model="requested-model", system_prompt="s", user_prompt="u")

        assert result.text == "ok"

        with recorder._audit_logger._get_cursor() as cursor:
            cursor.execute(
                "SELECT model, prompt_tokens, completion_tokens, total_tokens, usage_missing "
                "FROM llm_usage"
            )
            row = cursor.fetchone()

        assert row == ("actual-model", 10, 5, 15, 0)


class TestSelfCorrectionRetryUsageAggregation:
    """Regression coverage: json_object mode's Self-Correction can trigger
    multiple real client.chat() round trips within a single call_llm_api()
    invocation (see llm.py::_call_with_json_object_mode). Each round trip
    consumes real, billable tokens. The bridge used to keep only the LAST
    snapshot ("last write wins"), silently dropping earlier failed attempts'
    token cost from the audit trail. It must now sum every attempt recorded
    during this call() into the one audit row."""

    def test_two_attempts_within_one_call_are_summed_not_last_write_wins(
        self, client, recorder
    ):
        def fake_call_llm_api(**kwargs):
            # Simulate _call_with_json_object_mode: first attempt fails JSON
            # parsing (but still consumed real tokens), second attempt
            # succeeds. Both call record_chat_result_usage(), exactly like
            # the real retry loop does on every client.chat() round trip.
            usage_context.record_chat_result_usage(
                model="attempt-1-model",
                usage=TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            )
            usage_context.record_chat_result_usage(
                model="attempt-2-model",
                usage=TokenUsage(prompt_tokens=100, completion_tokens=30, total_tokens=130),
            )
            return "final result text"

        with patch(
            "video_transcript_api.llm.core.llm_client.call_llm_api",
            side_effect=fake_call_llm_api,
        ), patch(
            "video_transcript_api.llm.core.llm_client.get_usage_recorder",
            return_value=recorder,
        ):
            result = client.call(model="requested-model", system_prompt="s", user_prompt="u")

        assert result.text == "final result text"

        with recorder._audit_logger._get_cursor() as cursor:
            cursor.execute(
                "SELECT model, prompt_tokens, completion_tokens, total_tokens, usage_missing "
                "FROM llm_usage"
            )
            row = cursor.fetchone()

        # Exactly ONE audit row for this ONE call(), but its token counts are
        # the SUM of both real API round trips (100+100=200, 20+30=50,
        # 120+130=250) -- not just the last attempt's 100/30/130. Pre-fix,
        # this would have recorded ("attempt-2-model", 100, 30, 130, 0),
        # silently losing attempt 1's 120 tokens from the audit trail.
        assert row == ("attempt-2-model", 200, 50, 250, 0)

    def test_retry_with_partially_missing_usage_counts_known_but_flags_incomplete(
        self, client, recorder
    ):
        """A provider that omits usage on the (eventually superseded) first
        attempt but reports it on the successful retry must still have the
        retry's real tokens counted as a lower-bound sum -- but the row must
        be flagged usage_missing=True, not silently reported as complete.
        The first attempt's real (but unknown) token cost is missing from
        the sum, so treating it as a "complete, accurate" 60-token total
        would misrepresent the audit data (ci-gate review, final round:
        the previous behavior only checked "at least one attempt reported
        usage", not "every attempt reported usage")."""

        def fake_call_llm_api(**kwargs):
            usage_context.record_chat_result_usage(model="attempt-1-model", usage=None)
            usage_context.record_chat_result_usage(
                model="attempt-2-model",
                usage=TokenUsage(prompt_tokens=50, completion_tokens=10, total_tokens=60),
            )
            return "final result text"

        with patch(
            "video_transcript_api.llm.core.llm_client.call_llm_api",
            side_effect=fake_call_llm_api,
        ), patch(
            "video_transcript_api.llm.core.llm_client.get_usage_recorder",
            return_value=recorder,
        ):
            client.call(model="requested-model", system_prompt="s", user_prompt="u")

        with recorder._audit_logger._get_cursor() as cursor:
            cursor.execute(
                "SELECT model, prompt_tokens, completion_tokens, total_tokens, usage_missing "
                "FROM llm_usage"
            )
            row = cursor.fetchone()

        # Known attempt's tokens (50/10/60) are still summed as a useful
        # lower bound, but usage_missing=1 makes clear the total is
        # incomplete -- attempt 1's real cost is unaccounted for.
        assert row == ("attempt-2-model", 50, 10, 60, 1)
