"""Cross-library refusal detection contract tests (llm-compat v0.10.0).

Uses real llm_compat.refusal.detect_refusal — the library itself is never mocked.
Wire-format response dicts mirror what SyncLLMClient receives from providers.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from llm_compat import ContentPolicyError
from llm_compat.refusal import detect_refusal

from video_transcript_api.llm.core.errors import RetryableError, map_llm_compat_error


def _is_refusal(data: dict[str, Any]) -> bool:
    """Normalize detect_refusal return across llm-compat API versions."""
    result = detect_refusal(data)
    if isinstance(result, bool):
        return result
    return result.is_refusal


def _make_response(content: str | None, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"content": content},
                "finish_reason": finish_reason,
            }
        ]
    }


# >300 chars, legal/medical tone with repeated 违反 (production false-positive pattern)
LONG_LEGAL_SUMMARY = (
    "这是一段关于法律题材的长文总结，涵盖医疗合规与侵权责任。"
    + "该行为可能违反相关法律法规，涉及敏感话题的讨论需要谨慎对待。"
    * 15
)

# >300 chars, embed every legacy substring keyword from pre-v0.10.0 table
_LEGACY_KEYWORDS = ["无法提供", "涉及敏感", "不适合讨论", "违反"]
LONG_ALL_LEGACY_KEYWORDS = (
    "法律医疗题材总结。"
    + "。".join(
        f"段落{i}讨论{k}相关问题"
        for i in range(30)
        for k in _LEGACY_KEYWORDS
    )
)


REFUSAL_CONTRACT_CASES: list[tuple[str, str | None, str, bool, str]] = [
    (
        "long_with_wei_fan",
        LONG_LEGAL_SUMMARY,
        "stop",
        False,
        "long legal/medical summary containing wei_fan",
    ),
    (
        "long_all_legacy_keywords",
        LONG_ALL_LEGACY_KEYWORDS,
        "stop",
        False,
        "long text with all legacy keyword substrings",
    ),
    (
        "short_cn_refusal",
        "抱歉，我无法提供这方面的帮助。",
        "stop",
        True,
        "short CN first-person refusal at sentence start",
    ),
    (
        "short_en_refusal",
        "I'm sorry, but I cannot assist with that request.",
        "stop",
        True,
        "short EN first-person refusal",
    ),
    (
        "pseudo_refusal_with_turn",
        "我不能协助这个请求，但可以换个角度说明。",
        "stop",
        False,
        "pseudo refusal with concessive turn (but/however)",
    ),
    (
        "content_filter",
        "ok",
        "content_filter",
        True,
        "provider finish_reason content_filter",
    ),
    (
        "malformed_none_content",
        None,
        "stop",
        True,
        "None content with finish_reason stop",
    ),
]


@pytest.mark.parametrize(
    "case_id,content,finish_reason,expected,detail",
    REFUSAL_CONTRACT_CASES,
    ids=[c[0] for c in REFUSAL_CONTRACT_CASES],
)
def test_detect_refusal_contract(
    case_id: str,
    content: str | None,
    finish_reason: str,
    expected: bool,
    detail: str,
) -> None:
    """Table-driven contract: real detect_refusal on wire-format response dicts."""
    if case_id.startswith("long"):
        text = content if isinstance(content, str) else ""
        assert len(text) > 300, f"case {case_id} requires >300 chars, got {len(text)}"

    data = _make_response(content, finish_reason)
    actual = _is_refusal(data)
    assert actual == expected, f"case {case_id}: {detail}"


class TestSyncLLMClientRefusalPolicy:
    """Construction-time on_all_refused must stay raise (not v0.10.0 default)."""

    @pytest.fixture(autouse=True)
    def _restore_llm_module_globals(self):
        """Restore module-level globals mutated by set_default_config().

        set_default_config() writes _default_config and _sync_client. Without
        this fixture the mock client built here leaks into any later test that
        reads the module default instead of setting its own, making results
        depend on collection order.
        """
        from video_transcript_api.llm import llm as llm_mod

        saved_config = llm_mod._default_config
        saved_client = llm_mod._sync_client
        try:
            yield
        finally:
            llm_mod._default_config = saved_config
            llm_mod._sync_client = saved_client

    @patch("video_transcript_api.llm.llm.SyncLLMClient")
    def test_on_all_refused_raise_passed_at_construction(self, mock_client_cls) -> None:
        from video_transcript_api.llm.llm import set_default_config

        config = {
            "llm": {
                "api_key": "test-key",
                "base_url": "https://api.test.com/v1",
            },
        }
        set_default_config(config)
        call_kwargs = mock_client_cls.call_args[1]
        assert call_kwargs.get("on_all_refused") == "raise"


class TestContentPolicyErrorChain:
    """All-refused terminal path: ContentPolicyError -> map_llm_compat_error -> RetryableError."""

    def test_content_policy_error_maps_to_retryable(self) -> None:
        err = ContentPolicyError(
            "All models refused",
            attempted_models=["deepseek-v4", "gemini-3-flash"],
            raw_content="I cannot assist",
            original_model="deepseek-v4",
        )
        result = map_llm_compat_error(err)
        assert isinstance(result, RetryableError)
