"""Integration: verify _call_with_* functions pass correct params to SyncLLMClient.

Post llm-compat migration, provider translation (reasoning_effort -> thinking params)
is handled by llm-compat internally. These tests verify the project code passes the
right model, messages, reasoning_effort, and response_format to SyncLLMClient.chat().
"""
from dataclasses import dataclass
from unittest.mock import patch, MagicMock

import pytest


@dataclass
class _FakeChatResult:
    content: str = ""
    fallback_from: str = None
    model: str = "test"
    def __str__(self):
        return self.content


class TestTextOutputPassesCorrectParams:
    @patch("video_transcript_api.llm.llm.get_sync_client")
    def test_passes_reasoning_effort(self, mock_get_client):
        from video_transcript_api.llm.llm import _call_with_text_output

        mock_client = MagicMock()
        mock_client.chat.return_value = _FakeChatResult(content="hello")
        mock_get_client.return_value = mock_client

        _call_with_text_output(
            model="deepseek-v4-flash",
            prompt="hi",
            system_prompt="sys",
            reasoning_effort="disabled",
            task_type="test",
        )

        call_kwargs = mock_client.chat.call_args
        assert call_kwargs.kwargs["reasoning_effort"] == "disabled"
        assert call_kwargs.args[0] == "deepseek-v4-flash"

    @patch("video_transcript_api.llm.llm.get_sync_client")
    def test_none_effort_passed_as_none(self, mock_get_client):
        from video_transcript_api.llm.llm import _call_with_text_output

        mock_client = MagicMock()
        mock_client.chat.return_value = _FakeChatResult(content="hi")
        mock_get_client.return_value = mock_client

        _call_with_text_output(
            model="deepseek-v4-flash",
            prompt="hi",
            system_prompt="sys",
            reasoning_effort=None,
            task_type="test",
        )

        call_kwargs = mock_client.chat.call_args
        assert call_kwargs.kwargs["reasoning_effort"] is None


class TestJsonSchemaPassesResponseFormat:
    @patch("video_transcript_api.llm.llm.get_sync_client")
    def test_response_format_passed(self, mock_get_client):
        from video_transcript_api.llm.llm import _call_with_json_schema_mode

        mock_client = MagicMock()
        mock_client.chat.return_value = _FakeChatResult(content='{"ok": true}')
        mock_get_client.return_value = mock_client

        _call_with_json_schema_mode(
            model="deepseek-v4-flash",
            prompt="hi",
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            system_prompt="sys",
            reasoning_effort="disabled",
            task_type="test",
        )

        call_kwargs = mock_client.chat.call_args
        rf = call_kwargs.kwargs["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["strict"] is True


class TestJsonObjectPassesResponseFormat:
    @patch("video_transcript_api.llm.llm.get_sync_client")
    def test_response_format_json_object_passed(self, mock_get_client):
        from video_transcript_api.llm.llm import _call_with_json_object_mode

        mock_client = MagicMock()
        mock_client.chat.return_value = _FakeChatResult(content='{"ok": true}')
        mock_get_client.return_value = mock_client

        _call_with_json_object_mode(
            model="deepseek-v4-flash",
            prompt="hi",
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            config={"llm": {"json_output": {"max_retries": 0}}},
            system_prompt="sys",
            reasoning_effort="disabled",
            task_type="test",
        )

        call_kwargs = mock_client.chat.call_args
        rf = call_kwargs.kwargs["response_format"]
        assert rf["type"] == "json_object"
