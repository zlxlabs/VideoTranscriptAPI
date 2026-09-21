"""Unit tests for ViewTokenResolver extracted from CacheManager contracts."""

from pathlib import Path

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


def _make_failed_task(cm, media_id, error_message, *, save_transcript=None):
    """Create a failed task, optionally with a CapsWriter cache body."""
    if save_transcript is not None:
        cm.save_cache(
            platform="youtube",
            url=f"https://example.com/{media_id}",
            media_id=media_id,
            use_speaker_recognition=False,
            transcript_data=save_transcript,
            transcript_type="capswriter",
            title="Failed Video",
            author="Author",
            description="A failed video",
        )
    task = cm.create_task(
        url=f"https://example.com/{media_id}",
        platform="youtube",
        media_id=media_id,
    )
    cm.update_task_status(
        task["task_id"],
        "failed",
        platform="youtube",
        media_id=media_id,
        error_message=error_message,
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

    def test_generated_notes_returns_real_text(self, cm, resolver):
        task = _make_success_task(cm, "vid-notes-generated")
        cm.save_llm_result(
            platform="youtube",
            media_id="vid-notes-generated",
            use_speaker_recognition=False,
            llm_type="notes",
            content="## Chapter\n- Detailed note.",
        )
        cm.save_llm_status(
            platform="youtube",
            media_id="vid-notes-generated",
            use_speaker_recognition=False,
            notes_status="generated",
        )

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["notes_state"] == "generated"
        assert view_data["notes"] == "## Chapter\n- Detailed note."

    def test_failed_notes_has_no_placeholder_text(self, cm, resolver):
        task = _make_success_task(cm, "vid-notes-failed")
        cm.save_llm_status(
            platform="youtube",
            media_id="vid-notes-failed",
            use_speaker_recognition=False,
            notes_status="failed",
        )

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["notes_state"] == "failed"
        assert view_data["notes"] is None

    def test_notes_artifact_without_generated_status_is_hidden(self, cm, resolver):
        task = _make_success_task(cm, "vid-notes-orphan")
        cm.save_llm_result(
            platform="youtube",
            media_id="vid-notes-orphan",
            use_speaker_recognition=False,
            llm_type="notes",
            content="orphan notes artifact",
        )

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["notes_state"] is None
        assert view_data["notes"] is None

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

    def test_failed_task_with_transcript_returns_interrupted(self, cm, resolver):
        """Red E: failed task row + non-empty cache body -> interrupted."""
        reason = "orphan recovered after deploy restart"
        task = _make_failed_task(
            cm,
            "vid-interrupted-body",
            reason,
            save_transcript="Hello world. This is a test transcript.",
        )

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["status"] == "interrupted"
        assert view_data["transcript"]
        assert "Hello world" in view_data["transcript"]
        assert view_data["interrupted_reason"] == reason
        assert view_data["interrupted_at"]
        assert view_data["llm_config"] is None or isinstance(view_data["llm_config"], dict)
        assert view_data["cache_dir"]

    def test_failed_task_with_calibrated_text_returns_interrupted(
        self, cm, resolver
    ):
        """Red E variant: llm_calibrated.txt is also a usable payload."""
        reason = "killed during summary stage"
        task = _make_failed_task(
            cm,
            "vid-interrupted-calibrated",
            reason,
            save_transcript="raw transcript body",
        )
        cm.save_llm_result(
            platform="youtube",
            media_id="vid-interrupted-calibrated",
            use_speaker_recognition=False,
            llm_type="calibrated",
            content="calibrated transcript body",
        )

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["status"] == "interrupted"
        assert view_data["transcript"] == "calibrated transcript body"
        assert view_data["interrupted_reason"] == reason

    def test_failed_task_with_empty_transcript_stays_failed(self, cm, resolver):
        """Reverse red: cache hit without body text must stay failed."""
        reason = "orphan recovered after deploy restart"
        task = _make_failed_task(
            cm,
            "vid-empty-shell",
            reason,
            save_transcript="",
        )

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["status"] == "failed"
        assert view_data["message"] == reason
        assert "transcript" not in view_data

    def test_failed_cache_dir_without_text_fields_stays_failed(
        self, cm, resolver, monkeypatch
    ):
        """Reverse red: directory existence must not count as usable payload."""
        reason = "orphan recovered after deploy restart"
        task = _make_failed_task(cm, "vid-dir-only", reason)
        cache_dir = Path(cm.cache_dir) / "shell-only"
        cache_dir.mkdir(parents=True)
        (cache_dir / "key_info.json").write_text("{}", encoding="utf-8")

        def fake_get_cache(**kwargs):
            return {
                "title": "Shell",
                "author": "",
                "description": "",
                "file_path": str(cache_dir),
                "platform": "youtube",
                "use_speaker_recognition": False,
                "llm_calibrated": "",
                "transcript_data": "",
            }

        monkeypatch.setattr(cm, "get_cache", fake_get_cache)

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["status"] == "failed"
        assert view_data["message"] == reason
        assert "transcript" not in view_data

    @pytest.mark.parametrize(
        "transcript_data",
        [
            {
                "task_id": "t1",
                "segments": [
                    {"start_time": 0.0, "end_time": 1.2, "text": "hello from funasr"},
                    {"start_time": 1.2, "end_time": 2.0, "text": "second paragraph"},
                ],
            },
            [
                {"start_time": 0.0, "end_time": 1.2, "text": "hello from funasr"},
                {"start_time": 1.2, "end_time": 2.0, "text": "second paragraph"},
            ],
        ],
    )
    def test_failed_funasr_segments_return_interrupted(
        self, cm, resolver, monkeypatch, transcript_data
    ):
        reason = "orphan recovered after deploy restart"
        task = _make_failed_task(cm, "vid-funasr-ok", reason)

        def fake_get_cache(**kwargs):
            return {
                "title": "FunASR Video",
                "author": "",
                "description": "",
                "file_path": str(cm.cache_dir),
                "platform": "youtube",
                "use_speaker_recognition": True,
                "transcript_data": transcript_data,
            }

        monkeypatch.setattr(cm, "get_cache", fake_get_cache)
        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["status"] == "interrupted"
        assert "hello from funasr" in view_data["transcript"]
        assert "second paragraph" in view_data["transcript"]
        assert "{'" not in view_data["transcript"]
        assert "segments" not in view_data["transcript"]

    @pytest.mark.parametrize(
        "transcript_data",
        [
            {"task_id": "t1", "segments": []},
            {"task_id": "t1", "segments": [{"start_time": 0.0, "end_time": 1.0}]},
            {"task_id": "t1", "segments": [{"text": "   "}]},
        ],
    )
    def test_failed_funasr_dict_without_text_stays_failed(
        self, cm, resolver, monkeypatch, transcript_data
    ):
        reason = "orphan recovered after deploy restart"
        task = _make_failed_task(cm, "vid-funasr-empty", reason)

        def fake_get_cache(**kwargs):
            return {
                "title": "FunASR Video",
                "author": "",
                "description": "",
                "file_path": str(cm.cache_dir),
                "platform": "youtube",
                "use_speaker_recognition": True,
                "transcript_data": transcript_data,
            }

        monkeypatch.setattr(cm, "get_cache", fake_get_cache)
        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["status"] == "failed"
        assert view_data["message"] == reason

    def test_failed_task_with_key_info_only_stays_failed(self, cm, resolver):
        """Accident shell: directory exists with only key_info.json."""
        reason = "orphan recovered after deploy restart"
        task = _make_failed_task(
            cm,
            "vid-key-info-only",
            reason,
            save_transcript="temporary body that will be deleted",
        )
        view_before = resolver.get_view_data_by_token(task["view_token"])
        cache_dir = Path(view_before["cache_dir"])
        for name in (
            "transcript_capswriter.txt",
            "transcript_capswriter.json",
            "transcript_funasr.json",
            "llm_calibrated.txt",
        ):
            target = cache_dir / name
            if target.exists():
                target.unlink()
        (cache_dir / "key_info.json").write_text("{}", encoding="utf-8")

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["status"] == "failed"
        assert view_data["message"] == reason

    def test_failed_task_without_cache_keeps_default_failed_page(self, cm, resolver):
        task = _make_failed_task(cm, "vid-no-cache", "download failed")

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["status"] == "failed"
        assert view_data["message"] == "download failed"
        assert "transcript" not in view_data

    def test_failed_task_without_error_message_uses_default_copy(self, cm, resolver):
        task = cm.create_task(
            url="https://example.com/vid-default-msg",
            platform="youtube",
            media_id="vid-default-msg",
        )
        cm.update_task_status(
            task["task_id"],
            "failed",
            platform="youtube",
            media_id="vid-default-msg",
        )

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["status"] == "failed"
        assert view_data["message"] == "转录任务失败，请重新提交"

    def test_success_task_still_returns_success(self, cm, resolver):
        task = _make_success_task(cm, "vid-still-success")

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["status"] == "success"
        assert "interrupted_reason" not in view_data

    @pytest.mark.parametrize(
        ("task_status", "expected_view_status"),
        [
            ("queued", "processing"),
            ("processing", "processing"),
            ("calibrating", "processing"),
        ],
    )
    def test_in_flight_statuses_stay_processing(
        self, cm, resolver, task_status, expected_view_status
    ):
        _save_sample_capswriter(cm, f"vid-{task_status}")
        task = cm.create_task(
            url=f"https://example.com/vid-{task_status}",
            platform="youtube",
            media_id=f"vid-{task_status}",
        )
        cm.update_task_status(
            task["task_id"],
            task_status,
            platform="youtube",
            media_id=f"vid-{task_status}",
        )

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["status"] == expected_view_status
        assert "transcript" not in view_data

    def test_success_without_cache_returns_file_cleaned(self, cm, resolver):
        task = cm.create_task(
            url="https://example.com/vid-cleaned",
            platform="youtube",
            media_id="vid-cleaned",
        )
        cm.update_task_status(
            task["task_id"],
            "success",
            platform="youtube",
            media_id="vid-cleaned",
        )

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["status"] == "file_cleaned"

    def test_success_without_platform_returns_incomplete(self, cm, resolver):
        task = cm.create_task(url="https://example.com/vid-incomplete")
        cm.update_task_status(task["task_id"], "success")

        view_data = resolver.get_view_data_by_token(task["view_token"])

        assert view_data["status"] == "incomplete"
