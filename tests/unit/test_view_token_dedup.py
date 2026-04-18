"""Unit tests for view_token deduplication by (platform, media_id).

Tests the fix for: same video submitted via different URL formats
(e.g., youtu.be/ID vs youtube.com/watch?v=ID) should reuse the same view_token.

TDD: These tests are written BEFORE the implementation.
"""
import pytest

from src.video_transcript_api.cache.cache_manager import CacheManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cache_dir(tmp_path):
    """Provide a temporary cache directory."""
    return tmp_path / "cache"


@pytest.fixture
def cm(cache_dir):
    """Create a CacheManager with a temporary directory."""
    manager = CacheManager(cache_dir=str(cache_dir))
    yield manager
    manager.close()


# ---------------------------------------------------------------------------
# Tests: get_existing_task_by_media()
# ---------------------------------------------------------------------------

class TestGetExistingTaskByMedia:
    """Tests for the new get_existing_task_by_media() method."""

    def test_found_matching_task(self, cm):
        """Should find an existing task by (platform, media_id)."""
        # Arrange: create a task with platform and media_id
        task_info = cm.create_task(
            url="https://youtu.be/Hrbq66XqtCo",
            use_speaker_recognition=False,
            platform="youtube",
            media_id="Hrbq66XqtCo",
        )

        # Act: look up by platform + media_id
        result = cm.get_existing_task_by_media(
            platform="youtube",
            media_id="Hrbq66XqtCo",
            use_speaker_recognition=False,
        )

        # Assert
        assert result is not None
        assert result["view_token"] == task_info["view_token"]
        assert result["platform"] == "youtube"
        assert result["media_id"] == "Hrbq66XqtCo"

    def test_not_found(self, cm):
        """Should return None when no matching task exists."""
        result = cm.get_existing_task_by_media(
            platform="youtube",
            media_id="nonexistent",
            use_speaker_recognition=False,
        )
        assert result is None

    def test_none_params_returns_none(self, cm):
        """Should return None when platform or media_id is None."""
        assert cm.get_existing_task_by_media(
            platform=None, media_id="abc", use_speaker_recognition=False
        ) is None
        assert cm.get_existing_task_by_media(
            platform="youtube", media_id=None, use_speaker_recognition=False
        ) is None
        assert cm.get_existing_task_by_media(
            platform=None, media_id=None, use_speaker_recognition=False
        ) is None

    def test_prioritizes_success_status(self, cm):
        """Should return the successful task over failed/queued ones."""
        # Create a failed task first
        cm.create_task(
            url="https://youtu.be/ABC123",
            use_speaker_recognition=False,
            platform="youtube",
            media_id="ABC123",
        )
        failed_task = cm.get_existing_task_by_media(
            platform="youtube", media_id="ABC123", use_speaker_recognition=False
        )
        cm.update_task_status(failed_task["task_id"], "failed",
                              platform="youtube", media_id="ABC123")

        # Create a successful task with different URL
        task2 = cm.create_task(
            url="https://youtube.com/watch?v=ABC123",
            use_speaker_recognition=False,
            platform="youtube",
            media_id="ABC123",
        )
        cm.update_task_status(task2["task_id"], "success",
                              platform="youtube", media_id="ABC123")

        # Act
        result = cm.get_existing_task_by_media(
            platform="youtube", media_id="ABC123", use_speaker_recognition=False
        )

        # Assert: should return the successful task
        assert result is not None
        assert result["status"] == "success"
        assert result["task_id"] == task2["task_id"]

    def test_respects_speaker_recognition_flag(self, cm):
        """Should only match tasks with same use_speaker_recognition setting."""
        cm.create_task(
            url="https://youtu.be/XYZ789",
            use_speaker_recognition=True,
            platform="youtube",
            media_id="XYZ789",
        )

        # Look up with different speaker recognition setting
        result = cm.get_existing_task_by_media(
            platform="youtube",
            media_id="XYZ789",
            use_speaker_recognition=False,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Tests: create_task() with platform+media_id deduplication
# ---------------------------------------------------------------------------

class TestCreateTaskMediaDedup:
    """Tests for enhanced create_task() with (platform, media_id) fallback."""

    def test_different_url_same_media_reuses_view_token(self, cm):
        """Core test: different URL formats for same video should reuse view_token."""
        # First task: short URL
        task1 = cm.create_task(
            url="https://youtu.be/Hrbq66XqtCo",
            use_speaker_recognition=False,
            platform="youtube",
            media_id="Hrbq66XqtCo",
        )

        # Second task: full URL (different string, same video)
        task2 = cm.create_task(
            url="https://m.youtube.com/watch?v=Hrbq66XqtCo",
            use_speaker_recognition=False,
            platform="youtube",
            media_id="Hrbq66XqtCo",
        )

        # Should have different task_ids but same view_token
        assert task1["task_id"] != task2["task_id"]
        assert task1["view_token"] == task2["view_token"]

    def test_same_url_reuses_view_token(self, cm):
        """Regression: same URL should still reuse view_token (existing behavior)."""
        task1 = cm.create_task(
            url="https://youtu.be/ABC123",
            use_speaker_recognition=False,
            platform="youtube",
            media_id="ABC123",
        )
        task2 = cm.create_task(
            url="https://youtu.be/ABC123",
            use_speaker_recognition=False,
            platform="youtube",
            media_id="ABC123",
        )

        assert task1["task_id"] != task2["task_id"]
        assert task1["view_token"] == task2["view_token"]

    def test_new_video_gets_new_token(self, cm):
        """New video should get a new view_token."""
        task1 = cm.create_task(
            url="https://youtu.be/VIDEO_A",
            use_speaker_recognition=False,
            platform="youtube",
            media_id="VIDEO_A",
        )
        task2 = cm.create_task(
            url="https://youtu.be/VIDEO_B",
            use_speaker_recognition=False,
            platform="youtube",
            media_id="VIDEO_B",
        )

        assert task1["view_token"] != task2["view_token"]

    def test_stores_platform_media_id_at_creation(self, cm):
        """create_task should store platform and media_id in task_status."""
        task_info = cm.create_task(
            url="https://youtu.be/STORE_TEST",
            use_speaker_recognition=False,
            platform="youtube",
            media_id="STORE_TEST",
        )

        # Verify via direct task lookup
        task = cm.get_task_by_id(task_info["task_id"])
        assert task["platform"] == "youtube"
        assert task["media_id"] == "STORE_TEST"

    def test_no_platform_degrades_to_url_match(self, cm):
        """When platform is None, should skip media dedup and use URL match only."""
        task1 = cm.create_task(
            url="https://unknown-site.com/video/123",
            use_speaker_recognition=False,
            platform=None,
            media_id=None,
        )
        # Same URL should still reuse via URL match
        task2 = cm.create_task(
            url="https://unknown-site.com/video/123",
            use_speaker_recognition=False,
            platform=None,
            media_id=None,
        )
        assert task1["view_token"] == task2["view_token"]

        # Different URL with no platform info should get new token
        task3 = cm.create_task(
            url="https://unknown-site.com/video/456",
            use_speaker_recognition=False,
            platform=None,
            media_id=None,
        )
        assert task1["view_token"] != task3["view_token"]

    def test_backward_compatible_without_platform_params(self, cm):
        """create_task should work without platform/media_id params (backward compat)."""
        task_info = cm.create_task(
            url="https://youtu.be/COMPAT_TEST",
            use_speaker_recognition=False,
        )
        assert "task_id" in task_info
        assert "view_token" in task_info
