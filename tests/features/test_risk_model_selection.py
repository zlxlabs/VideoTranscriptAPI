"""
风险模型选择功能测试脚本（新架构）

测试 LLMConfig.select_models_for_task 在风险/非风险场景下的模型选择逻辑。
"""

import os
import sys

try:
    import commentjson as json
except ImportError:
    import json

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)
sys.path.insert(0, os.path.join(project_root, 'src'))

from video_transcript_api.llm import LLMConfig


def load_config():
    """加载实际的配置文件（支持 JSONC）"""
    config_path = os.path.join(project_root, 'config', 'config.jsonc')
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def test_feature_disabled():
    """测试1：功能关闭时，始终使用默认模型"""
    print("=" * 80)
    print("Test 1: Risk Model Selection Disabled")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = False

    llm_config = LLMConfig.from_dict(config)

    result = llm_config.select_models_for_task(has_risk=True)

    print("\nOutput:")
    print(f"  Selected Model: {result['summary_model']}")
    print(f"  Selected Reasoning Effort: {result['summary_reasoning_effort']}")
    print(f"  has_risk flag: {result['has_risk']}")

    assert result['summary_model'] == llm_config.summary_model
    assert result['summary_reasoning_effort'] == llm_config.summary_reasoning_effort
    assert result['has_risk'] is False

    print("\n[PASS] Test 1: Feature disabled, using default model")


def test_feature_enabled_no_risk():
    """测试2：功能开启，无风险内容，使用默认模型"""
    print("\n" + "=" * 80)
    print("Test 2: Risk Model Selection Enabled - No Risk Content")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True

    llm_config = LLMConfig.from_dict(config)

    result = llm_config.select_models_for_task(has_risk=False)

    print("\nOutput:")
    print(f"  Selected Model: {result['summary_model']}")
    print(f"  Selected Reasoning Effort: {result['summary_reasoning_effort']}")
    print(f"  has_risk flag: {result['has_risk']}")

    assert result['summary_model'] == llm_config.summary_model
    assert result['summary_reasoning_effort'] == llm_config.summary_reasoning_effort
    assert result['has_risk'] is False

    print("\n[PASS] Test 2: No risk detected, using default model")


def test_feature_enabled_with_risk():
    """测试3：功能开启，检测到风险，切换到风险模型"""
    print("\n" + "=" * 80)
    print("Test 3: Risk Model Selection Enabled - Risk Content Detected")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True
    config['llm']['risk_summary_model'] = 'risk-summary-model'
    config['llm']['risk_summary_reasoning_effort'] = 'low'

    llm_config = LLMConfig.from_dict(config)

    result = llm_config.select_models_for_task(has_risk=True)

    print("\nOutput:")
    print(f"  Selected Model: {result['summary_model']}")
    print(f"  Selected Reasoning Effort: {result['summary_reasoning_effort']}")
    print(f"  has_risk flag: {result['has_risk']}")

    assert result['summary_model'] == 'risk-summary-model'
    assert result['summary_reasoning_effort'] == 'low'
    assert result['has_risk'] is True

    print("\n[PASS] Test 3: Risk detected, switched to risk model")


def test_reasoning_effort_propagation():
    """测试4：验证 reasoning_effort 在模型选择中正确传递"""
    print("\n" + "=" * 80)
    print("Test 4: Reasoning Effort Propagation")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True
    config['llm']['risk_calibrate_model'] = 'risk-calibrate-model'
    config['llm']['risk_calibrate_reasoning_effort'] = 'high'
    config['llm']['risk_summary_model'] = 'risk-summary-model'
    config['llm']['risk_summary_reasoning_effort'] = 'low'

    llm_config = LLMConfig.from_dict(config)

    result = llm_config.select_models_for_task(has_risk=True)

    print("\nOutput:")
    print(f"  Calibrate Model: {result['calibrate_model']}")
    print(f"  Calibrate Reasoning Effort: {result['calibrate_reasoning_effort']}")
    print(f"  Summary Model: {result['summary_model']}")
    print(f"  Summary Reasoning Effort: {result['summary_reasoning_effort']}")

    assert result['calibrate_model'] == 'risk-calibrate-model'
    assert result['calibrate_reasoning_effort'] == 'high'
    assert result['summary_model'] == 'risk-summary-model'
    assert result['summary_reasoning_effort'] == 'low'

    print("\n[PASS] Test 4: Reasoning effort correctly propagated")


def test_fallback_when_risk_model_missing():
    """测试5：风险模型未配置时回退到默认模型"""
    print("\n" + "=" * 80)
    print("Test 5: Fallback When Risk Model Missing")
    print("=" * 80)

    config = load_config()
    config['llm']['enable_risk_model_selection'] = True
    config['llm']['risk_summary_model'] = None

    llm_config = LLMConfig.from_dict(config)

    result = llm_config.select_models_for_task(has_risk=True)

    print("\nOutput:")
    print(f"  Summary Model: {result['summary_model']}")
    print(f"  Expected Default: {llm_config.summary_model}")

    assert result['summary_model'] == llm_config.summary_model
    assert result['has_risk'] is True

    print("\n[PASS] Test 5: Missing risk model falls back to default")


def run_all_tests():
    """运行所有测试"""
    print("\n" + "=" * 80)
    print("RISK MODEL SELECTION FEATURE TEST SUITE (NEW ARCH)")
    print("=" * 80)

    try:
        test_feature_disabled()
        test_feature_enabled_no_risk()
        test_feature_enabled_with_risk()
        test_reasoning_effort_propagation()
        test_fallback_when_risk_model_missing()

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
