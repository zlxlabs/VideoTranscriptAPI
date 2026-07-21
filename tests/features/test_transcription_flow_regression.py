import types
from unittest.mock import MagicMock

import pytest

import video_transcript_api.api.services.transcription as transcription
from video_transcript_api.downloaders.models import VideoMetadata, DownloadInfo
from video_transcript_api.downloaders.subtitle_types import SubtitleResult
from video_transcript_api.utils.llm_status import CalibrationStatus, ChaptersStatus


class DummyQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class DummyNotifier:
    def __init__(self, webhook=None):
        self.webhook = webhook
        self.messages = []

    def notify_task_status(self, *args, **kwargs):
        self.messages.append(("notify", args, kwargs))

    def send_text(self, text, **kwargs):
        self.messages.append(("send_text", text, kwargs))

    def _clean_url(self, url):
        return url


class DummyCacheManager:
    def __init__(self, cache_data=None):
        self.cache_data = cache_data
        self.saved = []
        self.status_updates = []
        self.tasks = {}

    def get_cache(self, platform, media_id, use_speaker_recognition):
        return self.cache_data

    def save_cache(self, **kwargs):
        self.saved.append(kwargs)
        return True

    def update_task_status(self, task_id, status, **kwargs):
        self.status_updates.append((task_id, status, kwargs))
        # Real CacheManager.update_task_status is a compare-and-set that
        # returns True on a genuine win (see H2 fix, local codex review
        # round 7: process_transcription's cache-hit branch now gates its
        # completion notification on this return value). This double has
        # no terminal-stickiness model of its own -- callers that need to
        # simulate a CAS loss should stub this method directly rather than
        # relying on the default.
        return True

    def get_task_by_id(self, task_id):
        return self.tasks.get(task_id)


class DummyTranscriber:
    def __init__(self, *args, **kwargs):
        pass

    def transcribe(self, local_file, output_base):
        return {"transcript": "transcribed text"}


class DummyFunASR:
    def __init__(self, *args, **kwargs):
        pass

    def transcribe_sync(self, local_file):
        return {
            "formatted_text": "funasr text",
            "transcription_result": [{"speaker": "spk_0", "text": "hello"}],
        }

    def format_transcript_with_speakers(self, data):
        return "funasr formatted"


class YoutubeDownloader:
    def __init__(self, subtitle=None, download_url="http://example.com/audio.mp3", filename="test.mp3"):
        self._subtitle = subtitle
        self._download_url = download_url
        self._filename = filename
        self.use_api_server = False

    def get_metadata(self, url):
        return VideoMetadata(
            video_id="abc123",
            platform="youtube",
            title="test title",
            author="test author",
            description="test desc",
        )

    def get_download_info(self, url):
        return DownloadInfo(
            download_url=self._download_url,
            file_ext="mp3",
            filename=self._filename,
        )

    def get_subtitle(self, url):
        return self._subtitle

    def get_subtitle_result(self, url):
        # 生产代码自 T2 起改走 get_subtitle_result 保留 timeline；
        # dummy 无时间轴需求，segments 恒为 None。
        if self._subtitle is None:
            return None
        return SubtitleResult(text=self._subtitle, segments=None)

    def download_file(self, url, filename):
        return "C:/tmp/test.mp3"

    def fetch_for_transcription(self, *args, **kwargs):
        raise AssertionError("fetch_for_transcription should not be called in this test")


class GenericDownloader:
    def __init__(self):
        self.calls = []

    def download_file(self, url, filename):
        self.calls.append((url, filename))
        return "C:/tmp/direct.mp3"


@pytest.fixture
def patch_runtime(monkeypatch):
    queue = DummyQueue()
    monkeypatch.setattr(transcription, "llm_task_queue", queue)
    monkeypatch.setattr(transcription, "WechatNotifier", DummyNotifier)
    monkeypatch.setattr(transcription, "send_long_text_wechat", lambda *args, **kwargs: None)
    monkeypatch.setattr(transcription, "Transcriber", DummyTranscriber)
    monkeypatch.setattr(transcription, "FunASRSpeakerClient", DummyFunASR)
    monkeypatch.setattr(transcription, "get_base_url", lambda: "http://test")
    return queue


def test_flow_cache_hit(monkeypatch, patch_runtime):
    cache_data = {
        "platform": "youtube",
        "media_id": "abc123",
        "title": "cached title",
        "author": "cached author",
        "description": "cached desc",
        "transcript_type": "capswriter",
        "transcript_data": "cached transcript",
        "use_speaker_recognition": False,
        "llm_calibrated": "calibrated",
        "llm_summary": "summary",
        # llm_status.json 是校对层"已提交完成"的权威标记（本地 codex
        # review 第 16 轮 Q2）；没有它，has_llm_calibrated=True 不再足以
        # 判定校对层已满足。这里模拟一次真正完整落盘（含状态文件）的
        # 历史full-flow结果，因为本用例要验证的是"完全命中不再入队"，
        # 不是缺状态文件那条独立路径（后者见
        # test_layered_cache.py::test_missing_llm_status_does_not_trust_existing_calibrated_file）。
        "llm_status": {"calibration_status": CalibrationStatus.FULL,
                       "chapters_status": ChaptersStatus.SKIPPED_SHORT},
    }
    cache_manager = DummyCacheManager(cache_data=cache_data)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    def fail_create_downloader(url):
        raise AssertionError("create_downloader should not be called on cache hit")

    monkeypatch.setattr(transcription, "create_downloader", fail_create_downloader)

    result = transcription.process_transcription(
        task_id="task_cache_hit",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
    )

    assert result["status"] == "success"
    assert result["data"]["cached"] is True
    assert len(patch_runtime.items) == 0


def test_flow_cache_hit_with_disabled_calibration_notifies_disclaimer(monkeypatch, patch_runtime):
    """ci-gate review: a full cache hit whose calibration layer was produced
    by a prior calibrate=False run (locally-formatted placeholder text, not
    real LLM calibration) must disclose this in the notification -- otherwise
    a user who requested calibrate=False & summarize=True the first time,
    then triggers this exact "everything's cached" branch on a later
    request, silently receives a summary/"calibrated" text built from
    unedited ASR output with zero indication of that fact."""
    cache_data = {
        "platform": "youtube",
        "media_id": "abc123",
        "title": "cached title",
        "author": "cached author",
        "description": "cached desc",
        "transcript_type": "capswriter",
        "transcript_data": "cached transcript",
        "use_speaker_recognition": False,
        "llm_calibrated": "locally formatted placeholder (never LLM-calibrated)",
        "llm_summary": "summary built from that placeholder",
        "llm_status": {
            "calibration_status": CalibrationStatus.DISABLED,
            "summary_status": "generated",
            "chapters_status": ChaptersStatus.SKIPPED_SHORT,
        },
    }
    cache_manager = DummyCacheManager(cache_data=cache_data)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    def fail_create_downloader(url):
        raise AssertionError("create_downloader should not be called on cache hit")

    monkeypatch.setattr(transcription, "create_downloader", fail_create_downloader)

    notification_router = MagicMock()
    notification_router.send_long_text = MagicMock()
    notification_router.send_text = MagicMock()
    monkeypatch.setattr(transcription, "get_notification_router", lambda: notification_router)

    result = transcription.process_transcription(
        task_id="task_cache_hit_disabled_calibration",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
        # 必须显式传 calibrate=False，与本次请求一致，才能让分层缓存命中判定
        # 走到"全命中"分支——calibrate_requested 默认(None)按 True 兜底，
        # 会误判成"要求真校对但缓存里没有"，转而重新排队补校对，根本不会
        # 触碰到这里要验证的通知拼接代码。
        processing_options={"calibrate": False, "summarize": True},
    )

    assert result["status"] == "success"
    assert result["data"]["cached"] is True

    assert notification_router.send_long_text.called
    sent_text = notification_router.send_long_text.call_args.kwargs["text"]
    assert "未启用" in sent_text


