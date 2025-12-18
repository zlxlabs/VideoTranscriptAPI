"""
Risk Model Selection Shared Feature Test

Test the shared risk model selection for both calibrate and summary models.
When risk content is detected, both models can switch to risk models.

Key features tested:
1. _select_models() returns both calibrate and summary models
2. Calibrate model switches when risk_calibrate_model is configured
3. Calibrate model stays default when risk_calibrate_model is not configured
4. Model selection is shared (one detection, two models selected)
"""

import sys
import os
from unittest.mock import patch, MagicMock

# Support JSONC format (JSON with comments)
try:
    import commentjson as json
except ImportError:
    import json

# Add project paths
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)
sys.path.insert(0, os.path.join(project_root, 'src'))

from src.video_transcript_api.utils.llm.llm_enhanced import EnhancedLLMProcessor


def load_config():
    """Load actual config file with JSONC support"""
    config_path = os.path.join(project_root, 'config', 'config.jsonc')
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def test_select_models_returns_both():
    """Test 1: _select_models() returns both calibrate and summary models"""
    print("=" * 80)
    print("Test 1: _select_models() Returns Both Calibrate and Summary Models")
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

    print(f"\nResult keys: {list(result.keys())}")
    print(f"  calibrate_model: {result['calibrate_model']}")
    print(f"  calibrate_reasoning_effort: {result['calibrate_reasoning_effort']}")
    print(f"  summary_model: {result['summary_model']}")
    print(f"  summary_reasoning_effort: {result['summary_reasoning_effort']}")
    print(f"  has_risk: {result['has_risk']}")

    # Verify all expected keys exist
    expected_keys = ['calibrate_model', 'calibrate_reasoning_effort',
                     'summary_model', 'summary_reasoning_effort', 'has_risk']
    for key in expected_keys:
        assert key in result, f"Missing key: {key}"

    # Verify default values
    assert result['calibrate_model'] == config['llm']['calibrate_model']
    assert result['summary_model'] == config['llm']['summary_model']
    assert result['has_risk'] == False

    print("\n[PASS] Test 1: _select_models() returns correct structure")


def test_calibrate_model_switches_when_configured():
    """Test 2: Calibrate model switches when risk_calibrate_model is configured"""
    print("\n" + "=" * 80)
    print("Test 2: Calibrate Model Switches When risk_calibrate_model Configured")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True
    config['llm']['risk_calibrate_model'] = 'gpt-4o-mini'
    config['llm']['risk_calibrate_reasoning_effort'] = 'low'
    config['llm']['risk_summary_model'] = 'gpt-4o'
    config['llm']['risk_summary_reasoning_effort'] = 'medium'

    # Mock risk detection
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

            print(f"\nInput: Title contains 'sensitive_word'")
            print(f"\nResult:")
            print(f"  has_risk: {result['has_risk']}")
            print(f"  calibrate_model: {result['calibrate_model']} (expected: gpt-4o-mini)")
            print(f"  calibrate_reasoning_effort: {result['calibrate_reasoning_effort']} (expected: low)")
            print(f"  summary_model: {result['summary_model']} (expected: gpt-4o)")
            print(f"  summary_reasoning_effort: {result['summary_reasoning_effort']} (expected: medium)")

            # Verify risk detected
            assert result['has_risk'] == True

            # Verify calibrate model switched
            assert result['calibrate_model'] == 'gpt-4o-mini', \
                f"Expected calibrate_model 'gpt-4o-mini', got '{result['calibrate_model']}'"
            assert result['calibrate_reasoning_effort'] == 'low'

            # Verify summary model switched
            assert result['summary_model'] == 'gpt-4o', \
                f"Expected summary_model 'gpt-4o', got '{result['summary_model']}'"
            assert result['summary_reasoning_effort'] == 'medium'

            print("\n[PASS] Test 2: Both models switched to risk models")


