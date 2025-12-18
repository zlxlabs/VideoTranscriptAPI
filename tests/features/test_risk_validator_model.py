"""
Risk Validator Model Selection Test

Test that validator_model correctly switches to risk_validator_model
when sensitive content is detected in metadata.
"""

import sys
import os
import json
from unittest.mock import patch, MagicMock

# Add project paths
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)
sys.path.insert(0, os.path.join(project_root, 'src'))

from video_transcript_api.utils.llm.llm_enhanced import EnhancedLLMProcessor


def load_config():
    """Load actual config file with JSONC support using commentjson"""
    import commentjson
    config_path = os.path.join(project_root, 'config', 'config.jsonc')
    with open(config_path, 'r', encoding='utf-8') as f:
        return commentjson.load(f)


def test_select_models_includes_validator():
    """Test 1: _select_models returns validator_model and validator_reasoning_effort"""
    print("=" * 80)
    print("Test 1: _select_models Returns Validator Model")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = False

    processor = EnhancedLLMProcessor(config)

    result = processor._select_models(
        task_id="test_001",
        title="Normal title",
        author="Author",
        description="Description"
    )

    print(f"\nReturned keys: {list(result.keys())}")
    print(f"validator_model: {result.get('validator_model')}")
    print(f"validator_reasoning_effort: {result.get('validator_reasoning_effort')}")

    assert 'validator_model' in result, "Result should contain validator_model"
    assert 'validator_reasoning_effort' in result, "Result should contain validator_reasoning_effort"

    expected_validator = config['llm']['structured_calibration'].get('validator_model')
    assert result['validator_model'] == expected_validator, \
        f"Expected {expected_validator}, got {result['validator_model']}"

    print("\n[PASS] Test 1: _select_models returns validator model correctly")


def test_validator_switches_on_risk():
    """Test 2: validator_model switches to risk_validator_model when risk detected"""
    print("\n" + "=" * 80)
    print("Test 2: Validator Model Switches on Risk Detection")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True

    # Ensure risk_validator_model is configured
    calibration_config = config['llm'].setdefault('structured_calibration', {})
    calibration_config['validator_model'] = 'default-validator'
    calibration_config['risk_validator_model'] = 'risk-validator-model'
    calibration_config['risk_validator_reasoning_effort'] = 'low'

    with patch('video_transcript_api.utils.risk_control.is_enabled', return_value=True):
        with patch('video_transcript_api.utils.risk_control.sanitize_text') as mock_sanitize:
            mock_sanitize.return_value = {
                'has_sensitive': True,
                'sensitive_words': ['sensitive_word'],
                'sanitized_text': 'cleaned text'
            }

            processor = EnhancedLLMProcessor(config)

            result = processor._select_models(
                task_id="test_002",
                title="Title with sensitive_word",
                author="Author",
                description="Description"
            )

            print(f"\nDefault validator_model: default-validator")
            print(f"Risk validator_model: risk-validator-model")
            print(f"Selected validator_model: {result['validator_model']}")
            print(f"Selected validator_reasoning_effort: {result['validator_reasoning_effort']}")
            print(f"has_risk: {result['has_risk']}")

            assert result['has_risk'] == True, "Should detect risk"
            assert result['validator_model'] == 'risk-validator-model', \
                f"Should switch to risk validator, got {result['validator_model']}"
            assert result['validator_reasoning_effort'] == 'low', \
                f"Should use risk validator effort, got {result['validator_reasoning_effort']}"

    print("\n[PASS] Test 2: Validator model switches correctly on risk")


def test_validator_unchanged_when_no_risk():
    """Test 3: validator_model unchanged when no risk detected"""
    print("\n" + "=" * 80)
    print("Test 3: Validator Model Unchanged When No Risk")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True

    calibration_config = config['llm'].setdefault('structured_calibration', {})
    calibration_config['validator_model'] = 'default-validator'
    calibration_config['risk_validator_model'] = 'risk-validator-model'

    with patch('video_transcript_api.utils.risk_control.is_enabled', return_value=True):
        with patch('video_transcript_api.utils.risk_control.sanitize_text') as mock_sanitize:
            mock_sanitize.return_value = {
                'has_sensitive': False,
                'sensitive_words': [],
                'sanitized_text': 'clean text'
            }

            processor = EnhancedLLMProcessor(config)

            result = processor._select_models(
                task_id="test_003",
                title="Clean title without issues",
                author="Author",
                description="Description"
            )

            print(f"\nDefault validator_model: default-validator")
            print(f"Selected validator_model: {result['validator_model']}")
            print(f"has_risk: {result['has_risk']}")

            assert result['has_risk'] == False, "Should not detect risk"
            assert result['validator_model'] == 'default-validator', \
                f"Should keep default validator, got {result['validator_model']}"

    print("\n[PASS] Test 3: Validator model unchanged when no risk")


def test_validator_unchanged_when_risk_validator_not_configured():
    """Test 4: validator_model unchanged when risk_validator_model is empty"""
    print("\n" + "=" * 80)
    print("Test 4: Validator Unchanged When Risk Validator Not Configured")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True

    calibration_config = config['llm'].setdefault('structured_calibration', {})
    calibration_config['validator_model'] = 'default-validator'
    calibration_config['risk_validator_model'] = ''  # Empty = not configured

    with patch('video_transcript_api.utils.risk_control.is_enabled', return_value=True):
        with patch('video_transcript_api.utils.risk_control.sanitize_text') as mock_sanitize:
            mock_sanitize.return_value = {
                'has_sensitive': True,
                'sensitive_words': ['risk_word'],
                'sanitized_text': 'cleaned'
            }

            processor = EnhancedLLMProcessor(config)

            result = processor._select_models(
                task_id="test_004",
                title="Title with risk_word",
                author="Author",
                description="Description"
            )

            print(f"\nDefault validator_model: default-validator")
            print(f"Risk validator_model: (empty/not configured)")
            print(f"Selected validator_model: {result['validator_model']}")
            print(f"has_risk: {result['has_risk']}")

            assert result['has_risk'] == True, "Should detect risk"
            # Should keep default validator since risk_validator_model is empty
            assert result['validator_model'] == 'default-validator', \
                f"Should keep default when risk_validator not configured, got {result['validator_model']}"

    print("\n[PASS] Test 4: Validator unchanged when risk_validator not configured")


