"""Calibration retry strategy tests

Tests for the calibration retry optimization:
- Error classification (timeout, truncation, format errors)
- Self-Correction smart behavior (only for format errors)
- max_retries parameter respect
- LLMClient layer differentiated retry
- Per-chunk time budget
- Default timeout value
"""

import json
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import requests.exceptions

from video_transcript_api.llm.core.errors import (
    classify_error,
    FatalError,
    RetryableError,
    TimeoutError as LLMTimeoutError,
    TruncationError,
)


# ============================================================
# Phase 1: Error classification
# ============================================================


class TestErrorClassification:
    """Test enhanced error classification for timeout and truncation."""

    def test_classify_read_timeout(self):
        """Read timeout should be classified as TimeoutError."""
        error = Exception(
            "HTTPConnectionPool(host='100.107.95.24', port=3001): "
            "Read timed out. (read timeout=900)"
        )
        assert classify_error(error) == LLMTimeoutError

    def test_classify_connect_timeout(self):
        """Connection timeout should be classified as TimeoutError."""
        error = Exception("Connection timed out")
        assert classify_error(error) == LLMTimeoutError

    def test_classify_generic_timeout(self):
        """Generic timeout message should be classified as TimeoutError."""
        error = Exception("Request timeout after 120s")
        assert classify_error(error) == LLMTimeoutError

    def test_classify_unterminated_string_as_truncation(self):
        """Unterminated string JSON error indicates output truncation."""
        error = Exception(
            "Structured output failed: json_object call failed: "
            "JSON parse failed: Unterminated string starting at: "
            "line 206 column 21 (char 9793)"
        )
        assert classify_error(error) == TruncationError

    def test_classify_unexpected_end_as_truncation(self):
        """Unexpected end of JSON indicates output truncation."""
        error = Exception(
            "JSON parse failed: Unexpected end of JSON input"
        )
        assert classify_error(error) == TruncationError

    def test_subclass_backward_compatible(self):
        """New error types must be subclasses of RetryableError."""
        assert issubclass(LLMTimeoutError, RetryableError)
        assert issubclass(TruncationError, RetryableError)

    def test_fatal_errors_unchanged(self):
        """Fatal error patterns should still work as before."""
        assert classify_error(Exception("401 Unauthorized")) == FatalError
        assert classify_error(Exception("403 Forbidden")) == FatalError
        assert classify_error(Exception("404 Not Found")) == FatalError

    def test_generic_retryable_unchanged(self):
        """Unknown errors should still be classified as RetryableError."""
        assert classify_error(Exception("Internal server error 500")) == RetryableError

    def test_timeout_takes_priority_over_retryable(self):
        """Timeout classification should be specific, not generic RetryableError."""
        error = Exception("Read timed out")
        result = classify_error(error)
        assert result == LLMTimeoutError
        assert result != RetryableError  # specific subclass, not the base


# ============================================================
# Phase 2: Self-Correction smart behavior
# ============================================================


