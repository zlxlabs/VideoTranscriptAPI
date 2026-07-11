"""
Manual test for JSON Schema structured output functionality.

This script tests the new structured output feature with real LLM API calls.
Run this script to verify the upgrade is working correctly.

Usage:
    python tests/manual/test_json_schema_output.py
"""
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.llm import (
    call_llm_api,
    set_default_config,
    StructuredResult,
    get_llm_stats,
    reset_llm_stats,
    log_llm_stats,
)
from video_transcript_api.llm.schemas import CALIBRATION_RESULT_SCHEMA
# SPEAKER_MAPPING_SCHEMA 唯一定义在 prompts.schemas（llm.schemas 中的重复定义已删除）
from video_transcript_api.llm.prompts.schemas import SPEAKER_MAPPING_SCHEMA
from video_transcript_api.api.context import get_config


def test_text_output():
    """Test traditional text output (backward compatibility)."""
    print("\n" + "=" * 60)
    print("TEST 1: Text Output (Backward Compatibility)")
    print("=" * 60)

    config = get_config()
    llm_config = config.get('llm', {})

    result = call_llm_api(
        model=llm_config.get('calibrate_model', 'gpt-4o-mini'),
        prompt="Say 'Hello, World!' in Chinese.",
        api_key=llm_config['api_key'],
        base_url=llm_config['base_url'],
        max_retries=1,
        retry_delay=2,
        task_type="test_text"
    )

    print(f"Result type: {type(result)}")
    print(f"Result: {result}")

    assert isinstance(result, str), "Text output should return str"
    print("[PASS] Text output works correctly")


def test_structured_output_json_schema():
    """Test structured output with json_schema mode (GPT models)."""
    print("\n" + "=" * 60)
    print("TEST 2: Structured Output (json_schema mode)")
    print("=" * 60)

    config = get_config()
    llm_config = config.get('llm', {})
    set_default_config(config)

    # Use a model that supports json_schema (e.g., GPT)
    model = llm_config.get('calibrate_model', 'gpt-4o-mini')

    # Simple test schema
    test_schema = {
        "type": "object",
        "properties": {
            "greeting": {"type": "string"},
            "language": {"type": "string"}
        },
        "required": ["greeting", "language"],
        "additionalProperties": False
    }

    result: StructuredResult = call_llm_api(
        model=model,
        prompt="Generate a greeting in Chinese. Return the greeting text and the language name.",
        api_key=llm_config['api_key'],
        base_url=llm_config['base_url'],
        max_retries=1,
        retry_delay=2,
        task_type="test_json_schema",
        response_schema=test_schema
    )

    print(f"Result type: {type(result)}")
    print(f"Success: {result.success}")
    print(f"Data: {result.data}")
    print(f"Error: {result.error}")

    if result.success:
        assert "greeting" in result.data, "Should have 'greeting' field"
        assert "language" in result.data, "Should have 'language' field"
        print("[PASS] json_schema mode works correctly")
    else:
        print(f"[WARN] json_schema mode failed: {result.error}")


def test_structured_output_json_object():
    """Test structured output with json_object mode (DeepSeek models)."""
    print("\n" + "=" * 60)
    print("TEST 3: Structured Output (json_object mode - DeepSeek)")
    print("=" * 60)

    config = get_config()
    llm_config = config.get('llm', {})
    set_default_config(config)

    # Use DeepSeek model (should use json_object mode)
    model = llm_config.get('summary_model', 'deepseek-v4-flash')

    # Check if it's a DeepSeek model
    if 'deepseek' not in model.lower():
        print(f"[SKIP] summary_model is '{model}', not a DeepSeek model")
        return

    test_schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "word_count": {"type": "number"}
        },
        "required": ["summary", "word_count"],
        "additionalProperties": False
    }

    result: StructuredResult = call_llm_api(
        model=model,
        prompt="Summarize this text in one sentence: 'The quick brown fox jumps over the lazy dog.' Also count the words.",
        api_key=llm_config['api_key'],
        base_url=llm_config['base_url'],
        max_retries=1,
        retry_delay=2,
        task_type="test_json_object",
        response_schema=test_schema
    )

    print(f"Result type: {type(result)}")
    print(f"Success: {result.success}")
    print(f"Data: {result.data}")
    print(f"Error: {result.error}")

    if result.success:
        assert "summary" in result.data, "Should have 'summary' field"
        assert "word_count" in result.data, "Should have 'word_count' field"
        print("[PASS] json_object mode works correctly")
    else:
        print(f"[WARN] json_object mode failed: {result.error}")


