"""
Real-world test for wechat notification with actual webhook

This test demonstrates:
1. No need to worry about text length - wecom-notifier handles it automatically
2. Fully async mode - messages are submitted and processed in background
3. No blocking of worker threads - high throughput

Note: Since messages are sent asynchronously, we add delays between tests
      to allow background processing to complete.
"""

import sys
import os

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(project_root, "src"))

from video_transcript_api.utils.wechat import (
    WechatNotifier,
    send_long_text_wechat,
)
from video_transcript_api.utils.logger import setup_logger

# Setup logger
logger = setup_logger("test_wechat_real")

# Real webhook URL
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=01ae2f25-ec29-4256-9fc1-22450f88add7"


def test_short_message():
    """Test 1: Short message (basic test)"""
    logger.info("=" * 60)
    logger.info("Test 1: Short message")
    logger.info("=" * 60)

    notifier = WechatNotifier(webhook=WEBHOOK_URL)

    message = """# Wechat Migration Test - Short Message

This is a **short test message** to verify the basic functionality.

## Features Verified
- Basic markdown_v2 sending
- URL protection in risk control
- Automatic rate limiting
- Fully async mode (no blocking)

Test timestamp: 2025-10-19 20:15:00
"""

    # In async mode, success means "submitted successfully", not "sent successfully"
    success = notifier.send_markdown_v2(message)
    logger.info(f"Short message test (submitted): {'PASS' if success else 'FAIL'}")

    # Give background thread time to process
    import time
    logger.info("Waiting 2 seconds for background processing...")
    time.sleep(2)

    return success


def test_medium_message():
    """Test 2: Medium message (~2KB)"""
    logger.info("=" * 60)
    logger.info("Test 2: Medium message (~2KB)")
    logger.info("=" * 60)

    # Generate medium-length content
    content_lines = [
        f"Line {i}: This is a medium-length test message. "
        f"We are testing automatic segmentation handling. "
        f"The wecom-notifier package should handle this seamlessly. "
        f"中文测试：这是中文内容测试，验证字节计算是否正确。"
        for i in range(20)
    ]
    content = "\n\n".join(content_lines)

    message = f"""# Medium Message Test

{content}

## Test Info
- Message length: {len(content)} characters
- Estimated bytes: {len(content.encode('utf-8'))} bytes
- Expected: Single segment
"""

    notifier = WechatNotifier(webhook=WEBHOOK_URL)
    success = notifier.send_markdown_v2(message)

    logger.info(f"Medium message ({len(content)} chars): {'PASS' if success else 'FAIL'}")
    return success