def test_flow_cache_hit_with_summary_disabled_shows_not_enabled_label(monkeypatch, patch_runtime):
    """ci-gate review (cloud CI): a full cache hit whose summary layer was
    disabled by a prior summarize=False request must say "未启用" in the
    notification, not the generic "未生成" -- otherwise the honest status
    model this PR introduces (SummaryStatus.DISABLED distinct from
    SKIPPED_SHORT/FAILED) is meaningless on this notification path, since it
    doesn't consume summary_status at all and hardcodes "未生成" for every
    reason a summary might be missing."""
    cache_data = {
        "platform": "youtube",
        "media_id": "abc123",
        "title": "cached title",
        "author": "cached author",
        "description": "cached desc",
        "transcript_type": "capswriter",
        "transcript_data": "cached transcript",
        "use_speaker_recognition": False,
        "llm_calibrated": "calibrated",
        # summarize=False never writes llm_summary.txt -- no "llm_summary" key.
        "llm_status": {
            "calibration_status": CalibrationStatus.FULL,
            "summary_status": "disabled",
        },
    }
    cache_manager = DummyCacheManager(cache_data=cache_data)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    def fail_create_downloader(url):
        raise AssertionError("create_downloader should not be called on cache hit")

    monkeypatch.setattr(transcription, "create_downloader", fail_create_downloader)

    notification_router = MagicMock()
    notification_router.send_long_text = MagicMock()
    notification_router.send_text = MagicMock()
    monkeypatch.setattr(transcription, "get_notification_router", lambda: notification_router)

    result = transcription.process_transcription(
        task_id="task_cache_hit_summary_disabled",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
        processing_options={"calibrate": True, "summarize": False},
    )

    assert result["status"] == "success"
    assert result["data"]["cached"] is True

    assert notification_router.send_long_text.called
    sent_text = notification_router.send_long_text.call_args.kwargs["text"]
    assert "未启用" in sent_text
    assert "未生成" not in sent_text


def test_flow_cache_hit_with_full_calibration_has_no_disclaimer(monkeypatch, patch_runtime):
    """Sanity check: a normal, real calibration cache hit must NOT show the
    'calibration disabled' disclaimer."""
    cache_data = {
        "platform": "youtube",
        "media_id": "abc123",
        "title": "cached title",
        "author": "cached author",
        "description": "cached desc",
        "transcript_type": "capswriter",
        "transcript_data": "cached transcript",
        "use_speaker_recognition": False,
        "llm_calibrated": "calibrated",
        "llm_summary": "summary",
        "llm_status": {
            "calibration_status": CalibrationStatus.FULL,
            "summary_status": "generated",
            "chapters_status": ChaptersStatus.SKIPPED_SHORT,
        },
    }
    cache_manager = DummyCacheManager(cache_data=cache_data)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)
    monkeypatch.setattr(
        transcription, "create_downloader",
        lambda url: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    notification_router = MagicMock()
    notification_router.send_long_text = MagicMock()
    notification_router.send_text = MagicMock()
    monkeypatch.setattr(transcription, "get_notification_router", lambda: notification_router)

    result = transcription.process_transcription(
        task_id="task_cache_hit_full_calibration",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
        processing_options={"calibrate": True, "summarize": True},
    )

    assert result["status"] == "success"
    assert notification_router.send_long_text.called
    sent_text = notification_router.send_long_text.call_args.kwargs["text"]
    assert "未启用" not in sent_text


def test_flow_subtitle_preferred(monkeypatch, patch_runtime):
    cache_manager = DummyCacheManager(cache_data=None)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    downloader = YoutubeDownloader(subtitle="subtitle text")
    monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

    result = transcription.process_transcription(
        task_id="task_subtitle",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
    )

    assert result["status"] == "success"
    assert result["data"]["transcript"] == "subtitle text"
    assert cache_manager.saved
    saved = cache_manager.saved[0]
    assert saved["transcript_type"] == "capswriter"
    assert saved["transcript_data"] == "subtitle text"


def test_flow_download_capswriter(monkeypatch, patch_runtime):
    cache_manager = DummyCacheManager(cache_data=None)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    downloader = YoutubeDownloader(subtitle=None, download_url="http://example.com/audio.mp3", filename="audio.mp3")
    monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

    result = transcription.process_transcription(
        task_id="task_download_caps",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
    )

    assert result["status"] == "success"
    assert result["data"]["transcript"] == "transcribed text"
    assert cache_manager.saved
    saved = cache_manager.saved[0]
    assert saved["transcript_type"] == "capswriter"
    assert saved["use_speaker_recognition"] is False


def test_flow_download_funasr(monkeypatch, patch_runtime):
    cache_manager = DummyCacheManager(cache_data=None)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    downloader = YoutubeDownloader(subtitle=None, download_url="http://example.com/audio.mp3", filename="audio.mp3")
    monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

    result = transcription.process_transcription(
        task_id="task_download_funasr",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=True,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
    )

    assert result["status"] == "success"
    assert result["data"]["speaker_recognition"] is True
    assert cache_manager.saved
    saved = cache_manager.saved[0]
    assert saved["transcript_type"] == "funasr"
    assert saved["use_speaker_recognition"] is True


def test_flow_separate_download_url(monkeypatch, patch_runtime):
    cache_manager = DummyCacheManager(cache_data=None)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    metadata_downloader = YoutubeDownloader(subtitle=None)
    monkeypatch.setattr(transcription, "create_downloader", lambda url: metadata_downloader)

    generic_downloader = GenericDownloader()
    import video_transcript_api.downloaders.generic as generic_module
    monkeypatch.setattr(generic_module, "GenericDownloader", lambda: generic_downloader)

    result = transcription.process_transcription(
        task_id="task_separate_url",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url="http://example.com/file.mp3",
        metadata_override=None,
    )

    assert result["status"] == "success"
    assert generic_downloader.calls
    assert generic_downloader.calls[0][0] == "http://example.com/file.mp3"
    assert generic_downloader.calls[0][1] == "file.mp3"


def test_flow_download_url_skips_youtube_api(monkeypatch, patch_runtime):
    cache_manager = DummyCacheManager(cache_data=None)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    downloader = YoutubeDownloader(subtitle=None)
    downloader.use_api_server = True
    monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

    generic_downloader = GenericDownloader()
    import video_transcript_api.downloaders.generic as generic_module
    monkeypatch.setattr(generic_module, "GenericDownloader", lambda: generic_downloader)

    result = transcription.process_transcription(
        task_id="task_download_url_skip_api",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url="http://example.com/file.mp3",
        metadata_override=None,
    )

    assert result["status"] == "success"
    assert generic_downloader.calls


def test_flow_final_exception_persists_failed_before_notifying(monkeypatch, patch_runtime):
    """The top-level `except Exception` at the tail of process_transcription
    must persist TaskStatus.FAILED before notifying -- matching the queue
    processor's "write terminal state, then notify" ordering used elsewhere
    in this module (run_and_finalize's except block). Doing it backwards
    means a slow/misbehaving notifier could delay the FAILED write, leaving
    GET /api/task showing a stale in-flight status longer than necessary.

    Forces this exact final except by making cache_manager.save_cache raise
    during the download+transcribe branch's post-transcription save step: an
    otherwise fully successful "no subtitle, must download" flow reaches
    that save_cache call as a plain, locally-unguarded statement, so the
    raise propagates straight to the outer handler under test.

    Retargeted from the CALIBRATING transition (round 15, local codex
    review): that transition -- along with its own exception handling --
    now lives inside _handoff_to_llm_stage (see TestHandoffToLlmStageFailure
    Modes below), so a CALIBRATING-write exception no longer propagates
    past that point; it is caught, converged to a local failed response and
    returned directly instead of reaching this outer handler. save_cache
    remains a genuinely unguarded call site, preserving this test's original
    "write terminal state before notifying" assertion.
    """
    order = []

    class OrderTrackingCacheManager(DummyCacheManager):
        def save_cache(self, **kwargs):
            raise RuntimeError("boom-save-cache")

        def update_task_status(self, task_id, status, **kwargs):
            # M1 修复（PR3 review hardening 收尾轮）随手修正：这里此前遗漏了
            # return，导致本方法恒隐式返回 None（falsy）——旧代码在 CAS=False
            # 分支对"非 success"情况仍会发通知，掩盖了这个 fixture bug；M1
            # 收紧门控后不再发，之前被掩盖的 bug 就会让这个测试假红。补上
            # return 让它如实转发真正的 CAS 结果（这里恒为 True，模拟本次
            # 调用就是真正的 CAS 胜者，与测试文档字符串"write terminal state
            # before notifying"的意图一致）。
            won = super().update_task_status(task_id, status, **kwargs)
            if status == transcription.TaskStatus.FAILED:
                order.append("update_failed")
            return won

    class OrderTrackingNotifier:
        """Stands in for the shared router used both by the per-task
        progress notifier (many calls throughout the flow, e.g. "开始处理",
        "正在下载视频", ...) and directly by the final except block under
        test. Only the except block's own call uses status="转录异常", so
        filter on that to avoid the order log being drowned out by the
        earlier progress notifications that share this same router
        instance (it is fetched once via get_notification_router() at the
        top of process_transcription and reused for every task_notifier
        call across the whole function).
        """

        def notify_task_status(self, *args, **kwargs):
            if kwargs.get("status") == "转录异常":
                order.append("notify")

    cache_manager = OrderTrackingCacheManager(cache_data=None)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)
    monkeypatch.setattr(
        transcription, "get_notification_router", lambda: OrderTrackingNotifier()
    )

    downloader = YoutubeDownloader(
        subtitle=None, download_url="http://example.com/audio.mp3", filename="audio.mp3"
    )
    monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

    result = transcription.process_transcription(
        task_id="task_final_exc_order",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
    )

    assert result["status"] == "failed"
    assert order == ["update_failed", "notify"]