def test_calibration_schema():
    """Test with the actual calibration schema."""
    print("\n" + "=" * 60)
    print("TEST 4: Calibration Schema")
    print("=" * 60)

    config = get_config()
    llm_config = config.get('llm', {})
    set_default_config(config)

    model = llm_config.get('calibrate_model', 'gpt-4o-mini')

    prompt = """Please calibrate the following dialog:

Input:
{
  "dialogs": [
    {"start_time": "00:00:05", "speaker": "Speaker1", "text": "hello how r u today"}
  ]
}

Return the calibrated version."""

    result: StructuredResult = call_llm_api(
        model=model,
        prompt=prompt,
        api_key=llm_config['api_key'],
        base_url=llm_config['base_url'],
        max_retries=1,
        retry_delay=2,
        task_type="test_calibration",
        response_schema=CALIBRATION_RESULT_SCHEMA
    )

    print(f"Success: {result.success}")
    print(f"Data: {result.data}")

    if result.success:
        assert "calibrated_dialogs" in result.data, "Should have 'calibrated_dialogs' field"
        dialogs = result.data["calibrated_dialogs"]
        assert len(dialogs) > 0, "Should have at least one dialog"
        assert "start_time" in dialogs[0], "Dialog should have 'start_time'"
        assert "speaker" in dialogs[0], "Dialog should have 'speaker'"
        assert "text" in dialogs[0], "Dialog should have 'text'"
        print("[PASS] Calibration schema works correctly")
    else:
        print(f"[WARN] Calibration schema failed: {result.error}")


def test_risk_calibrate_model():
    """Test risk_calibrate_model if configured."""
    print("\n" + "=" * 60)
    print("TEST 5: Risk Calibrate Model")
    print("=" * 60)

    config = get_config()
    llm_config = config.get('llm', {})
    set_default_config(config)

    model = llm_config.get('risk_calibrate_model', '')
    if not model:
        print("[SKIP] risk_calibrate_model not configured")
        return

    print(f"Testing model: {model}")

    test_schema = {
        "type": "object",
        "properties": {
            "result": {"type": "string"},
            "confidence": {"type": "number"}
        },
        "required": ["result", "confidence"],
        "additionalProperties": False
    }

    result: StructuredResult = call_llm_api(
        model=model,
        prompt="Translate 'Hello' to French. Return the result and your confidence (0-1).",
        api_key=llm_config['api_key'],
        base_url=llm_config['base_url'],
        max_retries=1,
        retry_delay=2,
        task_type="test_risk_calibrate",
        response_schema=test_schema
    )

    print(f"Success: {result.success}")
    print(f"Data: {result.data}")
    print(f"Error: {result.error}")

    if result.success:
        assert "result" in result.data, "Should have 'result' field"
        assert "confidence" in result.data, "Should have 'confidence' field"
        print(f"[PASS] risk_calibrate_model ({model}) works correctly")
    else:
        print(f"[WARN] risk_calibrate_model ({model}) failed: {result.error}")


def test_risk_summary_model():
    """Test risk_summary_model if configured."""
    print("\n" + "=" * 60)
    print("TEST 6: Risk Summary Model")
    print("=" * 60)

    config = get_config()
    llm_config = config.get('llm', {})
    set_default_config(config)

    model = llm_config.get('risk_summary_model', '')
    if not model:
        print("[SKIP] risk_summary_model not configured")
        return

    print(f"Testing model: {model}")

    test_schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "key_points": {
                "type": "array",
                "items": {"type": "string"}
            }
        },
        "required": ["summary", "key_points"],
        "additionalProperties": False
    }

    result: StructuredResult = call_llm_api(
        model=model,
        prompt="Summarize: 'Python is a programming language. It is popular for AI development.' Return a summary and key points.",
        api_key=llm_config['api_key'],
        base_url=llm_config['base_url'],
        max_retries=1,
        retry_delay=2,
        task_type="test_risk_summary",
        response_schema=test_schema
    )

    print(f"Success: {result.success}")
    print(f"Data: {result.data}")
    print(f"Error: {result.error}")

    if result.success:
        assert "summary" in result.data, "Should have 'summary' field"
        assert "key_points" in result.data, "Should have 'key_points' field"
        print(f"[PASS] risk_summary_model ({model}) works correctly")
    else:
        print(f"[WARN] risk_summary_model ({model}) failed: {result.error}")


def main():
    """Run all tests."""
    print("\n" + "#" * 60)
    print("# JSON Schema Structured Output - Integration Tests")
    print("#" * 60)

    # Show configured models
    config = get_config()
    llm_config = config.get('llm', {})
    print("\nConfigured models:")
    print(f"  calibrate_model:      {llm_config.get('calibrate_model', 'N/A')}")
    print(f"  summary_model:        {llm_config.get('summary_model', 'N/A')}")
    print(f"  risk_calibrate_model: {llm_config.get('risk_calibrate_model', 'N/A')}")
    print(f"  risk_summary_model:   {llm_config.get('risk_summary_model', 'N/A')}")

    reset_llm_stats()

    try:
        test_text_output()
        test_structured_output_json_schema()
        test_structured_output_json_object()
        test_calibration_schema()
        test_risk_calibrate_model()
        test_risk_summary_model()
    except Exception as e:
        print(f"\n[ERROR] Test failed with exception: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n" + "=" * 60)
        print("LLM Call Statistics")
        print("=" * 60)
        log_llm_stats()

    print("\n" + "#" * 60)
    print("# Tests Completed")
    print("#" * 60)


if __name__ == "__main__":
    main()
