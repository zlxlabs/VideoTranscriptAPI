"""Unit tests for CacheManager.

NOTE: The integration-style tests in tests/cache/test_cache_manager.py cover
manual end-to-end flows. This file focuses on isolated, pytest-based unit tests
with tmp_path fixtures and no side effects.
"""
import json
import threading
import time

import pytest

import src.video_transcript_api.cache.cache_manager as cache_manager_module
from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.utils.task_status import TaskStatus


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
        assert data["format_version"] == "v3"
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


# ---------------------------------------------------------------------------
# llm_config fallback for cache-hit tasks
# ---------------------------------------------------------------------------

class TestLLMConfigFallback:
    """Tests for llm_config fallback when viewing cache-hit tasks.

    When the same URL is submitted multiple times, later cache-hit tasks
    have no llm_config. The view should fall back to the llm_config from
    an earlier task under the same view_token.
    """

    def _create_task_at_time(self, cm, url, timestamp, llm_config_dict=None):
        """Helper: create a task at a specific timestamp, optionally with llm_config."""
        task_info = cm.create_task(url=url, platform="youtube", media_id="vid1")
        task_id = task_info["task_id"]
        cm.update_task_status(task_id, "success", platform="youtube", media_id="vid1")
        # Force a specific created_at to control ordering
        with cm._get_cursor() as cursor:
            cursor.execute(
                "UPDATE task_status SET created_at = ? WHERE task_id = ?",
                (timestamp, task_id),
            )
        if llm_config_dict:
            cm.update_task_llm_config(task_id, llm_config_dict)
        return task_info

    def test_fallback_returns_llm_config_from_earlier_task(self, cm):
        """Cache-hit task should inherit llm_config from the original LLM task."""
        _save_sample_capswriter(cm)
        config = {"calibrate_model": "deepseek-v4", "summary_model": "deepseek-v4"}

        # T=00:00: LLM task with config
        self._create_task_at_time(
            cm, "https://example.com/vid1", "2026-01-01 00:00:00", config
        )
        # T=01:00: cache-hit task, no config
        cache_hit = self._create_task_at_time(
            cm, "https://example.com/vid1", "2026-01-01 01:00:00"
        )

        view_data = cm.get_view_data_by_token(cache_hit["view_token"])
        assert view_data is not None
        assert view_data.get("llm_config") is not None
        assert view_data["llm_config"]["calibrate_model"] == "deepseek-v4"

    def test_fallback_returns_most_recent_llm_config(self, cm):
        """When multiple tasks have llm_config, the most recent one wins."""
        _save_sample_capswriter(cm)
        old_config = {"calibrate_model": "old-model", "summary_model": "old-model"}
        new_config = {"calibrate_model": "new-model", "summary_model": "new-model"}

        # T=00:00: first LLM task
        self._create_task_at_time(
            cm, "https://example.com/vid1", "2026-01-01 00:00:00", old_config
        )
        # T=01:00: second LLM task (recalibrate)
        self._create_task_at_time(
            cm, "https://example.com/vid1", "2026-01-01 01:00:00", new_config
        )
        # T=02:00: cache-hit task
        cache_hit = self._create_task_at_time(
            cm, "https://example.com/vid1", "2026-01-01 02:00:00"
        )

        view_data = cm.get_view_data_by_token(cache_hit["view_token"])
        assert view_data["llm_config"]["calibrate_model"] == "new-model"

    def test_no_fallback_needed_when_latest_task_has_config(self, cm):
        """Direct llm_config on the latest task should be used without fallback."""
        _save_sample_capswriter(cm)
        config = {"calibrate_model": "direct-model", "summary_model": "direct-model"}

        task = self._create_task_at_time(
            cm, "https://example.com/vid1", "2026-01-01 00:00:00", config
        )

        view_data = cm.get_view_data_by_token(task["view_token"])
        assert view_data["llm_config"]["calibrate_model"] == "direct-model"

    def test_no_llm_config_anywhere_returns_none(self, cm):
        """If no task under this view_token has llm_config, return None."""
        _save_sample_capswriter(cm)
        cache_hit = self._create_task_at_time(
            cm, "https://example.com/vid1", "2026-01-01 00:00:00"
        )

        view_data = cm.get_view_data_by_token(cache_hit["view_token"])
        assert view_data is not None
        assert view_data.get("llm_config") is None