# ---------------------------------------------------------------------------
# Y4 (PR3 review hardening 加固轮): save_cache returning its documented
# failure signal (a falsy value -- CacheManager.save_cache's real
# implementation always catches internally and returns None, never raises;
# see cache/cache_manager.py) must not be treated as "log an error and keep
# going". Before this fix, every one of the module's `if not cache_result:`
# branches only called logger.error() and then fell straight through to
# _handoff_to_llm_stage / the success return -- the transcript was never
# actually persisted (no cache row, no files on disk), yet the task was
# reported "success" and handed off to the LLM stage. After a restart there
# is nothing on disk to recover: the terminal snapshot and the real cache
# state silently disagree. Each of these three tests targets one of the
# `if not cache_result:` sites (platform-subtitle branch, and the two
# transcribe-then-save branches inside the main download+transcribe flow)
# and asserts the same convergence: the task ends up FAILED (with an
# error_message, which update_task_status also folds into terminal_snapshot
# -- see its own docstring), and nothing is hand off to the LLM queue.
# ---------------------------------------------------------------------------


def test_flow_subtitle_save_cache_failure_persists_failed_and_skips_llm_handoff(
    monkeypatch, patch_runtime,
):
    """Targets the "平台字幕" (non-youtube-api) branch's save_cache call."""
    cache_manager = DummyCacheManager(cache_data=None)
    cache_manager.save_cache = lambda **kwargs: None
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    downloader = YoutubeDownloader(subtitle="subtitle text")
    monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

    result = transcription.process_transcription(
        task_id="task_subtitle_save_fail",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
    )

    assert result["status"] == "failed"
    failed_updates = [
        u for u in cache_manager.status_updates if u[1] == transcription.TaskStatus.FAILED
    ]
    assert failed_updates, "must persist a FAILED status update when save_cache fails"
    assert failed_updates[-1][2].get("error_message"), (
        "FAILED write must carry an error_message (folded into terminal_snapshot "
        "by update_task_status)"
    )
    assert patch_runtime.items == [], (
        "must not hand off to the LLM stage -- the transcript was never persisted"
    )


def test_flow_download_capswriter_save_cache_failure_persists_failed_and_skips_llm_handoff(
    monkeypatch, patch_runtime,
):
    """Targets the CapsWriter branch of the main download+transcribe flow's
    save_cache call (companion happy-path baseline: test_flow_download_capswriter)."""
    cache_manager = DummyCacheManager(cache_data=None)
    cache_manager.save_cache = lambda **kwargs: None
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    downloader = YoutubeDownloader(
        subtitle=None, download_url="http://example.com/audio.mp3", filename="audio.mp3",
    )
    monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

    result = transcription.process_transcription(
        task_id="task_download_caps_save_fail",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=False,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
    )

    assert result["status"] == "failed"
    failed_updates = [
        u for u in cache_manager.status_updates if u[1] == transcription.TaskStatus.FAILED
    ]
    assert failed_updates, "must persist a FAILED status update when save_cache fails"
    assert patch_runtime.items == [], (
        "must not hand off to the LLM stage -- the transcript was never persisted"
    )


def test_flow_download_funasr_save_cache_failure_persists_failed_and_skips_llm_handoff(
    monkeypatch, patch_runtime,
):
    """Targets the FunASR branch of the main download+transcribe flow's
    save_cache call (companion happy-path baseline: test_flow_download_funasr)."""
    cache_manager = DummyCacheManager(cache_data=None)
    cache_manager.save_cache = lambda **kwargs: None
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    downloader = YoutubeDownloader(
        subtitle=None, download_url="http://example.com/audio.mp3", filename="audio.mp3",
    )
    monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

    result = transcription.process_transcription(
        task_id="task_download_funasr_save_fail",
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=True,
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
    )

    assert result["status"] == "failed"
    failed_updates = [
        u for u in cache_manager.status_updates if u[1] == transcription.TaskStatus.FAILED
    ]
    assert failed_updates, "must persist a FAILED status update when save_cache fails"
    assert patch_runtime.items == [], (
        "must not hand off to the LLM stage -- the transcript was never persisted"
    )


# ---------------------------------------------------------------------------
# LLM handoff registration (local codex review round 13, sole finding):
# transcription.py's internal llm_task_queue.put() call sites must register
# the task into inflight_registry's "llm" bucket *before* handing off --
# otherwise the task sits in neither bucket between the "transcription"
# worker's future completing (put() succeeded, worker returns quickly) and
# the eventual LLM future completing (queued + executed, which can take far
# longer), and runtime reconciliation (app.py::_periodic_maintenance) can
# misclassify it as an orphan and CAS it to failed while it is still
# legitimately queued/processing. These tests bind a real RuntimeContext so
# transcription._register_llm_handoff's get_inflight_registry() call
# resolves to a real, observable bucket instead of the disposable per-call
# fallback every *other* test in this file relies on (see
# test_unbound_runtime_does_not_raise_or_block_handoff below for proof that
# fallback path stays harmless).
# ---------------------------------------------------------------------------


