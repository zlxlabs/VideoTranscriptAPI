"""
Test script for wechat notification migration to wecom-notifier

Test cases:
1. Basic text message sending
2. Long text auto-segmentation
3. URL protection in risk control
4. Task status notification
5. View link sending
"""

import sys
import os

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(project_root, "src"))

from video_transcript_api.utils.notifications import (
    WechatNotifier,
    send_long_text_wechat,
    send_view_link_wechat,
)
from video_transcript_api.utils.logging import setup_logger

# Setup logger
logger = setup_logger("test_wechat_migration")


def test_basic_message():
    """Test 1: Basic message sending"""
    logger.info("=" * 60)
    logger.info("Test 1: Basic message sending")
    logger.info("=" * 60)

    notifier = WechatNotifier()

    test_message = """# Test Message

This is a test message for **wecom-notifier** migration.

## Features
- Auto rate limiting
- Auto segmentation
- Markdown_v2 support

Test URL: https://www.youtube.com/watch?v=test123
"""

    success = notifier.send_markdown_v2(test_message)
    logger.info(f"Test 1 result: {'PASS' if success else 'FAIL'}")
    return success


def test_long_text_segmentation():
    """Test 2: Long text auto-segmentation"""
    logger.info("=" * 60)
    logger.info("Test 2: Long text auto-segmentation")
    logger.info("=" * 60)

    # Generate a long text (over 4096 bytes)
    long_text = "\n".join([
        f"Line {i}: This is a test line with some content to make it longer. "
        f"Adding more text to ensure we exceed the byte limit. "
        f"Chinese characters: 这是中文测试内容，用来测试字节计算是否正确。"
        for i in range(200)
    ])

    logger.info(f"Generated long text with {len(long_text)} characters, "
                f"{len(long_text.encode('utf-8'))} bytes")

    success = send_long_text_wechat(
        title="Long Text Segmentation Test",
        url="https://www.youtube.com/watch?v=test_long_text",
        text=long_text,
        is_summary=False
    )

    logger.info(f"Test 2 result: {'PASS' if success else 'FAIL'}")
    return success


def test_url_protection():
    """Test 3: URL protection in risk control"""
    logger.info("=" * 60)
    logger.info("Test 3: URL protection in risk control")
    logger.info("=" * 60)

    # Text with multiple URLs
    text_with_urls = """# URL Protection Test

Here are some test URLs that should be protected:
- YouTube: https://www.youtube.com/watch?v=test123
- Bilibili: https://www.bilibili.com/video/BV1234567890
- Xiaohongshu: https://www.xiaohongshu.com/explore/123456?xsec_token=abc123

These URLs should NOT be affected by risk control.

Test content with normal text that might need risk control.
"""

    notifier = WechatNotifier()
    success = notifier.send_markdown_v2(text_with_urls)

    logger.info(f"Test 3 result: {'PASS' if success else 'FAIL'}")
    return success


def test_task_status_notification():
    """Test 4: Task status notification"""
    logger.info("=" * 60)
    logger.info("Test 4: Task status notification")
    logger.info("=" * 60)

    notifier = WechatNotifier()

    # Test different status notifications
    test_cases = [
        {
            "url": "https://www.youtube.com/watch?v=test_status",
            "status": "Download started",
            "title": "Test Video Title",
            "author": "Test Author"
        },
        {
            "url": "https://www.bilibili.com/video/BV1234567890",
            "status": "Transcription completed",
            "title": "Another Test Video",
            "author": "Another Author",
            "transcript": "This is a test transcript preview. " * 10
        },
        {
            "url": "https://www.xiaohongshu.com/explore/123456",
            "status": "Processing failed",
            "title": "Failed Test",
            "error": "Test error message"
        }
    ]

    all_success = True
    for i, test_case in enumerate(test_cases, 1):
        logger.info(f"Sending status notification {i}/3...")
        success = notifier.notify_task_status(**test_case)
        all_success = all_success and success

        # Small delay between messages
        import time
        time.sleep(1)

    logger.info(f"Test 4 result: {'PASS' if all_success else 'FAIL'}")
    return all_success


def test_view_link():
    """Test 5: View link sending"""
    logger.info("=" * 60)
    logger.info("Test 5: View link sending")
    logger.info("=" * 60)

    try:
        success = send_view_link_wechat(
            title="Test View Link",
            view_token="test_token_123",
            original_url="https://www.youtube.com/watch?v=test_view_link"
        )

        logger.info(f"Test 5 result: {'PASS' if success else 'FAIL'}")
        return success
    except Exception as e:
        logger.error(f"Test 5 failed with exception: {e}")
        return False


def run_all_tests():
    """Run all test cases"""
    logger.info("\n" + "=" * 60)
    logger.info("Starting WeChat Migration Tests")
    logger.info("=" * 60 + "\n")

    tests = [
        ("Basic Message", test_basic_message),
        ("Long Text Segmentation", test_long_text_segmentation),
        ("URL Protection", test_url_protection),
        ("Task Status Notification", test_task_status_notification),
        ("View Link", test_view_link)
    ]

    results = []

    for test_name, test_func in tests:
        try:
            result = test_func()
            results.append((test_name, result))
        except Exception as e:
            logger.exception(f"Test '{test_name}' raised exception: {e}")
            results.append((test_name, False))

        # Delay between tests
        import time
        time.sleep(2)

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

    return passed == total


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test wechat notification migration")
    parser.add_argument(
        "--test",
        choices=["all", "basic", "long", "url", "status", "view"],
        default="all",
        help="Which test to run"
    )

    args = parser.parse_args()

    if args.test == "all":
        success = run_all_tests()
    elif args.test == "basic":
        success = test_basic_message()
    elif args.test == "long":
        success = test_long_text_segmentation()
    elif args.test == "url":
        success = test_url_protection()
    elif args.test == "status":
        success = test_task_status_notification()
    elif args.test == "view":
        success = test_view_link()

    sys.exit(0 if success else 1)