# ---------------------------------------------------------------------------
# get_task_by_view_token 排序优先级
# ---------------------------------------------------------------------------

class TestGetTaskByViewToken:
    """回归测试：get_task_by_view_token 的三段式排序（success 最高 / failed 垫底 / 其余按最新）。

    生产事故复现：某 URL 先失败留下一条 status='failed' 记录，后来同一 URL
    重新提交并进入 status='calibrating'（LLM 校对/总结中）。旧的 SQL 用
    CASE status WHEN ... 穷举分支，未列出 'calibrating'，导致它落入 ELSE
    分支、优先级比 'failed' 还低，页面因此展示了过时的失败记录。
    """

    def _create_task_at_time(self, cm, url, status, timestamp, media_id="vid1", platform="youtube"):
        """Helper: create a task with a specific status at a specific timestamp.

        同一 url 重复调用会复用同一个 view_token（不同 task_id），
        用于模拟同一任务多次提交在不同状态下留下的多条记录。
        """
        task_info = cm.create_task(url=url, platform=platform, media_id=media_id)
        task_id = task_info["task_id"]
        cm.update_task_status(task_id, status, platform=platform, media_id=media_id)
        # Force a specific created_at to control ordering
        with cm._get_cursor() as cursor:
            cursor.execute(
                "UPDATE task_status SET created_at = ? WHERE task_id = ?",
                (timestamp, task_id),
            )
        return task_info

    def test_calibrating_beats_earlier_failed(self, cm):
        """核心回归场景：更晚的 calibrating 记录应该战胜更早的 failed 记录。"""
        url = "https://example.com/vid1"
        failed_task = self._create_task_at_time(
            cm, url, "failed", "2026-01-01 00:00:00"
        )
        calibrating_task = self._create_task_at_time(
            cm, url, TaskStatus.CALIBRATING, "2026-01-01 01:00:00"
        )

        result = cm.get_task_by_view_token(failed_task["view_token"])
        assert result is not None
        assert result["task_id"] == calibrating_task["task_id"]
        assert result["task_id"] != failed_task["task_id"]

    def test_success_beats_earlier_failed(self, cm):
        """success 优先级仍然高于 failed（既有语义不变）。"""
        url = "https://example.com/vid1"
        failed_task = self._create_task_at_time(
            cm, url, "failed", "2026-01-01 00:00:00"
        )
        success_task = self._create_task_at_time(
            cm, url, TaskStatus.SUCCESS, "2026-01-01 01:00:00"
        )

        result = cm.get_task_by_view_token(failed_task["view_token"])
        assert result is not None
        assert result["task_id"] == success_task["task_id"]

    def test_success_beats_later_non_terminal(self, cm):
        """success 优先级仍然最高，不会被 created_at 更晚的非终态任务反超。"""
        url = "https://example.com/vid1"
        success_task = self._create_task_at_time(
            cm, url, TaskStatus.SUCCESS, "2026-01-01 00:00:00"
        )
        processing_task = self._create_task_at_time(
            cm, url, TaskStatus.PROCESSING, "2026-01-01 01:00:00"
        )

        result = cm.get_task_by_view_token(success_task["view_token"])
        assert result is not None
        assert result["task_id"] == success_task["task_id"]
        assert result["task_id"] != processing_task["task_id"]

    def test_non_terminal_group_returns_latest(self, cm):
        """多条非终态记录（无 success/failed）时，返回 created_at 最新的一条。"""
        url = "https://example.com/vid1"
        queued_task = self._create_task_at_time(
            cm, url, TaskStatus.QUEUED, "2026-01-01 00:00:00"
        )
        calibrating_task = self._create_task_at_time(
            cm, url, TaskStatus.CALIBRATING, "2026-01-01 01:00:00"
        )

        result = cm.get_task_by_view_token(queued_task["view_token"])
        assert result is not None
        assert result["task_id"] == calibrating_task["task_id"]
        assert result["task_id"] != queued_task["task_id"]


