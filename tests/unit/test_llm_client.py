"""
LLMClient unit tests.

Covers:
- Successful call (text and structured output)
- Retry logic on retryable errors
- No retry on fatal errors (401, 403, 404)
- Exponential backoff delay calculation
- Max retry exhaustion raises RetryableError

All console output must be in English only (no emoji, no Chinese).
"""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest


from video_transcript_api.llm.core.llm_client import LLMClient, LLMResponse
from video_transcript_api.llm.core.errors import RetryableError, FatalError
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

    @patch.object(LLMClient, "_actual_call")
    def test_text_response(self, mock_call, client):
        """Should return LLMResponse with text on success."""
        mock_call.return_value = LLMResponse(text="calibrated text")

        result = client.call(
            model="test-model",
            system_prompt="system",
            user_prompt="user",
        )

        assert result.text == "calibrated text"
        assert result.structured_output is None
        assert mock_call.call_count == 1

    @patch.object(LLMClient, "_actual_call")
    def test_structured_response(self, mock_call, client):
        """Should return LLMResponse with structured_output."""
        mock_call.return_value = LLMResponse(
            text="",
            structured_output={"key": "value"},
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
# Retry Logic Tests
# ============================================================


class TestLLMClientRetry:
    """Verify retry behavior on retryable errors."""

    @patch("video_transcript_api.llm.core.llm_client.time.sleep")
    @patch.object(LLMClient, "_actual_call")
    def test_no_retry_on_timeout(self, mock_call, mock_sleep, client):
        """Should NOT retry on timeout errors (timeout retries are ineffective)."""
        from video_transcript_api.llm.core.errors import TimeoutError as LLMTimeoutError

        mock_call.side_effect = LLMCallError("Request timed out")

        with pytest.raises(LLMTimeoutError):
            client.call(
                model="test-model",
                system_prompt="system",
                user_prompt="user",
            )

        assert mock_call.call_count == 1
        mock_sleep.assert_not_called()

    @patch("video_transcript_api.llm.core.llm_client.time.sleep")
    @patch.object(LLMClient, "_actual_call")
    def test_retry_on_500_error(self, mock_call, mock_sleep, client):
        """Should retry on server errors (500)."""
        mock_call.side_effect = [
            LLMCallError("500 Internal Server Error"),
            LLMCallError("500 Internal Server Error"),
            LLMResponse(text="success after 2 retries"),
        ]

        result = client.call(
            model="test-model",
            system_prompt="system",
            user_prompt="user",
        )

        assert result.text == "success after 2 retries"
        assert mock_call.call_count == 3  # initial + 2 retries

    @patch("video_transcript_api.llm.core.llm_client.time.sleep")
    @patch.object(LLMClient, "_actual_call")
    def test_max_retries_exhausted(self, mock_call, mock_sleep, client):
        """Should raise RetryableError after max retries."""
        mock_call.side_effect = LLMCallError("Server overloaded")

        with pytest.raises(RetryableError) as exc_info:
            client.call(
                model="test-model",
                system_prompt="system",
                user_prompt="user",
            )

        assert "2 retries" in str(exc_info.value)
        assert mock_call.call_count == 3  # initial + 2 retries


# ============================================================
# Fatal Error Tests
# ============================================================


class TestLLMClientFatalErrors:
    """Verify immediate failure on fatal errors (no retry)."""

    @patch.object(LLMClient, "_actual_call")
    def test_no_retry_on_401(self, mock_call, client):
        """Should not retry on 401 Unauthorized."""
        mock_call.side_effect = LLMCallError("401 Unauthorized: Invalid API key")

        with pytest.raises(FatalError):
            client.call(
                model="test-model",
                system_prompt="system",
                user_prompt="user",
            )

        assert mock_call.call_count == 1  # No retry

    @patch.object(LLMClient, "_actual_call")
    def test_no_retry_on_403(self, mock_call, client):
        """Should not retry on 403 Forbidden."""
        mock_call.side_effect = LLMCallError("403 Forbidden: Access denied")

        with pytest.raises(FatalError):
            client.call(
                model="test-model",
                system_prompt="system",
                user_prompt="user",
            )

        assert mock_call.call_count == 1

    @patch.object(LLMClient, "_actual_call")
    def test_no_retry_on_invalid_model(self, mock_call, client):
        """Should not retry on invalid model error."""
        mock_call.side_effect = LLMCallError("Invalid model: test-model does not exist")

        with pytest.raises(FatalError):
            client.call(
                model="test-model",
                system_prompt="system",
                user_prompt="user",
            )

        assert mock_call.call_count == 1


# ============================================================
# Exponential Backoff Tests
# ============================================================


class TestLLMClientBackoff:
    """Verify exponential backoff delay calculation."""

    def test_backoff_calculation(self, client):
        """Delay should double with each attempt."""
        assert client._calculate_delay(0) == 1.0   # 1 * 2^0
        assert client._calculate_delay(1) == 2.0   # 1 * 2^1
        assert client._calculate_delay(2) == 4.0   # 1 * 2^2

    def test_backoff_max_cap(self, client):
        """Delay should be capped at 60 seconds."""
        assert client._calculate_delay(10) == 60.0  # 1 * 2^10 = 1024, capped to 60

    def test_backoff_with_larger_base(self):
        """Verify backoff with larger retry_delay."""
        client = LLMClient(
            api_key="test", base_url="https://test.com",
            retry_delay=5,
        )
        assert client._calculate_delay(0) == 5.0
        assert client._calculate_delay(1) == 10.0
        assert client._calculate_delay(2) == 20.0
        assert client._calculate_delay(3) == 40.0
        assert client._calculate_delay(4) == 60.0  # 5 * 16 = 80, capped to 60

    @patch("video_transcript_api.llm.core.llm_client.time.sleep")
    @patch.object(LLMClient, "_actual_call")
    def test_backoff_delays_increase(self, mock_call, mock_sleep, client):
        """Sleep durations should increase between retries."""
        mock_call.side_effect = LLMCallError("connection error")

        with pytest.raises(RetryableError):
            client.call(
                model="test-model",
                system_prompt="system",
                user_prompt="user",
            )

        # With retry_delay=1, delays should be 1.0 and 2.0
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays == [1.0, 2.0]


# ============================================================
# Error Classification Tests
# ============================================================


class TestErrorClassification:
    """Verify error classification from llm/core/errors.py."""

    def test_classify_timeout_as_timeout_error(self):
        """Timeout errors should be classified as TimeoutError (subclass of RetryableError)."""
        from video_transcript_api.llm.core.errors import classify_error, TimeoutError as LLMTimeoutError
        result = classify_error(Exception("Request timed out"))
        assert result == LLMTimeoutError
        assert issubclass(result, RetryableError)

    def test_classify_401_as_fatal(self):
        """401 errors should be classified as fatal."""
        from video_transcript_api.llm.core.errors import classify_error
        result = classify_error(Exception("401 Unauthorized"))
        assert result == FatalError

    def test_classify_unknown_as_retryable(self):
        """Unknown errors should default to retryable."""
        from video_transcript_api.llm.core.errors import classify_error
        result = classify_error(Exception("something weird happened"))
        assert result == RetryableError