@pytest.fixture
def bound_runtime(tmp_path):
    """A real, minimally-configured RuntimeContext bound for one test --
    gives get_inflight_registry() a real "llm" bucket to observe."""
    from video_transcript_api.api.context import RuntimeContext, bind_runtime, unbind_runtime

    config = {
        "api": {"host": "127.0.0.1", "port": 8000, "auth_token": "test-token"},
        "concurrent": {"max_workers": 1, "queue_size": 2, "llm_max_workers": 1},
        "storage": {
            "cache_dir": str(tmp_path / "cache"),
            "workspace_dir": str(tmp_path / "workspace"),
            "temp_dir": str(tmp_path / "temp"),
            "audit_db": str(tmp_path / "audit.db"),
        },
        "web": {"base_url": "http://localhost:8000"},
        "llm": {
            "api_key": "test-llm-key",
            "base_url": "http://127.0.0.1:1/v1",
            "calibrate_model": "test-calibrate-model",
            "summary_model": "test-summary-model",
        },
        "log": {"file": str(tmp_path / "app.log")},
    }
    runtime = RuntimeContext(config)
    # .start() populates temp_manager/cache_manager/etc -- process_transcription
    # reaches get_temp_manager() early on even in these synchronous,
    # queue/executor-bypassing calls. The executors it also creates are never
    # submitted to here (tests call process_transcription() directly, not via
    # the queue+executor pipeline) but are shut down below for hygiene.
    # wait=True: nothing is ever submitted to them, so there is nothing to
    # wait for -- waiting keeps teardown deterministic and avoids a worker
    # thread from a `wait=False` shutdown still exiting after this fixture
    # tears down and polluting an unrelated later test's
    # threading.active_count() snapshot (e.g. test_runtime_lifecycle.py's
    # side-effect-free checks).
    runtime.start()
    token = bind_runtime(runtime)
    try:
        yield runtime
    finally:
        unbind_runtime(token)
        runtime.executor.shutdown(wait=True, cancel_futures=True)
        runtime.llm_executor.shutdown(wait=True, cancel_futures=True)
        runtime.maintenance_executor.shutdown(wait=True, cancel_futures=True)
        runtime.cache_manager.close()


class TestLlmHandoffRegistersBeforePut:
    """Covers three of the five llm_task_queue.put() call sites (the cache-
    hit requeue branch, the platform-subtitle branch, and the actual
    download+transcribe branch) -- the two remaining sites (the YouTube API
    Server fast path's "has platform transcript" / "needs transcription"
    branches) are structurally identical one-line insertions calling the
    same _register_llm_handoff helper, verified by direct code inspection
    rather than duplicated here (reaching them requires mocking
    fetch_for_transcription's full response contract, disproportionate to
    what is otherwise a single shared helper call)."""

    def test_cache_hit_requeue_registers_llm_bucket(
        self, monkeypatch, patch_runtime, bound_runtime
    ):
        """Cache already has a real calibration but no summary -- reusing
        the calibrated text as summary input requeues via the cache-hit
        branch's llm_task_queue.put() (transcription.py's first site)."""
        cache_data = {
            "platform": "youtube",
            "media_id": "abc123",
            "title": "cached title",
            "author": "cached author",
            "description": "cached desc",
            "transcript_type": "capswriter",
            "transcript_data": "cached transcript",
            "use_speaker_recognition": False,
            "llm_calibrated": "real calibrated text",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }
        cache_manager = DummyCacheManager(cache_data=cache_data)
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)
        monkeypatch.setattr(
            transcription, "create_downloader",
            lambda url: (_ for _ in ()).throw(AssertionError("should not be called")),
        )

        result = transcription.process_transcription(
            task_id="task_cache_hit_handoff",
            url="https://www.youtube.com/watch?v=abc123",
            use_speaker_recognition=False,
            wechat_webhook=None,
            download_url=None,
            metadata_override=None,
        )

        assert result["status"] == "success"
        assert len(patch_runtime.items) == 1
        assert bound_runtime.inflight_registry.size("llm") == 1
        assert "task_cache_hit_handoff" in bound_runtime.inflight_registry.all_task_ids()

    def test_subtitle_branch_registers_llm_bucket(
        self, monkeypatch, patch_runtime, bound_runtime
    ):
        """Platform-subtitle branch (transcription.py's fourth site)."""
        cache_manager = DummyCacheManager(cache_data=None)
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)
        downloader = YoutubeDownloader(subtitle="subtitle text")
        monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

        result = transcription.process_transcription(
            task_id="task_subtitle_handoff",
            url="https://www.youtube.com/watch?v=abc123",
            use_speaker_recognition=False,
            wechat_webhook=None,
            download_url=None,
            metadata_override=None,
        )

        assert result["status"] == "success"
        assert len(patch_runtime.items) == 1
        assert bound_runtime.inflight_registry.size("llm") == 1
        assert "task_subtitle_handoff" in bound_runtime.inflight_registry.all_task_ids()

    def test_download_transcribe_branch_registers_llm_bucket(
        self, monkeypatch, patch_runtime, bound_runtime
    ):
        """Actual download+transcribe branch (transcription.py's fifth,
        last site)."""
        cache_manager = DummyCacheManager(cache_data=None)
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)
        downloader = YoutubeDownloader(
            subtitle=None, download_url="http://example.com/audio.mp3", filename="audio.mp3"
        )
        monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

        result = transcription.process_transcription(
            task_id="task_download_handoff",
            url="https://www.youtube.com/watch?v=abc123",
            use_speaker_recognition=False,
            wechat_webhook=None,
            download_url=None,
            metadata_override=None,
        )

        assert result["status"] == "success"
        assert len(patch_runtime.items) == 1
        assert bound_runtime.inflight_registry.size("llm") == 1
        assert "task_download_handoff" in bound_runtime.inflight_registry.all_task_ids()

    def test_unbound_runtime_does_not_raise_or_block_handoff(self, monkeypatch, patch_runtime):
        """No bound runtime (this file's ~20 other tests, and production
        scripts outside create_app()'s lifespan) -- _register_llm_handoff's
        get_inflight_registry() call must degrade to a harmless throwaway
        registry (see get_inflight_registry's docstring), never raising and
        never preventing the actual llm_task_queue.put() handoff."""
        cache_manager = DummyCacheManager(cache_data=None)
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)
        downloader = YoutubeDownloader(subtitle="subtitle text")
        monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)

        result = transcription.process_transcription(
            task_id="task_unbound_handoff",
            url="https://www.youtube.com/watch?v=abc123",
            use_speaker_recognition=False,
            wechat_webhook=None,
            download_url=None,
            metadata_override=None,
        )

        assert result["status"] == "success"
        assert len(patch_runtime.items) == 1


