"""
LLMClient unit tests (post llm-compat migration).

LLMClient is now a thin wrapper: parameter assembly + call_llm_api forwarding
+ result wrapping + error mapping. Retry/backoff handled by llm-compat internally.

Covers:
- Successful call (text and structured output)
- Error mapping from llm-compat to project error types
- Error classification (classify_error still works)

All console output must be in English only (no emoji, no Chinese).
"""

from unittest.mock import patch, MagicMock

import pytest

from video_transcript_api.llm.core.llm_client import LLMClient, LLMResponse
from video_transcript_api.llm.core.errors import (
    RetryableError,
    FatalError,
    classify_error,
    map_llm_compat_error,
    TimeoutError as LLMTimeoutError,
    TruncationError,
)
from video_transcript_api.llm.llm import LLMCallError, StructuredResult


@pytest.fixture
def client():
    """Create a LLMClient with test configuration."""
    return LLMClient(
        api_key="test-key",
        base_url="https://api.test.com",
        max_retries=2,
        retry_delay=1,
    )


# ============================================================
# Successful Call Tests
# ============================================================


class TestLLMClientSuccess:
    """Verify successful LLM API calls."""

    @patch("video_transcript_api.llm.core.llm_client.call_llm_api")
    def test_text_response(self, mock_call, client):
        """Should return LLMResponse with text on success."""
        mock_call.return_value = "calibrated text"

        result = client.call(
            model="test-model",
            system_prompt="system",
            user_prompt="user",
        )

        assert result.text == "calibrated text"
        assert result.structured_output is None
        assert mock_call.call_count == 1

    @patch("video_transcript_api.llm.core.llm_client.call_llm_api")
    def test_structured_response(self, mock_call, client):
        """Should return LLMResponse with structured_output."""
        mock_call.return_value = StructuredResult(
            success=True,
            data={"key": "value"},
        )

        result = client.call(
            model="test-model",
            system_prompt="system",
            user_prompt="user",
            response_schema={"type": "object"},
        )

        assert result.structured_output == {"key": "value"}
        assert mock_call.call_count == 1


# ============================================================
# Error Mapping Tests (llm-compat errors -> project errors)
# ============================================================


class TestLLMClientErrorMapping:
    """Verify llm-compat error mapping in LLMClient."""

    @patch("video_transcript_api.llm.core.llm_client.call_llm_api")
    def test_fatal_error_raises(self, mock_call, client):
        """llm-compat FatalError should propagate as project FatalError."""
        from llm_compat import FatalError as LCFatal
        mock_call.side_effect = LCFatal("401 Unauthorized")

        with pytest.raises(FatalError):
            client.call(model="test", system_prompt="s", user_prompt="u")

    @patch("video_transcript_api.llm.core.llm_client.call_llm_api")
    def test_timeout_error_raises(self, mock_call, client):
        """llm-compat TimeoutError should propagate as project TimeoutError."""
        from llm_compat import TimeoutError as LCTimeout
        mock_call.side_effect = LCTimeout("timed out")

        with pytest.raises(LLMTimeoutError):
            client.call(model="test", system_prompt="s", user_prompt="u")

    @patch("video_transcript_api.llm.core.llm_client.call_llm_api")
    def test_structured_failure_raises_llm_call_error(self, mock_call, client):
        """StructuredResult with success=False should raise LLMCallError."""
        mock_call.return_value = StructuredResult(
            success=False,
            error="json_object call failed",
        )

        with pytest.raises(LLMCallError):
            client.call(
                model="test",
                system_prompt="s",
                user_prompt="u",
                response_schema={"type": "object"},
            )


# ============================================================
# Error Classification Tests (unchanged)
# ============================================================


class TestErrorClassification:
    """Verify error classification from llm/core/errors.py."""

    def test_classify_timeout_as_timeout_error(self):
        result = classify_error(Exception("Request timed out"))
        assert result == LLMTimeoutError
        assert issubclass(result, RetryableError)

    def test_classify_401_as_fatal(self):
        result = classify_error(Exception("401 Unauthorized"))
        assert result == FatalError

    def test_classify_unknown_as_retryable(self):
        result = classify_error(Exception("something weird happened"))
        assert result == RetryableError