def test_all_three_models_switch_together():
    """Test 5: calibrate, summary, and validator all switch when risk detected"""
    print("\n" + "=" * 80)
    print("Test 5: All Three Models Switch Together on Risk")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True
    config['llm']['calibrate_model'] = 'default-calibrate'
    config['llm']['risk_calibrate_model'] = 'risk-calibrate'
    config['llm']['summary_model'] = 'default-summary'
    config['llm']['risk_summary_model'] = 'risk-summary'

    calibration_config = config['llm'].setdefault('structured_calibration', {})
    calibration_config['validator_model'] = 'default-validator'
    calibration_config['risk_validator_model'] = 'risk-validator'

    with patch('video_transcript_api.utils.risk_control.is_enabled', return_value=True):
        with patch('video_transcript_api.utils.risk_control.sanitize_text') as mock_sanitize:
            mock_sanitize.return_value = {
                'has_sensitive': True,
                'sensitive_words': ['sensitive'],
                'sanitized_text': 'cleaned'
            }

            processor = EnhancedLLMProcessor(config)

            result = processor._select_models(
                task_id="test_005",
                title="Title with sensitive content",
                author="Author",
                description="Description"
            )

            print(f"\nModel Selection Results:")
            print(f"  calibrate_model: {result['calibrate_model']} (expected: risk-calibrate)")
            print(f"  summary_model: {result['summary_model']} (expected: risk-summary)")
            print(f"  validator_model: {result['validator_model']} (expected: risk-validator)")

            assert result['calibrate_model'] == 'risk-calibrate', \
                f"Calibrate should switch, got {result['calibrate_model']}"
            assert result['summary_model'] == 'risk-summary', \
                f"Summary should switch, got {result['summary_model']}"
            assert result['validator_model'] == 'risk-validator', \
                f"Validator should switch, got {result['validator_model']}"

    print("\n[PASS] Test 5: All three models switch together correctly")


def test_with_actual_config():
    """Test 6: Test with actual config values"""
    print("\n" + "=" * 80)
    print("Test 6: Test With Actual Config Values")
    print("=" * 80)

    config = load_config()

    print(f"\nActual config values:")
    print(f"  enable_risk_model_selection: {config['llm'].get('enable_risk_model_selection')}")
    print(f"  calibrate_model: {config['llm'].get('calibrate_model')}")
    print(f"  risk_calibrate_model: {config['llm'].get('risk_calibrate_model')}")
    print(f"  summary_model: {config['llm'].get('summary_model')}")
    print(f"  risk_summary_model: {config['llm'].get('risk_summary_model')}")

    calibration_config = config['llm'].get('structured_calibration', {})
    print(f"  validator_model: {calibration_config.get('validator_model')}")
    print(f"  risk_validator_model: {calibration_config.get('risk_validator_model')}")

    # Skip test if risk model selection is disabled
    if not config['llm'].get('enable_risk_model_selection'):
        print("\n[SKIP] Risk model selection is disabled in config")
        return

    with patch('video_transcript_api.utils.risk_control.is_enabled', return_value=True):
        with patch('video_transcript_api.utils.risk_control.sanitize_text') as mock_sanitize:
            mock_sanitize.return_value = {
                'has_sensitive': True,
                'sensitive_words': ['test_sensitive'],
                'sanitized_text': 'cleaned'
            }

            processor = EnhancedLLMProcessor(config)

            result = processor._select_models(
                task_id="test_006",
                title="Title with test_sensitive word",
                author="Author",
                description="Description"
            )

            print(f"\nSelected models after risk detection:")
            print(f"  calibrate_model: {result['calibrate_model']}")
            print(f"  summary_model: {result['summary_model']}")
            print(f"  validator_model: {result['validator_model']}")

            # Verify against actual config
            expected_summary = config['llm'].get('risk_summary_model')
            assert result['summary_model'] == expected_summary, \
                f"Summary should be {expected_summary}, got {result['summary_model']}"

            expected_validator = calibration_config.get('risk_validator_model')
            if expected_validator:
                assert result['validator_model'] == expected_validator, \
                    f"Validator should be {expected_validator}, got {result['validator_model']}"

    print("\n[PASS] Test 6: Actual config values work correctly")


def run_all_tests():
    """Run all tests"""
    print("\n" + "=" * 80)
    print("RISK VALIDATOR MODEL SELECTION TEST SUITE")
    print("=" * 80)

    try:
        test_select_models_includes_validator()
        test_validator_switches_on_risk()
        test_validator_unchanged_when_no_risk()
        test_validator_unchanged_when_risk_validator_not_configured()
        test_all_three_models_switch_together()
        test_with_actual_config()

        print("\n" + "=" * 80)
        print("ALL TESTS PASSED")
        print("=" * 80)

    except AssertionError as e:
        print(f"\n[FAIL] TEST FAILED: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] UNEXPECTED ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