# ---------------------------------------------------------------------------
# _handoff_to_llm_stage failure modes (local codex review round 15, sole
# finding): before this fix, all five llm_task_queue.put() call sites did
# "register_internal + put() first, write CALIBRATING after" -- once put()
# succeeds the queued item is visible to (and possibly already claimed by)
# the LLM consumer, so a CALIBRATING write that then fails or loses its
# terminal-stickiness CAS could not be undone: the task would present some
# terminal status to callers while an LLM worker kept processing it
# regardless, eventually burning tokens on an abandoned task whose success
# CAS is silently rejected. The fix reorders to "write CALIBRATING first,
# check its result, only register+put on a genuine win" via one shared
# helper, _handoff_to_llm_stage -- these tests exercise its three failure
# branches directly (cheaper and more precise than re-deriving each one
# through every one of the five process_transcription call sites), plus one
# end-to-end regression test through the actual call site the original bug
# report called out by name.
# ---------------------------------------------------------------------------
class TestHandoffToLlmStageFailureModes:
    def _kwargs(self, task_id="task_handoff"):
        return dict(
            task_id=task_id,
            llm_payload={"task_id": task_id},
            calibrating_status_kwargs={
                "platform": "youtube",
                "media_id": "abc123",
                "title": "t",
                "author": "a",
                "download_url": None,
            },
            task_notifier=DummyNotifier(),
            log_context="test",
        )

    def test_calibrating_write_exception_skips_handoff_and_converges_failed(
        self, monkeypatch, patch_runtime, bound_runtime
    ):
        """(a) CALIBRATING write itself raises: no put, no llm registration,
        task converges to failed (best-effort), no registry slot leaked
        (there was nothing to leak -- register_internal is never reached)."""

        class RaisingCalibratingCacheManager(DummyCacheManager):
            def update_task_status(self, task_id, status, **kwargs):
                if status == transcription.TaskStatus.CALIBRATING:
                    raise RuntimeError("boom-calibrating")
                return super().update_task_status(task_id, status, **kwargs)

        cache_manager = RaisingCalibratingCacheManager()
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)

        result = transcription._handoff_to_llm_stage(**self._kwargs("task_a"))

        assert result == {
            "status": "failed",
            "message": "任务状态写入异常: boom-calibrating",
        }
        assert len(patch_runtime.items) == 0
        assert bound_runtime.inflight_registry.size("llm") == 0
        assert "task_a" not in bound_runtime.inflight_registry.all_task_ids()
        failed_writes = [
            u for u in cache_manager.status_updates
            if u[1] == transcription.TaskStatus.FAILED
        ]
        assert len(failed_writes) == 1
        assert "boom-calibrating" in failed_writes[0][2]["error_message"]

    def test_calibrating_cas_false_skips_handoff_and_reports_actual_status(
        self, monkeypatch, patch_runtime, bound_runtime
    ):
        """(b) CALIBRATING write returns False (terminal stickiness: the
        task was already CAS'd to a terminal status by some concurrent
        process -- reconciliation, shutdown drain, a racing duplicate
        request): no put, no llm registration, no overwrite of the existing
        terminal row, and the response honestly reflects whatever that
        terminal status actually is rather than hardcoding "failed"."""

        class CasBlockedCacheManager(DummyCacheManager):
            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.CALIBRATING:
                    return False
                return True

        cache_manager = CasBlockedCacheManager()
        cache_manager.tasks["task_b_failed"] = {"status": "failed"}
        cache_manager.tasks["task_b_success"] = {"status": "success"}
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)

        result_failed = transcription._handoff_to_llm_stage(**self._kwargs("task_b_failed"))
        assert result_failed["status"] == "failed"
        assert "failed" in result_failed["message"]

        result_success = transcription._handoff_to_llm_stage(**self._kwargs("task_b_success"))
        assert result_success["status"] == "success"
        assert "success" in result_success["message"]

        assert len(patch_runtime.items) == 0
        assert bound_runtime.inflight_registry.size("llm") == 0
        # No FAILED write was ever attempted for either task -- CAS-blocked
        # means the row is already terminal; overwriting it would violate
        # the CAS contract this whole registry/status-write ordering exists
        # to protect.
        assert not any(
            u[1] == transcription.TaskStatus.FAILED for u in cache_manager.status_updates
        )

    def test_put_failure_after_successful_calibrating_releases_llm_slot(
        self, monkeypatch, patch_runtime, bound_runtime
    ):
        """(c) CALIBRATING succeeds, then put() raises: this is the
        pre-existing "queue put failed" handling path (fail the task,
        notify) -- confirmed here to newly also release the llm registry
        slot that register_internal claimed immediately beforehand. Before
        this fix no call site released it on this path, so a task that
        failed to queue would permanently occupy a slot in the "llm" bucket
        (see _InflightTaskRegistry's docstring) despite never being handed
        to any LLM worker."""

        class RaisingQueue:
            def put(self, item):
                raise RuntimeError("boom-put")

        monkeypatch.setattr(transcription, "llm_task_queue", RaisingQueue())
        cache_manager = DummyCacheManager()
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)
        notifier = DummyNotifier()

        kwargs = self._kwargs("task_c")
        kwargs["task_notifier"] = notifier
        result = transcription._handoff_to_llm_stage(**kwargs)

        assert result == {
            "status": "failed",
            "message": "LLM任务加入队列失败: boom-put",
        }
        assert bound_runtime.inflight_registry.size("llm") == 0
        assert "task_c" not in bound_runtime.inflight_registry.all_task_ids()
        assert any(msg[0] == "send_text" for msg in notifier.messages)
        failed_writes = [
            u for u in cache_manager.status_updates
            if u[1] == transcription.TaskStatus.FAILED
        ]
        assert len(failed_writes) == 1
        assert "boom-put" in failed_writes[0][2]["error_message"]

    def test_put_failure_with_raising_notifier_still_writes_failed_and_releases_slot(
        self, monkeypatch, patch_runtime, bound_runtime
    ):
        """R3 + W5 (PR3 review hardening, 二轮修正): put() fails AND the
        failure notification itself raises (e.g. webhook timeout/rate-limit).

        R3 already moved release("llm", task_id) ahead of the notify call
        (release() is a lock-guarded dict.pop that cannot itself raise, so
        the slot is always released here regardless of what the notifier
        does). This test used to additionally assert that the notifier's
        RuntimeError propagated OUT of _handoff_to_llm_stage -- but that was
        pinning down the SECOND half of the same bug class R3 fixed the
        first half of: the FAILED terminal-status write sat *after*
        task_notifier.send_text() in the except block, so a raising notifier
        skipped it entirely too. The task was left stuck in CALIBRATING (a
        non-terminal status) forever -- clients polling it would never see
        a terminal result, and this test asserted that as correct behavior
        by expecting the exception to escape uncaught.

        W5 fixes the ordering for real: FAILED is now written (and its CAS
        return value checked) *before* the notify attempt, and the notify
        call itself is wrapped in its own try/except (mirrors R2/K3 in
        llm_ops.py) so a raising notifier can never again swallow a
        terminal-status write. This test now asserts the corrected
        contract: no exception escapes, FAILED is durably written with the
        original put() failure recorded in error_message, send_text was
        still attempted (best-effort notification), and the llm registry
        slot is released -- the function returns its normal failed response
        dict instead of raising."""

        class RaisingQueue:
            def put(self, item):
                raise RuntimeError("boom-put")

        class RaisingNotifier(DummyNotifier):
            def send_text(self, text, **kwargs):
                super().send_text(text, **kwargs)
                raise RuntimeError("webhook timeout")

        monkeypatch.setattr(transcription, "llm_task_queue", RaisingQueue())
        cache_manager = DummyCacheManager()
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)
        notifier = RaisingNotifier()

        kwargs = self._kwargs("task_d")
        kwargs["task_notifier"] = notifier

        result = transcription._handoff_to_llm_stage(**kwargs)

        assert result == {
            "status": "failed",
            "message": "LLM任务加入队列失败: boom-put",
        }
        assert bound_runtime.inflight_registry.size("llm") == 0
        assert "task_d" not in bound_runtime.inflight_registry.all_task_ids()
        assert any(msg[0] == "send_text" for msg in notifier.messages)
        failed_writes = [
            u for u in cache_manager.status_updates
            if u[1] == transcription.TaskStatus.FAILED
        ]
        assert len(failed_writes) == 1, (
            "FAILED must still be written even though the notifier raised "
            "(red on the pre-W5 code: notify-before-write meant this write "
            "never happened)"
        )
        assert "boom-put" in failed_writes[0][2]["error_message"]

    def test_calibrating_write_exception_and_failed_fallback_also_raises_propagates(
        self, monkeypatch, patch_runtime, bound_runtime
    ):
        """G1 (CI review round 2, major): (a)'s best-effort FAILED fallback
        write can itself fail (e.g. the same DB outage that broke the
        CALIBRATING write). Before this fix, that second exception was only
        logged -- the function then fell through to `return {"status":
        "failed", ...}`, so every caller (process_transcription's five
        internal call sites) believed the terminal write had succeeded and
        moved on, while the task row was actually still stuck in whatever
        non-terminal status it had before (typically PROCESSING). Nothing
        would ever surface this except a runtime reconciliation sweep, up
        to ~27h later. The fix re-raises instead: the exception now
        propagates out of _handoff_to_llm_stage uncaught by anything else in
        this function, straight to whichever process_transcription call
        site invoked it -- and from there to that function's own outer
        `except Exception as exc:` handler, and ultimately the worker
        future. No llm registry slot was ever claimed here (register_
        internal is only reached after a *successful* CALIBRATING write), so
        there is nothing left to release."""

        class DoublyRaisingCacheManager(DummyCacheManager):
            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.CALIBRATING:
                    raise RuntimeError("boom-calibrating")
                if status == transcription.TaskStatus.FAILED:
                    raise RuntimeError("boom-failed-fallback")
                return True

        cache_manager = DoublyRaisingCacheManager()
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)

        with pytest.raises(RuntimeError, match="boom-failed-fallback"):
            transcription._handoff_to_llm_stage(**self._kwargs("task_e"))

        assert len(patch_runtime.items) == 0
        assert bound_runtime.inflight_registry.size("llm") == 0
        assert "task_e" not in bound_runtime.inflight_registry.all_task_ids()

    def test_put_failure_and_failed_fallback_also_raises_skips_notify_and_propagates(
        self, monkeypatch, patch_runtime, bound_runtime
    ):
        """G1 companion for branch (c): put() fails, and the FAILED
        terminal write that follows it ALSO raises. The llm registry slot
        is released unconditionally before this try/except (R3, unaffected
        by this fix), so that part of cleanup is already guaranteed
        regardless of whether the FAILED write below it succeeds; the
        exception must still propagate instead of being swallowed (G1).

        L1 fix (CI review round 5, P1): this test used to also assert the
        best-effort notify still fired from an unconditional `finally` --
        that was itself the bug. The FAILED write never durably landed (it
        raised), so there is no terminal state to report; sending a
        confident "failed" notification anyway contradicts whatever the
        task's real status ends up being. The notify call is no longer
        reachable on this path at all -- it now lives strictly after a
        confirmed `fail_status_written is True`, which this branch never
        reaches."""

        class RaisingQueue:
            def put(self, item):
                raise RuntimeError("boom-put")

        class DoublyRaisingCacheManager(DummyCacheManager):
            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.FAILED:
                    raise RuntimeError("boom-failed-fallback")
                return True

        monkeypatch.setattr(transcription, "llm_task_queue", RaisingQueue())
        cache_manager = DoublyRaisingCacheManager()
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)
        notifier = DummyNotifier()

        kwargs = self._kwargs("task_f")
        kwargs["task_notifier"] = notifier

        with pytest.raises(RuntimeError, match="boom-failed-fallback"):
            transcription._handoff_to_llm_stage(**kwargs)

        assert bound_runtime.inflight_registry.size("llm") == 0
        assert "task_f" not in bound_runtime.inflight_registry.all_task_ids()
        assert notifier.messages == [], (
            "the FAILED CAS write itself raised -- the terminal state was "
            "never durably written, so no failure notification may be "
            "sent (this used to fire unconditionally from a `finally`)"
        )

    def test_put_failure_cas_false_already_success_skips_notify_and_reports_success(
        self, monkeypatch, patch_runtime, bound_runtime
    ):
        """L1 fix (CI review round 5, P1): put() fails, and the FAILED
        fallback write's CAS returns False because the task was already
        concurrently CAS'd to success by some other process. Sending a
        failure notification here would directly contradict the task's
        real terminal state, and hardcoding the response to "failed" would
        lie to the caller too -- both must instead honestly reflect the
        already-recorded success."""

        class RaisingQueue:
            def put(self, item):
                raise RuntimeError("boom-put")

        class CasBlockedCacheManager(DummyCacheManager):
            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.FAILED:
                    return False
                return True

        monkeypatch.setattr(transcription, "llm_task_queue", RaisingQueue())
        cache_manager = CasBlockedCacheManager()
        cache_manager.tasks["task_g"] = {"status": "success"}
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)
        notifier = DummyNotifier()

        kwargs = self._kwargs("task_g")
        kwargs["task_notifier"] = notifier

        result = transcription._handoff_to_llm_stage(**kwargs)

        assert result == {
            "status": transcription.TaskStatus.SUCCESS,
            "message": "任务已被并发流程标记为 success，跳过失败通知（test）",
        }
        assert notifier.messages == [], (
            "must not send a failure notification for a task that is "
            "already terminally success"
        )
        assert bound_runtime.inflight_registry.size("llm") == 0
        assert "task_g" not in bound_runtime.inflight_registry.all_task_ids()

    def test_put_failure_cas_false_already_failed_skips_notify(
        self, monkeypatch, patch_runtime, bound_runtime
    ):
        """M1 fix (PR3 review hardening 收尾轮): CAS-False companion when the
        existing terminal state is failed (not success). update_task_status's
        CAS returns False both when the row is already terminal AND when the
        row doesn't exist at all -- the return value alone cannot tell these
        apart (see cache_manager.update_task_status's `WHERE task_id=? AND
        status NOT IN ('success','failed')`: rowcount is 0 either way).
        Whoever actually won that CAS already sent their own notification;
        sending a second one here would be a duplicate. This test used to
        assert the opposite (still notifies) -- that was exactly the gap M1
        closes; L1 only tightened the success branch."""

        class RaisingQueue:
            def put(self, item):
                raise RuntimeError("boom-put")

        class CasBlockedCacheManager(DummyCacheManager):
            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.FAILED:
                    return False
                return True

        monkeypatch.setattr(transcription, "llm_task_queue", RaisingQueue())
        cache_manager = CasBlockedCacheManager()
        cache_manager.tasks["task_h"] = {"status": "failed"}
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)
        notifier = DummyNotifier()

        kwargs = self._kwargs("task_h")
        kwargs["task_notifier"] = notifier

        result = transcription._handoff_to_llm_stage(**kwargs)

        assert result["status"] == "failed"
        assert notifier.messages == [], (
            "the real CAS winner already sent its own notification -- this "
            "call must not send a duplicate"
        )

    def test_put_failure_cas_false_row_missing_skips_notify(
        self, monkeypatch, patch_runtime, bound_runtime
    ):
        """M1 fix (PR3 review hardening 收尾轮): CAS-False companion when the
        task row does not exist in the store at all (get_task_by_id returns
        None). update_task_status's CAS-miss return value cannot distinguish
        this from "already terminal", so the same no-notify rule applies --
        fabricating a failure notification for a terminal state that was
        never actually recorded would be a lie."""

        class RaisingQueue:
            def put(self, item):
                raise RuntimeError("boom-put")

        class CasBlockedCacheManager(DummyCacheManager):
            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.FAILED:
                    return False
                return True

        monkeypatch.setattr(transcription, "llm_task_queue", RaisingQueue())
        cache_manager = CasBlockedCacheManager()
        # 故意不写入 cache_manager.tasks["task_i"] —— 模拟任务行根本不存在
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)
        notifier = DummyNotifier()

        kwargs = self._kwargs("task_i")
        kwargs["task_notifier"] = notifier

        result = transcription._handoff_to_llm_stage(**kwargs)

        assert result["status"] == "failed"
        assert notifier.messages == [], (
            "task row doesn't exist -- must not fabricate a failure "
            "notification for a terminal state that was never recorded"
        )

    def test_cache_hit_branch_calibrating_exception_no_longer_reports_success(
        self, monkeypatch, patch_runtime, bound_runtime
    ):
        """End-to-end regression test for the original bug report's
        headline example: the cache-hit requeue branch (transcription.py's
        first llm_task_queue.put() call site) used to share one try/except
        across both register_internal+put() *and* the CALIBRATING write --
        a CALIBRATING-write exception was misclassified as a put() failure
        (wrong log message, wrong error_message text), FAILED was written,
        and the function then fell through to an unconditional `return
        {"status": "success", ...}` below the try/except regardless. Assert
        the function is now honest: a CALIBRATING-write exception must
        surface as a failed result, never success."""

        class RaisingCalibratingCacheManager(DummyCacheManager):
            def update_task_status(self, task_id, status, **kwargs):
                if status == transcription.TaskStatus.CALIBRATING:
                    raise RuntimeError("boom-calibrating")
                return super().update_task_status(task_id, status, **kwargs)

        cache_data = {
            "platform": "youtube",
            "media_id": "abc123",
            "title": "cached title",
            "author": "cached author",
            "description": "cached desc",
            "transcript_type": "capswriter",
            "transcript_data": "cached transcript",
            "use_speaker_recognition": False,
            "llm_calibrated": "real calibrated text",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }
        cache_manager = RaisingCalibratingCacheManager(cache_data=cache_data)
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)
        monkeypatch.setattr(
            transcription, "create_downloader",
            lambda url: (_ for _ in ()).throw(AssertionError("should not be called")),
        )

        result = transcription.process_transcription(
            task_id="task_cache_hit_calibrating_exc",
            url="https://www.youtube.com/watch?v=abc123",
            use_speaker_recognition=False,
            wechat_webhook=None,
            download_url=None,
            metadata_override=None,
        )

        assert result["status"] == "failed"
        assert len(patch_runtime.items) == 0
        assert bound_runtime.inflight_registry.size("llm") == 0
        failed_writes = [
            u for u in cache_manager.status_updates
            if u[1] == transcription.TaskStatus.FAILED
        ]
        assert len(failed_writes) == 1


