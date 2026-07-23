"""Unit tests for ViewTokenResolver extracted from CacheManager contracts."""

import pytest

from src.video_transcript_api.api.services.view_token_resolver import (
    ViewTokenResolver,
)
from src.video_transcript_api.cache.cache_manager import CacheManager


@pytest.fixture
def cm(tmp_path):
    """Create an isolated CacheManager backed by a temporary cache directory."""
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


@pytest.fixture
def resolver(cm):
    """Create the resolver with the existing CacheManager instance."""
    return ViewTokenResolver(cm)


def _save_sample_capswriter(cm, media_id="vid1"):
    """Save a minimal transcript cache entry for a successful task."""
    return cm.save_cache(
        platform="youtube",
        url=f"https://example.com/{media_id}",
        media_id=media_id,
        use_speaker_recognition=False,
        transcript_data="Hello world. This is a test transcript.",
        transcript_type="capswriter",
        title="Test Video",
        author="Author",
        description="A test video",
    )


def _make_success_task(cm, media_id="vid1"):
    """Create a successful task linked to a saved cache entry."""
    _save_sample_capswriter(cm, media_id)
    task = cm.create_task(
        url=f"https://example.com/{media_id}",
        platform="youtube",
        media_id=media_id,
    )
    cm.update_task_status(
        task["task_id"], "success", platform="youtube", media_id=media_id
    )
    return task


class TestViewTokenResolver:
    """Direct contracts moved from CacheManager's view-token methods."""

    @pytest.mark.parametrize(
        ("summary_status", "expected_state"),
        [
            ("skipped_short", "skipped_short"),
            ("failed", "failed"),
            ("pending", "pending"),
            ("disabled", "disabled"),
        ],
    )
    def test_non_generated_summary_states_have_no_placeholder(
        self, cm, resolver, summary_status, expected_state
    ):
        task = _make_success_task(cm, f"vid-{summary_status}")
        cm.save_llm_status(
            platform="youtube",
            media_id=f"vid-{summary_status}",
            use_speaker_recognition=False,
            summary_status=summary_status,
        )

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["summary_state"] == expected_state
        assert view_data["summary"] is None

    def test_generated_summary_returns_real_text(self, cm, resolver):
        task = _make_success_task(cm, "vid-generated")
        cm.save_llm_result(
            platform="youtube",
            media_id="vid-generated",
            use_speaker_recognition=False,
            llm_type="summary",
            content="A real generated summary.",
        )
        cm.save_llm_status(
            platform="youtube",
            media_id="vid-generated",
            use_speaker_recognition=False,
            summary_status="generated",
        )

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["summary_state"] == "generated"
        assert view_data["summary"] == "A real generated summary."

    def test_legacy_summary_without_status_is_skipped_short(self, cm, resolver):
        task = _make_success_task(cm, "vid-legacy")

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["summary_state"] == "skipped_short"
        assert view_data["summary"] is None

    def test_cache_data_by_token_includes_task_info(self, cm, resolver):
        task = _make_success_task(cm, "vid-cache")

        cache_data = resolver.get_cache_by_view_token(task["view_token"])

        assert cache_data is not None
        assert cache_data["task_info"]["task_id"] == task["task_id"]
        assert cache_data["transcript_data"].startswith("Hello world")

    def test_missing_view_token_returns_none(self, resolver):
        assert resolver.get_view_data_by_token("missing") is None
        assert resolver.get_cache_by_view_token("missing") is None

    def test_llm_config_falls_back_to_latest_task_for_shared_token(self, cm, resolver):
        _save_sample_capswriter(cm, "vid-config")
        config = {"calibrate_model": "test-model", "summary_model": "test-model"}
        original = cm.create_task(
            url="https://example.com/vid-config",
            platform="youtube",
            media_id="vid-config",
        )
        cm.update_task_status(
            original["task_id"], "success", platform="youtube", media_id="vid-config"
        )
        cm.update_task_llm_config(original["task_id"], config)
        cache_hit = cm.create_task(
            url="https://example.com/vid-config",
            platform="youtube",
            media_id="vid-config",
        )
        cm.update_task_status(
            cache_hit["task_id"], "success", platform="youtube", media_id="vid-config"
        )

        view_data = resolver.get_view_data_by_token(cache_hit["view_token"])

        assert view_data["llm_config"] == config