def test_large_message():
    """Test 3: Large message (~10KB, should auto-segment)"""
    logger.info("=" * 60)
    logger.info("Test 3: Large message (~10KB)")
    logger.info("=" * 60)

    # Generate large content
    content_lines = [
        f"Paragraph {i}: This is a large-scale test to verify automatic segmentation. "
        f"We are generating enough content to exceed the 4096-byte limit. "
        f"The wecom-notifier package should automatically split this into multiple segments. "
        f"Each segment should be sent sequentially with proper rate limiting. "
        f"中文段落 {i}：这是一段中文内容，用于测试多字节字符的处理。"
        f"我们需要确保分段算法能正确处理 UTF-8 编码。"
        for i in range(100)
    ]
    content = "\n\n".join(content_lines)

    total_chars = len(content)
    total_bytes = len(content.encode('utf-8'))
    estimated_segments = (total_bytes // 4000) + 1

    message = f"""# Large Message Test

## Test Parameters
- Total characters: {total_chars}
- Total bytes: {total_bytes}
- Estimated segments: {estimated_segments}

## Content

{content}

## End of Test
This message was sent without manual segmentation handling.
The wecom-notifier package handled everything automatically.
"""

    logger.info(f"Sending large message: {total_chars} chars, {total_bytes} bytes")

    notifier = WechatNotifier(webhook=WEBHOOK_URL)
    success = notifier.send_markdown_v2(message)

    logger.info(f"Large message test: {'PASS' if success else 'FAIL'}")
    return success


def test_very_large_message():
    """Test 4: Very large message (~40KB, multiple segments)"""
    logger.info("=" * 60)
    logger.info("Test 4: Very large message (~40KB)")
    logger.info("=" * 60)

    # Generate very large content
    content_lines = [
        f"Section {i}: This is a very large test to verify automatic handling of extremely long text. "
        f"We are generating content that will require multiple segments (10+ segments). "
        f"The wecom-notifier package should handle this transparently. "
        f"Rate limiting should be applied automatically between segments. "
        f"The application code doesn't need to worry about text length at all. "
        f"第 {i} 节：这是一段很长的中文测试内容，用于验证系统对超长文本的处理能力。"
        f"系统应该能够自动分段发送，并且正确处理 UTF-8 多字节字符。"
        f"我们不需要在应用代码中手动处理分段逻辑。"
        for i in range(300)
    ]
    content = "\n\n".join(content_lines)

    total_chars = len(content)
    total_bytes = len(content.encode('utf-8'))
    estimated_segments = (total_bytes // 4000) + 1

    message = f"""# Very Large Message Test

## Test Parameters
- Total characters: {total_chars}
- Total bytes: {total_bytes}
- Estimated segments: {estimated_segments}

## Content

{content}

## Test Summary
This extremely long message demonstrates:
1. Automatic multi-segment handling
2. Proper UTF-8 byte calculation
3. Automatic rate limiting between segments
4. No manual intervention required
5. Application code can send any length text directly

Test completed successfully!
"""

    logger.info(f"Sending very large message: {total_chars} chars, {total_bytes} bytes")

    notifier = WechatNotifier(webhook=WEBHOOK_URL)
    success = notifier.send_markdown_v2(message)

    logger.info(f"Very large message test: {'PASS' if success else 'FAIL'}")
    return success


def test_long_text_function():
    """Test 5: Using send_long_text_wechat function"""
    logger.info("=" * 60)
    logger.info("Test 5: send_long_text_wechat function")
    logger.info("=" * 60)

    # Generate content for the function test
    content = "\n\n".join([
        f"Calibrated text line {i}: This is a test of the send_long_text_wechat function. "
        f"This function used to have complex manual segmentation logic (100+ lines). "
        f"Now it's simplified to just ~40 lines, delegating all the hard work to wecom-notifier. "
        f"校对文本第 {i} 行：这是对发送长文本函数的测试。"
        f"该函数之前有复杂的手动分段逻辑（100多行代码）。"
        f"现在简化到只有约40行，所有复杂工作都交给 wecom-notifier 处理。"
        for i in range(80)
    ])

    total_chars = len(content)
    total_bytes = len(content.encode('utf-8'))

    logger.info(f"Sending calibrated text: {total_chars} chars, {total_bytes} bytes")

    success = send_long_text_wechat(
        title="Long Text Function Test",
        url="https://www.youtube.com/watch?v=test_long_function",
        text=content,
        is_summary=False,
        webhook=WEBHOOK_URL,
        has_speaker_recognition=True
    )

    logger.info(f"send_long_text_wechat test: {'PASS' if success else 'FAIL'}")
    return success


def test_with_urls_and_risk_control():
    """Test 6: Message with multiple URLs and risk control"""
    logger.info("=" * 60)
    logger.info("Test 6: URLs and risk control")
    logger.info("=" * 60)

    message = """# URL Protection and Risk Control Test

## Test URLs
These URLs should be protected from risk control processing:

1. YouTube: https://www.youtube.com/watch?v=dQw4w9WgXcQ
2. Bilibili: https://www.bilibili.com/video/BV1xx411c7mD
3. Xiaohongshu: https://www.xiaohongshu.com/explore/123456?xsec_token=test_token_123
4. GitHub: https://github.com/anthropics/claude-code
5. Documentation: https://docs.claude.com/en/docs/claude-code

## Content with URLs
This is a paragraph containing inline URLs like https://example.com/path/to/resource
and https://api.example.com/v1/endpoint?param=value&key=secret.

These URLs should remain intact even if they contain keywords that might trigger
risk control in other contexts.

## Risk Control Test
The risk control system should:
1. Extract and protect all URLs before processing
2. Apply risk control to non-URL content
3. Restore URLs after processing
4. Ensure URLs are not modified

Test completed!
"""

    notifier = WechatNotifier(webhook=WEBHOOK_URL)
    success = notifier.send_markdown_v2(message)

    logger.info(f"URL protection test: {'PASS' if success else 'FAIL'}")
    return success


def run_all_tests():
    """Run all real-world tests"""
    logger.info("\n" + "=" * 60)
    logger.info("Real-World WeChat Notification Tests")
    logger.info("Testing with actual webhook URL")
    logger.info("=" * 60 + "\n")

    tests = [
        ("Short Message (~1KB)", test_short_message),
        ("Medium Message (~2KB)", test_medium_message),
        ("Large Message (~10KB)", test_large_message),
        ("Very Large Message (~40KB)", test_very_large_message),
        ("send_long_text_wechat Function", test_long_text_function),
        ("URL Protection & Risk Control", test_with_urls_and_risk_control),
    ]

    results = []

    for test_name, test_func in tests:
        try:
            result = test_func()
            results.append((test_name, result))
        except Exception as e:
            logger.exception(f"Test '{test_name}' raised exception: {e}")
            results.append((test_name, False))

        # Delay between tests to avoid rate limiting
        # Longer delay for tests with many segments
        import time
        delay = 10 if test_name in ["Large Message (~10KB)", "Very Large Message (~40KB)"] else 5
        logger.info(f"Waiting {delay} seconds before next test...")
        time.sleep(delay)

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("Test Summary")
    logger.info("=" * 60)

    for test_name, result in results:
        status = "PASS" if result else "FAIL"
        logger.info(f"{test_name}: {status}")

    passed = sum(1 for _, result in results if result)
    total = len(results)

    logger.info("=" * 60)
    logger.info(f"Total: {passed}/{total} tests passed")
    logger.info("=" * 60)

    # Key insight
    logger.info("\n" + "=" * 60)
    logger.info("KEY INSIGHTS")
    logger.info("=" * 60)
    logger.info("With wecom-notifier package + Async mode:")
    logger.info("")
    logger.info("1. Text Length: No need to worry, auto-segmentation")
    logger.info("2. Rate Limiting: Handled automatically in background")
    logger.info("3. Worker Threads: Never blocked by notifications")
    logger.info("4. Throughput: High - submit and continue immediately")
    logger.info("5. Reliability: Auto-retry on failures (65s wait for rate limit)")
    logger.info("")
    logger.info("Note: 'PASS' means 'submitted successfully', not 'sent successfully'.")
    logger.info("      Actual sending happens in background threads.")
    logger.info("      Check wecom_notifier logs for actual send status.")
    logger.info("=" * 60 + "\n")

    return passed == total


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Real-world wechat notification tests")
    parser.add_argument(
        "--test",
        choices=["all", "short", "medium", "large", "very_large", "function", "url"],
        default="all",
        help="Which test to run"
    )

    args = parser.parse_args()

    if args.test == "all":
        success = run_all_tests()
    elif args.test == "short":
        success = test_short_message()
    elif args.test == "medium":
        success = test_medium_message()
    elif args.test == "large":
        success = test_large_message()
    elif args.test == "very_large":
        success = test_very_large_message()
    elif args.test == "function":
        success = test_long_text_function()
    elif args.test == "url":
        success = test_with_urls_and_risk_control()

    sys.exit(0 if success else 1)