class TestSelfCorrectionSmartBehavior:
    """Test that _call_with_json_object_mode only retries on format errors."""

    @patch("video_transcript_api.llm.llm.requests.post")
    def test_timeout_raises_immediately(self, mock_post):
        """Timeout should raise LLMCallError immediately, no Self-Correction."""
        from video_transcript_api.llm.llm import (
            _call_with_json_object_mode,
            LLMCallError,
        )

        mock_post.side_effect = requests.exceptions.ReadTimeout("Read timed out")

        config = {"llm": {"json_output": {"max_retries": 2}}}

        with pytest.raises(LLMCallError, match="timeout"):
            _call_with_json_object_mode(
                model="deepseek-chat",
                prompt="test prompt",
                schema={"type": "object", "properties": {}, "required": []},
                api_key="test-key",
                base_url="http://test/v1/chat/completions",
                config=config,
                system_prompt="test",
                max_retries=2,
                retry_delay=0,
                reasoning_effort=None,
                task_type="calibrate_chunk",
            )

        # Only 1 call, no Self-Correction retry
        assert mock_post.call_count == 1

    @patch("video_transcript_api.llm.llm.requests.post")
    def test_connection_error_raises_immediately(self, mock_post):
        """Connection error should raise immediately, no Self-Correction."""
        from video_transcript_api.llm.llm import (
            _call_with_json_object_mode,
            LLMCallError,
        )

        mock_post.side_effect = requests.exceptions.ConnectionError("Connection refused")

        config = {"llm": {"json_output": {"max_retries": 2}}}

        with pytest.raises(LLMCallError, match="[Cc]onnection"):
            _call_with_json_object_mode(
                model="deepseek-chat",
                prompt="test",
                schema={"type": "object", "properties": {}, "required": []},
                api_key="test-key",
                base_url="http://test/v1/chat/completions",
                config=config,
                system_prompt="test",
                max_retries=2,
                retry_delay=0,
                reasoning_effort=None,
                task_type="calibrate_chunk",
            )

        assert mock_post.call_count == 1

    @patch("video_transcript_api.llm.llm.requests.post")
    def test_truncation_raises_immediately(self, mock_post):
        """Truncated JSON output should raise immediately, no Self-Correction."""
        from video_transcript_api.llm.llm import (
            _call_with_json_object_mode,
            LLMCallError,
        )

        # Simulate truncated JSON response
        truncated_json = '{"calibrated_dialogs": [{"start_time": "00:00:01", "speaker": "A", "text": "hello'
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "choices": [{"message": {"content": truncated_json}}]
        }
        mock_post.return_value = mock_response

        config = {"llm": {"json_output": {"max_retries": 2}}}

        with pytest.raises(LLMCallError, match="[Tt]runcated"):
            _call_with_json_object_mode(
                model="deepseek-chat",
                prompt="test",
                schema={
                    "type": "object",
                    "properties": {"calibrated_dialogs": {"type": "array"}},
                    "required": ["calibrated_dialogs"],
                },
                api_key="test-key",
                base_url="http://test/v1/chat/completions",
                config=config,
                system_prompt="test",
                max_retries=2,
                retry_delay=0,
                reasoning_effort=None,
                task_type="calibrate_chunk",
            )

        # Only 1 call, no Self-Correction for truncation
        assert mock_post.call_count == 1

    @patch("video_transcript_api.llm.llm.requests.post")
    def test_json_format_error_allows_self_correction(self, mock_post):
        """JSON format errors (non-truncation) should allow Self-Correction."""
        from video_transcript_api.llm.llm import _call_with_json_object_mode

        # First call: invalid JSON (missing required field)
        bad_response = MagicMock()
        bad_response.status_code = 200
        bad_response.raise_for_status.return_value = None
        bad_response.json.return_value = {
            "choices": [{"message": {"content": '{"wrong_field": true}'}}]
        }

        # Second call: valid JSON
        good_response = MagicMock()
        good_response.status_code = 200
        good_response.raise_for_status.return_value = None
        good_response.json.return_value = {
            "choices": [{"message": {"content": '{"calibrated_dialogs": []}'}}]
        }

        mock_post.side_effect = [bad_response, good_response]

        config = {"llm": {"json_output": {"max_retries": 2}}}

        result = _call_with_json_object_mode(
            model="deepseek-chat",
            prompt="test",
            schema={
                "type": "object",
                "properties": {"calibrated_dialogs": {"type": "array"}},
                "required": ["calibrated_dialogs"],
            },
            api_key="test-key",
            base_url="http://test/v1/chat/completions",
            config=config,
            system_prompt="test",
            max_retries=2,
            retry_delay=0,
            reasoning_effort=None,
            task_type="calibrate_chunk",
        )

        assert result.success is True
        assert mock_post.call_count == 2  # Self-Correction worked


# ============================================================
# Phase 3: max_retries parameter respect
# ============================================================


class TestMaxRetriesParameterRespect:
    """Test that _call_with_json_object_mode respects max_retries parameter."""

    @patch("video_transcript_api.llm.llm.requests.post")
    def test_max_retries_zero_single_attempt(self, mock_post):
        """max_retries=0 should mean exactly 1 attempt, no retry."""
        from video_transcript_api.llm.llm import _call_with_json_object_mode

        # Return invalid JSON every time
        bad_response = MagicMock()
        bad_response.status_code = 200
        bad_response.raise_for_status.return_value = None
        bad_response.json.return_value = {
            "choices": [{"message": {"content": '{"wrong": true}'}}]
        }
        mock_post.return_value = bad_response

        config = {"llm": {"json_output": {"max_retries": 5}}}  # config says 5

        result = _call_with_json_object_mode(
            model="deepseek-chat",
            prompt="test",
            schema={
                "type": "object",
                "properties": {"data": {"type": "string"}},
                "required": ["data"],
            },
            api_key="test-key",
            base_url="http://test/v1/chat/completions",
            config=config,
            system_prompt="test",
            max_retries=0,  # parameter says 0 — should take priority
            retry_delay=0,
            reasoning_effort=None,
            task_type="calibrate_chunk",
        )

        assert result.success is False
        assert mock_post.call_count == 1  # only 1 attempt

    @patch("video_transcript_api.llm.llm.requests.post")
    def test_max_retries_none_uses_config(self, mock_post):
        """max_retries=None should fall back to config value."""
        from video_transcript_api.llm.llm import _call_with_json_object_mode

        bad_response = MagicMock()
        bad_response.status_code = 200
        bad_response.raise_for_status.return_value = None
        bad_response.json.return_value = {
            "choices": [{"message": {"content": '{"wrong": true}'}}]
        }
        mock_post.return_value = bad_response

        config = {"llm": {"json_output": {"max_retries": 1}}}

        result = _call_with_json_object_mode(
            model="deepseek-chat",
            prompt="test",
            schema={
                "type": "object",
                "properties": {"data": {"type": "string"}},
                "required": ["data"],
            },
            api_key="test-key",
            base_url="http://test/v1/chat/completions",
            config=config,
            system_prompt="test",
            max_retries=None,  # None means use config
            retry_delay=0,
            reasoning_effort=None,
            task_type="calibrate_chunk",
        )

        assert result.success is False
        assert mock_post.call_count == 2  # 1 + 1 retry from config