# ---------------------------------------------------------------------------
# get_existing_task_by_url 排序优先级
# ---------------------------------------------------------------------------

class TestGetExistingTaskByUrl:
    """回归测试：get_existing_task_by_url 的三段式排序（success 最高 / failed 垫底 / 其余按最新）。

    与 TestGetTaskByViewToken 是同一种 bug 模式的姊妹场景：查重逻辑按
    (url, use_speaker_recognition) 而非 view_token 查找现有任务，旧的 CASE
    语句同样未列出 calibrating（也未列出 failed），两者都落入 ELSE 分支，
    导致更早的 failed 记录可能掩盖更晚的、仍在处理甚至已成功的记录，
    使 create_task 的去重逻辑误判并复用了错误的旧任务。
    """

    def _create_task_at_time(self, cm, url, status, timestamp,
                              use_speaker_recognition=False, media_id="vid1", platform="youtube"):
        """Helper: create a task with a specific status at a specific timestamp.

        同一 url（+ use_speaker_recognition）重复调用会复用同一个 view_token
        （不同 task_id），用于模拟同一 URL 多次提交在不同状态下留下的多条记录。
        """
        task_info = cm.create_task(
            url=url, use_speaker_recognition=use_speaker_recognition,
            platform=platform, media_id=media_id,
        )
        task_id = task_info["task_id"]
        cm.update_task_status(task_id, status, platform=platform, media_id=media_id)
        # Force a specific created_at to control ordering
        with cm._get_cursor() as cursor:
            cursor.execute(
                "UPDATE task_status SET created_at = ? WHERE task_id = ?",
                (timestamp, task_id),
            )
        return task_info

    def test_calibrating_beats_earlier_failed(self, cm):
        """核心回归场景：更晚的 calibrating 记录应该战胜更早的 failed 记录。"""
        url = "https://example.com/existing-url-vid1"
        failed_task = self._create_task_at_time(
            cm, url, "failed", "2026-01-01 00:00:00"
        )
        calibrating_task = self._create_task_at_time(
            cm, url, TaskStatus.CALIBRATING, "2026-01-01 01:00:00"
        )

        result = cm.get_existing_task_by_url(url, use_speaker_recognition=False)
        assert result is not None
        assert result["task_id"] == calibrating_task["task_id"]
        assert result["task_id"] != failed_task["task_id"]

    def test_success_beats_earlier_failed(self, cm):
        """success 优先级仍然高于 failed（既有语义不变）。"""
        url = "https://example.com/existing-url-vid2"
        failed_task = self._create_task_at_time(
            cm, url, "failed", "2026-01-01 00:00:00"
        )
        success_task = self._create_task_at_time(
            cm, url, TaskStatus.SUCCESS, "2026-01-01 01:00:00"
        )

        result = cm.get_existing_task_by_url(url, use_speaker_recognition=False)
        assert result is not None
        assert result["task_id"] == success_task["task_id"]
        assert result["task_id"] != failed_task["task_id"]

    def test_success_beats_later_non_terminal(self, cm):
        """success 优先级仍然最高，不会被 created_at 更晚的非终态任务反超。"""
        url = "https://example.com/existing-url-vid3"
        success_task = self._create_task_at_time(
            cm, url, TaskStatus.SUCCESS, "2026-01-01 00:00:00"
        )
        processing_task = self._create_task_at_time(
            cm, url, TaskStatus.PROCESSING, "2026-01-01 01:00:00"
        )

        result = cm.get_existing_task_by_url(url, use_speaker_recognition=False)
        assert result is not None
        assert result["task_id"] == success_task["task_id"]
        assert result["task_id"] != processing_task["task_id"]

    def test_non_terminal_group_returns_latest(self, cm):
        """多条非终态记录（无 success/failed）时，返回 created_at 最新的一条。"""
        url = "https://example.com/existing-url-vid4"
        queued_task = self._create_task_at_time(
            cm, url, TaskStatus.QUEUED, "2026-01-01 00:00:00"
        )
        calibrating_task = self._create_task_at_time(
            cm, url, TaskStatus.CALIBRATING, "2026-01-01 01:00:00"
        )

        result = cm.get_existing_task_by_url(url, use_speaker_recognition=False)
        assert result is not None
        assert result["task_id"] == calibrating_task["task_id"]
        assert result["task_id"] != queued_task["task_id"]