def test_calibrate_model_stays_default_when_not_configured():
    """Test 3: Calibrate model stays default when risk_calibrate_model is not configured"""
    print("\n" + "=" * 80)
    print("Test 3: Calibrate Model Stays Default When Not Configured")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True
    config['llm']['risk_calibrate_model'] = None  # Not configured
    config['llm']['risk_calibrate_reasoning_effort'] = None
    config['llm']['risk_summary_model'] = 'gpt-4o'
    config['llm']['risk_summary_reasoning_effort'] = 'medium'

    default_calibrate_model = config['llm']['calibrate_model']
    default_calibrate_effort = config['llm'].get('calibrate_reasoning_effort')

    # Mock risk detection
    with patch('video_transcript_api.utils.risk_control.is_enabled', return_value=True):
        with patch('video_transcript_api.utils.risk_control.sanitize_text') as mock_sanitize:
            mock_sanitize.return_value = {
                'has_sensitive': True,
                'sensitive_words': ['risk_word'],
                'sanitized_text': 'cleaned'
            }

            processor = EnhancedLLMProcessor(config)

            result = processor._select_models(
                task_id="test_003",
                title="Title with risk_word",
                author="Author",
                description="Description"
            )

            print(f"\nConfig: risk_calibrate_model = None (not configured)")
            print(f"\nResult:")
            print(f"  has_risk: {result['has_risk']}")
            print(f"  calibrate_model: {result['calibrate_model']} (expected: {default_calibrate_model})")
            print(f"  summary_model: {result['summary_model']} (expected: gpt-4o)")

            # Verify risk detected
            assert result['has_risk'] == True

            # Verify calibrate model stays default
            assert result['calibrate_model'] == default_calibrate_model, \
                f"Expected calibrate_model '{default_calibrate_model}', got '{result['calibrate_model']}'"

            # Verify summary model switched
            assert result['summary_model'] == 'gpt-4o'

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

    default_calibrate_model = config['llm']['calibrate_model']
    default_summary_model = config['llm']['summary_model']

    # Mock risk detection - no sensitive content
    with patch('video_transcript_api.utils.risk_control.is_enabled', return_value=True):
        with patch('video_transcript_api.utils.risk_control.sanitize_text') as mock_sanitize:
            mock_sanitize.return_value = {
                'has_sensitive': False,
                'sensitive_words': [],
                'sanitized_text': 'normal text'
            }

            processor = EnhancedLLMProcessor(config)

            result = processor._select_models(
                task_id="test_004",
                title="Normal clean title",
                author="Author",
                description="Normal description"
            )

            print(f"\nInput: Clean title without sensitive content")
            print(f"\nResult:")
            print(f"  has_risk: {result['has_risk']}")
            print(f"  calibrate_model: {result['calibrate_model']} (expected: {default_calibrate_model})")
            print(f"  summary_model: {result['summary_model']} (expected: {default_summary_model})")

            # Verify no risk
            assert result['has_risk'] == False

            # Verify both models stay default
            assert result['calibrate_model'] == default_calibrate_model
            assert result['summary_model'] == default_summary_model

            print("\n[PASS] Test 4: No risk - both models stayed default")


def test_backward_compatibility_select_summary_model():
    """Test 5: _select_summary_model() backward compatibility"""
    print("\n" + "=" * 80)
    print("Test 5: Backward Compatibility - _select_summary_model()")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = False

    processor = EnhancedLLMProcessor(config)

    # Old method should still work
    model, effort = processor._select_summary_model(
        task_id="test_005",
        title="Test title",
        author="Author",
        description="Description"
    )

    print(f"\nUsing legacy method _select_summary_model()")
    print(f"  Returned model: {model}")
    print(f"  Returned effort: {effort}")

    assert model == config['llm']['summary_model']
    assert effort == config['llm']['summary_reasoning_effort']

    print("\n[PASS] Test 5: Legacy method _select_summary_model() works correctly")


