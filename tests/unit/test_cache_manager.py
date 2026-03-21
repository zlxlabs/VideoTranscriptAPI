"""Unit tests for CacheManager.

NOTE: The integration-style tests in tests/cache/test_cache_manager.py cover
manual end-to-end flows. This file focuses on isolated, pytest-based unit tests
with tmp_path fixtures and no side effects.
"""
import json
import threading

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


def _save_sample_capswriter(cm, media_id="vid1", platform="youtube"):
    """Helper: save a capswriter transcript and return the result dict."""
    return cm.save_cache(
        platform=platform,
        url=f"https://example.com/{media_id}",
        media_id=media_id,
        use_speaker_recognition=False,
        transcript_data="Hello world. This is a test transcript.",
        transcript_type="capswriter",
        title="Test Video",
        author="Author",
        description="A test video",
    )


def _save_sample_funasr(cm, media_id="vid2", platform="bilibili"):
    """Helper: save a funasr transcript and return the result dict."""
    funasr_data = {
        "speakers": ["Speaker1", "Speaker2"],
        "segments": [
            {"speaker": "Speaker1", "text": "Hello", "start": 0, "end": 1},
            {"speaker": "Speaker2", "text": "Hi", "start": 1, "end": 2},
        ],
    }
    return cm.save_cache(
        platform=platform,
        url=f"https://example.com/{media_id}",
        media_id=media_id,
        use_speaker_recognition=True,
        transcript_data=funasr_data,
        transcript_type="funasr",
        title="FunASR Video",
        author="Author2",
        description="A funasr test",
    )


# ---------------------------------------------------------------------------
# save_cache
# ---------------------------------------------------------------------------

class TestSaveCache:
    """Tests for CacheManager.save_cache."""

    def test_returns_dict_on_success(self, cm):
        result = _save_sample_capswriter(cm)
        assert result is not None
        assert result["platform"] == "youtube"
        assert result["media_id"] == "vid1"

    def test_creates_directory_structure(self, cm, cache_dir):
        _save_sample_capswriter(cm)
        # Directory should exist under cache_dir/youtube/YYYY/YYYYMM/vid1
        dirs = list(cache_dir.rglob("vid1"))
        assert len(dirs) == 1
        assert dirs[0].is_dir()

    def test_capswriter_creates_txt_file(self, cm, cache_dir):
        _save_sample_capswriter(cm)
        txt_files = list(cache_dir.rglob("transcript_capswriter.txt"))
        assert len(txt_files) == 1
        content = txt_files[0].read_text(encoding="utf-8")
        assert "Hello world" in content

    def test_funasr_creates_json_file(self, cm, cache_dir):
        _save_sample_funasr(cm)
        json_files = list(cache_dir.rglob("transcript_funasr.json"))
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert "speakers" in data
        assert len(data["segments"]) == 2

    def test_extra_json_data_saved(self, cm, cache_dir):
        extra = {"compat": True, "segments": []}
        cm.save_cache(
            platform="youtube",
            url="https://example.com/extra",
            media_id="extra1",
            use_speaker_recognition=False,
            transcript_data="text content",
            transcript_type="capswriter",
            extra_json_data=extra,
        )
        json_files = list(cache_dir.rglob("transcript_capswriter.json"))
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert data["compat"] is True


# ---------------------------------------------------------------------------
# get_cache
# ---------------------------------------------------------------------------

class TestGetCache:
    """Tests for CacheManager.get_cache."""

    def test_returns_saved_capswriter_data(self, cm):
        _save_sample_capswriter(cm)
        result = cm.get_cache(platform="youtube", media_id="vid1")
        assert result is not None
        assert result["transcript_type"] == "capswriter"
        assert "Hello world" in result["transcript_data"]

    def test_returns_saved_funasr_data(self, cm):
        _save_sample_funasr(cm)
        result = cm.get_cache(
            platform="bilibili", media_id="vid2", use_speaker_recognition=True
        )
        assert result is not None
        assert result["transcript_type"] == "funasr"
        assert len(result["transcript_data"]["speakers"]) == 2

    def test_returns_none_for_missing(self, cm):
        result = cm.get_cache(platform="youtube", media_id="nonexistent")
        assert result is None

    def test_returns_none_when_no_params(self, cm):
        result = cm.get_cache()
        assert result is None

    def test_query_by_url(self, cm):
        _save_sample_capswriter(cm)
        result = cm.get_cache(url="https://example.com/vid1")
        assert result is not None
        assert result["media_id"] == "vid1"

    def test_returns_metadata_fields(self, cm):
        _save_sample_capswriter(cm)
        result = cm.get_cache(platform="youtube", media_id="vid1")
        assert result["title"] == "Test Video"
        assert result["author"] == "Author"
        assert result["platform"] == "youtube"


# ---------------------------------------------------------------------------
# save_llm_result
# ---------------------------------------------------------------------------

