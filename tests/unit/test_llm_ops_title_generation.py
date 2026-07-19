"""
_generate_title_if_needed() call_llm_api integration tests.

Regression coverage for a stale call-site bug: _generate_title_if_needed()
used to invoke call_llm_api() with 6 positional arguments
(model, prompt, api_key, base_url, max_retries, retry_delay), but the
current call_llm_api() signature only accepts 4 positional args
(model, prompt, reasoning_effort, task_type) -- everything else is
keyword-only. Every real invocation raised TypeError, silently swallowed by
the surrounding try/except, so title generation always fell back to the
default "自定义文件总结" string and never actually reached the LLM.

The fix routes the call through the app-wide LLMCoordinator's already
wired-up LLMClient (the same object used for calibration/summary), wrapped
in usage_context.set_context(stage="title") so the call also gets picked up
by the token-usage audit trail.

All console output must be in English only (no emoji, no Chinese).
"""

from unittest.mock import MagicMock, patch

import pytest

from video_transcript_api.api.services import llm_ops
from video_transcript_api.llm.core.llm_client import LLMClient
from video_transcript_api.llm.core import usage_context
from video_transcript_api.utils.logging.audit_logger import AuditLogger
from video_transcript_api.utils.logging.usage_recorder import UsageRecorder


@pytest.fixture(autouse=True)
def _reset_usage_bridge():
    """The usage bridge slot must never leak a stale snapshot between tests."""
    usage_context.pop_chat_result_usage()
    yield
    usage_context.pop_chat_result_usage()


@pytest.fixture
def real_llm_client():
    """A real LLMClient wired to a mocked call_llm_api.

    Using the real LLMClient (rather than a MagicMock stand-in) lets the
    fix's usage-audit plumbing (LLMClient.call() -> _record_usage()) run for
    real, exactly like the production code path does -- this is what proves
    the "usage 记账产生 stage=title 记录" requirement, not just that some
    mock got called.
    """
    return LLMClient(api_key="test-key", base_url="https://api.test.com")


@pytest.fixture
def fake_coordinator(real_llm_client):
    """Stand-in for the app-wide LLMCoordinator singleton: only the
    `llm_client` attribute _generate_title_if_needed() actually touches."""
    coordinator = MagicMock()
    coordinator.llm_client = real_llm_client
    return coordinator


@pytest.fixture
def generic_llm_task():
    """A task from a generic downloader with no title -- the only branch
    that reaches the LLM title-generation call."""
    return {"is_generic": True}


def _patch_module_singletons(monkeypatch, coordinator, config=None):
    monkeypatch.setattr(llm_ops, "llm_coordinator", coordinator)
    monkeypatch.setattr(
        llm_ops, "config", config or {"llm": {"summary_model": "test-summary-model"}}
    )


class TestGenerateTitleCallsLLMWithCorrectSignature:
    def test_irrelevant_speaker_gate_does_not_enable_title_llm(self):
        assert llm_ops._requires_llm_title(
            {
                "calibrate": False,
                "summarize": False,
                "infer_speaker_names": True,
            },
            use_speaker_recognition=False,
        ) is False

    """Locks in the fixed call: call_llm_api() (via LLMClient.call()) must
    receive the current-signature keyword arguments, not the stale
    6-positional-arg form that always raised TypeError."""

    @patch("video_transcript_api.llm.core.llm_client.call_llm_api")
    def test_call_llm_api_receives_correct_kwargs(
        self, mock_call_llm_api, monkeypatch, fake_coordinator, generic_llm_task
    ):
        mock_call_llm_api.return_value = "Generated Title"
        _patch_module_singletons(monkeypatch, fake_coordinator)

        result = llm_ops._generate_title_if_needed(
            generic_llm_task, "", "some transcript content"
        )

        assert result == "Generated Title"
        assert mock_call_llm_api.call_count == 1

        kwargs = mock_call_llm_api.call_args.kwargs
        assert kwargs["model"] == "test-summary-model"
        assert "some transcript content" in kwargs["prompt"]
        assert kwargs["task_type"] == "title"
        # Plain text output only -- title generation never needs a schema.
        assert kwargs["response_schema"] is None
        # The stale call site passed api_key/base_url/max_retries/retry_delay
        # positionally; the fixed call site must not reintroduce them.
        assert "api_key" not in kwargs
        assert "base_url" not in kwargs
        assert "max_retries" not in kwargs
        assert "retry_delay" not in kwargs

    @patch("video_transcript_api.llm.core.llm_client.call_llm_api")
    def test_non_generic_task_never_calls_llm(
        self, mock_call_llm_api, monkeypatch, fake_coordinator
    ):
        """Non-generic downloader tasks already have a title -- must not
        reach the LLM call at all."""
        _patch_module_singletons(monkeypatch, fake_coordinator)

        result = llm_ops._generate_title_if_needed(
            {"is_generic": False}, "", "transcript"
        )

        assert result == ""
        assert mock_call_llm_api.call_count == 0


class TestGenerateTitleFailureFallback:
    """The try/except fallback to the default title is a reasonable
    degradation and must stay -- but failures must be logged at warning
    level so real faults (bad model config, auth errors) aren't silently
    invisible."""

    @patch("video_transcript_api.llm.core.llm_client.call_llm_api")
    def test_llm_error_falls_back_to_default_title_and_logs_warning(
        self, mock_call_llm_api, monkeypatch, fake_coordinator, generic_llm_task
    ):
        mock_call_llm_api.side_effect = RuntimeError("simulated LLM failure")
        _patch_module_singletons(monkeypatch, fake_coordinator)

        mock_logger = MagicMock()
        monkeypatch.setattr(llm_ops, "logger", mock_logger)

        result = llm_ops._generate_title_if_needed(
            generic_llm_task, "", "some transcript content"
        )

        assert result == "自定义文件总结"
        assert mock_logger.warning.call_count == 1
        warning_message = mock_logger.warning.call_args.args[0]
        assert "simulated LLM failure" in warning_message
        # Failure must not be logged as an unhandled error -- it's an
        # expected, handled degradation path.
        assert mock_logger.error.call_count == 0


class TestGenerateTitleUsageAudit:
    """The title-generation call must be captured by the same token-usage
    audit trail as calibration/summary, tagged stage="title"."""

    @patch("video_transcript_api.llm.core.llm_client.call_llm_api")
    def test_usage_recorded_with_stage_title(
        self, mock_call_llm_api, monkeypatch, fake_coordinator, generic_llm_task, tmp_path
    ):
        mock_call_llm_api.return_value = "Generated Title"
        _patch_module_singletons(monkeypatch, fake_coordinator)

        audit_logger = AuditLogger(db_path=str(tmp_path / "audit.db"))
        recorder = UsageRecorder(audit_logger=audit_logger)

        # Mirrors the real call context nesting: _handle_llm_task binds
        # task_id once via bind_task_id() at the thread entry point, and our
        # fix's set_context(stage="title") only refines the stage on top of
        # it. Using the `with` form here (rather than bind_task_id) keeps
        # this test's context change from leaking into later tests.
        with usage_context.set_context(task_id="title-task-123"), patch(
            "video_transcript_api.llm.core.llm_client.get_usage_recorder",
            return_value=recorder,
        ):
            llm_ops._generate_title_if_needed(
                generic_llm_task, "", "some transcript content"
            )

        with audit_logger._get_cursor() as cursor:
            cursor.execute(
                "SELECT task_id, stage, model FROM llm_usage"
            )
            row = cursor.fetchone()

        assert row is not None
        assert row[0] == "title-task-123"
        assert row[1] == "title"
