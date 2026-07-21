"""
Unit tests for _call_with_text_output empty-response retry + thinking downgrade.

Covers:
- Empty -> non-empty sequence: retry succeeds, second attempt forces
  reasoning_effort="disabled".
- Persistently empty responses: returns "" after max_retries+1 attempts.
- Non-empty on first attempt: exactly one call, original effort preserved.
- llm.text_output.max_retries=0: no retry on empty response.
- Missing text_output config: defaults to 2 retries.

The real _call_with_text_output code path is exercised by patching
llm.get_sync_client() to return a fake SyncLLMClient whose .chat() returns
controllable llm_compat ChatResult objects (same idiom as
test_llm_usage_capture.py). time.sleep and _record_chat_usage are patched
to keep the tests fast and side-effect free.

All console output must be in English only (no emoji, no Chinese).
"""

from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

from llm_compat import ChatResult

from video_transcript_api.llm.llm import _call_with_text_output


def _fake_sync_client(*chat_results: ChatResult) -> MagicMock:
    """Build a MagicMock standing in for llm_compat.SyncLLMClient."""
    fake = MagicMock()
    fake.chat.side_effect = list(chat_results)
    return fake


def _chat_result(content: str) -> ChatResult:
    return ChatResult(content=content, model="deepseek-v4-pro")


def _run(
    fake_client: MagicMock,
    config: Optional[Dict[str, Any]],
    reasoning_effort: Optional[str] = "high",
) -> str:
    with patch(
        "video_transcript_api.llm.llm.get_sync_client",
        return_value=fake_client,
    ), patch(
        "video_transcript_api.llm.llm._record_chat_usage"
    ), patch(
        "video_transcript_api.llm.llm.time.sleep"
    ):
        return _call_with_text_output(
            model="deepseek-v4-pro",
            prompt="p",
            system_prompt="s",
            reasoning_effort=reasoning_effort,
            task_type="summary",
            config=config,
        )


def _efforts(fake_client: MagicMock) -> list:
    return [c.kwargs["reasoning_effort"] for c in fake_client.chat.call_args_list]


class TestEmptyResponseRetry:
    def test_empty_then_nonempty_retries_and_disables_thinking(self):
        """First attempt empty, second non-empty: retry succeeds and the
        second attempt forces reasoning_effort='disabled'."""
        fake = _fake_sync_client(_chat_result(""), _chat_result("summary text"))

        result = _run(fake, {"llm": {"text_output": {"max_retries": 2}}})

        assert result == "summary text"
        assert fake.chat.call_count == 2
        assert _efforts(fake) == ["high", "disabled"]

    def test_persistent_empty_returns_empty_after_all_attempts(self):
        """All attempts empty: returns "" with exactly max_retries+1 calls,
        every retry downgraded to 'disabled'."""
        fake = _fake_sync_client(_chat_result(""), _chat_result("  "), _chat_result(""))

        result = _run(fake, {"llm": {"text_output": {"max_retries": 2}}})

        assert result == ""
        assert fake.chat.call_count == 3  # max_retries + 1
        assert _efforts(fake) == ["high", "disabled", "disabled"]

    def test_nonempty_first_attempt_does_not_retry(self):
        """Non-empty on the first attempt: exactly one call, original effort."""
        fake = _fake_sync_client(_chat_result("summary text"))

        result = _run(fake, {"llm": {"text_output": {"max_retries": 2}}})

        assert result == "summary text"
        assert fake.chat.call_count == 1
        assert _efforts(fake) == ["high"]

    def test_max_retries_zero_disables_retry(self):
        """llm.text_output.max_retries=0: empty response returns "" immediately."""
        fake = _fake_sync_client(_chat_result(""))

        result = _run(fake, {"llm": {"text_output": {"max_retries": 0}}})

        assert result == ""
        assert fake.chat.call_count == 1

    def test_missing_text_output_config_defaults_to_two_retries(self):
        """No text_output config (or config=None): falls back to default of
        2 retries, i.e. up to 3 attempts on persistent empty responses."""
        for config in (None, {}, {"llm": {}}):
            fake = _fake_sync_client(
                _chat_result(""), _chat_result(""), _chat_result("")
            )

            result = _run(fake, config)

            assert result == ""
            assert fake.chat.call_count == 3
