"""Calibration retry strategy tests

Tests for the calibration retry optimization:
- Error classification (timeout, truncation, format errors)
- Self-Correction smart behavior (only for format errors)
- LLMClient error mapping (llm-compat errors -> project errors)
- Per-chunk time budget
- Default timeout value
"""

import json
import time
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from video_transcript_api.llm.core.errors import (
    classify_error,
    map_llm_compat_error,
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
        error = Exception(
            "HTTPConnectionPool(host='100.107.95.24', port=3001): "
            "Read timed out. (read timeout=900)"
        )
        assert classify_error(error) == LLMTimeoutError

    def test_classify_connect_timeout(self):
        error = Exception("Connection timed out")
        assert classify_error(error) == LLMTimeoutError

    def test_classify_generic_timeout(self):
        error = Exception("Request timeout after 120s")
        assert classify_error(error) == LLMTimeoutError

    def test_classify_unterminated_string_as_truncation(self):
        error = Exception(
            "Structured output failed: json_object call failed: "
            "JSON parse failed: Unterminated string starting at: "
            "line 206 column 21 (char 9793)"
        )
        assert classify_error(error) == TruncationError

    def test_classify_unexpected_end_as_truncation(self):
        error = Exception("JSON parse failed: Unexpected end of JSON input")
        assert classify_error(error) == TruncationError

    def test_subclass_backward_compatible(self):
        assert issubclass(LLMTimeoutError, RetryableError)
        assert issubclass(TruncationError, RetryableError)

    def test_fatal_errors_unchanged(self):
        assert classify_error(Exception("401 Unauthorized")) == FatalError
        assert classify_error(Exception("403 Forbidden")) == FatalError
        assert classify_error(Exception("404 Not Found")) == FatalError

    def test_generic_retryable_unchanged(self):
        assert classify_error(Exception("Internal server error 500")) == RetryableError

    def test_timeout_takes_priority_over_retryable(self):
        error = Exception("Read timed out")
        result = classify_error(error)
        assert result == LLMTimeoutError
        assert result != RetryableError


# ============================================================
# Phase 2: Self-Correction smart behavior (via llm-compat)
# ============================================================


@dataclass
class _FakeChatResult:
    """Minimal ChatResult stand-in for tests."""
    content: str = ""
    fallback_from: str = None
    model: str = "test"
    def __str__(self):
        return self.content


class TestSelfCorrectionSmartBehavior:
    """Test that _call_with_json_object_mode only retries on format errors."""

    @patch("video_transcript_api.llm.llm.get_sync_client")
    def test_truncation_raises_immediately(self, mock_get_client):
        """Truncated JSON output should raise immediately, no Self-Correction."""
        from video_transcript_api.llm.llm import (
            _call_with_json_object_mode,
            LLMCallError,
        )

        truncated_json = '{"calibrated_dialogs": [{"start_time": "00:00:01", "speaker": "A", "text": "hello'
        mock_client = MagicMock()
        mock_client.chat.return_value = _FakeChatResult(content=truncated_json)
        mock_get_client.return_value = mock_client

        config = {"llm": {"json_output": {"max_retries": 2}}}

        with pytest.raises(LLMCallError, match="[Tt]runcated"):
            _call_with_json_object_mode(
                model="deepseek-v4-flash",
                prompt="test",
                schema={
                    "type": "object",
                    "properties": {"calibrated_dialogs": {"type": "array"}},
                    "required": ["calibrated_dialogs"],
                },
                config=config,
                system_prompt="test",
                reasoning_effort=None,
                task_type="calibrate_chunk",
            )

        assert mock_client.chat.call_count == 1

    @patch("video_transcript_api.llm.llm.get_sync_client")
    def test_json_format_error_allows_self_correction(self, mock_get_client):
        """JSON format errors (non-truncation) should allow Self-Correction."""
        from video_transcript_api.llm.llm import _call_with_json_object_mode

        mock_client = MagicMock()
        mock_client.chat.side_effect = [
            _FakeChatResult(content='{"wrong_field": true}'),
            _FakeChatResult(content='{"calibrated_dialogs": []}'),
        ]
        mock_get_client.return_value = mock_client

        config = {"llm": {"json_output": {"max_retries": 2}}}

        result = _call_with_json_object_mode(
            model="deepseek-v4-flash",
            prompt="test",
            schema={
                "type": "object",
                "properties": {"calibrated_dialogs": {"type": "array"}},
                "required": ["calibrated_dialogs"],
            },
            config=config,
            system_prompt="test",
            reasoning_effort=None,
            task_type="calibrate_chunk",
        )

        assert result.success is True
        assert mock_client.chat.call_count == 2

    @patch("video_transcript_api.llm.llm.get_sync_client")
    def test_config_max_retries_respected(self, mock_get_client):
        """json_output.max_retries from config controls Self-Correction attempts."""
        from video_transcript_api.llm.llm import _call_with_json_object_mode

        mock_client = MagicMock()
        mock_client.chat.return_value = _FakeChatResult(content='{"wrong": true}')
        mock_get_client.return_value = mock_client

        config = {"llm": {"json_output": {"max_retries": 1}}}

        result = _call_with_json_object_mode(
            model="deepseek-v4-flash",
            prompt="test",
            schema={
                "type": "object",
                "properties": {"data": {"type": "string"}},
                "required": ["data"],
            },
            config=config,
            system_prompt="test",
            reasoning_effort=None,
            task_type="calibrate_chunk",
        )

        assert result.success is False
        assert mock_client.chat.call_count == 2  # 1 + 1 retry from config


# ============================================================
# Phase 4: LLMClient error mapping (llm-compat -> project)
# ============================================================


class TestLLMClientErrorMapping:
    """Test that map_llm_compat_error maps llm-compat errors correctly."""

    def test_llm_compat_timeout_mapped(self):
        from llm_compat import TimeoutError as LCTimeout
        err = LCTimeout("timed out")
        mapped = map_llm_compat_error(err)
        assert isinstance(mapped, LLMTimeoutError)

    def test_llm_compat_fatal_mapped(self):
        from llm_compat import FatalError as LCFatal
        err = LCFatal("401 Unauthorized")
        mapped = map_llm_compat_error(err)
        assert isinstance(mapped, FatalError)

    def test_llm_compat_json_parse_mapped(self):
        from llm_compat import JSONParseError
        err = JSONParseError("bad json", raw_content="{", model="test", request_id="r1")
        mapped = map_llm_compat_error(err)
        assert isinstance(mapped, TruncationError)

    def test_generic_error_mapped_to_retryable(self):
        err = RuntimeError("something broke")
        mapped = map_llm_compat_error(err)
        assert isinstance(mapped, RetryableError)


# ============================================================
# Phase 5: Per-chunk time budget
# ============================================================


class TestChunkTimeBudget:
    """Test per-chunk time budget enforcement."""

    def test_chunk_time_budget_triggers_fallback(self):
        from video_transcript_api.llm.core.config import LLMConfig

        config = LLMConfig(
            api_key="test",
            base_url="http://test",
            calibrate_model="test-model",
            summary_model="test-model",
        )
        assert hasattr(config, "chunk_time_budget")
        assert config.chunk_time_budget == 300


# ============================================================
# Phase 6: Default timeout value
# ============================================================


class TestDefaultTimeout:
    """Test the default LLM timeout value."""

    def test_default_timeout_is_180(self):
        from video_transcript_api.llm.llm import DEFAULT_LLM_TIMEOUT
        assert DEFAULT_LLM_TIMEOUT == 180