# ---------------------------------------------------------------------------
# get_existing_task_by_media 排序优先级
# ---------------------------------------------------------------------------

class TestGetExistingTaskByMedia:
    """回归测试：get_existing_task_by_media 的三段式排序（success 最高 / failed 垫底 / 其余按最新）。

    与 TestGetTaskByViewToken / TestGetExistingTaskByUrl 是同一种 bug 模式的第三次
    复制粘贴传播：语义去重逻辑按 (platform, media_id, use_speaker_recognition) 而非
    view_token/url 查找现有任务，旧的 CASE 语句是更早的穷举写法（success/
    processing/queued 三个分支 + ELSE），既没有列出 calibrating 也没有列出
    failed，两者都落入 ELSE 分支，导致更早的 failed 记录可能掩盖同一
    (platform, media_id) 下更晚的、仍在处理甚至已成功的记录。
    """

    def _create_task_at_time(self, cm, platform, media_id, status, timestamp,
                              use_speaker_recognition=False):
        """Helper: create a task with a specific status at a specific timestamp.

        每次调用使用按 timestamp 派生的不同 url，确保测试只通过
        (platform, media_id, use_speaker_recognition) 关联多条记录，不依赖
        URL 精确匹配路径，用于模拟同一媒体不同 URL 格式多次提交在不同状态下
        留下的多条记录。
        """
        url = f"https://example.com/media-test/{platform}/{media_id}?t={timestamp}"
        task_info = cm.create_task(
            url=url, use_speaker_recognition=use_speaker_recognition,
            platform=platform, media_id=media_id,
        )
        task_id = task_info["task_id"]
        cm.update_task_status(task_id, status, platform=platform, media_id=media_id)
        # Force a specific created_at to control ordering
        with cm._get_cursor() as cursor:
            cursor.execute(
                "UPDATE task_status SET created_at = ? WHERE task_id = ?",
                (timestamp, task_id),
            )
        return task_info

    def test_calibrating_beats_earlier_failed(self, cm):
        """核心回归场景：更晚的 calibrating 记录应该战胜更早的 failed 记录。"""
        platform, media_id = "youtube", "media-vid1"
        failed_task = self._create_task_at_time(
            cm, platform, media_id, "failed", "2026-01-01 00:00:00"
        )
        calibrating_task = self._create_task_at_time(
            cm, platform, media_id, TaskStatus.CALIBRATING, "2026-01-01 01:00:00"
        )

        result = cm.get_existing_task_by_media(platform, media_id, use_speaker_recognition=False)
        assert result is not None
        assert result["task_id"] == calibrating_task["task_id"]
        assert result["task_id"] != failed_task["task_id"]

    def test_success_beats_earlier_failed(self, cm):
        """success 优先级仍然高于 failed（既有语义不变）。"""
        platform, media_id = "youtube", "media-vid2"
        failed_task = self._create_task_at_time(
            cm, platform, media_id, "failed", "2026-01-01 00:00:00"
        )
        success_task = self._create_task_at_time(
            cm, platform, media_id, TaskStatus.SUCCESS, "2026-01-01 01:00:00"
        )

        result = cm.get_existing_task_by_media(platform, media_id, use_speaker_recognition=False)
        assert result is not None
        assert result["task_id"] == success_task["task_id"]
        assert result["task_id"] != failed_task["task_id"]

    def test_success_beats_later_non_terminal(self, cm):
        """success 优先级仍然最高，不会被 created_at 更晚的非终态任务反超。"""
        platform, media_id = "youtube", "media-vid3"
        success_task = self._create_task_at_time(
            cm, platform, media_id, TaskStatus.SUCCESS, "2026-01-01 00:00:00"
        )
        processing_task = self._create_task_at_time(
            cm, platform, media_id, TaskStatus.PROCESSING, "2026-01-01 01:00:00"
        )

        result = cm.get_existing_task_by_media(platform, media_id, use_speaker_recognition=False)
        assert result is not None
        assert result["task_id"] == success_task["task_id"]
        assert result["task_id"] != processing_task["task_id"]

    def test_non_terminal_group_returns_latest(self, cm):
        """多条非终态记录（无 success/failed）时，返回 created_at 最新的一条。"""
        platform, media_id = "youtube", "media-vid4"
        queued_task = self._create_task_at_time(
            cm, platform, media_id, TaskStatus.QUEUED, "2026-01-01 00:00:00"
        )
        calibrating_task = self._create_task_at_time(
            cm, platform, media_id, TaskStatus.CALIBRATING, "2026-01-01 01:00:00"
        )

        result = cm.get_existing_task_by_media(platform, media_id, use_speaker_recognition=False)
        assert result is not None
        assert result["task_id"] == calibrating_task["task_id"]
        assert result["task_id"] != queued_task["task_id"]