class TestSaveLLMResult:
    """Tests for CacheManager.save_llm_result."""

    def test_save_calibrated(self, cm, cache_dir):
        _save_sample_capswriter(cm)
        ok = cm.save_llm_result(
            platform="youtube",
            media_id="vid1",
            use_speaker_recognition=False,
            llm_type="calibrated",
            content="Calibrated text here.",
        )
        assert ok is True
        files = list(cache_dir.rglob("llm_calibrated.txt"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == "Calibrated text here."

    def test_save_summary(self, cm, cache_dir):
        _save_sample_capswriter(cm)
        ok = cm.save_llm_result(
            platform="youtube",
            media_id="vid1",
            use_speaker_recognition=False,
            llm_type="summary",
            content="Summary bullet points.",
        )
        assert ok is True
        files = list(cache_dir.rglob("llm_summary.txt"))
        assert len(files) == 1

    def test_save_structured(self, cm, cache_dir):
        _save_sample_capswriter(cm)
        structured = {"sections": [{"title": "Intro", "content": "Hello"}]}
        ok = cm.save_llm_result(
            platform="youtube",
            media_id="vid1",
            use_speaker_recognition=False,
            llm_type="structured",
            content=structured,
        )
        assert ok is True
        files = list(cache_dir.rglob("llm_processed.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["format_version"] == "v2"
        assert data["sections"][0]["title"] == "Intro"

    def test_structured_rejects_non_dict(self, cm):
        _save_sample_capswriter(cm)
        ok = cm.save_llm_result(
            platform="youtube",
            media_id="vid1",
            use_speaker_recognition=False,
            llm_type="structured",
            content="not a dict",
        )
        assert ok is False

    def test_returns_false_for_missing_cache(self, cm):
        ok = cm.save_llm_result(
            platform="youtube",
            media_id="nonexistent",
            use_speaker_recognition=False,
            llm_type="calibrated",
            content="text",
        )
        assert ok is False

    def test_unknown_llm_type_returns_false(self, cm):
        _save_sample_capswriter(cm)
        ok = cm.save_llm_result(
            platform="youtube",
            media_id="vid1",
            use_speaker_recognition=False,
            llm_type="unknown_type",
            content="text",
        )
        assert ok is False

    def test_get_cache_includes_llm_results(self, cm):
        _save_sample_capswriter(cm)
        cm.save_llm_result(
            platform="youtube",
            media_id="vid1",
            use_speaker_recognition=False,
            llm_type="calibrated",
            content="Calibrated.",
        )
        cm.save_llm_result(
            platform="youtube",
            media_id="vid1",
            use_speaker_recognition=False,
            llm_type="summary",
            content="Summary.",
        )
        result = cm.get_cache(platform="youtube", media_id="vid1")
        assert "llm_calibrated" in result
        assert result["llm_calibrated"] == "Calibrated."
        assert "llm_summary" in result
        assert result["llm_summary"] == "Summary."


# ---------------------------------------------------------------------------
# list_cache
# ---------------------------------------------------------------------------

class TestListCache:
    """Tests for CacheManager.list_cache."""

    def test_empty_returns_empty_list(self, cm):
        assert cm.list_cache() == []

    def test_returns_saved_records(self, cm):
        _save_sample_capswriter(cm, media_id="a")
        _save_sample_capswriter(cm, media_id="b")
        results = cm.list_cache()
        assert len(results) == 2

    def test_filter_by_platform(self, cm):
        _save_sample_capswriter(cm, media_id="a", platform="youtube")
        _save_sample_funasr(cm, media_id="b", platform="bilibili")
        results = cm.list_cache(platform="youtube")
        assert len(results) == 1
        assert results[0]["platform"] == "youtube"

    def test_pagination_limit(self, cm):
        for i in range(5):
            _save_sample_capswriter(cm, media_id=f"v{i}")
        results = cm.list_cache(limit=3)
        assert len(results) == 3

    def test_pagination_offset(self, cm):
        for i in range(5):
            _save_sample_capswriter(cm, media_id=f"v{i}")
        all_results = cm.list_cache(limit=100)
        offset_results = cm.list_cache(limit=100, offset=2)
        assert len(offset_results) == len(all_results) - 2


# ---------------------------------------------------------------------------
# get_cache_stats
# ---------------------------------------------------------------------------

class TestGetCacheStats:
    """Tests for CacheManager.get_cache_stats."""

    def test_empty_stats(self, cm):
        stats = cm.get_cache_stats()
        assert stats["total_records"] == 0
        assert stats["platform_stats"] == {}

    def test_counts_records(self, cm):
        _save_sample_capswriter(cm, media_id="a")
        _save_sample_funasr(cm, media_id="b")
        stats = cm.get_cache_stats()
        assert stats["total_records"] == 2

    def test_platform_stats(self, cm):
        _save_sample_capswriter(cm, media_id="a", platform="youtube")
        _save_sample_capswriter(cm, media_id="b", platform="youtube")
        _save_sample_funasr(cm, media_id="c", platform="bilibili")
        stats = cm.get_cache_stats()
        assert stats["platform_stats"]["youtube"] == 2
        assert stats["platform_stats"]["bilibili"] == 1

    def test_speaker_recognition_stats(self, cm):
        _save_sample_capswriter(cm, media_id="a")  # speaker=False
        _save_sample_funasr(cm, media_id="b")      # speaker=True
        stats = cm.get_cache_stats()
        assert stats["speaker_recognition_stats"][False] == 1
        assert stats["speaker_recognition_stats"][True] == 1

    def test_cache_size_present(self, cm):
        _save_sample_capswriter(cm)
        stats = cm.get_cache_stats()
        assert "cache_size_mb" in stats
        assert stats["cache_size_mb"] >= 0


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    """Tests for per-thread database connections."""

    def test_get_connection_returns_per_thread_connections(self, cm):
        """_get_connection should return different connections in different threads."""
        main_conn = cm._get_connection()
        thread_conn = [None]

        def worker():
            thread_conn[0] = cm._get_connection()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert thread_conn[0] is not None
        assert thread_conn[0] is not main_conn

    def test_concurrent_saves_do_not_raise(self, cm):
        """Multiple threads saving concurrently should not raise exceptions."""
        errors = []

        def worker(idx):
            try:
                cm.save_cache(
                    platform="youtube",
                    url=f"https://example.com/thread{idx}",
                    media_id=f"thread{idx}",
                    use_speaker_recognition=False,
                    transcript_data=f"Transcript from thread {idx}",
                    transcript_type="capswriter",
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        results = cm.list_cache()
        assert len(results) == 5
