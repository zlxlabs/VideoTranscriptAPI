"""
Performance tracker unit tests.

Covers:
- Stage timing via context manager
- Counter tracking
- Summary generation
- Error handling (failed stages)
- Thread safety

All console output must be in English only (no emoji, no Chinese).
"""

import os
import sys
import threading
import time

import pytest


from video_transcript_api.utils.perf_tracker import PerfTracker


class TestPerfTrackerTiming:
    """Verify stage timing functionality."""

    def test_track_records_elapsed_time(self):
        """track() should record elapsed time for a stage."""
        tracker = PerfTracker(task_id="test-001")
        with tracker.track("download"):
            time.sleep(0.05)

        elapsed = tracker.get_elapsed("download")
        assert elapsed is not None
        assert elapsed >= 40  # At least 40ms (accounting for OS scheduling)

    def test_track_multiple_stages(self):
        """Multiple stages should all be recorded."""
        tracker = PerfTracker(task_id="test-002")
        with tracker.track("stage_a"):
            time.sleep(0.01)
        with tracker.track("stage_b"):
            time.sleep(0.01)

        summary = tracker.summary()
        assert "stage_a" in summary["stages"]
        assert "stage_b" in summary["stages"]
        assert summary["total_ms"] > 0

    def test_track_failed_stage(self):
        """Failed stages should be recorded with success=False."""
        tracker = PerfTracker(task_id="test-003")

        with pytest.raises(ValueError):
            with tracker.track("failing"):
                raise ValueError("simulated error")

        summary = tracker.summary()
        assert summary["stages"]["failing"]["failures"] == 1
        assert summary["stages"]["failing"]["count"] == 1

    def test_observation_contains_completed_interval_counts_only(self):
        tracker = PerfTracker(task_id="private-task-id")
        with tracker.track("download"):
            pass
        with pytest.raises(ValueError):
            with tracker.track("transcription"):
                raise ValueError("private error body")

        observation = tracker.observation()

        assert observation["stages"]["download"]["successes"] == 1
        assert observation["stages"]["download"]["failures"] == 0
        assert observation["stages"]["transcription"]["successes"] == 0
        assert observation["stages"]["transcription"]["failures"] == 1
        assert "private-task-id" not in str(observation)
        assert "private error body" not in str(observation)

    def test_observation_allowlists_cache_hit_counters(self):
        tracker = PerfTracker(task_id="private-task-id")
        tracker.count("cache_hit")
        tracker.count("cache_hit_partial", delta=2)
        tracker.count("secret_counter", delta=17)

        observation = tracker.observation()

        assert observation["counters"] == {"cache_hit": 1, "cache_hit_partial": 2}

    def test_track_same_stage_multiple_times(self):
        """Same stage can be tracked multiple times; durations accumulate."""
        tracker = PerfTracker(task_id="test-004")
        with tracker.track("retry"):
            time.sleep(0.01)
        with tracker.track("retry"):
            time.sleep(0.01)

        summary = tracker.summary()
        assert summary["stages"]["retry"]["count"] == 2


class TestPerfTrackerCounters:
    """Verify counter functionality."""

    def test_count_increments(self):
        """count() should increment a named counter."""
        tracker = PerfTracker(task_id="test-005")
        tracker.count("cache_hit")
        tracker.count("cache_hit")
        tracker.count("cache_miss")

        summary = tracker.summary()
        assert summary["counters"]["cache_hit"] == 2
        assert summary["counters"]["cache_miss"] == 1

    def test_count_custom_delta(self):
        """count() should support custom delta values."""
        tracker = PerfTracker(task_id="test-006")
        tracker.count("bytes_downloaded", delta=1024)
        tracker.count("bytes_downloaded", delta=2048)

        summary = tracker.summary()
        assert summary["counters"]["bytes_downloaded"] == 3072


class TestPerfTrackerSummary:
    """Verify summary output."""

    def test_empty_summary(self):
        """Empty tracker should return zeroed summary."""
        tracker = PerfTracker(task_id="test-007")
        summary = tracker.summary()
        assert summary["task_id"] == "test-007"
        assert summary["total_ms"] == 0
        assert summary["stages"] == {}
        assert summary["counters"] == {}

    def test_get_elapsed_missing_stage(self):
        """get_elapsed for non-existent stage should return None."""
        tracker = PerfTracker(task_id="test-008")
        assert tracker.get_elapsed("nonexistent") is None

    def test_log_summary_no_error(self):
        """log_summary() should not raise."""
        tracker = PerfTracker(task_id="test-009")
        with tracker.track("test"):
            pass
        tracker.count("hits", 5)
        # Should not raise
        tracker.log_summary()


class TestPerfTrackerThreadSafety:
    """Verify thread safety of PerfTracker."""

    def test_concurrent_tracking(self):
        """Multiple threads recording stages concurrently should not lose data."""
        tracker = PerfTracker(task_id="test-010")
        errors = []

        def track_stage(name):
            try:
                with tracker.track(name):
                    time.sleep(0.01)
                tracker.count("ops")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=track_stage, args=(f"stage_{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        summary = tracker.summary()
        assert len(summary["stages"]) == 10
        assert summary["counters"]["ops"] == 10