# ---------------------------------------------------------------------------
# L1 (CI review round 5, P1): _fail_task_and_notify (the closure shared by
# ~10 download/subtitle failure branches inside process_transcription) and
# process_transcription's own outermost `except Exception as exc:` (site C)
# had the same bug as _handoff_to_llm_stage's put()-failure branch above --
# both sent their failure notification from an unconditional `finally`,
# regardless of whether the FAILED CAS write that was supposed to precede it
# actually landed. Driven end-to-end through process_transcription (these are
# closures, not standalone functions) via the simplest available lever: a
# downloader whose download_file() returns falsy, which reaches
# _fail_task_and_notify's plainest call site ("下载文件失败", default
# notify_status="下载失败"). A RecordingRouter observes both
# _fail_task_and_notify's notification (via the per-task _TaskNotifier) and
# site C's own direct call, since get_notification_router() is fetched once
# near the top of process_transcription and reused everywhere -- exactly the
# real single-router topology this bug lived in.
# ---------------------------------------------------------------------------


class RecordingRouter:
    """Records every notify_task_status call's `status` kwarg. Stands in for
    get_notification_router()'s return value so both _fail_task_and_notify
    (via the per-task _TaskNotifier wrapper) and process_transcription's
    outer except block can be observed through one shared instance."""

    def __init__(self):
        self.statuses = []

    def notify_task_status(self, *args, **kwargs):
        self.statuses.append(kwargs.get("status"))

    def send_text(self, *args, **kwargs):
        pass


