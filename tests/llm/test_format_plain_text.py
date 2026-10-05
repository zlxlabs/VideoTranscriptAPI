"""Test for _format_plain_text method in PlainTextProcessor

Test scenarios:
- Type A: Text wall (long text without line breaks)
- Type B: Over-segmented text (one sentence per line)
- Type C: Reasonable paragraphs (should keep original)
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root / "src"))

from video_transcript_api.llm.processors.plain_text_processor import PlainTextProcessor
from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.core.llm_client import LLMClient
from video_transcript_api.llm.core.key_info_extractor import KeyInfoExtractor
from video_transcript_api.llm.validators.unified_quality_validator import UnifiedQualityValidator


def create_test_processor():
    """Create a test processor instance"""
    config = LLMConfig(
        api_key="test",
        base_url="http://test",
        calibrate_model="test-model",
        summary_model="test-model",
        enable_threshold=5000,
        segment_size=2000,
        max_segment_size=3000,
        concurrent_workers=10,
        min_calibrate_ratio=0.8,
    )
    llm_client = LLMClient(
        api_key="test",
        base_url="http://test",
    )
    key_info_extractor = KeyInfoExtractor(llm_client, "test-model")
    quality_validator = UnifiedQualityValidator(
        llm_client=llm_client,
        model="test-model",
        reasoning_effort=None,
        score_weights={
            "accuracy": 0.4,
            "completeness": 0.3,
            "fluency": 0.2,
            "format": 0.1,
        },
        overall_score_threshold=8.0,
        minimum_single_score=7.0,
    )

    return PlainTextProcessor(
        config=config,
        llm_client=llm_client,
        key_info_extractor=key_info_extractor,
        quality_validator=quality_validator,
    )


def test_type_a_text_wall():
    """Test Type A: Long text wall without line breaks"""
    print("=" * 60)
    print("Test Type A: Text Wall")
    print("=" * 60)

    processor = create_test_processor()

    # Simulate a long text without line breaks
    text = (
        "今天天气很好。我决定去公园散步。公园里有很多人。"
        "有的在跑步，有的在遛狗。我找了一个长椅坐下。"
        "看着远处的湖面，心情很平静。突然听到有人叫我的名字。"
        "原来是老朋友小王。我们很久没见面了。他告诉我他最近换了工作。"
        "新工作离家很近，不用挤地铁了。我为他高兴。"
        "我们聊了很久，约好下次一起吃饭。"
    )

    print(f"Original text ({len(text)} chars, {len(text.split('。'))} sentences):")
    print(text)
    print("\nFormatted text:")

    formatted = processor._format_plain_text(text)
    print(formatted)

    paragraphs = formatted.split('\n\n')
    print(f"\nResult: {len(paragraphs)} paragraphs")
    for i, para in enumerate(paragraphs, 1):
        print(f"  Paragraph {i}: {len(para)} chars, {para.count('。')} sentences")


def test_type_b_over_segmented():
    """Test Type B: Over-segmented text (one sentence per line)"""
    print("\n" + "=" * 60)
    print("Test Type B: Over-Segmented Text")
    print("=" * 60)

    processor = create_test_processor()

    # Simulate over-segmented text (this is the problem you encountered)
    text = """今天天气很好。
我决定去公园散步。
公园里有很多人。
有的在跑步，有的在遛狗。
我找了一个长椅坐下。
看着远处的湖面，心情很平静。
突然听到有人叫我的名字。
原来是老朋友小王。
我们很久没见面了。
他告诉我他最近换了工作。
新工作离家很近，不用挤地铁了。
我为他高兴。
我们聊了很久。
约好下次一起吃饭。"""

    lines = [line for line in text.split('\n') if line.strip()]
    print(f"Original text ({len(text)} chars, {len(lines)} lines):")
    print(text)
    print("\nFormatted text:")

    formatted = processor._format_plain_text(text)
    print(formatted)

    paragraphs = formatted.split('\n\n')
    print(f"\nResult: {len(paragraphs)} paragraphs")
    for i, para in enumerate(paragraphs, 1):
        print(f"  Paragraph {i}: {len(para)} chars, {para.count('。')} sentences")


def test_type_c_reasonable_paragraphs():
    """Test Type C: Reasonable paragraphs (should keep original)"""
    print("\n" + "=" * 60)
    print("Test Type C: Reasonable Paragraphs")
    print("=" * 60)

    processor = create_test_processor()

    # Simulate text with reasonable paragraph structure
    text = """今天天气很好，我决定去公园散步。公园里有很多人，有的在跑步，有的在遛狗。

我找了一个长椅坐下，看着远处的湖面，心情很平静。突然听到有人叫我的名字，原来是老朋友小王。

我们很久没见面了，他告诉我他最近换了工作。新工作离家很近，不用挤地铁了，我为他高兴。

我们聊了很久，约好下次一起吃饭。天色渐晚，我们互相道别，各自回家。"""

    lines = [line for line in text.split('\n') if line.strip()]
    print(f"Original text ({len(text)} chars, {len(lines)} lines):")
    print(text)
    print("\nFormatted text:")

    formatted = processor._format_plain_text(text)
    print(formatted)

    if formatted == text:
        print("\nResult: Text structure is reasonable, kept original")
    else:
        print("\nResult: Text was reformatted")


def test_short_text():
    """Test short text (should keep original)"""
    print("\n" + "=" * 60)
    print("Test: Short Text")
    print("=" * 60)

    processor = create_test_processor()

    text = "这是一段很短的文本。"

    print(f"Original text ({len(text)} chars):")
    print(text)

    formatted = processor._format_plain_text(text)
    print("\nFormatted text:")
    print(formatted)

    if formatted == text:
        print("\nResult: Short text kept original")


if __name__ == "__main__":
    test_type_a_text_wall()
    test_type_b_over_segmented()
    test_type_c_reasonable_paragraphs()
    test_short_text()

    print("\n" + "=" * 60)
    print("All tests completed")
    print("=" * 60)