# ============================================================
# Phase 4: LLMClient layer differentiated retry
# ============================================================


class TestLLMClientDifferentiatedRetry:
    """Test that LLMClient does not retry on timeout/truncation."""

    def _make_client(self):
        from video_transcript_api.llm.core.llm_client import LLMClient

        return LLMClient(
            api_key="test-key",
            base_url="http://test/v1/chat/completions",
            max_retries=3,
            retry_delay=0,
        )

    @patch("video_transcript_api.llm.core.llm_client.time.sleep")
    def test_no_retry_on_truncation(self, mock_sleep):
        """Truncation errors should not be retried at LLMClient level."""
        client = self._make_client()

        with patch.object(client, "_actual_call") as mock_call:
            from video_transcript_api.llm.core.errors import LLMError

            mock_call.side_effect = Exception(
                "Structured output failed: json_object call failed: "
                "JSON parse failed: Unterminated string starting at: line 206"
            )

            with pytest.raises(TruncationError):
                client.call(
                    model="deepseek-chat",
                    system_prompt="test",
                    user_prompt="test",
                    task_type="calibrate_chunk",
                )

            assert mock_call.call_count == 1
            mock_sleep.assert_not_called()

    @patch("video_transcript_api.llm.core.llm_client.time.sleep")
    def test_no_retry_on_timeout(self, mock_sleep):
        """Timeout errors should not be retried at LLMClient level."""
        client = self._make_client()

        with patch.object(client, "_actual_call") as mock_call:
            mock_call.side_effect = Exception(
                "Read timed out. (read timeout=900)"
            )

            with pytest.raises(LLMTimeoutError):
                client.call(
                    model="deepseek-chat",
                    system_prompt="test",
                    user_prompt="test",
                    task_type="calibrate_chunk",
                )

            assert mock_call.call_count == 1
            mock_sleep.assert_not_called()

    @patch("video_transcript_api.llm.core.llm_client.time.sleep")
    def test_generic_error_still_retries(self, mock_sleep):
        """Non-timeout/truncation retryable errors should still be retried."""
        client = self._make_client()

        with patch.object(client, "_actual_call") as mock_call:
            mock_call.side_effect = Exception("Internal server error 500")

            with pytest.raises(Exception):
                client.call(
                    model="deepseek-chat",
                    system_prompt="test",
                    user_prompt="test",
                    task_type="calibrate_chunk",
                )

            # Should have retried: 1 initial + 3 retries = 4 calls
            assert mock_call.call_count == 4


# ============================================================
# Phase 5: Per-chunk time budget
# ============================================================


class TestChunkTimeBudget:
    """Test per-chunk time budget enforcement."""

    def test_chunk_time_budget_triggers_fallback(self):
        """When time budget is exhausted, chunk should fallback immediately."""
        from video_transcript_api.llm.core.config import LLMConfig

        config = LLMConfig(
            api_key="test",
            base_url="http://test",
            calibrate_model="test-model",
            summary_model="test-model",
        )
        assert hasattr(config, "chunk_time_budget")
        assert config.chunk_time_budget == 300  # default 5 minutes


# ============================================================
# Phase 6: Default timeout value
# ============================================================


class TestDefaultTimeout:
    """Test the default LLM timeout value."""

    def test_default_timeout_is_180(self):
        """DEFAULT_LLM_TIMEOUT should be 180 seconds."""
        from video_transcript_api.llm.llm import DEFAULT_LLM_TIMEOUT

        assert DEFAULT_LLM_TIMEOUT == 180
