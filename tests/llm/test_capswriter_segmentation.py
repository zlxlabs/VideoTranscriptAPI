"""
测试 CapsWriter 格式文本分段和校对功能

验证目标:
1. 检测到 CapsWriter 格式（短句换行，无标点）
2. 正确分段（应该产生多个段落，而不是一个）
3. 校对后长度不会大幅压缩（保持在原始长度的80%以上）
"""
import os
import sys
import json

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, project_root)

from src.video_transcript_api.utils.text_segmentation import TextSegmentationProcessor
from src.video_transcript_api.utils.llm_segmented import SegmentedLLMProcessor
from src.video_transcript_api.utils.logger import setup_logger

logger = setup_logger(__name__)


def load_config():
    """Load configuration from config file"""
    config_path = os.path.join(project_root, 'config', 'config.json')

    if not os.path.exists(config_path):
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_capswriter_text():
    """Load CapsWriter format text from test file"""
    test_file = os.path.join(
        project_root,
        'data', 'cache', 'youtube', '2025', '202510', 'g5Q8NK5fXSE',
        'transcript_capswriter.txt'
    )

    if not os.path.exists(test_file):
        logger.error(f"Test file not found: {test_file}")
        sys.exit(1)

    with open(test_file, 'r', encoding='utf-8') as f:
        return f.read()


def main():
    """Main test function"""
    logger.info("Starting CapsWriter segmentation and calibration test")

    # Load configuration
    config = load_config()

    # Load test text
    original_text = load_capswriter_text()
    original_length = len(original_text)
    original_lines = len([line for line in original_text.split('\n') if line.strip()])

    logger.info(f"Original text loaded:")
    logger.info(f"  - Length: {original_length} characters")
    logger.info(f"  - Lines: {original_lines} lines")

    # Test 1: Check format detection and segmentation
    logger.info(f"\n{'='*60}")
    logger.info("TEST 1: Format Detection and Segmentation")
    logger.info(f"{'='*60}")

    segmentation_processor = TextSegmentationProcessor(config)
    segments = segmentation_processor.segment_txt_content(original_text)

    logger.info(f"Segmentation results:")
    logger.info(f"  - Number of segments: {len(segments)}")
    for i, segment in enumerate(segments):
        logger.info(f"  - Segment {i+1} length: {len(segment)} characters")

    # Check if segmentation is working correctly
    if len(segments) <= 1:
        logger.error("FAILED: Text was not properly segmented (only 1 segment created)")
    else:
        logger.info(f"SUCCESS: Text properly segmented into {len(segments)} segments")

    # Test 2: Calibration without compression (using first 10000 characters as sample)
    logger.info(f"\n{'='*60}")
    logger.info("TEST 2: Calibration Compression Check")
    logger.info(f"{'='*60}")

    # Use first 10000 characters to speed up test
    sample_text = original_text[:10000]
    sample_length = len(sample_text)
    logger.info(f"Using sample text: {sample_length} characters")

    # Initialize processor
    llm_processor = SegmentedLLMProcessor(config)

    # Perform segmentation on sample
    sample_segments = segmentation_processor.segment_txt_content(sample_text)
    logger.info(f"Sample segmented into {len(sample_segments)} segments")

    # Calibrate first segment only (to save API calls)
    if len(sample_segments) > 0:
        first_segment = sample_segments[0]
        first_segment_length = len(first_segment)
        logger.info(f"Testing calibration on first segment ({first_segment_length} characters)")

        # Generate calibration prompt
        prompt = llm_processor._generate_calibrate_prompt(
            first_segment,
            use_speaker_recognition=False,
            title="娃哈哈国有资产流失解密",
            author="",
            description="解密娃哈哈国有资产流失过程和宗庆后家族内部冲突"
        )

        # Call LLM for calibration
        from src.video_transcript_api.utils.llm import call_llm_api
        calibrated_segment = call_llm_api(
            model=config['llm']['calibrate_model'],
            prompt=prompt,
            api_key=config['llm']['api_key'],
            base_url=config['llm']['base_url'],
            max_retries=config['llm']['max_retries'],
            retry_delay=config['llm']['retry_delay']
        )

        calibrated_length = len(calibrated_segment)
        compression_ratio = calibrated_length / first_segment_length

        logger.info(f"\n{'='*60}")
        logger.info("CALIBRATION RESULTS")
        logger.info(f"{'='*60}")
        logger.info(f"Original segment length: {first_segment_length} characters")
        logger.info(f"Calibrated segment length: {calibrated_length} characters")
        logger.info(f"Compression ratio: {compression_ratio:.2%}")
        logger.info(f"Length change: {calibrated_length - first_segment_length:+d} characters")

        # Save results for inspection
        output_dir = os.path.join(project_root, 'tests', 'llm', 'output')
        os.makedirs(output_dir, exist_ok=True)

        original_output = os.path.join(output_dir, 'capswriter_original_segment.txt')
        calibrated_output = os.path.join(output_dir, 'capswriter_calibrated_segment.txt')

        with open(original_output, 'w', encoding='utf-8') as f:
            f.write(first_segment)

        with open(calibrated_output, 'w', encoding='utf-8') as f:
            f.write(calibrated_segment)

        logger.info(f"\nOriginal segment saved to: {original_output}")
        logger.info(f"Calibrated segment saved to: {calibrated_output}")

        # Check if calibration preserved content length
        if compression_ratio < 0.8:
            logger.warning(f"WARNING: Calibration compressed content by more than 20%!")
            logger.warning(f"Expected ratio >= 0.80, got {compression_ratio:.2%}")
        else:
            logger.info(f"SUCCESS: Calibration preserved content length (ratio: {compression_ratio:.2%})")

        # Show preview
        logger.info(f"\nOriginal segment preview (first 300 chars):")
        logger.info(f"{first_segment[:300]}...")
        logger.info(f"\nCalibrated segment preview (first 300 chars):")
        logger.info(f"{calibrated_segment[:300]}...")

    logger.info(f"\n{'='*60}")
    logger.info("Test completed")
    logger.info(f"{'='*60}")


if __name__ == '__main__':
    main()
