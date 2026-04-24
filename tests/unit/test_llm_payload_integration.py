"""Integration: verify 3 _call_with_* functions construct correct payloads via providers.

Mocks requests.post and asserts the actual JSON body sent to the server.
This catches the class of bugs where providers.py is correct but call-sites
don't route through it.
"""
from unittest.mock import patch, MagicMock

import pytest

from video_transcript_api.llm.llm import (
    _call_with_text_output,
    _call_with_json_object_mode,
    _call_with_json_schema_mode,
)


def _mock_ok_response(content='{"ok": true}'):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
    }
    return resp


class TestTextOutputPayloadShape:
    @patch("video_transcript_api.llm.llm.requests.post")
    def test_deepseek_disabled_uses_extra_body(self, mock_post):
        mock_post.return_value = _mock_ok_response("hello")
        _call_with_text_output(
            model="deepseek-v4-flash",
            prompt="hi",
            api_key="k",
            base_url="http://fake/v1/chat/completions",
            system_prompt="sys",
            max_retries=0,
            retry_delay=0,
            reasoning_effort="disabled",
            task_type="test",
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["extra_body"] == {"thinking": {"type": "disabled"}}
        assert "reasoning_effort" not in body

    @patch("video_transcript_api.llm.llm.requests.post")
    def test_deepseek_high_uses_reasoning_effort(self, mock_post):
        mock_post.return_value = _mock_ok_response("hi")
        _call_with_text_output(
            model="deepseek-v4-flash",
            prompt="hi",
            api_key="k",
            base_url="http://fake/v1/chat/completions",
            system_prompt="sys",
            max_retries=0,
            retry_delay=0,
            reasoning_effort="high",
            task_type="test",
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["reasoning_effort"] == "high"
        assert "extra_body" not in body

    @patch("video_transcript_api.llm.llm.requests.post")
    def test_gpt4_drops_effort(self, mock_post):
        mock_post.return_value = _mock_ok_response("hi")
        _call_with_text_output(
            model="gpt-4o",
            prompt="hi",
            api_key="k",
            base_url="http://fake",
            system_prompt="sys",
            max_retries=0,
            retry_delay=0,
            reasoning_effort="high",
            task_type="test",
        )
        body = mock_post.call_args.kwargs["json"]
        # GPT-4.x 不支持 reasoning_effort，必须丢弃
        assert "reasoning_effort" not in body
        assert "extra_body" not in body

    @patch("video_transcript_api.llm.llm.requests.post")
    def test_none_effort_no_thinking_fields(self, mock_post):
        mock_post.return_value = _mock_ok_response("hi")
        _call_with_text_output(
            model="deepseek-v4-flash",
            prompt="hi",
            api_key="k",
            base_url="http://fake",
            system_prompt="sys",
            max_retries=0,
            retry_delay=0,
            reasoning_effort=None,
            task_type="test",
        )
        body = mock_post.call_args.kwargs["json"]
        assert "reasoning_effort" not in body
        assert "extra_body" not in body


class TestJsonSchemaPayloadShape:
    @patch("video_transcript_api.llm.llm.requests.post")
    def test_deepseek_disabled_routes_through_providers(self, mock_post):
        mock_post.return_value = _mock_ok_response('{"ok": true}')
        _call_with_json_schema_mode(
            model="deepseek-v4-flash",
            prompt="hi",
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            api_key="k",
            base_url="http://fake",
            system_prompt="sys",
            max_retries=0,
            retry_delay=0,
            reasoning_effort="disabled",
            task_type="test",
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["extra_body"] == {"thinking": {"type": "disabled"}}
        # 原 response_format 应保留（深合并不覆盖）
        assert body["response_format"]["type"] == "json_schema"


class TestJsonObjectPayloadShape:
    @patch("video_transcript_api.llm.llm.requests.post")
    def test_deepseek_disabled_routes_through_providers(self, mock_post):
        mock_post.return_value = _mock_ok_response('{"ok": true}')
        _call_with_json_object_mode(
            model="deepseek-v4-flash",
            prompt="hi",
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            api_key="k",
            base_url="http://fake",
            config={"llm": {"json_output": {"max_retries": 0}}},
            system_prompt="sys",
            max_retries=0,
            retry_delay=0,
            reasoning_effort="disabled",
            task_type="test",
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["extra_body"] == {"thinking": {"type": "disabled"}}
        assert body["response_format"]["type"] == "json_object"
