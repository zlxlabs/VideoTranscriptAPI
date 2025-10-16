"""
测试改进后的总结prompt，验证是否避免了英文解释中文的问题

使用 data/cache/youtube/2025/202510/g5Q8NK5fXSE/llm_calibrated.txt 的前3000字符进行测试
"""
import os
import sys
import json

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, project_root)

from src.video_transcript_api.utils.llm_enhanced import EnhancedLLMProcessor
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


def load_test_text():
    """Load first 3000 characters from calibrated text"""
    test_file = os.path.join(
        project_root,
        'data', 'cache', 'youtube', '2025', '202510', 'g5Q8NK5fXSE',
        'llm_calibrated.txt'
    )

    if not os.path.exists(test_file):
        logger.error(f"Test file not found: {test_file}")
        sys.exit(1)

    with open(test_file, 'r', encoding='utf-8') as f:
        full_text = f.read()

    # Return first 3000 characters
    return full_text[:3000]


def main():
    """Main test function"""
    logger.info("Starting summary prompt improvement test")

    # Load configuration
    config = load_config()

    # Load test text (first 3000 characters)
    test_text = load_test_text()
    logger.info(f"Loaded test text, length: {len(test_text)} characters")

    # Initialize enhanced LLM processor
    processor = EnhancedLLMProcessor(config)

    # Prepare test task
    llm_task = {
        'task_id': 'test_summary_prompt',
        'transcript': test_text,
        'use_speaker_recognition': False,
        'video_title': '娃哈哈国有资产流失解密',
        'author': '王教授财经频道',
        'description': '解密娃哈哈国有资产流失过程和宗庆后家族内部冲突',
        'transcription_data': None
    }

    logger.info("Processing LLM task with improved prompt...")

    # Process the task
    result = processor._process_original_logic(llm_task)

    # Save results to files for inspection
    output_dir = os.path.join(project_root, 'tests', 'llm', 'output')
    os.makedirs(output_dir, exist_ok=True)

    calibrated_output = os.path.join(output_dir, 'test_calibrated.txt')
    summary_output = os.path.join(output_dir, 'test_summary.txt')

    with open(calibrated_output, 'w', encoding='utf-8') as f:
        f.write(result.get('校对文本', ''))

    with open(summary_output, 'w', encoding='utf-8') as f:
        f.write(result.get('内容总结', ''))

    logger.info(f"Calibrated text saved to: {calibrated_output}")
    logger.info(f"Summary text saved to: {summary_output}")

    # Check for English annotations in summary
    summary_text = result.get('内容总结', '')

    # Count parentheses with English content
    import re
    english_pattern = r'\([A-Za-z\s,\.;:]+\)'
    english_matches = re.findall(english_pattern, summary_text)

    logger.info(f"\n{'='*60}")
    logger.info("IMPROVEMENT CHECK RESULTS")
    logger.info(f"{'='*60}")
    logger.info(f"Total English annotations found: {len(english_matches)}")

    if english_matches:
        logger.warning("Found English annotations in summary:")
        for i, match in enumerate(english_matches[:10], 1):  # Show first 10
            logger.warning(f"  {i}. {match}")
        if len(english_matches) > 10:
            logger.warning(f"  ... and {len(english_matches) - 10} more")
    else:
        logger.info("SUCCESS: No English annotations found in summary!")

    logger.info(f"{'='*60}")
    logger.info(f"Summary length: {len(summary_text)} characters")
    logger.info(f"Calibrated text length: {len(result.get('校对文本', ''))} characters")
    logger.info(f"{'='*60}\n")

    # Display first 500 characters of summary as preview
    logger.info("Summary preview (first 500 characters):")
    logger.info(f"\n{summary_text[:500]}...\n")

    logger.info("Test completed successfully")


if __name__ == '__main__':
    main()
