"""
ASR monitor alert unit tests.

Covers:
- Alert trigger after N consecutive failures
- Debounce (no repeat alert within 30 min)
- Recovery notification after service comes back
- Service check logic

All console output must be in English only (no emoji, no Chinese).
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest


from video_transcript_api.utils.asr_monitor import ASRMonitor


@pytest.fixture
def mock_notifier():
    """Create a mock notifier."""
    return MagicMock()


@pytest.fixture
def monitor(mock_notifier):
    """Create an ASRMonitor with test configuration."""
    return ASRMonitor(
        services={"TestASR": "ws://localhost:9999"},
        check_interval=1,
        failure_threshold=3,
        debounce_seconds=60,
        notifier=mock_notifier,
    )


class TestAlertTrigger:
    """Verify alert trigger after consecutive failures."""

    def test_no_alert_below_threshold(self, monitor, mock_notifier):
        """Should not alert if failure count is below threshold."""
        # Simulate 2 failures (below threshold of 3)
        monitor._handle_check_result("TestASR", "ws://localhost:9999", False)
        monitor._handle_check_result("TestASR", "ws://localhost:9999", False)

        mock_notifier.send_text.assert_not_called()

    def test_alert_at_threshold(self, monitor, mock_notifier):
        """Should alert when failure count reaches threshold."""
        for _ in range(3):
            monitor._handle_check_result("TestASR", "ws://localhost:9999", False)

        mock_notifier.send_text.assert_called_once()
        alert_msg = mock_notifier.send_text.call_args[0][0]
        assert "TestASR" in alert_msg
        assert "3" in alert_msg  # failure count

    def test_counter_resets_on_success(self, monitor, mock_notifier):
        """Success should reset failure counter."""
        # 2 failures
        monitor._handle_check_result("TestASR", "ws://localhost:9999", False)
        monitor._handle_check_result("TestASR", "ws://localhost:9999", False)
        # 1 success resets
        monitor._handle_check_result("TestASR", "ws://localhost:9999", True)
        # 2 more failures (still below threshold)
        monitor._handle_check_result("TestASR", "ws://localhost:9999", False)
        monitor._handle_check_result("TestASR", "ws://localhost:9999", False)

        mock_notifier.send_text.assert_not_called()


class TestDebounce:
    """Verify alert debounce mechanism."""

    def test_debounce_prevents_repeat_alert(self, monitor, mock_notifier):
        """Should not send repeat alert within debounce period."""
        # Trigger first alert
        for _ in range(3):
            monitor._handle_check_result("TestASR", "ws://localhost:9999", False)

        assert mock_notifier.send_text.call_count == 1

        # More failures should not trigger another alert (within debounce)
        for _ in range(3):
            monitor._handle_check_result("TestASR", "ws://localhost:9999", False)

        assert mock_notifier.send_text.call_count == 1  # Still just 1

    def test_alert_after_debounce_period(self, monitor, mock_notifier):
        """Should send alert again after debounce period expires."""
        # Trigger first alert
        for _ in range(3):
            monitor._handle_check_result("TestASR", "ws://localhost:9999", False)

        assert mock_notifier.send_text.call_count == 1

        # Simulate debounce expiry
        monitor._last_alert_time["TestASR"] = time.time() - 61  # Past 60s debounce

        # More failures should now trigger
        for _ in range(3):
            monitor._handle_check_result("TestASR", "ws://localhost:9999", False)

        assert mock_notifier.send_text.call_count == 2


class TestRecoveryNotification:
    """Verify recovery notification."""

    def test_recovery_after_down(self, monitor, mock_notifier):
        """Should send recovery notification when service comes back."""
        # Service goes down
        for _ in range(3):
            monitor._handle_check_result("TestASR", "ws://localhost:9999", False)

        assert mock_notifier.send_text.call_count == 1  # Down alert

        # Service comes back
        monitor._handle_check_result("TestASR", "ws://localhost:9999", True)

        assert mock_notifier.send_text.call_count == 2  # Recovery alert
        recovery_msg = mock_notifier.send_text.call_args[0][0]
        assert "recovery" in recovery_msg.lower() or "恢复" in recovery_msg

    def test_no_recovery_if_never_down(self, monitor, mock_notifier):
        """Should not send recovery if service was never down."""
        monitor._handle_check_result("TestASR", "ws://localhost:9999", True)
        mock_notifier.send_text.assert_not_called()


class TestServiceCheck:
    """Verify service connectivity check.

    Note: the connectivity-level cases (real WS handshake, TCP-only false
    positive, unreachable port) live in ``test_asr_monitor_ws_probe.py``.
    Here we only cover the trivially-mockable unreachable-host case.
    """

    def test_service_unreachable_no_raise(self, monitor):
        """Probing a closed port must return False, not raise."""
        # 127.0.0.1:1 is reserved/closed; the probe should fail fast.
        assert monitor.check_service("TestASR", "ws://127.0.0.1:1") is False

    def test_start_and_stop(self, monitor):
        """Monitor should start and stop cleanly."""
        monitor.start()
        assert monitor._running is True
        monitor.stop()
        assert monitor._running is False