class FailingDownloadYoutubeDownloader(YoutubeDownloader):
    """download_file() returns falsy -- reaches _fail_task_and_notify's
    simplest call site ("下载文件失败") without needing to fake a subtitle
    or save_cache failure first."""

    def download_file(self, url, filename):
        return None


class TestFailTaskAndNotifyCasConsistency:
    def _run(self, monkeypatch, patch_runtime, cache_manager, router):
        downloader = FailingDownloadYoutubeDownloader(
            subtitle=None, download_url="http://example.com/audio.mp3",
            filename="audio.mp3",
        )
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)
        monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)
        monkeypatch.setattr(transcription, "get_notification_router", lambda: router)

        return transcription.process_transcription(
            task_id="task_dl_fail",
            url="https://www.youtube.com/watch?v=abc123",
            use_speaker_recognition=False,
            wechat_webhook=None,
            download_url=None,
            metadata_override=None,
        )

    def test_cas_write_raises_skips_notify_and_propagates_to_outer_handler(
        self, monkeypatch, patch_runtime,
    ):
        """CAS write raises: _fail_task_and_notify must not send its own
        "下载失败" notification and must re-raise. The exception is not
        caught anywhere local to this call site, so it propagates all the
        way to process_transcription's outermost except block (site C),
        which retries the same FAILED write for the same task_id -- this
        second attempt succeeds (simulating a transient DB blip that
        recovered), so site C converges normally: writes FAILED, sends its
        own "转录异常" notification, and returns a failed response instead
        of the exception continuing to propagate further."""

        class RaiseOnceThenSucceedCacheManager(DummyCacheManager):
            def __init__(self):
                super().__init__()
                self._failed_write_count = 0

            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.FAILED:
                    self._failed_write_count += 1
                    if self._failed_write_count == 1:
                        raise RuntimeError("boom-failed-write-1")
                return True

        cache_manager = RaiseOnceThenSucceedCacheManager()
        router = RecordingRouter()

        result = self._run(monkeypatch, patch_runtime, cache_manager, router)

        assert result["status"] == "failed"
        assert "下载失败" not in router.statuses, (
            "_fail_task_and_notify's own CAS write raised -- it must not "
            "send its own failure notification before re-raising"
        )
        assert router.statuses.count("转录异常") == 1, (
            "the exception must propagate to process_transcription's outer "
            "handler, which retries the FAILED write and notifies once "
            "on its own successful (second) attempt"
        )

    def test_cas_false_already_success_skips_notify_and_returns_success(
        self, monkeypatch, patch_runtime,
    ):
        """CAS returns False and the task is already terminally success:
        no "下载失败" notification, and process_transcription's overall
        result is success -- not hardcoded failed -- because every
        `_fail_task_and_notify(...); return {"status": "failed", ...}`
        call site was collapsed to `return _fail_task_and_notify(...)`,
        which now returns a dict reflecting the real terminal state."""

        class CasBlockedCacheManager(DummyCacheManager):
            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.FAILED:
                    return False
                return True

        cache_manager = CasBlockedCacheManager()
        cache_manager.tasks["task_dl_fail"] = {"status": "success"}
        router = RecordingRouter()

        result = self._run(monkeypatch, patch_runtime, cache_manager, router)

        assert result["status"] == transcription.TaskStatus.SUCCESS
        assert "下载失败" not in router.statuses
        assert "转录异常" not in router.statuses

    def test_cas_false_already_failed_skips_notify(
        self, monkeypatch, patch_runtime,
    ):
        """M1 fix (PR3 review hardening 收尾轮): CAS returns False and the
        task is already terminally failed -- some other CAS winner got there
        first and already sent its own notification, so this call must not
        send a duplicate "下载失败" notification."""

        class CasBlockedCacheManager(DummyCacheManager):
            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.FAILED:
                    return False
                return True

        cache_manager = CasBlockedCacheManager()
        cache_manager.tasks["task_dl_fail"] = {"status": "failed"}
        router = RecordingRouter()

        result = self._run(monkeypatch, patch_runtime, cache_manager, router)

        assert result["status"] == "failed"
        assert "下载失败" not in router.statuses
        assert "转录异常" not in router.statuses

    def test_cas_false_row_missing_skips_notify(
        self, monkeypatch, patch_runtime,
    ):
        """M1 fix (PR3 review hardening 收尾轮): CAS returns False and the
        task row doesn't exist at all -- update_task_status's CAS-miss
        return value can't distinguish this from "already terminal" (see the
        function's `WHERE task_id=? AND status NOT IN (...)` clause), so the
        same no-notify rule applies rather than fabricating a notification
        for a terminal state that was never recorded."""

        class CasBlockedCacheManager(DummyCacheManager):
            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.FAILED:
                    return False
                return True

        cache_manager = CasBlockedCacheManager()
        # 故意不写入 cache_manager.tasks["task_dl_fail"] —— 模拟任务行根本不存在
        router = RecordingRouter()

        result = self._run(monkeypatch, patch_runtime, cache_manager, router)

        assert result["status"] == "failed"
        assert "下载失败" not in router.statuses
        assert "转录异常" not in router.statuses

    def test_cas_write_succeeds_notifies_download_failure(
        self, monkeypatch, patch_runtime,
    ):
        """Baseline: CAS write succeeds (True) -- unchanged behavior, a
        "下载失败" notification is sent exactly once and the result is
        failed."""
        cache_manager = DummyCacheManager()
        router = RecordingRouter()

        result = self._run(monkeypatch, patch_runtime, cache_manager, router)

        assert result["status"] == "failed"
        assert router.statuses.count("下载失败") == 1


