"""
Risk Model Selection Shared Feature Test (New Architecture)

Validates LLMConfig.select_models_for_task for calibrate/summary selection.
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


def test_select_models_returns_both():
    """Test 1: select_models_for_task returns both calibrate and summary models"""
    print("=" * 80)
    print("Test 1: select_models_for_task() Returns Both Calibrate and Summary Models")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = False

    llm_config = LLMConfig.from_dict(config)
    result = llm_config.select_models_for_task(has_risk=False)

    print(f"\nResult keys: {list(result.keys())}")
    print(f"  calibrate_model: {result['calibrate_model']}")
    print(f"  summary_model: {result['summary_model']}")
    print(f"  has_risk: {result['has_risk']}")

    expected_keys = [
        'calibrate_model', 'calibrate_reasoning_effort',
        'summary_model', 'summary_reasoning_effort',
        'validator_model', 'validator_reasoning_effort',
        'has_risk'
    ]
    for key in expected_keys:
        assert key in result, f"Missing key: {key}"

    assert result['calibrate_model'] == llm_config.calibrate_model
    assert result['summary_model'] == llm_config.summary_model
    assert result['has_risk'] is False

    print("\n[PASS] Test 1: select_models_for_task() returns correct structure")


def test_models_switch_when_configured():
    """Test 2: calibrate + summary models switch when risk models configured"""
    print("\n" + "=" * 80)
    print("Test 2: Models Switch When Risk Models Configured")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True
    config['llm']['risk_calibrate_model'] = 'risk-calibrate-model'
    config['llm']['risk_calibrate_reasoning_effort'] = 'low'
    config['llm']['risk_summary_model'] = 'risk-summary-model'
    config['llm']['risk_summary_reasoning_effort'] = 'medium'

    llm_config = LLMConfig.from_dict(config)
    result = llm_config.select_models_for_task(has_risk=True)

    print(f"\nResult:")
    print(f"  calibrate_model: {result['calibrate_model']}")
    print(f"  calibrate_reasoning_effort: {result['calibrate_reasoning_effort']}")
    print(f"  summary_model: {result['summary_model']}")
    print(f"  summary_reasoning_effort: {result['summary_reasoning_effort']}")

    assert result['has_risk'] is True
    assert result['calibrate_model'] == 'risk-calibrate-model'
    assert result['calibrate_reasoning_effort'] == 'low'
    assert result['summary_model'] == 'risk-summary-model'
    assert result['summary_reasoning_effort'] == 'medium'

    print("\n[PASS] Test 2: Both models switched to risk models")


def test_calibrate_model_stays_default_when_not_configured():
    """Test 3: Calibrate model stays default when risk_calibrate_model not configured"""
    print("\n" + "=" * 80)
    print("Test 3: Calibrate Model Stays Default When Not Configured")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True
    config['llm']['risk_calibrate_model'] = None
    config['llm']['risk_calibrate_reasoning_effort'] = None
    config['llm']['risk_summary_model'] = 'risk-summary-model'

    llm_config = LLMConfig.from_dict(config)
    result = llm_config.select_models_for_task(has_risk=True)

    print(f"\nResult:")
    print(f"  calibrate_model: {result['calibrate_model']}")
    print(f"  summary_model: {result['summary_model']}")

    assert result['has_risk'] is True
    assert result['calibrate_model'] == llm_config.calibrate_model
    assert result['summary_model'] == 'risk-summary-model'

    print("\n[PASS] Test 3: Calibrate model stayed default, summary model switched")


def test_no_risk_both_models_default():
    """Test 4: No risk detected, both models stay default"""
    print("\n" + "=" * 80)
    print("Test 4: No Risk Detected - Both Models Stay Default")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True
    config['llm']['risk_calibrate_model'] = 'risk-calibrate-model'
    config['llm']['risk_summary_model'] = 'risk-summary-model'

    llm_config = LLMConfig.from_dict(config)
    result = llm_config.select_models_for_task(has_risk=False)

    print(f"\nResult:")
    print(f"  calibrate_model: {result['calibrate_model']}")
    print(f"  summary_model: {result['summary_model']}")

    assert result['has_risk'] is False
    assert result['calibrate_model'] == llm_config.calibrate_model
    assert result['summary_model'] == llm_config.summary_model

    print("\n[PASS] Test 4: No risk - both models stayed default")


def test_empty_risk_calibrate_model_string():
    """Test 5: Empty string for risk_calibrate_model treated as not configured"""
    print("\n" + "=" * 80)
    print("Test 5: Empty String risk_calibrate_model Treated as Not Configured")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True
    config['llm']['risk_calibrate_model'] = ''
    config['llm']['risk_summary_model'] = 'risk-summary-model'

    llm_config = LLMConfig.from_dict(config)
    result = llm_config.select_models_for_task(has_risk=True)

    print(f"\nResult:")
    print(f"  calibrate_model: {result['calibrate_model']}")
    print(f"  summary_model: {result['summary_model']}")

    assert result['calibrate_model'] == llm_config.calibrate_model
    assert result['summary_model'] == 'risk-summary-model'

    print("\n[PASS] Test 5: Empty string treated as not configured")


def run_all_tests():
    """Run all tests"""
    print("\n" + "=" * 80)
    print("RISK MODEL SELECTION - SHARED FEATURE TEST SUITE (NEW ARCH)")
    print("=" * 80)

    try:
        test_select_models_returns_both()
        test_models_switch_when_configured()
        test_calibrate_model_stays_default_when_not_configured()
        test_no_risk_both_models_default()
        test_empty_risk_calibrate_model_string()

        print("\n" + "=" * 80)
        print("ALL TESTS PASSED")
        print("=" * 80)
        return True

    except AssertionError as e:
        print(f"\n[FAIL] TEST FAILED: {str(e)}")
        import traceback
        traceback.print_exc()
        return False
    except Exception as e:
        print(f"\n[ERROR] UNEXPECTED ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
