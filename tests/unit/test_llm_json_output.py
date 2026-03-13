"""
Unit tests for LLM JSON structured output functionality.

Tests the new JSON Schema structured output feature including:
- Model-based JSON mode selection
- Required field validation
- JSON extraction from responses
- StructuredResult handling
"""
import pytest
from unittest.mock import patch, MagicMock

from video_transcript_api.llm.llm import (
    _get_json_mode_for_model,
    _validate_required_fields,
    _extract_json_from_response,
    _schema_to_prompt_instruction,
    set_default_config,
    get_default_config,
    reset_llm_stats,
    get_llm_stats,
    StructuredResult,
)


class TestGetJsonModeForModel:
    """Tests for _get_json_mode_for_model function."""

    def test_deepseek_uses_json_object(self):
        """DeepSeek models should use json_object mode."""
        config = {
            "llm": {
                "json_output": {
                    "mode_by_model": {
                        "deepseek*": "json_object",
                        "*": "json_schema"
                    },
                    "enable_fallback": True
                }
            }
        }

        assert _get_json_mode_for_model("deepseek-chat", config) == "json_object"
        assert _get_json_mode_for_model("deepseek-coder", config) == "json_object"
        assert _get_json_mode_for_model("DEEPSEEK-CHAT", config) == "json_object"

    def test_gpt_uses_json_schema(self):
        """GPT models should use json_schema mode."""
        config = {
            "llm": {
                "json_output": {
                    "mode_by_model": {
                        "deepseek*": "json_object",
                        "*": "json_schema"
                    },
                    "enable_fallback": True
                }
            }
        }

        assert _get_json_mode_for_model("gpt-4o", config) == "json_schema"
        assert _get_json_mode_for_model("gpt-4-turbo", config) == "json_schema"
        assert _get_json_mode_for_model("gpt-3.5-turbo", config) == "json_schema"

    def test_qwen_uses_json_object(self):
        """Qwen models should use json_object mode."""
        config = {
            "llm": {
                "json_output": {
                    "mode_by_model": {
                        "deepseek*": "json_object",
                        "qwen*": "json_object",
                        "*": "json_schema"
                    },
                    "enable_fallback": True
                }
            }
        }

        assert _get_json_mode_for_model("qwen-plus", config) == "json_object"
        assert _get_json_mode_for_model("qwen-max", config) == "json_object"

    def test_fallback_disabled_uses_json_schema(self):
        """When enable_fallback is false, always use json_schema."""
        config = {
            "llm": {
                "json_output": {
                    "mode_by_model": {
                        "deepseek*": "json_object",
                        "*": "json_schema"
                    },
                    "enable_fallback": False
                }
            }
        }

        assert _get_json_mode_for_model("deepseek-chat", config) == "json_schema"

    def test_empty_config_uses_json_schema(self):
        """Empty config should default to json_schema."""
        config = {}
        assert _get_json_mode_for_model("any-model", config) == "json_schema"

    def test_pattern_order_matters(self):
        """First matching pattern should be used."""
        config = {
            "llm": {
                "json_output": {
                    "mode_by_model": {
                        "deepseek-chat": "json_object",
                        "deepseek*": "json_schema",
                        "*": "json_schema"
                    },
                    "enable_fallback": True
                }
            }
        }

        assert _get_json_mode_for_model("deepseek-chat", config) == "json_object"
        assert _get_json_mode_for_model("deepseek-coder", config) == "json_schema"


class TestValidateRequiredFields:
    """Tests for _validate_required_fields function."""

    def test_valid_json_with_all_required_fields(self):
        """Valid JSON with all required fields should pass."""
        schema = {"required": ["name", "age"]}
        parsed_json = {"name": "test", "age": 18}

        valid, error = _validate_required_fields(parsed_json, schema)
        assert valid is True
        assert error == ""

    def test_missing_required_field(self):
        """Missing required field should fail."""
        schema = {"required": ["name", "age"]}
        parsed_json = {"name": "test"}

        valid, error = _validate_required_fields(parsed_json, schema)
        assert valid is False
        assert "age" in error

    def test_multiple_missing_fields(self):
        """Multiple missing fields should be reported."""
        schema = {"required": ["name", "age", "email"]}
        parsed_json = {"name": "test"}

        valid, error = _validate_required_fields(parsed_json, schema)
        assert valid is False
        assert "age" in error
        assert "email" in error

    def test_non_dict_input(self):
        """Non-dict input should fail."""
        schema = {"required": ["name"]}

        valid, error = _validate_required_fields([1, 2, 3], schema)
        assert valid is False
        assert "not a dict" in error

        valid, error = _validate_required_fields("string", schema)
        assert valid is False
        assert "not a dict" in error

    def test_empty_required_list(self):
        """Empty required list should always pass."""
        schema = {"required": []}
        parsed_json = {}

        valid, error = _validate_required_fields(parsed_json, schema)
        assert valid is True

    def test_no_required_key_in_schema(self):
        """Schema without required key should pass."""
        schema = {"type": "object"}
        parsed_json = {}

        valid, error = _validate_required_fields(parsed_json, schema)
        assert valid is True


