"""测试分段质量验证逻辑

测试场景：
1. enable_validation=false: 不进行验证，直接返回校对结果
2. enable_validation=true: 每个chunk独立验证
   - 验证通过：保留校对结果
   - 验证失败：该chunk降级到原文
3. 合并后不再进行整体验证
"""

import sys
from pathlib import Path

# Add src to path
src_path = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(src_path))

from video_transcript_api.utils.llm.core.config import LLMConfig
from video_transcript_api.utils.llm.core.llm_client import LLMClient
from video_transcript_api.utils.llm.core.key_info_extractor import KeyInfoExtractor
from video_transcript_api.utils.llm.core.speaker_inferencer import SpeakerInferencer
from video_transcript_api.utils.llm.core.quality_validator import QualityValidator
from video_transcript_api.utils.llm.core.cache_manager import CacheManager
from video_transcript_api.utils.llm.processors.speaker_aware_processor import SpeakerAwareProcessor


def load_config():
    """Load config from config.jsonc"""
    import json
    import re

    config_path = Path(__file__).parent.parent.parent / "config" / "config.jsonc"

    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Remove comments
    content = re.sub(r'//.*', '', content)
    content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)

    return json.loads(content)


def create_test_dialogs():
    """Create test dialogs"""
    return [
        {"speaker": "Speaker1", "text": "Hello, how are you today?", "start_time": 0.0},
        {"speaker": "Speaker2", "text": "I am fine, thank you. What about you?", "start_time": 2.5},
        {"speaker": "Speaker1", "text": "I am good too. Let's discuss the project.", "start_time": 5.0},
        {"speaker": "Speaker2", "text": "Sure, what do you want to talk about?", "start_time": 7.5},
        {"speaker": "Speaker1", "text": "I think we need to improve the quality validation logic.", "start_time": 10.0},
    ] * 40  # Repeat to create longer text


def test_chunk_validation_disabled():
    """Test 1: enable_validation=false"""
    print("\n" + "=" * 80)
    print("Test 1: enable_validation=false (No validation)")
    print("=" * 80)

    config_dict = load_config()

    # Override config
    config_dict["llm"]["structured_calibration"]["enable_validation"] = False

    config = LLMConfig.from_dict(config_dict)

    print(f"enable_validation: {config.enable_validation}")

    # Create components
    llm_client = LLMClient(
        api_key=config.api_key,
        base_url=config.base_url,
        max_retries=config.max_retries,
        retry_delay=config.retry_delay,
    )

    cache_manager = CacheManager(config_dict["storage"]["cache_dir"])

    key_info_extractor = KeyInfoExtractor(
        llm_client=llm_client,
        cache_manager=cache_manager,
        model=config.key_info_model or config.calibrate_model,
        reasoning_effort=config.key_info_reasoning_effort,
    )

    speaker_inferencer = SpeakerInferencer(
        llm_client=llm_client,
        cache_manager=cache_manager,
        model=config.speaker_model or config.calibrate_model,
        reasoning_effort=config.speaker_reasoning_effort,
    )

    quality_validator = QualityValidator(
        llm_client=llm_client,
        overall_score_threshold=config.overall_score_threshold,
        minimum_single_score=config.minimum_single_score,
        validator_model=config.validator_model or config.calibrate_model,
        validator_reasoning_effort=config.validator_reasoning_effort,
    )

    processor = SpeakerAwareProcessor(
        config=config,
        llm_client=llm_client,
        key_info_extractor=key_info_extractor,
        speaker_inferencer=speaker_inferencer,
        quality_validator=quality_validator,
    )

    # Process
    dialogs = create_test_dialogs()

    result = processor.process(
        dialogs=dialogs,
        title="Test Video - Chunk Validation Disabled",
        author="Test Author",
        description="Testing chunk validation logic",
        platform="test",
        media_id="test_001",
    )

    print("\nResult:")
    print(f"Original length: {result['stats']['original_length']}")
    print(f"Calibrated length: {result['stats']['calibrated_length']}")
    print(f"Dialog count: {result['stats']['dialog_count']}")
    print(f"Chunk count: {result['stats']['chunk_count']}")
    print("\nExpected: No validation, all chunks should use calibrated results")


def test_chunk_validation_enabled():
    """Test 2: enable_validation=true"""
    print("\n" + "=" * 80)
    print("Test 2: enable_validation=true (Validate each chunk)")
    print("=" * 80)

    config_dict = load_config()

    # Override config
    config_dict["llm"]["structured_calibration"]["enable_validation"] = True

    config = LLMConfig.from_dict(config_dict)

    print(f"enable_validation: {config.enable_validation}")

    # Create components (same as test 1)
    llm_client = LLMClient(
        api_key=config.api_key,
        base_url=config.base_url,
        max_retries=config.max_retries,
        retry_delay=config.retry_delay,
    )

    cache_manager = CacheManager(config_dict["storage"]["cache_dir"])

    key_info_extractor = KeyInfoExtractor(
        llm_client=llm_client,
        cache_manager=cache_manager,
        model=config.key_info_model or config.calibrate_model,
        reasoning_effort=config.key_info_reasoning_effort,
    )

    speaker_inferencer = SpeakerInferencer(
        llm_client=llm_client,
        cache_manager=cache_manager,
        model=config.speaker_model or config.calibrate_model,
        reasoning_effort=config.speaker_reasoning_effort,
    )

    quality_validator = QualityValidator(
        llm_client=llm_client,
        overall_score_threshold=config.overall_score_threshold,
        minimum_single_score=config.minimum_single_score,
        validator_model=config.validator_model or config.calibrate_model,
        validator_reasoning_effort=config.validator_reasoning_effort,
    )

    processor = SpeakerAwareProcessor(
        config=config,
        llm_client=llm_client,
        key_info_extractor=key_info_extractor,
        speaker_inferencer=speaker_inferencer,
        quality_validator=quality_validator,
    )

    # Process
    dialogs = create_test_dialogs()

    result = processor.process(
        dialogs=dialogs,
        title="Test Video - Chunk Validation Enabled",
        author="Test Author",
        description="Testing chunk validation logic",
        platform="test",
        media_id="test_002",
    )

    print("\nResult:")
    print(f"Original length: {result['stats']['original_length']}")
    print(f"Calibrated length: {result['stats']['calibrated_length']}")
    print(f"Dialog count: {result['stats']['dialog_count']}")
    print(f"Chunk count: {result['stats']['chunk_count']}")
    print("\nExpected: Each chunk validated independently, failed chunks fall back to original")


if __name__ == "__main__":
    try:
        # Test 1: Validation disabled
        test_chunk_validation_disabled()

        # Test 2: Validation enabled
        test_chunk_validation_enabled()

        print("\n" + "=" * 80)
        print("All tests completed successfully!")
        print("=" * 80)

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