# ---------------------------------------------------------------------------
# task_status.calibration_status / summary_status columns (migration)
# ---------------------------------------------------------------------------

class TestLLMStatusColumnMigration:
    """Tests for the calibration_status/summary_status column migration."""

    def test_columns_exist_after_init(self, cm):
        with cm._get_cursor() as cursor:
            cursor.execute("PRAGMA table_info(task_status)")
            columns = [col[1] for col in cursor.fetchall()]
        assert "calibration_status" in columns
        assert "summary_status" in columns

    def test_migration_is_idempotent(self, cache_dir):
        """Re-initializing CacheManager against the same on-disk DB (simulating
        a process restart) must not error and must not duplicate the columns."""
        cm1 = CacheManager(cache_dir=str(cache_dir))
        cm1.close()

        cm2 = CacheManager(cache_dir=str(cache_dir))
        try:
            with cm2._get_cursor() as cursor:
                cursor.execute("PRAGMA table_info(task_status)")
                columns = [col[1] for col in cursor.fetchall()]
            assert columns.count("calibration_status") == 1
            assert columns.count("summary_status") == 1
        finally:
            cm2.close()


class TestUpdateTaskStatusLLMStatusColumns:
    """Tests for update_task_status writing the new calibration_status/summary_status columns."""

    def test_sets_calibration_and_summary_status_columns(self, cm):
        task = cm.create_task(url="https://example.com/colvid", platform="youtube", media_id="colvid")
        cm.update_task_status(
            task["task_id"], "success", platform="youtube", media_id="colvid",
            calibration_status="full", summary_status="generated",
        )
        row = cm.get_task_by_id(task["task_id"])
        assert row["calibration_status"] == "full"
        assert row["summary_status"] == "generated"

    def test_omitted_status_columns_stay_null(self, cm):
        task = cm.create_task(url="https://example.com/colvid2", platform="youtube", media_id="colvid2")
        cm.update_task_status(task["task_id"], "queued")
        row = cm.get_task_by_id(task["task_id"])
        assert row["calibration_status"] is None
        assert row["summary_status"] is None


# ---------------------------------------------------------------------------
# save_llm_status: llm_status.json read-modify-write (honest status model)
# ---------------------------------------------------------------------------

