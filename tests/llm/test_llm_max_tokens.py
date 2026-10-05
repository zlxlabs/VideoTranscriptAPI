"""Tests for max_tokens propagation into llm-compat chat payload."""

from unittest.mock import MagicMock, patch

import pytest

from video_transcript_api.llm.llm import call_llm_api, set_default_config


class _FakeChatResult:
  def __init__(self, content: str):
    self._content = content
    self.fallback_from = None
    self.model = "test-model"

  def __str__(self) -> str:
    return self._content


@pytest.fixture(autouse=True)
def _init_llm_client():
  config = {
    "llm": {
      "api_key": "test-key",
      "base_url": "https://api.test.com/v1",
      "text_output": {"max_retries": 0},
    }
  }
  set_default_config(config)
  yield


class TestMaxTokensPayload:
  @patch("video_transcript_api.llm.llm.get_sync_client")
  def test_none_max_tokens_omits_field(self, mock_get_client):
    client = MagicMock()
    client.chat.return_value = _FakeChatResult("ok")
    mock_get_client.return_value = client

    call_llm_api(
      model="test-model",
      prompt="user",
      system_prompt="system",
      task_type="summary",
    )

    _, kwargs = client.chat.call_args
    assert "max_tokens" not in kwargs

  @patch("video_transcript_api.llm.llm.get_sync_client")
  def test_max_tokens_forwarded_when_set(self, mock_get_client):
    client = MagicMock()
    client.chat.return_value = _FakeChatResult("ok")
    mock_get_client.return_value = client

    call_llm_api(
      model="test-model",
      prompt="user",
      system_prompt="system",
      task_type="summary",
      max_tokens=6750,
    )

    _, kwargs = client.chat.call_args
    assert kwargs.get("max_tokens") == 6750

  @patch("video_transcript_api.llm.core.llm_client.call_llm_api")
  def test_llm_client_forwards_max_tokens(self, mock_call):
    from video_transcript_api.llm.core.llm_client import LLMClient

    mock_call.return_value = "text"
    client = LLMClient(api_key="k", base_url="http://test")
    client.call(
      model="m",
      system_prompt="s",
      user_prompt="u",
      max_tokens=12000,
    )
    assert mock_call.call_args.kwargs.get("max_tokens") == 12000

  @patch("video_transcript_api.llm.core.llm_client.call_llm_api")
  def test_llm_client_none_max_tokens(self, mock_call):
    from video_transcript_api.llm.core.llm_client import LLMClient

    mock_call.return_value = "text"
    client = LLMClient(api_key="k", base_url="http://test")
    client.call(model="m", system_prompt="s", user_prompt="u")
    assert mock_call.call_args.kwargs.get("max_tokens") is None