# ---------------------------------------------------------------------------
# L1 (CI review round 5, P1): process_transcription's own outermost
# `except Exception as exc:` (site C) has the same CAS-then-notify ordering
# bug as the two closures covered above. Reuses
# test_flow_final_exception_persists_failed_before_notifying's lever
# (cache_manager.save_cache raises during the download+transcribe branch's
# post-transcription save step, a genuinely try/except-unguarded call site)
# so the FAILED write under test is site C's own, not one already consumed
# by _fail_task_and_notify.
# ---------------------------------------------------------------------------


class TestOuterExceptHandlerCasConsistency:
    def _run(self, monkeypatch, patch_runtime, cache_manager, router):
        downloader = YoutubeDownloader(
            subtitle=None, download_url="http://example.com/audio.mp3", filename="audio.mp3",
        )
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)
        monkeypatch.setattr(transcription, "create_downloader", lambda url: downloader)
        monkeypatch.setattr(transcription, "get_notification_router", lambda: router)

        return transcription.process_transcription(
            task_id="task_outer_exc",
            url="https://www.youtube.com/watch?v=abc123",
            use_speaker_recognition=False,
            wechat_webhook=None,
            download_url=None,
            metadata_override=None,
        )

    def test_cas_write_raises_skips_notify_and_propagates_out(
        self, monkeypatch, patch_runtime,
    ):
        """save_cache raises -> reaches site C. Site C's own FAILED write
        also raises (e.g. the same outage that broke save_cache): no
        "转录异常" notification may be sent, and the exception must
        propagate all the way out of process_transcription uncaught (site C
        is the last line of defense; nothing above it exists to retry)."""

        class RaisingSaveCacheManager(DummyCacheManager):
            def save_cache(self, **kwargs):
                raise RuntimeError("boom-save-cache")

            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.FAILED:
                    raise RuntimeError("boom-outer-failed-write")
                return True

        cache_manager = RaisingSaveCacheManager()
        router = RecordingRouter()

        with pytest.raises(RuntimeError, match="boom-outer-failed-write"):
            self._run(monkeypatch, patch_runtime, cache_manager, router)

        assert "转录异常" not in router.statuses, (
            "site C's own FAILED CAS write raised -- it must not send a "
            "confident failure notification before re-raising"
        )

    def test_cas_false_already_success_skips_notify_and_returns_success(
        self, monkeypatch, patch_runtime,
    ):
        """save_cache raises -> reaches site C. Site C's FAILED write CAS
        returns False because the task is already terminally success (e.g.
        a concurrent duplicate request finished first): no "转录异常"
        notification, and the response must honestly report success
        instead of the previous hardcoded failed."""

        class CasBlockedSaveCacheManager(DummyCacheManager):
            def save_cache(self, **kwargs):
                raise RuntimeError("boom-save-cache")

            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.FAILED:
                    return False
                return True

        cache_manager = CasBlockedSaveCacheManager()
        cache_manager.tasks["task_outer_exc"] = {"status": "success"}
        router = RecordingRouter()

        result = self._run(monkeypatch, patch_runtime, cache_manager, router)

        assert result["status"] == transcription.TaskStatus.SUCCESS
        assert "转录异常" not in router.statuses

    def test_cas_false_already_failed_skips_notify(
        self, monkeypatch, patch_runtime,
    ):
        """M1 fix (PR3 review hardening 收尾轮): save_cache raises -> reaches
        site C. Site C's FAILED write CAS returns False because the task is
        already terminally failed -- another CAS winner got there first and
        already sent its own "转录异常" notification, so site C must not
        send a duplicate."""

        class CasBlockedSaveCacheManager(DummyCacheManager):
            def save_cache(self, **kwargs):
                raise RuntimeError("boom-save-cache")

            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.FAILED:
                    return False
                return True

        cache_manager = CasBlockedSaveCacheManager()
        cache_manager.tasks["task_outer_exc"] = {"status": "failed"}
        router = RecordingRouter()

        result = self._run(monkeypatch, patch_runtime, cache_manager, router)

        assert result["status"] == "failed"
        assert "转录异常" not in router.statuses

    def test_cas_false_row_missing_skips_notify(
        self, monkeypatch, patch_runtime,
    ):
        """M1 fix (PR3 review hardening 收尾轮): save_cache raises -> reaches
        site C. Site C's FAILED write CAS returns False and the task row
        doesn't exist at all -- indistinguishable from "already terminal"
        via the CAS return value alone, so the same no-notify rule applies
        instead of fabricating a notification for a state that was never
        recorded."""

        class CasBlockedSaveCacheManager(DummyCacheManager):
            def save_cache(self, **kwargs):
                raise RuntimeError("boom-save-cache")

            def update_task_status(self, task_id, status, **kwargs):
                self.status_updates.append((task_id, status, kwargs))
                if status == transcription.TaskStatus.FAILED:
                    return False
                return True

        cache_manager = CasBlockedSaveCacheManager()
        # 故意不写入 cache_manager.tasks["task_outer_exc"] —— 模拟任务行根本不存在
        router = RecordingRouter()

        result = self._run(monkeypatch, patch_runtime, cache_manager, router)

        assert result["status"] == "failed"
        assert "转录异常" not in router.statuses

    def test_cas_write_succeeds_notifies_and_returns_failed(
        self, monkeypatch, patch_runtime,
    ):
        """Baseline: CAS write succeeds (True) -- unchanged behavior, one
        "转录异常" notification is sent and the result is failed."""

        class RaisingSaveCacheManager(DummyCacheManager):
            def save_cache(self, **kwargs):
                raise RuntimeError("boom-save-cache")

        cache_manager = RaisingSaveCacheManager()
        router = RecordingRouter()

        result = self._run(monkeypatch, patch_runtime, cache_manager, router)

        assert result["status"] == "failed"
        assert router.statuses.count("转录异常") == 1
