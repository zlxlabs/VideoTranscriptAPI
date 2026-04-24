"""Tests for llm/providers.py — unified thinking abstraction across OpenAI/Gemini/DeepSeek.

Covers (per ENG review test matrix):
  - detect_provider: 4+ families × legacy names + fallback
  - build_request_payload: deep merge + translation per family
  - describe_from_payload: derive from FINAL payload (not inputs)
  - Regression: existing extra_body preserved, not overwritten
"""
import pytest

from video_transcript_api.llm import providers


class TestDetectProvider:
    @pytest.mark.parametrize(
        "model,expected_family",
        [
            # DeepSeek V4 + legacy
            ("deepseek-v4-flash", "deepseek"),
            ("deepseek-v4-pro", "deepseek"),
            ("deepseek-chat", "deepseek"),
            ("deepseek-reasoner", "deepseek"),
            # Gemini 2.5 vs 3 distinction matters for disable semantics
            ("gemini-2.5-flash", "gemini_25"),
            ("gemini-2.5-pro", "gemini_25"),
            ("gemini-3-flash-preview", "gemini_3"),
            ("gemini-3-pro", "gemini_3"),
            ("gemini-3.1-pro", "gemini_3"),
            # OpenAI GPT family split
            ("gpt-5", "openai_gpt5"),
            ("gpt-5-mini", "openai_gpt5"),
            ("gpt-5.4-nano", "openai_gpt5"),
            ("gpt-4o", "openai_gpt4"),
            ("gpt-4.1-mini", "openai_gpt4"),
            # OpenAI o-series
            ("o1-pro", "openai_o"),
            ("o3-mini", "openai_o"),
            # Fallback
            ("qwen-turbo", "openai"),
            ("glm-4", "openai"),
        ],
    )
    def test_family_recognition(self, model, expected_family):
        assert providers.detect_provider(model) == expected_family

    def test_case_insensitive(self):
        assert providers.detect_provider("DeepSeek-V4-Flash") == "deepseek"
        assert providers.detect_provider("Gemini-3-Flash") == "gemini_3"

    def test_empty_model_returns_openai_with_warn(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            assert providers.detect_provider("") == "openai"
        assert "invalid" in caplog.text.lower() or "unknown" in caplog.text.lower()

    def test_none_model_returns_openai_defensive(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            assert providers.detect_provider(None) == "openai"

    def test_custom_patterns_override_defaults(self):
        custom = (("weirdbrand-*", "deepseek"),)
        # Without custom, weirdbrand falls to openai; with custom, it's deepseek
        assert providers.detect_provider("weirdbrand-v1", custom) == "deepseek"
        assert providers.detect_provider("weirdbrand-v1") == "openai"


class TestBuildRequestPayloadDeepSeek:
    """DeepSeek V4: disabled -> extra_body.thinking.type, effort -> reasoning_effort."""

    def _base(self, model="deepseek-v4-flash"):
        return {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": False}

    def test_disabled_sets_extra_body(self):
        payload = providers.build_request_payload(
            "deepseek-v4-flash", "disabled", self._base()
        )
        assert payload["extra_body"] == {"thinking": {"type": "disabled"}}
        assert "reasoning_effort" not in payload

    def test_high_sets_reasoning_effort(self):
        payload = providers.build_request_payload(
            "deepseek-v4-flash", "high", self._base()
        )
        assert payload["reasoning_effort"] == "high"
        assert "extra_body" not in payload

    def test_max_and_xhigh_passthrough(self):
        for effort in ("max", "xhigh"):
            payload = providers.build_request_payload(
                "deepseek-v4-flash", effort, self._base()
            )
            assert payload["reasoning_effort"] == effort

    def test_minimal_not_supported_falls_back(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            payload = providers.build_request_payload(
                "deepseek-v4-flash", "minimal", self._base()
            )
        # DeepSeek 不接受 minimal，应 clamp 到最低接受值 (low) 或警告
        assert payload.get("reasoning_effort") in ("low", None) or "extra_body" in payload

    def test_none_effort_sends_no_extra_fields(self):
        base = self._base()
        payload = providers.build_request_payload("deepseek-v4-flash", None, base)
        assert "reasoning_effort" not in payload
        assert "extra_body" not in payload
        assert payload["model"] == "deepseek-v4-flash"
        assert payload["messages"] == base["messages"]


class TestBuildRequestPayloadGemini25:
    """Gemini 2.5: disabled -> reasoning_effort=none, effort passthrough."""

    def _base(self):
        return {"model": "gemini-2.5-flash", "messages": [], "stream": False}

    def test_disabled_uses_reasoning_effort_none(self):
        payload = providers.build_request_payload(
            "gemini-2.5-flash", "disabled", self._base()
        )
        assert payload["reasoning_effort"] == "none"
        assert "extra_body" not in payload

    def test_high_passthrough(self):
        payload = providers.build_request_payload(
            "gemini-2.5-flash", "high", self._base()
        )
        assert payload["reasoning_effort"] == "high"

    def test_max_clamps_to_high(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            payload = providers.build_request_payload(
                "gemini-2.5-pro", "max", self._base()
            )
        assert payload["reasoning_effort"] == "high"


class TestBuildRequestPayloadGemini3:
    """Gemini 3: disabled -> minimal (best effort); Pro cannot disable at all."""

    def _base(self, model):
        return {"model": model, "messages": [], "stream": False}

    def test_disabled_falls_back_to_minimal(self):
        payload = providers.build_request_payload(
            "gemini-3-flash-preview", "disabled", self._base("gemini-3-flash-preview")
        )
        assert payload["reasoning_effort"] == "minimal"

    def test_minimal_passthrough(self):
        payload = providers.build_request_payload(
            "gemini-3-pro", "minimal", self._base("gemini-3-pro")
        )
        assert payload["reasoning_effort"] == "minimal"

    def test_high_passthrough(self):
        payload = providers.build_request_payload(
            "gemini-3-pro", "high", self._base("gemini-3-pro")
        )
        assert payload["reasoning_effort"] == "high"


class TestBuildRequestPayloadOpenAIGPT5:
    """GPT-5: minimal/low/medium/high supported."""

    def _base(self, model="gpt-5"):
        return {"model": model, "messages": []}

    def test_minimal_passthrough(self):
        payload = providers.build_request_payload("gpt-5", "minimal", self._base())
        assert payload["reasoning_effort"] == "minimal"

    def test_high_passthrough(self):
        payload = providers.build_request_payload("gpt-5", "high", self._base())
        assert payload["reasoning_effort"] == "high"

    def test_disabled_falls_back_to_minimal(self):
        payload = providers.build_request_payload("gpt-5-mini", "disabled", self._base("gpt-5-mini"))
        assert payload["reasoning_effort"] == "minimal"

    def test_max_clamps_to_high(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            payload = providers.build_request_payload("gpt-5", "max", self._base())
        assert payload["reasoning_effort"] == "high"


class TestBuildRequestPayloadOpenAIGPT4:
    """GPT-4.x: does NOT support reasoning_effort at all; any value dropped."""

    def _base(self):
        return {"model": "gpt-4o", "messages": []}

    def test_any_effort_dropped(self, caplog):
        import logging
        for effort in ("low", "medium", "high", "minimal", "max", "disabled"):
            with caplog.at_level(logging.WARNING):
                payload = providers.build_request_payload("gpt-4o", effort, self._base())
            assert "reasoning_effort" not in payload
            assert "extra_body" not in payload


class TestBuildRequestPayloadDeepMerge:
    """REGRESSION: existing extra_body in base_payload must be preserved, not overwritten."""

    def test_preserves_existing_extra_body(self):
        # Someone already set extra_body.custom_field; dispatcher must not obliterate it
        base = {
            "model": "deepseek-v4-flash",
            "messages": [],
            "extra_body": {"custom_field": "value"},
        }
        payload = providers.build_request_payload("deepseek-v4-flash", "disabled", base)
        assert payload["extra_body"]["custom_field"] == "value"
        assert payload["extra_body"]["thinking"]["type"] == "disabled"

    def test_does_not_mutate_input(self):
        base = {"model": "deepseek-v4-flash", "messages": []}
        original = dict(base)
        providers.build_request_payload("deepseek-v4-flash", "disabled", base)
        assert base == original


class TestDescribeFromPayload:
    """describe derives from FINAL payload, not from (model, effort) inputs."""

    def test_deepseek_disabled(self):
        payload = {
            "model": "deepseek-v4-flash",
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        desc = providers.describe_from_payload(payload)
        assert desc["provider"] == "deepseek"
        assert desc["model"] == "deepseek-v4-flash"
        assert desc["thinking_mode"] == "disabled"

    def test_deepseek_high(self):
        payload = {"model": "deepseek-v4-flash", "reasoning_effort": "high"}
        desc = providers.describe_from_payload(payload)
        assert desc["thinking_mode"] == "high"

    def test_deepseek_default_when_no_effort(self):
        # DeepSeek 默认 enabled@high；describe 应反映此事实（source=model_default）
        payload = {"model": "deepseek-v4-flash"}
        desc = providers.describe_from_payload(payload)
        assert desc["thinking_source"] == "model_default"
        assert "default" in desc["thinking_mode"].lower() or desc["thinking_mode"] in ("enabled", "high")

    def test_gpt4_na(self):
        payload = {"model": "gpt-4o"}
        desc = providers.describe_from_payload(payload)
        assert desc["thinking_mode"] == "n/a"

    def test_only_whitelisted_fields_returned(self):
        # 不能泄漏 api_key 等任何其他字段
        payload = {
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "secret prompt"}],
            "reasoning_effort": "high",
        }
        desc = providers.describe_from_payload(payload)
        assert set(desc.keys()) == {"provider", "model", "thinking_mode", "thinking_source"}
        assert "secret" not in str(desc).lower()
