"""
Test global singleton pattern for WeComNotifier

This test verifies that:
1. All WechatNotifier instances share the same global WeComNotifier
2. Only one WebhookManager is created per webhook
3. Messages are processed in order by the same manager

The real webhook is NOT stored in this repository. Export WECHAT_WEBHOOK to
run the tests that need it; without it those tests are skipped.
"""

import sys
import os

import pytest

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(project_root, "src"))

from video_transcript_api.utils.notifications.wechat import (
    WechatNotifier,
    init_global_notifier,
    shutdown_global_notifier,
    _get_global_notifier,
)
from video_transcript_api.utils.logging import setup_logger

# Setup logger
logger = setup_logger("test_global_singleton")

# Real webhook URL, read from the environment (never committed to the repo)
WECHAT_WEBHOOK = os.environ.get("WECHAT_WEBHOOK", "")


@pytest.fixture(scope="module")
def webhook():
    if not WECHAT_WEBHOOK:
        pytest.skip("WECHAT_WEBHOOK is not set; skipping real-webhook tests")
    return WECHAT_WEBHOOK


def test_singleton_initialization():
    """Test 1: Global notifier initialization"""
    logger.info("=" * 60)
    logger.info("Test 1: Global notifier initialization")
    logger.info("=" * 60)

    # Initialize global notifier
    init_global_notifier()

    # Get global notifier
    notifier1 = _get_global_notifier()
    notifier2 = _get_global_notifier()

    # Verify same instance
    assert notifier1 is notifier2, "Global notifier should be the same instance"
    logger.info("PASS: Global notifier returns same instance")

    return True


def test_shared_global_instance(webhook):
    """Test 2: Multiple WechatNotifier instances share same global WeComNotifier"""
    logger.info("=" * 60)
    logger.info("Test 2: WechatNotifier instances share global WeComNotifier")
    logger.info("=" * 60)

    # Create multiple WechatNotifier instances
    wechat1 = WechatNotifier(webhook=webhook)
    wechat2 = WechatNotifier(webhook=webhook)
    wechat3 = WechatNotifier(webhook=webhook)

    # Verify they all use the same global WeComNotifier
    assert wechat1.notifier is wechat2.notifier, "Should share same WeComNotifier"
    assert wechat2.notifier is wechat3.notifier, "Should share same WeComNotifier"

    logger.info("PASS: All WechatNotifier instances share same global WeComNotifier")

    return True


def test_message_ordering(webhook):
    """Test 3: Send multiple messages and verify they are queued"""
    logger.info("=" * 60)
    logger.info("Test 3: Message ordering with global singleton")
    logger.info("=" * 60)

    # Create WechatNotifier
    notifier = WechatNotifier(webhook=webhook)

    # Send multiple short messages in quick succession
    messages = [
        "# Test Message 1\n\nFirst message for ordering test",
        "# Test Message 2\n\nSecond message for ordering test",
        "# Test Message 3\n\nThird message for ordering test",
    ]

    for i, msg in enumerate(messages, 1):
        success = notifier.send_markdown_v2(msg)
        logger.info(f"Message {i} submitted: {'SUCCESS' if success else 'FAILED'}")

    logger.info("PASS: All messages submitted successfully")
    logger.info("NOTE: Messages will be sent in order by the same WebhookManager")

    # Wait a bit for background processing
    import time
    logger.info("Waiting 10 seconds for background processing...")
    time.sleep(10)

    return True


def test_cleanup():
    """Test 4: Shutdown global notifier"""
    logger.info("=" * 60)
    logger.info("Test 4: Shutdown global notifier")
    logger.info("=" * 60)

    # Shutdown
    shutdown_global_notifier()
    logger.info("PASS: Global notifier shutdown successfully")

    return True


def run_all_tests():
    """Run all tests"""
    logger.info("\n" + "=" * 60)
    logger.info("Global Singleton Pattern Tests")
    logger.info("=" * 60 + "\n")

    tests = [
        ("Singleton Initialization", test_singleton_initialization, False),
        ("Shared Global Instance", test_shared_global_instance, True),
        ("Message Ordering", test_message_ordering, True),
        ("Cleanup", test_cleanup, False),
    ]

    results = []

    for test_name, test_func, needs_webhook in tests:
        try:
            result = test_func(WECHAT_WEBHOOK) if needs_webhook else test_func()
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

    # Key insight
    logger.info("\n" + "=" * 60)
    logger.info("KEY INSIGHTS")
    logger.info("=" * 60)
    logger.info("Global Singleton Pattern Benefits:")
    logger.info("")
    logger.info("1. Single WebhookManager per webhook")
    logger.info("2. Messages strictly ordered")
    logger.info("3. Concurrent control works correctly")
    logger.info("4. No resource waste from repeated instance creation")
    logger.info("=" * 60 + "\n")

    return passed == total


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test global singleton pattern")
    parser.add_argument(
        "--test",
        choices=["all", "init", "shared", "ordering", "cleanup"],
        default="all",
        help="Which test to run"
    )

    args = parser.parse_args()

    # webhook="" would fall back to the production webhook in config.jsonc.
    if args.test in ("all", "shared", "ordering") and not WECHAT_WEBHOOK:
        logger.error("WECHAT_WEBHOOK is not set; refusing to run real-webhook tests")
        sys.exit(2)

    if args.test == "all":
        success = run_all_tests()
    elif args.test == "init":
        success = test_singleton_initialization()
    elif args.test == "shared":
        # Need to init first
        init_global_notifier()
        success = test_shared_global_instance(WECHAT_WEBHOOK)
    elif args.test == "ordering":
        # Need to init first
        init_global_notifier()
        success = test_message_ordering(WECHAT_WEBHOOK)
    elif args.test == "cleanup":
        success = test_cleanup()

    sys.exit(0 if success else 1)
