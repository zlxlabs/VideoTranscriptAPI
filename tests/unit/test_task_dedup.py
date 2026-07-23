"""Unit tests for TaskDedup extracted from CacheManager contracts."""

import pytest

from src.video_transcript_api.api.services.task_dedup import TaskDedup
from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.task_status import TaskStatus


@pytest.fixture
def cm(tmp_path):
    """Create an isolated CacheManager with its normal connection lifecycle."""
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


@pytest.fixture
def dedup(cm):
    """Create TaskDedup with the existing CacheManager instance."""
    return TaskDedup(cm)


def _create_task_at_time(cm, url, platform, media_id, status, timestamp):
    """Insert a task and make ordering deterministic for dedup assertions."""
    task = cm.create_task(url=url, platform=platform, media_id=media_id)
    cm.update_task_status(task["task_id"], status, platform=platform, media_id=media_id)
    with cm._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET created_at = ? WHERE task_id = ?",
            (timestamp, task["task_id"]),
        )
    return task


class TestTaskDedup:
    """Contracts moved from CacheManager's URL and media dedup methods."""

    def test_url_lookup_returns_existing_task_fields(self, cm, dedup):
        url = "https://example.com/url-match"
        task = _create_task_at_time(
            cm, url, "youtube", "url-match", TaskStatus.QUEUED, "2026-01-01 00:00:00"
        )

        result = dedup.get_existing_task_by_url(url)

        assert result is not None
        assert result["task_id"] == task["task_id"]
        assert result["view_token"] == task["view_token"]
        assert result["platform"] == "youtube"
        assert result["media_id"] == "url-match"

    def test_url_lookup_prioritizes_success_over_later_processing(self, cm, dedup):
        url = "https://example.com/url-priority"
        success_task = _create_task_at_time(
            cm, url, "youtube", "url-priority", TaskStatus.SUCCESS, "2026-01-01 00:00:00"
        )
        _create_task_at_time(
            cm, url, "youtube", "url-priority", TaskStatus.PROCESSING, "2026-01-01 01:00:00"
        )

        result = dedup.get_existing_task_by_url(url)

        assert result is not None
        assert result["task_id"] == success_task["task_id"]

    def test_url_lookup_returns_none_when_missing(self, dedup):
        assert dedup.get_existing_task_by_url("https://example.com/missing") is None

    def test_media_lookup_returns_existing_task_fields(self, cm, dedup):
        task = _create_task_at_time(
            cm,
            "https://youtu.be/media-match",
            "youtube",
            "media-match",
            TaskStatus.QUEUED,
            "2026-01-01 00:00:00",
        )

        result = dedup.get_existing_task_by_media("youtube", "media-match")

        assert result is not None
        assert result["task_id"] == task["task_id"]
        assert result["view_token"] == task["view_token"]
        assert result["url"] == "https://youtu.be/media-match"

    def test_media_lookup_prioritizes_calibrating_over_earlier_failed(self, cm, dedup):
        _create_task_at_time(
            cm,
            "https://example.com/media-priority/failed",
            "youtube",
            "media-priority",
            TaskStatus.FAILED,
            "2026-01-01 00:00:00",
        )
        calibrating_task = _create_task_at_time(
            cm,
            "https://example.com/media-priority/calibrating",
            "youtube",
            "media-priority",
            TaskStatus.CALIBRATING,
            "2026-01-01 01:00:00",
        )

        result = dedup.get_existing_task_by_media("youtube", "media-priority")

        assert result is not None
        assert result["task_id"] == calibrating_task["task_id"]

    @pytest.mark.parametrize(
        ("platform", "media_id"),
        [(None, "id"), ("youtube", None), ("", "id"), ("youtube", "")],
    )
    def test_media_lookup_returns_none_for_empty_key(self, dedup, platform, media_id):
        assert dedup.get_existing_task_by_media(platform, media_id) is None

    def test_media_lookup_respects_speaker_recognition(self, cm, dedup):
        cm.create_task(
            url="https://example.com/speaker-match",
            platform="youtube",
            media_id="speaker-match",
            use_speaker_recognition=True,
        )

        assert (
            dedup.get_existing_task_by_media(
                "youtube", "speaker-match", use_speaker_recognition=False
            )
            is None
        )