class TestSaveLLMStatus:
    """Tests for CacheManager.save_llm_status / llm_status.json persistence."""

    def test_writes_new_status_file(self, cm, cache_dir):
        _save_sample_capswriter(cm)
        ok = cm.save_llm_status(
            platform="youtube",
            media_id="vid1",
            use_speaker_recognition=False,
            calibration_status="full",
            calibration_stats={
                "total_segments": 2, "calibrated_segments": 2,
                "fallback_segments": 0, "low_quality_segments": 0,
            },
            summary_status="generated",
        )
        assert ok is True
        files = list(cache_dir.rglob("llm_status.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["calibration_status"] == "full"
        assert data["summary_status"] == "generated"
        assert data["calibration_stats"]["total_segments"] == 2
        assert "updated_at" in data

    def test_merge_preserves_untouched_fields(self, cm, cache_dir):
        """A later call that only updates calibration_status must not clobber
        the summary_status written by a previous call. This is what makes the
        calibrate_only recalibrate path (no summary re-run) safe: it can update
        calibration_status without accidentally erasing a prior GENERATED summary_status."""
        _save_sample_capswriter(cm)
        cm.save_llm_status(
            platform="youtube", media_id="vid1", use_speaker_recognition=False,
            calibration_status="full", summary_status="generated",
        )
        cm.save_llm_status(
            platform="youtube", media_id="vid1", use_speaker_recognition=False,
            calibration_status="partial",
            # summary_status intentionally omitted (None) -> must be preserved
        )
        files = list(cache_dir.rglob("llm_status.json"))
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["calibration_status"] == "partial"
        assert data["summary_status"] == "generated"

    def test_returns_false_when_cache_missing(self, cm):
        ok = cm.save_llm_status(
            platform="youtube", media_id="does-not-exist",
            use_speaker_recognition=False, calibration_status="full",
        )
        assert ok is False

    def test_concurrent_updates_to_different_fields_do_not_lose_either(
        self, cm, cache_dir, monkeypatch
    ):
        """Regression for the cross-task read-modify-write race (codex-review R2):
        two threads concurrently call save_llm_status for the SAME media, one
        updating only calibration_status (e.g. a recalibrate task) and the
        other updating only summary_status (e.g. a summary-backfill task).

        Without a per-media lock, the read-merge-write is not atomic across
        threads: both can read the same pre-update snapshot, then each writes
        its own merge back, and whichever writes last silently reverts the
        other's field to its pre-update value (a classic lost update).

        To make the race deterministic instead of relying on timing luck, the
        read step (json.load, used by both save_llm_status's own read and its
        internal get_cache() call) is slowed down so both threads are
        guaranteed to have read their snapshot before either writes back --
        this is the worst-case interleaving the per-media lock must prevent.
        With the lock, the second thread's critical section cannot start
        until the first's read-modify-write has fully completed, so its read
        always observes the first thread's write and neither field is lost.
        """
        _save_sample_capswriter(cm)
        # Seed a baseline file so both threads' reads hit an *existing*
        # llm_status.json (the slow-read hook only fires on json.load, which
        # is only invoked when there is a file to parse).
        cm.save_llm_status(
            platform="youtube", media_id="vid1", use_speaker_recognition=False,
            calibration_status="none",
        )

        original_load = json.load

        def slow_load(*args, **kwargs):
            result = original_load(*args, **kwargs)
            time.sleep(0.05)
            return result

        monkeypatch.setattr(cache_manager_module.json, "load", slow_load)

        barrier = threading.Barrier(2)
        errors = []

        def update_calibration():
            try:
                barrier.wait()
                cm.save_llm_status(
                    platform="youtube", media_id="vid1",
                    use_speaker_recognition=False,
                    calibration_status="full",
                )
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def update_summary():
            try:
                barrier.wait()
                cm.save_llm_status(
                    platform="youtube", media_id="vid1",
                    use_speaker_recognition=False,
                    summary_status="generated",
                )
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [
            threading.Thread(target=update_calibration),
            threading.Thread(target=update_summary),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        files = list(cache_dir.rglob("llm_status.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["calibration_status"] == "full", (
            "calibration_status lost to a concurrent write -- read-modify-write "
            "was not serialized per (platform, media_id)"
        )
        assert data["summary_status"] == "generated", (
            "summary_status lost to a concurrent write -- read-modify-write "
            "was not serialized per (platform, media_id)"
        )

    def test_write_is_atomic_concurrent_readers_never_see_partial_json(
        self, cm, cache_dir, monkeypatch
    ):
        """The write path must go through a temp-file-then-os.replace swap so
        a concurrent reader polling llm_status.json during a write always sees
        either the fully-old or fully-new content, never a truncated/empty
        file. A direct `open(path, 'w')` truncates the file immediately (at
        open time), so a slow writer leaves a 0-byte file on disk for the
        entire write duration -- exactly the window this test probes by
        slowing down json.dump and busy-polling the file from another thread.
        """
        _save_sample_capswriter(cm)
        cm.save_llm_status(
            platform="youtube", media_id="vid1", use_speaker_recognition=False,
            calibration_status="none",
        )
        status_file = list(cache_dir.rglob("llm_status.json"))[0]

        original_dump = json.dump

        def slow_dump(*args, **kwargs):
            result = original_dump(*args, **kwargs)
            time.sleep(0.05)
            return result

        monkeypatch.setattr(cache_manager_module.json, "dump", slow_dump)

        read_errors = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    text = status_file.read_text(encoding="utf-8")
                    json.loads(text)
                except json.JSONDecodeError as exc:
                    read_errors.append(str(exc) or "empty/partial content")
                except FileNotFoundError:
                    pass

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        try:
            cm.save_llm_status(
                platform="youtube", media_id="vid1", use_speaker_recognition=False,
                calibration_status="full",
            )
        finally:
            stop.set()
            reader_thread.join()

        assert read_errors == [], (
            f"Concurrent reader observed truncated/partial JSON: {read_errors}"
        )

        # No leftover .tmp artifact after a successful atomic swap.
        tmp_files = [
            f for f in status_file.parent.iterdir()
            if f.name.startswith("llm_status.json.tmp")
        ]
        assert tmp_files == []

    def test_corrupted_status_file_does_not_crash_save(self, cm, cache_dir):
        """save_llm_status's own read-merge-write must tolerate a corrupted
        (e.g. truncated by a crash mid-write) existing llm_status.json by
        treating it as empty and rewriting cleanly, rather than raising."""
        _save_sample_capswriter(cm)
        cm.save_llm_status(
            platform="youtube", media_id="vid1", use_speaker_recognition=False,
            calibration_status="full",
        )
        status_files = list(cache_dir.rglob("llm_status.json"))
        status_files[0].write_text("{not valid json!!", encoding="utf-8")

        ok = cm.save_llm_status(
            platform="youtube", media_id="vid1", use_speaker_recognition=False,
            summary_status="generated",
        )
        assert ok is True
        data = json.loads(status_files[0].read_text(encoding="utf-8"))
        assert data["summary_status"] == "generated"

    def test_corrupted_status_file_does_not_crash_get_cache(self, cm, cache_dir):
        """A hand-corrupted llm_status.json must not raise from get_cache;
        downstream readers (e.g. _resolve_summary_state) see no llm_status key
        and fall back to their legacy-compat inference instead of crashing."""
        _save_sample_capswriter(cm)
        cm.save_llm_status(
            platform="youtube", media_id="vid1", use_speaker_recognition=False,
            calibration_status="full", summary_status="generated",
        )
        status_files = list(cache_dir.rglob("llm_status.json"))
        status_files[0].write_text("{not valid json!!", encoding="utf-8")

        result = cm.get_cache(platform="youtube", media_id="vid1")
        assert result is not None
        assert "llm_status" not in result


# ---------------------------------------------------------------------------
# get_view_data_by_token: summary_state (honest status model, fixes the
# "总结处理中..." permanent placeholder bug)
# ---------------------------------------------------------------------------

class TestGetViewDataSummaryState:
    """Tests for the summary_state field on get_view_data_by_token, covering
    the four honest states plus the legacy (no llm_status.json) fallback."""

    def _make_success_task(self, cm, media_id="vidX"):
        _save_sample_capswriter(cm, media_id=media_id)
        task = cm.create_task(
            url=f"https://example.com/{media_id}",
            platform="youtube", media_id=media_id,
        )
        cm.update_task_status(
            task["task_id"], "success", platform="youtube", media_id=media_id,
        )
        return task

    def test_generated_state_returns_real_summary(self, cm):
        task = self._make_success_task(cm, "vidgen")
        cm.save_llm_result(
            platform="youtube", media_id="vidgen", use_speaker_recognition=False,
            llm_type="summary", content="A real generated summary.",
        )
        cm.save_llm_status(
            platform="youtube", media_id="vidgen", use_speaker_recognition=False,
            summary_status="generated",
        )

        view_data = cm.get_view_data_by_token(task["view_token"])
        assert view_data["summary_state"] == "generated"
        assert view_data["summary"] == "A real generated summary."

    def test_skipped_short_state_has_no_placeholder_summary(self, cm):
        task = self._make_success_task(cm, "vidskip")
        cm.save_llm_status(
            platform="youtube", media_id="vidskip", use_speaker_recognition=False,
            summary_status="skipped_short",
        )

        view_data = cm.get_view_data_by_token(task["view_token"])
        assert view_data["summary_state"] == "skipped_short"
        assert view_data["summary"] is None

    def test_failed_state_has_no_placeholder_summary(self, cm):
        task = self._make_success_task(cm, "vidfail")
        cm.save_llm_status(
            platform="youtube", media_id="vidfail", use_speaker_recognition=False,
            summary_status="failed",
        )

        view_data = cm.get_view_data_by_token(task["view_token"])
        assert view_data["summary_state"] == "failed"
        assert view_data["summary"] is None

    def test_pending_state_when_summary_status_pending(self, cm):
        task = self._make_success_task(cm, "vidpending")
        cm.save_llm_status(
            platform="youtube", media_id="vidpending", use_speaker_recognition=False,
            summary_status="pending",
        )

        view_data = cm.get_view_data_by_token(task["view_token"])
        assert view_data["summary_state"] == "pending"
        assert view_data["summary"] is None

    def test_disabled_state_has_no_placeholder_summary(self, cm):
        """summary_status=disabled (user turned off processing_options.summarize)
        must surface as its own state, with no fabricated summary text -- same
        shape as failed/skipped_short, distinct value."""
        task = self._make_success_task(cm, "viddisabled")
        cm.save_llm_status(
            platform="youtube", media_id="viddisabled", use_speaker_recognition=False,
            summary_status="disabled",
        )

        view_data = cm.get_view_data_by_token(task["view_token"])
        assert view_data["summary_state"] == "disabled"
        assert view_data["summary"] is None

    def test_legacy_no_status_file_with_summary_file_is_generated(self, cm):
        """Old cache predating llm_status.json but with a real llm_summary.txt
        must still show the summary."""
        task = self._make_success_task(cm, "vidlegacy1")
        cm.save_llm_result(
            platform="youtube", media_id="vidlegacy1", use_speaker_recognition=False,
            llm_type="summary", content="Legacy summary text.",
        )

        view_data = cm.get_view_data_by_token(task["view_token"])
        assert view_data["summary_state"] == "generated"
        assert view_data["summary"] == "Legacy summary text."

    def test_legacy_no_status_file_no_summary_file_is_not_placeholder(self, cm):
        """THE BUG THIS FIXES: an old task with no llm_status.json and no
        llm_summary.txt must NOT show the "processing..." placeholder forever.
        It must be reported as a definite non-pending state (no summary was
        ever generated), not as still-processing."""
        task = self._make_success_task(cm, "vidlegacy2")

        view_data = cm.get_view_data_by_token(task["view_token"])
        # Legacy tasks (predating this feature) can't be told apart from
        # "text was too short to summarize" vs "summary generation failed",
        # so they're conservatively bucketed as skipped_short (non-error UI).
        assert view_data["summary_state"] == "skipped_short"
        assert view_data["summary"] is None
        assert "处理中" not in (view_data.get("summary") or "")