class TestExtractJsonFromResponse:
    """Tests for _extract_json_from_response function."""

    def test_extract_from_json_code_block(self):
        """Should extract JSON from ```json ... ``` block."""
        response = '''Here is the result:
```json
{"name": "test", "value": 42}
```
'''
        result = _extract_json_from_response(response)
        assert result == '{"name": "test", "value": 42}'

    def test_extract_from_plain_code_block(self):
        """Should extract from plain ``` ... ``` block."""
        response = '''```
{"name": "test"}
```'''
        result = _extract_json_from_response(response)
        assert result == '{"name": "test"}'

    def test_plain_json_response(self):
        """Should handle plain JSON without code blocks."""
        response = '{"name": "test", "value": 42}'
        result = _extract_json_from_response(response)
        assert result == '{"name": "test", "value": 42}'

    def test_strip_whitespace(self):
        """Should strip leading/trailing whitespace."""
        response = '''
{"name": "test"}
   '''
        result = _extract_json_from_response(response)
        assert result == '{"name": "test"}'


class TestSchemaToPromptInstruction:
    """Tests for _schema_to_prompt_instruction function."""

    def test_generates_instruction_with_schema(self):
        """Should generate instruction containing schema."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"}
            },
            "required": ["name"]
        }

        instruction = _schema_to_prompt_instruction(schema)

        assert "JSON Schema" in instruction
        assert '"name"' in instruction
        assert '"type": "string"' in instruction
        assert "required" in instruction

    def test_empty_schema_returns_empty_string(self):
        """Empty schema should return empty string."""
        assert _schema_to_prompt_instruction({}) == ""
        assert _schema_to_prompt_instruction(None) == ""


class TestDefaultConfig:
    """Tests for default config management."""

    def teardown_method(self):
        """Reset config after each test."""
        set_default_config(None)

    def test_set_and_get_default_config(self):
        """Should store and retrieve default config."""
        config = {"llm": {"api_key": "test"}}
        set_default_config(config)
        assert get_default_config() == config

    def test_initial_config_is_none(self):
        """Initial config should be None."""
        set_default_config(None)
        assert get_default_config() is None


class TestLLMStats:
    """Tests for LLM statistics tracking."""

    def setup_method(self):
        """Reset stats before each test."""
        reset_llm_stats()

    def test_initial_stats_are_zero(self):
        """Initial stats should all be zero."""
        stats = get_llm_stats()
        assert stats.text_calls == 0
        assert stats.json_schema_calls == 0
        assert stats.json_object_calls == 0

    def test_reset_stats(self):
        """Reset should clear all stats."""
        stats = get_llm_stats()
        stats.text_calls = 10
        stats.json_schema_calls = 5

        reset_llm_stats()
        stats = get_llm_stats()

        assert stats.text_calls == 0
        assert stats.json_schema_calls == 0


class TestStructuredResult:
    """Tests for StructuredResult dataclass."""

    def test_success_result(self):
        """Success result should have data and no error."""
        result = StructuredResult(success=True, data={"key": "value"})
        assert result.success is True
        assert result.data == {"key": "value"}
        assert result.error is None

    def test_failure_result(self):
        """Failure result should have error and no data."""
        result = StructuredResult(success=False, error="Something went wrong")
        assert result.success is False
        assert result.data is None
        assert result.error == "Something went wrong"

    def test_default_values(self):
        """Default values should be None."""
        result = StructuredResult(success=True)
        assert result.data is None
        assert result.error is None
