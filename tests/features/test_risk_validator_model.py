"""
Risk Validator Model Selection Test (New Architecture)

Test that validator_model correctly switches to risk_validator_model
when risk content is detected.
"""

import sys
import os

try:
    import commentjson as json
except ImportError:
    import json

# Add project paths
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)
sys.path.insert(0, os.path.join(project_root, 'src'))

from video_transcript_api.llm import LLMConfig


def load_config():
    """Load actual config file with JSONC support"""
    config_path = os.path.join(project_root, 'config', 'config.jsonc')
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def test_select_models_includes_validator():
    """Test 1: select_models_for_task returns validator_model and validator_reasoning_effort"""
    print("=" * 80)
    print("Test 1: select_models_for_task Returns Validator Model")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = False

    llm_config = LLMConfig.from_dict(config)
    result = llm_config.select_models_for_task(has_risk=False)

    print(f"\nReturned keys: {list(result.keys())}")
    print(f"validator_model: {result.get('validator_model')}")
    print(f"validator_reasoning_effort: {result.get('validator_reasoning_effort')}")

    assert 'validator_model' in result
    assert 'validator_reasoning_effort' in result
    assert result['validator_model'] == llm_config.validator_model

    print("\n[PASS] Test 1: validator model returned correctly")


def test_validator_switches_on_risk():
    """Test 2: validator_model switches to risk_validator_model when risk detected"""
    print("\n" + "=" * 80)
    print("Test 2: Validator Model Switches on Risk Detection")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True

    calibration_config = config['llm'].setdefault('structured_calibration', {})
    calibration_config['validator_model'] = 'default-validator'
    calibration_config['risk_validator_model'] = 'risk-validator-model'
    calibration_config['risk_validator_reasoning_effort'] = 'low'

    llm_config = LLMConfig.from_dict(config)
    result = llm_config.select_models_for_task(has_risk=True)

    print(f"\nSelected validator_model: {result['validator_model']}")
    print(f"Selected validator_reasoning_effort: {result['validator_reasoning_effort']}")
    print(f"has_risk: {result['has_risk']}")

    assert result['has_risk'] is True
    assert result['validator_model'] == 'risk-validator-model'
    assert result['validator_reasoning_effort'] == 'low'

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

    llm_config = LLMConfig.from_dict(config)
    result = llm_config.select_models_for_task(has_risk=False)

    print(f"\nSelected validator_model: {result['validator_model']}")
    print(f"has_risk: {result['has_risk']}")

    assert result['has_risk'] is False
    assert result['validator_model'] == 'default-validator'

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
    calibration_config['risk_validator_model'] = ''

    llm_config = LLMConfig.from_dict(config)
    result = llm_config.select_models_for_task(has_risk=True)

    print(f"\nSelected validator_model: {result['validator_model']}")
    print(f"has_risk: {result['has_risk']}")

    assert result['has_risk'] is True
    assert result['validator_model'] == 'default-validator'

    print("\n[PASS] Test 4: Validator unchanged when risk validator not configured")


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

    llm_config = LLMConfig.from_dict(config)
    result = llm_config.select_models_for_task(has_risk=True)

    print("\nModel Selection Results:")
    print(f"  calibrate_model: {result['calibrate_model']}")
    print(f"  summary_model: {result['summary_model']}")
    print(f"  validator_model: {result['validator_model']}")

    assert result['calibrate_model'] == 'risk-calibrate'
    assert result['summary_model'] == 'risk-summary'
    assert result['validator_model'] == 'risk-validator'

    print("\n[PASS] Test 5: All three models switch together correctly")


def run_all_tests():
    """Run all tests"""
    print("\n" + "=" * 80)
    print("RISK VALIDATOR MODEL SELECTION TEST SUITE (NEW ARCH)")
    print("=" * 80)

    try:
        test_select_models_includes_validator()
        test_validator_switches_on_risk()
        test_validator_unchanged_when_no_risk()
        test_validator_unchanged_when_risk_validator_not_configured()
        test_all_three_models_switch_together()

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