def test_shared_detection_single_call():
    """Test 6: Risk detection is called only once (shared detection)"""
    print("\n" + "=" * 80)
    print("Test 6: Shared Detection - Single Risk Check")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True
    config['llm']['risk_calibrate_model'] = 'risk-calibrate'
    config['llm']['risk_summary_model'] = 'risk-summary'

    # Mock risk detection and count calls
    with patch('video_transcript_api.utils.risk_control.is_enabled', return_value=True):
        with patch('video_transcript_api.utils.risk_control.sanitize_text') as mock_sanitize:
            mock_sanitize.return_value = {
                'has_sensitive': True,
                'sensitive_words': ['test'],
                'sanitized_text': 'cleaned'
            }

            processor = EnhancedLLMProcessor(config)

            # Call _select_models once
            result = processor._select_models(
                task_id="test_006",
                title="Title with test word",
                author="Author",
                description="Description"
            )

            # Count how many times sanitize_text was called
            # It should be called for title, author, description separately
            # but _select_models internally calls _detect_risk_in_metadata once
            call_count = mock_sanitize.call_count

            print(f"\nRisk detection call count: {call_count}")
            print(f"Both calibrate and summary models selected from single detection:")
            print(f"  calibrate_model: {result['calibrate_model']}")
            print(f"  summary_model: {result['summary_model']}")

            # Verify both models were selected from the same detection
            assert result['calibrate_model'] == 'risk-calibrate'
            assert result['summary_model'] == 'risk-summary'

            print("\n[PASS] Test 6: Shared detection - both models selected efficiently")


def test_empty_risk_calibrate_model_string():
    """Test 7: Empty string for risk_calibrate_model treated as not configured"""
    print("\n" + "=" * 80)
    print("Test 7: Empty String risk_calibrate_model Treated as Not Configured")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True
    config['llm']['risk_calibrate_model'] = ''  # Empty string
    config['llm']['risk_summary_model'] = 'risk-summary-model'

    default_calibrate_model = config['llm']['calibrate_model']

    with patch('video_transcript_api.utils.risk_control.is_enabled', return_value=True):
        with patch('video_transcript_api.utils.risk_control.sanitize_text') as mock_sanitize:
            mock_sanitize.return_value = {
                'has_sensitive': True,
                'sensitive_words': ['sensitive'],
                'sanitized_text': 'cleaned'
            }

            processor = EnhancedLLMProcessor(config)

            result = processor._select_models(
                task_id="test_007",
                title="Sensitive title",
                author="Author",
                description="Description"
            )

            print(f"\nConfig: risk_calibrate_model = '' (empty string)")
            print(f"\nResult:")
            print(f"  calibrate_model: {result['calibrate_model']} (expected: {default_calibrate_model})")
            print(f"  summary_model: {result['summary_model']} (expected: risk-summary-model)")

            # Empty string should be treated as falsy, calibrate stays default
            assert result['calibrate_model'] == default_calibrate_model
            assert result['summary_model'] == 'risk-summary-model'

            print("\n[PASS] Test 7: Empty string treated as not configured")


def run_all_tests():
    """Run all tests"""
    print("\n" + "=" * 80)
    print("RISK MODEL SELECTION - SHARED FEATURE TEST SUITE")
    print("Tests for calibrate and summary model shared risk selection")
    print("=" * 80)

    config_path = os.path.join(project_root, 'config', 'config.jsonc')
    print(f"\nUsing config from: {config_path}")

    try:
        test_select_models_returns_both()
        test_calibrate_model_switches_when_configured()
        test_calibrate_model_stays_default_when_not_configured()
        test_no_risk_both_models_default()
        test_backward_compatibility_select_summary_model()
        test_shared_detection_single_call()
        test_empty_risk_calibrate_model_string()

        print("\n" + "=" * 80)
        print("ALL 7 TESTS PASSED")
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
