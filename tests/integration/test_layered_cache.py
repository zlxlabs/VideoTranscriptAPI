"""Integration tests for the layered cache hit/miss decision in
process_transcription() (transcription.py), covering the cache-hit matrix
described in the per-task processing-depth feature:

  1. full flow first, then transcript-only request  -> full hit, no re-queue
  2. transcript-only first, then full flow request   -> re-queue BOTH layers
     (calibrated+summary), transcript itself is never re-downloaded/re-run
  3. calibrate+no-summary first, then full flow       -> re-queue summary ONLY,
     and the queued task reuses the EXISTING calibrated text as input rather
     than the raw transcript (so llm_calibrated.txt is never touched again --
     the actual no-overwrite guarantee is unit-tested at the
     llm_ops._save_llm_results layer; here we assert the transcription.py
     decision that feeds it)
  4. resubmitting identical options twice             -> idempotent, no re-queue

Mirrors the DummyCacheManager/DummyQueue pattern already used in
tests/features/test_transcription_flow_regression.py.

All console output must be in English only (no emoji, no Chinese).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import video_transcript_api.api.services.transcription as transcription
from video_transcript_api.api.services import llm_ops
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.task_status import TaskStatus
from video_transcript_api.utils.llm_status import (
    CalibrationStatus,
    ChaptersStatus,
    SummaryStatus,
)


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
    """Minimal cache_manager stand-in exposing exactly what
    process_transcription's cache-hit branch touches."""

    def __init__(self, cache_data=None):
        self.cache_data = cache_data
        self.saved = []
        self.status_updates = []
        self.tasks = {}

    def get_cache(self, platform, media_id, use_speaker_recognition):
        return self.cache_data

    def get_speaker_mapping(self, *args, **kwargs):
        return self.cache_data.get("speaker_mapping") if self.cache_data else None

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


BASE_CACHE_DATA = {
    "platform": "youtube",
    "media_id": "abc123",
    "title": "cached title",
    "author": "cached author",
    "description": "cached desc",
    "transcript_type": "capswriter",
    "transcript_data": "RAW uncalibrated transcript",
    "use_speaker_recognition": False,
}


@pytest.fixture
def patch_runtime(monkeypatch):
    queue = DummyQueue()
    monkeypatch.setattr(transcription, "llm_task_queue", queue)
    monkeypatch.setattr(transcription, "WechatNotifier", DummyNotifier)
    monkeypatch.setattr(transcription, "send_long_text_wechat", lambda *a, **k: None)
    monkeypatch.setattr(transcription, "get_base_url", lambda: "http://test")

    def fail_create_downloader(url):
        raise AssertionError("create_downloader should not be called on cache hit")

    monkeypatch.setattr(transcription, "create_downloader", fail_create_downloader)
    return queue


def _run(monkeypatch, patch_runtime, cache_data, processing_options, task_id="t"):
    cache_manager = DummyCacheManager(cache_data=cache_data)
    monkeypatch.setattr(transcription, "cache_manager", cache_manager)

    result = transcription.process_transcription(
        task_id=task_id,
        url="https://www.youtube.com/watch?v=abc123",
        use_speaker_recognition=bool(
            cache_data and cache_data.get("use_speaker_recognition")
        ),
        wechat_webhook=None,
        download_url=None,
        metadata_override=None,
        processing_options=processing_options,
    )
    return result, patch_runtime.items


class TestLayeredCacheMatrix:
    def test_full_flow_then_transcript_only_is_full_hit(self, monkeypatch, patch_runtime):
        """(1) Cache already has both layers (a prior full-flow run). A
        transcript-only request (calibrate=False, summarize=False) must be a
        full hit -- extra layers are returned as-is, nothing re-queued."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "real calibrated text",
            "llm_summary": "real summary",
            "llm_status": {
                "calibration_status": CalibrationStatus.FULL,
                "chapters_status": ChaptersStatus.SKIPPED_NO_TIMELINE,
            },
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": False, "summarize": False},
        )

        assert result["status"] == "success"
        assert result["data"]["cached"] is True
        assert queued == []

    def test_missing_mapping_with_full_calibration_and_missing_structured_is_not_queued(
        self, monkeypatch, patch_runtime
    ):
        """单项修复（PR3 review hardening）：这个测试原名
        test_missing_speaker_artifact_requeues_only_name_inference，此前锁死的
        正是本轮要修的 bug——mapping 缺失（本媒体从未推断过说话人姓名，或
        fingerprint 未命中）+ 校对已确认 FULL + 结构化产物缺失（典型 legacy
        缓存形态：早于说话人展示层上线的旧数据）。

        旧代码在 need_speaker_names 判定里对"mapping 缺失"这条腿完全不看
        结构化产物是否存在，无条件排队；而 calibration_confirmed_full=True
        时 G2 又不会强制 calibrate=True，排队因此只会落进 calibrate=False
        的"仅推断"分支——SpeakerInferencer 真烧一次 LLM token 推断出新
        mapping、存进 speaker_mapping.json，但没有结构化产物可刷新
        （llm_ops._refresh_speaker_names_in_existing_structured_artifact 的
        "无旧产物可刷新"分支直接原样跳过），用户在 /view 页面永远看不到这次
        调用的结果；下一次请求 mapping 又命中缓存，不再重新排队——一次白烧
        token、且成功语义与可见结果完全不符的无效付费调用。

        修复后：mapping 缺失 + 结构化缺失时，只有校对未确认 FULL（会被 G2
        强制改成一次真实完整校对，结构化产物随之产出，映射有地方落地）才
        排队；FULL 时不排队，legacy 缺口交给用户显式 recalibrate 触发全流程
        处理（红：旧代码在这里断言 len(queued) == 1）。"""
        dialogs = {
            "segments": [
                {"speaker": "S1", "text": "hello"},
                {"speaker": "S2", "text": "world"},
            ]
        }
        cache_data = {
            **BASE_CACHE_DATA,
            "transcript_type": "funasr",
            "transcript_data": dialogs,
            "use_speaker_recognition": True,
            "llm_calibrated": "done",
            "llm_summary": "done",
            "llm_status": {
                "calibration_status": CalibrationStatus.FULL,
                "chapters_status": ChaptersStatus.SKIPPED_NO_TIMELINE,
            },
            # 故意不带 "speaker_mapping"（mapping 缺失）和 "llm_processed"
            # （结构化产物缺失）。
        }

        result, queued = _run(monkeypatch, patch_runtime, cache_data, None)

        assert result["status"] == "success"
        assert queued == [], (
            "mapping 缺失但没有结构化产物可承接推断结果，且校对已确认 FULL "
            "时不会被 G2 收编成完整流程——排队只会白烧一次 LLM token，结果永"
            "远不可见，必须直接判定为完整命中，不排队"
        )

    def test_malformed_structured_artifact_with_missing_mapping_is_treated_as_not_refreshable(
        self, monkeypatch, patch_runtime
    ):
        """J2 修复（本地增量复核第 3 轮）矩阵测试的排队侧断言：mapping 缺失
        （本轮需要真实 LLM 推断新映射）+ 结构化产物"存在但不可刷新"（这里用
        混合 schema——一条 dialog 带 speaker_id，另一条不带）时，此前排队侧
        只用 isinstance(x, dict) 判断"可刷新"，会把这份产物误判为可刷新
        （1a 分支）排队，真烧一次 LLM 推断；但 llm_ops._refresh_speaker_
        names_in_existing_structured_artifact 真正尝试写入前的 schema 校验
        （见 tests/unit/test_recalibrate.py::
        test_mixed_missing_speaker_id_skips_refresh_without_partial_commit）
        会认定这份产物不满足刷新前置条件、直接跳过——结果永远不可见，白烧
        token（红：旧代码在这里断言 len(queued) == 1）。现在排队侧改用
        llm_ops.structured_artifact_is_refreshable 的 schema 层判定，与
        helper 侧共用同一份实现，一开始就不会排队，和结构化产物彻底缺失
        （见上面 test_missing_mapping_with_full_calibration_and_missing_
        structured_is_not_queued）同等对待。"""
        dialogs = {
            "segments": [
                {"speaker": "S1", "text": "hello"},
                {"speaker": "S2", "text": "world"},
            ]
        }
        cache_data = {
            **BASE_CACHE_DATA,
            "transcript_type": "funasr",
            "transcript_data": dialogs,
            "use_speaker_recognition": True,
            "llm_calibrated": "done",
            "llm_summary": "done",
            "llm_status": {
                "calibration_status": CalibrationStatus.FULL,
                "chapters_status": ChaptersStatus.SKIPPED_NO_TIMELINE,
            },
            "llm_processed": {
                # 混合 schema：一条带 speaker_id，一条不带——不满足
                # structured_artifact_is_refreshable 的 schema 层
                # （dialogs 非空且全部相关 dialog 都带非空 speaker_id）。
                "dialogs": [
                    {"speaker_id": "S1", "speaker": "说话人1", "text": "hello"},
                    {"speaker": "说话人2", "text": "world"},
                ],
                "speaker_mapping": {"S1": "说话人1", "S2": "说话人2"},
            },
            # 故意不带 "speaker_mapping"：DummyCacheManager.get_speaker_mapping
            # 返回 None，模拟 fingerprint 未命中/mapping 缺失。
        }

        result, queued = _run(monkeypatch, patch_runtime, cache_data, None)

        assert result["status"] == "success"
        assert queued == [], (
            "结构化产物存在但不满足刷新前置条件（混合 schema）时，必须与"
            "产物彻底缺失同等对待——不排队，避免白烧一次不会被 helper 消费"
            "的 LLM 推断"
        )

    def test_missing_mapping_with_existing_structured_artifact_still_requeues_name_inference(
        self, monkeypatch, patch_runtime
    ):
        """上一个测试收窄的边界证明：mapping 缺失时"结构化产物是否存在"才是
        决定是否排队的关键，不是校对状态。这里结构化产物存在（有地方承接
        新推断出的姓名），即便校对已确认 FULL，也必须继续排队一次零成本的
        name-only 补层——这是 need_speaker_names 判定条件表里 1a 分支的正
        向覆盖，避免上面的收窄误伤这条本该继续工作的路径。"""
        dialogs = {
            "segments": [
                {"speaker": "S1", "text": "hello"},
                {"speaker": "S2", "text": "world"},
            ]
        }
        cache_data = {
            **BASE_CACHE_DATA,
            "transcript_type": "funasr",
            "transcript_data": dialogs,
            "use_speaker_recognition": True,
            "llm_calibrated": "done",
            "llm_summary": "done",
            "llm_status": {
                "calibration_status": CalibrationStatus.FULL,
                "chapters_status": ChaptersStatus.SKIPPED_NO_TIMELINE,
            },
            "llm_processed": {
                "dialogs": [
                    {"speaker_id": "S1", "speaker": "说话人1", "text": "hello"},
                    {"speaker_id": "S2", "speaker": "说话人2", "text": "world"},
                ],
                "speaker_mapping": {"S1": "说话人1", "S2": "说话人2"},
            },
            # 故意不带 "speaker_mapping"：DummyCacheManager.get_speaker_mapping
            # 返回 None，模拟 fingerprint 未命中/mapping 缺失。
        }

        result, queued = _run(monkeypatch, patch_runtime, cache_data, None)

        assert result["status"] == "success"
        assert len(queued) == 1
        assert queued[0]["transcription_data"] == dialogs
        assert queued[0]["processing_options"] == {
            "calibrate": False,
            "summarize": False,
            "infer_speaker_names": True,
            "chapters": False,
        }

    def test_explicitly_disabled_speaker_inference_does_not_require_artifact(
        self, monkeypatch, patch_runtime
    ):
        cache_data = {
            **BASE_CACHE_DATA,
            "transcript_type": "funasr",
            "transcript_data": {"segments": [{"speaker": "S1", "text": "hello"}]},
            "use_speaker_recognition": True,
            "llm_calibrated": "done",
            "llm_summary": "done",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }
        _, queued = _run(
            monkeypatch,
            patch_runtime,
            cache_data,
            {"calibrate": False, "summarize": False, "infer_speaker_names": False},
        )
        assert queued == []

    def test_transcript_only_then_full_flow_requeues_both_layers(
        self, monkeypatch, patch_runtime
    ):
        """(2) Cache only has a disabled-calibration placeholder (a prior
        transcript-only run) and no summary. A full-flow request must
        re-queue BOTH calibrate and summarize, and the transcript itself
        (raw, not the disabled placeholder) must be reused, not re-downloaded."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "disabled placeholder text",
            "llm_status": {"calibration_status": CalibrationStatus.DISABLED},
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True},
        )

        assert result["status"] == "success"
        assert len(queued) == 1
        task = queued[0]
        assert task["processing_options"] == {
            "calibrate": True,
            "summarize": True,
            "infer_speaker_names": False,
            "chapters": True,
        }
        # Real calibration is needed -> feed the raw transcript, not the
        # disabled placeholder, and no download/transcription was re-run
        # (no save_cache call for a fresh transcript).
        assert task["transcript"] == "RAW uncalibrated transcript"

    def test_calibration_none_then_full_flow_requeues_calibration_again(
        self, monkeypatch, patch_runtime
    ):
        """codex-review R4 #2: cache has a fallback-formatted-original
        llm_calibrated.txt from a PRIOR calibration attempt that fully
        degraded (calibration_status=none -- llm_ops._save_llm_results now
        persists that fallback artifact instead of dropping it). A
        subsequent full-flow request must still treat calibration as
        MISSING and re-queue a real attempt -- "an artifact exists" must not
        be conflated with "already satisfied", exactly like the existing
        disabled-placeholder case above, otherwise one failed attempt would
        permanently lock the media into the failed fallback text."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "fallback formatted original text (calibration fully failed)",
            "llm_status": {"calibration_status": CalibrationStatus.NONE},
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True},
        )

        assert result["status"] == "success"
        assert len(queued) == 1
        task = queued[0]
        assert task["processing_options"] == {
            "calibrate": True,
            "summarize": True,
            "infer_speaker_names": False,
            "chapters": True,
        }
        assert task["transcript"] == "RAW uncalibrated transcript"

    def test_calibrate_only_then_full_flow_requeues_summary_only(
        self, monkeypatch, patch_runtime
    ):
        """(3) Cache has a REAL calibrated layer (prior calibrate=True,
        summarize=False run) and no summary. A full-flow request must only
        request summarize, and must feed the EXISTING calibrated text as the
        summary input (not the raw transcript) -- this is what lets
        llm_ops._save_llm_results leave llm_calibrated.txt untouched."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "REAL calibrated text from a genuine LLM pass",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True},
        )

        assert result["status"] == "success"
        assert len(queued) == 1
        task = queued[0]
        assert task["processing_options"] == {
            "calibrate": False,
            "summarize": True,
            "infer_speaker_names": False,
            "chapters": True,
        }
        assert task["transcript"] == "REAL calibrated text from a genuine LLM pass"
        # Force plain-text routing downstream (no re-diarization LLM call).
        assert task["transcription_data"] is None

    def test_calibrate_only_speaker_cache_propagates_cached_speaker_count(
        self, monkeypatch, patch_runtime
    ):
        """codex-review R5 #3: same "只补总结" decision as the test above,
        but for a speaker-recognition cache. transcription_data is still
        forced to None (no re-diarization), but the real speaker count from
        the cached llm_processed.json structured data must be read and
        threaded onto the queued task as cached_speaker_count, so llm_ops/
        coordinator can override the (otherwise wrong, plain-text-implied)
        single-speaker auto-inference for the summary step."""
        cache_data = {
            **BASE_CACHE_DATA,
            "transcript_type": "funasr",
            "transcript_data": {
                "segments": [
                    {"speaker": "S0", "text": "hello", "start_time": 0, "end_time": 1}
                ]
            },
            "use_speaker_recognition": True,
            "llm_calibrated": "REAL calibrated text from a genuine speaker-aware pass",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
            "llm_processed": {
                "dialogs": [{"speaker": "Alice", "text": "hello"}],
                "speaker_mapping": {"S0": "Alice", "S1": "Bob", "S2": "Carol"},
            },
            # DummyCacheManager treats this as an already validated v1
            # artifact; production validates schema/fingerprint/speaker set.
            "speaker_mapping": {
                "mapping": {"S0": "Alice"},
                "meta": {"S0": {"name": "Alice", "confidence": 0.9}},
                "low_confidence": [],
            },
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True},
        )

        assert result["status"] == "success"
        assert len(queued) == 1
        task = queued[0]
        assert task["processing_options"] == {
            "calibrate": False,
            "summarize": True,
            "infer_speaker_names": False,
            "chapters": True,
        }
        assert task["transcription_data"] is None
        assert task["use_speaker_recognition"] is True
        assert task["cached_speaker_count"] == 3

    def test_stale_structured_artifact_forces_speaker_name_rebuild(
        self, monkeypatch, patch_runtime
    ):
        """codex-review 本地第 16 轮 Q3: llm_processed.json 是渲染层
        （dialog_renderer）直接消费的展示产物，也是 _save_llm_results 里除
        状态文件外最晚写入的一个。即便校对/总结/说话人映射本身都已确认
        完整（llm_status.json=FULL、speaker_mapping.json 指纹命中），
        llm_processed.json 仍可能因半提交或历史姓名刷新失败而独立分叉
        （新 schema，dialog 带 speaker_id 但姓名过期）——完整命中判定必须
        额外核验这份展示产物，不能对它视而不见，直接短路成功。"""
        dialogs = {
            "segments": [
                {"speaker": "S0", "text": "hello", "start_time": 0, "end_time": 1},
            ]
        }
        cache_data = {
            **BASE_CACHE_DATA,
            "transcript_type": "funasr",
            "transcript_data": dialogs,
            "use_speaker_recognition": True,
            "llm_calibrated": "REAL calibrated text",
            "llm_summary": "REAL summary",
            "llm_status": {
                "calibration_status": CalibrationStatus.FULL,
                "chapters_status": ChaptersStatus.SKIPPED_NO_TIMELINE,
            },
            "llm_processed": {
                # 新 schema：带 speaker_id，但姓名是过期的 "Old Name"——与
                # 下面 speaker_mapping 里的权威映射 "New Name" 不一致。
                "dialogs": [{"speaker_id": "S0", "speaker": "Old Name", "text": "hello"}],
                "speaker_mapping": {"S0": "Old Name"},
            },
            # DummyCacheManager.get_speaker_mapping 直接返回这份 dict，
            # 代表指纹已命中的权威映射（生产环境由 cache_manager 校验
            # schema/fingerprint/speaker 集合）。
            "speaker_mapping": {
                "mapping": {"S0": "New Name"},
                "meta": {"S0": {"name": "New Name", "confidence": 0.9}},
                "low_confidence": [],
            },
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True},
        )

        assert result["status"] == "success"
        # 不得短路为"无需处理"——必须重新排队一次零成本的展示刷新。
        assert len(queued) == 1
        task = queued[0]
        assert task["processing_options"] == {
            "calibrate": False,
            "summarize": False,
            "infer_speaker_names": True,
            "chapters": False,
        }

    def test_missing_structured_artifact_with_completed_calibration_is_full_hit(
        self, monkeypatch, patch_runtime
    ):
        """X1（PR3 review hardening 三轮）：此前（见本测试原先的名字/断言）
        "结构化产物缺失"无条件触发 need_speaker_names 补层重排队——但补层
        请求的 processing_options.calibrate 恒为 False（见下面 queued 断言），
        下游 SpeakerAwareProcessor.process(skip_calibration=True) 会直接把
        未经校对的原始 ASR dialogs 当作 calibrated_dialogs，再经
        llm_ops._refresh_speaker_names_in_existing_structured_artifact 的
        "无旧产物、首次落盘"分支（V5 修复）落盘——覆盖/伪装成权威产物，与
        这里已经真实存在、经过校对的 llm_calibrated.txt（llm_status 声明
        FULL）自相矛盾，公开页（DialogRenderer 无条件优先读结构化产物）从
        校对文本退化为生肉。

        修法：llm_status 已声明校对真正完整完成时，"结构化产物缺失"不再
        单独触发 need_speaker_names——渲染层对无结构化产物本就有平文本
        回退（继续显示 llm_calibrated.txt 的校对内容），不需要靠这条零
        成本"重建"路径冒险覆盖已有的真实产物。校对层本身还没有真正完成时，
        Q3 原有的重排队保护原样保留，见下面
        test_missing_structured_artifact_with_incomplete_calibration_still_
        forces_rebuild。"""
        dialogs = {
            "segments": [
                {"speaker": "S0", "text": "hello", "start_time": 0, "end_time": 1},
            ]
        }
        cache_data = {
            **BASE_CACHE_DATA,
            "transcript_type": "funasr",
            "transcript_data": dialogs,
            "use_speaker_recognition": True,
            "llm_calibrated": "REAL calibrated text",
            "llm_summary": "REAL summary",
            "llm_status": {
                "calibration_status": CalibrationStatus.FULL,
                "chapters_status": ChaptersStatus.SKIPPED_NO_TIMELINE,
            },
            # 故意不带 "llm_processed"。
            "speaker_mapping": {
                "mapping": {"S0": "New Name"},
                "meta": {"S0": {"name": "New Name", "confidence": 0.9}},
                "low_confidence": [],
            },
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True},
        )

        assert result["status"] == "success"
        # X1 修复：不再排队补层任务，直接判定为完整命中——公开页继续通过
        # 渲染层的平文本回退读取已有的真实校对文本，不会让补层任务用未经
        # 校对的原始 ASR dialogs 冒充首次落盘的结构化产物，覆盖这份真实
        # 产物。
        assert queued == []

    @pytest.mark.parametrize(
        "calibration_status",
        [CalibrationStatus.NONE, None],
        ids=["status-none", "status-missing"],
    )
    def test_missing_structured_artifact_with_known_mapping_and_incomplete_calibration_does_not_escalate_calibrate(
        self, monkeypatch, patch_runtime, calibration_status
    ):
        """J1 修复（本地增量复核第 3 轮）：此前 G2 会在 need_speaker_names=True
        且校对未确认 FULL 时，把 queued_calibrate 强制升级为 True——即便
        用户本次请求显式传了 calibrate=false，也会被系统偷偷改写成一次
        真实付费校对，违反 ProcessingOptions 每个开关必须相互独立、用户
        显式传值必须被尊重的设计合同（红：旧代码在这两种校对状态下都会
        把这里断言为 calibrate=True——本测试原名 test_missing_structured_
        artifact_with_incomplete_calibration_still_forces_rebuild，只锁死
        了 NONE 单一场景下的升级行为，现在改写成参数化用例，同时证明升级
        已被移除）。

        这个测试驱动的是"mapping 已知"这条腿（case 2c：speaker_mapping
        指纹命中、结构化产物缺失、calibrated_layer_satisfied 为 False，
        即 DISABLED/NONE/状态缺失——PARTIAL 不在这条腿里：PARTIAL 与 FULL
        同等被 calibrated_layer_satisfied 视为"层已满足"，落进不排队的
        2d 分支，与本测试要验证的"排队但不该被升级"场景无关，故不参数化
        进来）——排队本身仍然发生（codex-review 本地第 16 轮 Q3 原有的
        重排队保护，未变，走 need_speaker_names 条件表 2c 分支零成本自愈
        缺口，不受 J1 影响），但 queued_calibrate 现在纯由 need_calibrated
        决定：用户显式 calibrate=false 时 need_calibrated 天然为 False
        （与 calibrated_layer_satisfied/校对状态无关），queued_calibrate
        因此保持 False，不再被 need_speaker_names 或校对状态牵连升级为
        True。"""
        dialogs = {
            "segments": [
                {"speaker": "S0", "text": "hello", "start_time": 0, "end_time": 1},
            ]
        }
        cache_data = {
            **BASE_CACHE_DATA,
            "transcript_type": "funasr",
            "transcript_data": dialogs,
            "use_speaker_recognition": True,
            "llm_calibrated": "local formatted placeholder, not real calibration",
            "llm_summary": "REAL summary",
            # 故意不带 "llm_processed"。
            "speaker_mapping": {
                "mapping": {"S0": "New Name"},
                "meta": {"S0": {"name": "New Name", "confidence": 0.9}},
                "low_confidence": [],
            },
        }
        cache_data["llm_status"] = {
            "chapters_status": ChaptersStatus.SKIPPED_NO_TIMELINE,
        }
        if calibration_status is not None:
            cache_data["llm_status"]["calibration_status"] = calibration_status
        # calibration_status is None -> 只保留已经满足的章节层，模拟校对
        # 状态缺失的旧缓存。

        # calibrate=False（用户本轮未请求重新校对）——单独把 need_speaker_names
        # 的判定隔离出来验证，不被 need_calibrated 一起触发的重排队掩盖。
        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": False, "summarize": True},
        )

        assert result["status"] == "success"
        assert len(queued) == 1
        assert queued[0]["processing_options"] == {
            "calibrate": False,
            "summarize": False,
            "infer_speaker_names": True,
            "chapters": False,
        }, f"calibration_status={calibration_status!r} 时 calibrate 被隐式升级"

    @pytest.mark.parametrize(
        "calibration_status",
        [CalibrationStatus.NONE, CalibrationStatus.PARTIAL, None],
        ids=["status-none", "status-partial", "status-missing"],
    )
    def test_missing_mapping_and_missing_structured_artifact_is_not_queued_regardless_of_calibration_status(
        self, monkeypatch, patch_runtime, calibration_status
    ):
        """J1 修复（本地增量复核第 3 轮）：need_speaker_names 条件表 1b 分支
        合并进"不排队"——mapping 缺失（本媒体从未推断过说话人姓名，或
        fingerprint 未命中）且结构化产物缺失（无落点承接本轮新推断出的
        映射）时，无论校对是否确认 FULL，一律不排队。

        本测试原名 test_missing_structured_artifact_with_partial_
        calibration_forces_recalibration，只覆盖 PARTIAL 单一场景，且锁死
        的正是本轮要删除的行为：旧代码这里认为 PARTIAL/NONE/状态缺失三种
        非-FULL 状态都必须排队并强制 queued_calibrate=True（G2 逻辑），
        让"仅补说话人姓名"的请求偷偷变成一次真实付费校对。现在改写为参数
        化用例，覆盖三种非-FULL 状态，全部断言为不排队（红：旧代码在这里
        断言 len(queued) == 1 且 calibrate 被升级为 True）——原本"结构化
        产物缺失+校对未确认 FULL+要姓名"这个场景，已经和已确认 FULL 的
        场景（见上面 test_missing_mapping_with_full_calibration_and_
        missing_structured_is_not_queued）合并成同一条"无落点不排队"规则：
        H2 已经删除了"仅推断一轮的结果可以首次落盘"这条路径，继续排队只
        会换来一次没有任何落点的说话人推断，白烧 LLM token 且结果永远不
        可见，与已确认 FULL 时的问题完全同构。legacy 缺口交给用户显式
        recalibrate 触发全流程处理。"""
        dialogs = {
            "segments": [
                {"speaker": "S0", "text": "hello", "start_time": 0, "end_time": 1},
            ]
        }
        cache_data = {
            **BASE_CACHE_DATA,
            "transcript_type": "funasr",
            "transcript_data": dialogs,
            "use_speaker_recognition": True,
            "llm_calibrated": "REAL partially-calibrated text (some segments really went through LLM calibration)",
            "llm_summary": "REAL summary",
            # 故意不带 "llm_processed" 和 "speaker_mapping"：DummyCacheManager.
            # get_speaker_mapping 会返回 None（指纹未命中），驱动
            # need_speaker_names 走"映射缺失"这条腿。
        }
        cache_data["llm_status"] = {
            "chapters_status": ChaptersStatus.SKIPPED_NO_TIMELINE,
        }
        if calibration_status is not None:
            cache_data["llm_status"]["calibration_status"] = calibration_status
        # calibration_status is None -> 只保留已经满足的章节层，模拟校对
        # 状态缺失的旧缓存。

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": False, "summarize": True},
        )

        assert result["status"] == "success"
        assert queued == [], (
            f"calibration_status={calibration_status!r}：mapping 缺失且没有"
            "结构化产物可承接推断结果时，即便校对未确认 FULL，也不应排队"
            "——排队只会白烧一次 LLM token，结果永远不可见"
        )

    def test_consistent_structured_artifact_is_still_a_full_hit(
        self, monkeypatch, patch_runtime
    ):
        """健全性检查：展示产物与权威映射一致时（新 schema，带
        speaker_id），Q3 新增的核验不得误伤，仍然是完整命中，不触发任何
        多余的排队。"""
        dialogs = {
            "segments": [
                {"speaker": "S0", "text": "hello", "start_time": 0, "end_time": 1},
            ]
        }
        cache_data = {
            **BASE_CACHE_DATA,
            "transcript_type": "funasr",
            "transcript_data": dialogs,
            "use_speaker_recognition": True,
            "llm_calibrated": "REAL calibrated text",
            "llm_summary": "REAL summary",
            "llm_status": {
                "calibration_status": CalibrationStatus.FULL,
                "chapters_status": ChaptersStatus.SKIPPED_NO_TIMELINE,
            },
            "llm_processed": {
                "dialogs": [{"speaker_id": "S0", "speaker": "New Name", "text": "hello"}],
                "speaker_mapping": {"S0": "New Name"},
            },
            "speaker_mapping": {
                "mapping": {"S0": "New Name"},
                "meta": {"S0": {"name": "New Name", "confidence": 0.9}},
                "low_confidence": [],
            },
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True},
        )

        assert result["status"] == "success"
        assert queued == []

    def test_non_speaker_cache_leaves_cached_speaker_count_none(
        self, monkeypatch, patch_runtime
    ):
        """Non-speaker caches must not fabricate a speaker count."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "REAL calibrated text from a genuine LLM pass",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True},
        )

        assert len(queued) == 1
        assert queued[0]["cached_speaker_count"] is None

    def test_repeated_identical_full_options_is_idempotent(
        self, monkeypatch, patch_runtime
    ):
        """(4) Resubmitting the exact same (already-satisfied) options twice
        must be a full hit both times -- no re-queue on either call."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "real calibrated text",
            "llm_summary": "real summary",
            "llm_status": {
                "calibration_status": CalibrationStatus.FULL,
                "chapters_status": ChaptersStatus.SKIPPED_NO_TIMELINE,
            },
        }

        result1, queued1 = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True}, task_id="t1",
        )
        result2, queued2 = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": True, "summarize": True}, task_id="t2",
        )

        assert result1["status"] == "success"
        assert result2["status"] == "success"
        assert queued1 == []
        assert queued2 == []

    def test_transcript_only_repeated_is_full_hit_without_keyerror(
        self, monkeypatch, patch_runtime
    ):
        """(5) Regression for codex-review R1 item 2: a transcript-only cache
        (calibrate=False, summarize=False on the FIRST request) has a disabled
        calibration placeholder but NO llm_summary key at all (see the
        skip_summary=False/DISABLED path in llm_ops._save_llm_results, which
        never writes llm_summary.txt for a disabled layer). Resubmitting the
        SAME transcript-only options must still be a full hit -- not a
        KeyError from unconditionally indexing cache_data["llm_summary"]."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "disabled placeholder text",
            "llm_status": {"calibration_status": CalibrationStatus.DISABLED},
            # deliberately no "llm_summary" key -- summarize was never requested
        }

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": False, "summarize": False},
        )

        assert result["status"] == "success"
        assert result["data"]["cached"] is True
        assert queued == []

    def test_missing_processing_options_defaults_to_full_flow_legacy_gate(
        self, monkeypatch, patch_runtime
    ):
        """Backward compatibility: process_transcription(processing_options=None)
        must reproduce the pre-feature gate exactly (has_llm_calibrated and
        has_llm_summary) for a cache that DOES carry a confirmed llm_status.json
        (the normal, fully-committed case)."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "x",  # summary missing
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }

        result, queued = _run(monkeypatch, patch_runtime, cache_data, None)

        assert result["status"] == "success"
        assert len(queued) == 1
        # Legacy default (all True): calibrated layer already real and
        # confirmed by llm_status.json, so only summary is missing.
        assert queued[0]["processing_options"] == {
            "calibrate": False,
            "summarize": True,
            "infer_speaker_names": False,
            "chapters": True,
        }

    def test_missing_llm_status_does_not_trust_existing_calibrated_file(
        self, monkeypatch, patch_runtime
    ):
        """codex-review 本地第 16 轮 Q2: llm_status.json 是 _save_llm_results
        整段落盘序列里最后写入的提交标记。calibrated 文件存在但状态文件缺失
        无法区分"半提交（中途失败）"与"从未有状态的旧缓存"，统一按保守
        策略处理——不得把 has_llm_calibrated=True 当作层已满足，必须触发
        真实重新校对（而不是永久信任一份可能是全降级 NONE 兜底文本的产物）。
        """
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "x",
            # 故意不带 "llm_status"：模拟 save_llm_status 从未成功写入过
            # （半提交，或早于诚实状态模型上线的旧缓存——两者在磁盘上完全
            # 无法区分，见 transcription.py 里 calibrated_layer_satisfied
            # 定义处的注释）。
        }

        result, queued = _run(monkeypatch, patch_runtime, cache_data, None)

        assert result["status"] == "success"
        assert len(queued) == 1
        task = queued[0]
        # 校对层被视为未确认完成，触发真实重新校对（不再是 False）。
        assert task["processing_options"] == {
            "calibrate": True,
            "summarize": True,
            "infer_speaker_names": False,
            "chapters": True,
        }
        # 重新校对必须喂原始转录文本，而不是那份未经确认的 calibrated 产物。
        assert task["transcript"] == "RAW uncalibrated transcript"


class TestFullHitMirrorsCacheStatusOnTaskRow:
    """Regression for codex-review R2 item 2: a full cache hit takes no
    further LLM action and calls update_task_status(..., SUCCESS) directly --
    but the task_status row backing that call is BRAND NEW (created earlier
    by the endpoint handler via create_task, columns start out NULL). Without
    mirroring the media's real llm_status.json into that call,
    calibration_status/summary_status stay NULL on the row forever, so
    /api/audit/history reports empty status for a task whose underlying cache
    is actually fully processed.

    Unlike the rest of this file (which uses DummyCacheManager to isolate the
    hit/miss decision), this test uses a REAL CacheManager against a tmp_path
    SQLite DB + cache directory -- it seeds the cache the way a genuine prior
    full-flow run (with LLM calls mocked out) would leave it on disk, then
    drives the actual second-request full-hit code path end to end and reads
    back the real task_status columns, comparing them against the real
    llm_status.json file on disk.
    """

    def test_full_hit_task_row_mirrors_llm_status_json(
        self, monkeypatch, patch_runtime, tmp_path
    ):
        real_cm = CacheManager(cache_dir=str(tmp_path / "cache"))
        try:
            # ---- Simulate a prior full-flow run (LLM calls mocked out at
            # their own layer -- see test_llm_ops_status_backfill.py for that
            # coverage). What matters here is the ON-DISK end state such a
            # run leaves behind: transcript + both LLM layers + a real
            # llm_status.json with non-trivial (non-"full", non-default)
            # values, so a naive "hardcode full/generated" fix would not
            # accidentally pass this assertion.
            real_cm.save_cache(
                platform="youtube",
                url="https://www.youtube.com/watch?v=abc123",
                media_id="abc123",
                use_speaker_recognition=False,
                transcript_data="RAW uncalibrated transcript",
                transcript_type="capswriter",
                title="cached title",
                author="cached author",
                description="cached desc",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="abc123", use_speaker_recognition=False,
                llm_type="calibrated", content="real calibrated text",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="abc123", use_speaker_recognition=False,
                llm_type="summary", content="real summary",
            )
            real_cm.save_llm_status(
                platform="youtube", media_id="abc123", use_speaker_recognition=False,
                calibration_status=CalibrationStatus.PARTIAL,
                summary_status=SummaryStatus.GENERATED,
                chapters_status=ChaptersStatus.SKIPPED_NO_TIMELINE,
            )

            # ---- Second request for the SAME URL: the endpoint handler
            # would create_task() before enqueueing; replicate that here.
            task_id = real_cm.create_task(
                url="https://www.youtube.com/watch?v=abc123",
                use_speaker_recognition=False,
                platform="youtube",
                media_id="abc123",
            )["task_id"]

            monkeypatch.setattr(transcription, "cache_manager", real_cm)

            result = transcription.process_transcription(
                task_id=task_id,
                url="https://www.youtube.com/watch?v=abc123",
                use_speaker_recognition=False,
                wechat_webhook=None,
                download_url=None,
                metadata_override=None,
                processing_options={"calibrate": True, "summarize": True},
            )

            assert result["status"] == "success"
            assert result["data"]["cached"] is True

            row = real_cm.get_task_by_id(task_id)
            assert row is not None
            assert row["status"] == "success"

            cache_data = real_cm.get_cache(
                "youtube", "abc123", use_speaker_recognition=False
            )
            llm_status = cache_data["llm_status"]

            # The bug this fixes: these two used to be NULL on a full-hit row.
            assert row["calibration_status"] is not None
            assert row["summary_status"] is not None
            assert row["calibration_status"] == llm_status["calibration_status"]
            assert row["summary_status"] == llm_status["summary_status"]
            assert row["calibration_status"] == CalibrationStatus.PARTIAL
            assert row["summary_status"] == SummaryStatus.GENERATED
        finally:
            real_cm.close()

    def test_full_hit_does_not_fabricate_full_status_when_llm_status_missing(
        self, monkeypatch, patch_runtime, tmp_path
    ):
        """codex-review 本地第 16 轮 Q2: llm_status.json 缺失（半提交，或
        早于诚实状态模型上线的旧缓存）时，即便本轮请求 calibrate=False（无
        需触发真实重新校对，直接短路 success），也不能在镜像进
        task_status 行时把"未确认"悄悄编成 FULL——那会让 /api/audit/history
        展示一个从未真正确认过的状态，与磁盘上"根本没有 llm_status.json"
        的真实情况矛盾。"""
        real_cm = CacheManager(cache_dir=str(tmp_path / "cache"))
        try:
            real_cm.save_cache(
                platform="youtube",
                url="https://www.youtube.com/watch?v=abc123",
                media_id="abc123",
                use_speaker_recognition=False,
                transcript_data="RAW uncalibrated transcript",
                transcript_type="capswriter",
                title="cached title",
                author="cached author",
                description="cached desc",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="abc123", use_speaker_recognition=False,
                llm_type="calibrated", content="real calibrated text",
            )
            # 故意不调用 save_llm_status：模拟半提交（中途失败）或旧缓存。

            task_id = real_cm.create_task(
                url="https://www.youtube.com/watch?v=abc123",
                use_speaker_recognition=False,
                platform="youtube",
                media_id="abc123",
            )["task_id"]

            monkeypatch.setattr(transcription, "cache_manager", real_cm)

            result = transcription.process_transcription(
                task_id=task_id,
                url="https://www.youtube.com/watch?v=abc123",
                use_speaker_recognition=False,
                wechat_webhook=None,
                download_url=None,
                metadata_override=None,
                processing_options={"calibrate": False, "summarize": False},
            )

            assert result["status"] == "success"

            row = real_cm.get_task_by_id(task_id)
            assert row is not None
            assert row["status"] == "success"
            # 不得被推断为 FULL——保持 None，如实反映"未确认"。
            assert row["calibration_status"] is None
        finally:
            real_cm.close()


class TestSpeakerCacheSummaryOnlyBackfillEndToEnd:
    """Regression coverage for codex-review R4 item 1: a speaker-recognition
    (funasr) cache that already has a REAL calibrated layer + structured
    data (llm_processed.json) but no summary. A subsequent full-flow request
    for the same URL must only backfill the summary layer, reusing the
    existing calibrated text via the forced plain-text route
    (transcription_data=None) while use_speaker_recognition stays True on
    the queued task -- exactly the shape that once risked
    `structured_data["calibration_stats"] = ...` crashing on None.

    Unlike TestLayeredCacheMatrix (which only asserts the transcription.py
    queuing DECISION), this drives the queued task all the way through
    llm_ops._handle_llm_task/_save_llm_results against a REAL CacheManager,
    so it also asserts the actual on-disk outcome: task success, summary
    persisted, and the pre-existing llm_processed.json left untouched.

    Note: as of the suppress_calibration guard added for codex-review R3,
    this exact call path (processing_options={"calibrate": False, ...} with
    the calibrated layer already present) was already crash-safe -- this
    test documents/locks that invariant for the speaker-recognition case
    (previously only covered with use_speaker_recognition=False). The
    TypeError itself is reproduced and locked down at the unit level in
    tests/unit/test_llm_ops_helpers.py::
    TestSaveLLMResultsLayeredCacheSuppression::
    test_structured_data_none_does_not_crash_when_not_suppressed, which
    exercises the other real call path (calibrate_only=True recalibrate,
    where suppression is unconditionally bypassed).
    """

    def test_summary_only_backfill_preserves_existing_structured_data(self, tmp_path):
        real_cm = CacheManager(cache_dir=str(tmp_path / "cache"))
        try:
            # ---- Seed a prior full speaker-recognition run: real
            # calibration + structured data, summary missing.
            real_cm.save_cache(
                platform="youtube",
                url="https://www.youtube.com/watch?v=abc123",
                media_id="abc123",
                use_speaker_recognition=True,
                transcript_data={
                    "segments": [
                        {"speaker": "S0", "text": "hello", "start_time": 0, "end_time": 1}
                    ]
                },
                transcript_type="funasr",
                title="cached title",
                author="cached author",
                description="cached desc",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="abc123", use_speaker_recognition=True,
                llm_type="calibrated",
                content="REAL calibrated text from a genuine speaker-aware pass",
            )
            existing_structured = {
                "dialogs": [
                    {"speaker": "Alice", "text": "hello"},
                    {"speaker": "Bob", "text": "hi there"},
                ],
                "speaker_mapping": {"S0": "Alice", "S1": "Bob"},
            }
            real_cm.save_llm_result(
                platform="youtube", media_id="abc123", use_speaker_recognition=True,
                llm_type="structured", content=existing_structured,
            )
            real_cm.save_llm_status(
                platform="youtube", media_id="abc123", use_speaker_recognition=True,
                calibration_status=CalibrationStatus.FULL,
                calibration_stats={
                    "total_chunks": 1, "success_count": 1,
                    "fallback_count": 0, "failed_count": 0,
                },
                summary_status=None,
            )

            task_id = real_cm.create_task(
                url="https://www.youtube.com/watch?v=abc123",
                use_speaker_recognition=True,
                platform="youtube",
                media_id="abc123",
            )["task_id"]
            real_cm.update_task_status(task_id, TaskStatus.CALIBRATING)

            # ---- Mirrors transcription.py's "校对层已满足，只缺总结" queuing
            # decision (see TestLayeredCacheMatrix.
            # test_calibrate_only_then_full_flow_requeues_summary_only): the
            # calibrated text is reused as input, transcription_data is
            # forced None (plain-text routing), use_speaker_recognition
            # stays True.
            llm_task = {
                "task_id": task_id,
                "url": "https://www.youtube.com/watch?v=abc123",
                "display_url": "https://www.youtube.com/watch?v=abc123",
                "platform": "youtube",
                "media_id": "abc123",
                "video_title": "cached title",
                "author": "cached author",
                "description": "cached desc",
                "transcript": "REAL calibrated text from a genuine speaker-aware pass",
                "use_speaker_recognition": True,
                "transcription_data": None,
                # codex-review R5 #3: transcription.py reads this from the
                # cached llm_processed.json's speaker_mapping (see
                # TestLayeredCacheMatrix.
                # test_calibrate_only_speaker_cache_propagates_cached_speaker_count)
                # and threads it through so the coordinator doesn't
                # misjudge this as single-speaker just because
                # transcription_data was forced to None above.
                "cached_speaker_count": len(existing_structured["speaker_mapping"]),
                "is_generic": False,
                "wechat_webhook": None,
                "notification_channel": None,
                "notification_webhooks": {},
                "processing_options": {"calibrate": False, "summarize": True},
            }

            coordinator = MagicMock()
            # Real coordinator.process(skip_calibration=True) behavior for the
            # plain-text route: structured_data is None (only the
            # speaker-aware dialog-list route ever produces it).
            coordinator.process.return_value = {
                "calibrated_text": "REAL calibrated text from a genuine speaker-aware pass",
                "summary_text": "a real fresh summary",
                "stats": {
                    "calibration_status": CalibrationStatus.DISABLED,
                    "calibration_stats": {
                        "total_segments": 0, "calibrated_segments": 0,
                        "fallback_segments": 0, "low_quality_segments": 0,
                    },
                    "summary_status": SummaryStatus.GENERATED,
                },
                "models_used": {},
                "structured_data": None,
            }

            ctxs = [
                patch.object(llm_ops, "cache_manager", real_cm),
                patch.object(llm_ops, "llm_coordinator", coordinator),
                patch.object(llm_ops, "llm_task_queue", MagicMock()),
                patch.object(llm_ops, "_send_notification", MagicMock()),
                patch.object(llm_ops, "get_notification_router", lambda: MagicMock()),
                patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
            ]
            for c in ctxs:
                c.start()
            try:
                llm_ops._handle_llm_task(llm_task)
            finally:
                for c in ctxs:
                    c.stop()

            row = real_cm.get_task_by_id(task_id)
            assert row["status"] == "success"
            assert row["error_message"] is None

            # codex-review R5 #3: the real (>1) speaker count must reach the
            # coordinator despite content being plain text (transcription_data
            # forced None above) -- this is what lets SummaryProcessor pick
            # the multi-speaker prompt instead of silently defaulting to
            # single-speaker.
            coordinator.process.assert_called_once()
            assert coordinator.process.call_args.kwargs["speaker_count_hint"] == 2

            cache_data = real_cm.get_cache(
                "youtube", "abc123", use_speaker_recognition=True
            )
            assert cache_data["llm_summary"] == "a real fresh summary"

            # The pre-existing structured data (llm_processed.json) must
            # survive untouched on disk -- this round produced no new
            # structured data (plain-text route), so it must not be
            # overwritten/wiped. get_cache() doesn't surface this file's
            # content directly, so read it back from the cache dir.
            import json

            structured_file = Path(cache_data["file_path"]) / "llm_processed.json"
            assert structured_file.exists()
            with open(structured_file, "r", encoding="utf-8") as f:
                persisted_structured = json.load(f)
            assert persisted_structured["dialogs"] == existing_structured["dialogs"]
            assert persisted_structured["speaker_mapping"] == existing_structured["speaker_mapping"]
        finally:
            real_cm.close()


class TestTranscriptOnlyCacheBothSwitchesOffIsNotFullHit:
    """codex-review R5 #2: a cache that has ONLY the transcript layer (no
    llm_calibrated/llm_summary/llm_status at all -- e.g. an old pre-LLM
    cache, or simply the very first request for this media) combined with
    calibrate=False AND summarize=False must NOT be misjudged as "cache
    already has LLM results".

    Before the fix, need_calibrated/need_summary were computed False purely
    because the REQUEST didn't want those layers -- not because they
    already existed -- so the code took the "cache has full LLM results"
    display branch, read the nonexistent llm_calibrated as an empty string,
    sent an essentially blank calibration notification, and never marked
    the task row/llm_status.json as calibration-disabled (so a later
    calibrate=True request could not tell "already disabled" from
    "never attempted").

    The fix routes this case through the SAME enqueue-to-llm_task_queue
    path already used for genuine partial hits, so the existing
    skip_calibration/skip_summary machinery in llm_ops/coordinator
    produces exactly the outcome a brand-new calibrate=False&summarize=False
    request would -- no bespoke inline handling in transcription.py.
    """

    def test_missing_llm_layers_with_both_switches_off_is_queued_not_displayed(
        self, monkeypatch, patch_runtime
    ):
        """Decision-level check (mirrors TestLayeredCacheMatrix): a
        transcript-only cache must be queued for real (disabled) LLM
        processing, not treated as an already-complete full hit."""
        cache_data = {**BASE_CACHE_DATA}  # transcript layer ONLY

        result, queued = _run(
            monkeypatch, patch_runtime, cache_data,
            {"calibrate": False, "summarize": False},
        )

        assert result["status"] == "success"
        assert len(queued) == 1
        task = queued[0]
        assert task["processing_options"] == {
            "calibrate": False,
            "summarize": False,
            "infer_speaker_names": False,
            "chapters": False,
        }
        # Real (raw) transcript reused as input -- no re-download/re-transcribe.
        assert task["transcript"] == "RAW uncalibrated transcript"

    def test_queued_task_end_to_end_produces_disabled_status_and_real_notification(
        self, tmp_path
    ):
        """End-to-end: drive the queued task all the way through
        llm_ops._handle_llm_task/_save_llm_results against a REAL
        CacheManager and a captured notification router, and assert on the
        actual observable outcomes the review flagged as broken:
        - the push notification is non-empty and carries the real
          (locally-formatted) calibrated text, with the "disabled" wording
          the codebase already uses for a genuinely-off layer (not silently
          treated as "not yet generated")
        - the task row is marked calibration_status=disabled (not left NULL)
        - llm_calibrated.txt actually gets written to disk, so the view
          page's ?raw=calibrated can render real content instead of hitting
          the "file does not exist" branch forever
        """
        real_cm = CacheManager(cache_dir=str(tmp_path / "cache"))
        try:
            # ---- Seed a transcript-only cache: LLM has never run for this
            # media at all (no llm_calibrated/llm_summary/llm_status).
            real_cm.save_cache(
                platform="youtube",
                url="https://www.youtube.com/watch?v=abc123",
                media_id="abc123",
                use_speaker_recognition=False,
                transcript_data="RAW uncalibrated transcript",
                transcript_type="capswriter",
                title="cached title",
                author="cached author",
                description="cached desc",
            )

            task_id = real_cm.create_task(
                url="https://www.youtube.com/watch?v=abc123",
                use_speaker_recognition=False,
                platform="youtube",
                media_id="abc123",
            )["task_id"]
            real_cm.update_task_status(task_id, TaskStatus.CALIBRATING)

            # ---- Mirrors transcription.py's fixed queuing decision for this
            # scenario (see TestTranscriptOnlyCacheBothSwitchesOffIsNotFullHit
            # above): calibrate=False & summarize=False, both genuinely
            # missing -> queued for real (disabled) processing.
            llm_task = {
                "task_id": task_id,
                "url": "https://www.youtube.com/watch?v=abc123",
                "display_url": "https://www.youtube.com/watch?v=abc123",
                "platform": "youtube",
                "media_id": "abc123",
                "video_title": "cached title",
                "author": "cached author",
                "description": "cached desc",
                "transcript": "RAW uncalibrated transcript",
                "use_speaker_recognition": False,
                "transcription_data": None,
                "is_generic": False,
                "wechat_webhook": None,
                "notification_channel": None,
                "notification_webhooks": {},
                "processing_options": {"calibrate": False, "summarize": False},
            }

            # coordinator.process(skip_calibration=True, skip_summary=True):
            # calibration_status is DISABLED (local formatting, no LLM call),
            # summary_text/summary_status are None ("not attempted this
            # round" -- _save_llm_results is the one that turns the missing
            # summary layer into an explicit DISABLED status, exercised for
            # real below, not mocked).
            coordinator = MagicMock()
            coordinator.process.return_value = {
                "calibrated_text": "RAW uncalibrated transcript (locally formatted)",
                "summary_text": None,
                "stats": {
                    "calibration_status": CalibrationStatus.DISABLED,
                    "calibration_stats": {
                        "total_segments": 0, "calibrated_segments": 0,
                        "fallback_segments": 0, "low_quality_segments": 0,
                    },
                    "summary_status": None,
                },
                "models_used": {},
                "structured_data": None,
            }

            notification_router = MagicMock()
            notification_router.send_long_text = MagicMock()
            notification_router.send_text = MagicMock()

            ctxs = [
                patch.object(llm_ops, "cache_manager", real_cm),
                patch.object(llm_ops, "llm_coordinator", coordinator),
                patch.object(llm_ops, "llm_task_queue", MagicMock()),
                patch.object(llm_ops, "get_notification_router", lambda: notification_router),
                patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
            ]
            for c in ctxs:
                c.start()
            try:
                llm_ops._handle_llm_task(llm_task)
            finally:
                for c in ctxs:
                    c.stop()

            # ---- Task row: success, and calibration explicitly marked
            # disabled (not left NULL as the pre-fix full-hit branch did).
            row = real_cm.get_task_by_id(task_id)
            assert row["status"] == "success"
            assert row["calibration_status"] == CalibrationStatus.DISABLED
            assert row["summary_status"] == SummaryStatus.DISABLED

            # ---- llm_status.json on disk mirrors the same disabled states,
            # and llm_calibrated.txt is real -- the view page can render it.
            cache_data = real_cm.get_cache(
                "youtube", "abc123", use_speaker_recognition=False
            )
            assert cache_data["llm_status"]["calibration_status"] == CalibrationStatus.DISABLED
            assert cache_data["llm_status"]["summary_status"] == SummaryStatus.DISABLED
            assert cache_data["llm_calibrated"] == "RAW uncalibrated transcript (locally formatted)"

            calibrated_file = Path(cache_data["file_path"]) / "llm_calibrated.txt"
            assert calibrated_file.exists()
            assert calibrated_file.read_text(encoding="utf-8").strip() != ""

            # ---- Notification: non-empty, carries the real calibrated
            # text (not a blank string), and uses the codebase's existing
            # "disabled" wording rather than silently implying "not yet
            # generated".
            assert notification_router.send_long_text.called
            sent_text = notification_router.send_long_text.call_args.kwargs["text"]
            assert sent_text.strip() != ""
            assert "RAW uncalibrated transcript (locally formatted)" in sent_text
            assert "未启用" in sent_text
        finally:
            real_cm.close()


class TestCalibrateOnlyBackfillPreservesExistingSummaryNotification:
    """codex-review R8 #1: a cache that already has a REAL summary but a
    disabled/missing calibration layer (e.g. a prior calibrate=False &
    summarize=True request). A subsequent request that only needs to
    backfill calibration (processing_options={"calibrate": True,
    "summarize": False}, mirroring transcription.py's need_summary=False
    decision when llm_summary.txt already exists) must not lose the
    existing summary in the completion notification.

    Before the fix, _build_result_dict() derived skip_summary purely from
    THIS round's coordinator output (summary_text=None because the
    coordinator was told to skip summary) -- even though
    _save_llm_results()/save_llm_status() correctly preserved the real
    generated summary on disk via merge semantics. _send_notification()
    then consumed the stale in-memory result_dict and reported "总结未生成"
    (summary not generated), discarding a summary that genuinely exists in
    the cache.
    """

    def test_calibrate_backfill_with_existing_summary_notifies_real_summary(
        self, tmp_path
    ):
        real_cm = CacheManager(cache_dir=str(tmp_path / "cache"))
        try:
            # ---- Seed a prior calibrate=False & summarize=True run: a real
            # summary already exists, calibration is only a locally-formatted
            # disabled placeholder.
            real_cm.save_cache(
                platform="youtube",
                url="https://www.youtube.com/watch?v=abc123",
                media_id="abc123",
                use_speaker_recognition=False,
                transcript_data="RAW uncalibrated transcript",
                transcript_type="capswriter",
                title="cached title",
                author="cached author",
                description="cached desc",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="abc123", use_speaker_recognition=False,
                llm_type="calibrated",
                content="RAW uncalibrated transcript (locally formatted)",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="abc123", use_speaker_recognition=False,
                llm_type="summary",
                content="EXISTING real summary text from a prior generation",
            )
            real_cm.save_llm_status(
                platform="youtube", media_id="abc123", use_speaker_recognition=False,
                calibration_status=CalibrationStatus.DISABLED,
                summary_status=SummaryStatus.GENERATED,
            )

            task_id = real_cm.create_task(
                url="https://www.youtube.com/watch?v=abc123",
                use_speaker_recognition=False,
                platform="youtube",
                media_id="abc123",
            )["task_id"]
            real_cm.update_task_status(task_id, TaskStatus.CALIBRATING)

            # ---- Mirrors transcription.py's "校对层缺失/未启用，总结层已满足"
            # queuing decision: calibrate=True (real calibration requested),
            # summarize=False (llm_summary.txt already exists).
            llm_task = {
                "task_id": task_id,
                "url": "https://www.youtube.com/watch?v=abc123",
                "display_url": "https://www.youtube.com/watch?v=abc123",
                "platform": "youtube",
                "media_id": "abc123",
                "video_title": "cached title",
                "author": "cached author",
                "description": "cached desc",
                "transcript": "RAW uncalibrated transcript",
                "use_speaker_recognition": False,
                "transcription_data": None,
                "is_generic": False,
                "wechat_webhook": None,
                "notification_channel": None,
                "notification_webhooks": {},
                "processing_options": {"calibrate": True, "summarize": False},
            }

            # coordinator.process(skip_summary=True): this round performs a
            # real calibration pass but never touches summary -- summary_text
            # and summary_status are both None ("not attempted this round"),
            # exactly the signal _save_llm_results()/llm_ops relies on to
            # preserve the cached summary untouched.
            coordinator = MagicMock()
            coordinator.process.return_value = {
                "calibrated_text": "REAL calibrated text from this round",
                "summary_text": None,
                "stats": {
                    "calibration_status": CalibrationStatus.FULL,
                    "calibration_stats": {
                        "total_segments": 1, "calibrated_segments": 1,
                        "fallback_segments": 0, "low_quality_segments": 0,
                    },
                    "summary_status": None,
                },
                "models_used": {},
                "structured_data": None,
            }

            notification_router = MagicMock()
            notification_router.send_long_text = MagicMock()
            notification_router.send_text = MagicMock()

            ctxs = [
                patch.object(llm_ops, "cache_manager", real_cm),
                patch.object(llm_ops, "llm_coordinator", coordinator),
                patch.object(llm_ops, "llm_task_queue", MagicMock()),
                patch.object(llm_ops, "get_notification_router", lambda: notification_router),
                patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
            ]
            for c in ctxs:
                c.start()
            try:
                llm_ops._handle_llm_task(llm_task)
            finally:
                for c in ctxs:
                    c.stop()

            # ---- Task row: real calibration result, and the merged
            # (preserved) summary status -- not lost/reset to NULL.
            row = real_cm.get_task_by_id(task_id)
            assert row["status"] == "success"
            assert row["calibration_status"] == CalibrationStatus.FULL
            assert row["summary_status"] == SummaryStatus.GENERATED

            # ---- llm_status.json / cache content: the real summary text
            # survives untouched on disk, calibration is upgraded from the
            # disabled placeholder to the real text.
            cache_data = real_cm.get_cache(
                "youtube", "abc123", use_speaker_recognition=False
            )
            assert cache_data["llm_calibrated"] == "REAL calibrated text from this round"
            assert cache_data["llm_summary"] == "EXISTING real summary text from a prior generation"
            assert cache_data["llm_status"]["calibration_status"] == CalibrationStatus.FULL
            assert cache_data["llm_status"]["summary_status"] == SummaryStatus.GENERATED

            # ---- Notification: must carry the real cached summary text
            # and must NOT report "总结未生成" (summary not generated) --
            # this is the codex-review R8 #1 regression this test locks down.
            assert notification_router.send_long_text.called
            call_kwargs = notification_router.send_long_text.call_args.kwargs
            sent_text = call_kwargs["text"]
            assert "EXISTING real summary text from a prior generation" in sent_text
            assert "总结未生成" not in sent_text
            assert "未生成" not in sent_text
            assert call_kwargs["is_summary"] is True
        finally:
            real_cm.close()


class TestCalibrateOnlyBackfillDoesNotMisreportSkippedShortAsSummary:
    """codex-review R9 P2: a cache whose llm_summary.txt is actually the
    SKIPPED_SHORT honest-state fallback (the full calibrated text saved
    verbatim as a stand-in, per the honest-state model -- see
    _save_llm_results' SKIPPED_SHORT branch) must NOT be mistaken for a real
    generated summary by _restore_cached_summary_for_notification() just
    because the file exists on disk.

    Same "只补校对" (calibrate=True, summarize=False) shape as the sibling
    R8 #1 test above, but the seeded cache carries summary_status=
    SKIPPED_SHORT instead of GENERATED. Before the R9 fix, the restore
    helper only checked file existence/non-emptiness, so it would copy the
    stale fallback text into result_dict["内容总结"] and flip
    skip_summary=False -- which both mislabels the notification as "总结"
    and skips the skip_summary branch's 5000-char NOTIFICATION_TEXT_THRESHOLD
    truncation (the summary branch has no length cap at all).
    """

    def test_calibrate_backfill_with_skipped_short_cache_keeps_not_generated_wording(
        self, tmp_path
    ):
        real_cm = CacheManager(cache_dir=str(tmp_path / "cache"))
        try:
            # ---- Seed a prior run whose text was too short to summarize:
            # llm_summary.txt holds the SKIPPED_SHORT fallback -- the
            # calibrated text saved verbatim as a stand-in, NOT a real
            # summary. summary_status is SKIPPED_SHORT, not GENERATED.
            real_cm.save_cache(
                platform="youtube",
                url="https://www.youtube.com/watch?v=short1",
                media_id="short1",
                use_speaker_recognition=False,
                transcript_data="RAW short transcript",
                transcript_type="capswriter",
                title="cached title",
                author="cached author",
                description="cached desc",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="short1", use_speaker_recognition=False,
                llm_type="calibrated",
                content="RAW short transcript (locally formatted)",
            )
            real_cm.save_llm_result(
                platform="youtube", media_id="short1", use_speaker_recognition=False,
                llm_type="summary",
                content="STALE calibrated-as-summary fallback text (should not leak as real summary)",
            )
            real_cm.save_llm_status(
                platform="youtube", media_id="short1", use_speaker_recognition=False,
                calibration_status=CalibrationStatus.DISABLED,
                summary_status=SummaryStatus.SKIPPED_SHORT,
            )

            task_id = real_cm.create_task(
                url="https://www.youtube.com/watch?v=short1",
                use_speaker_recognition=False,
                platform="youtube",
                media_id="short1",
            )["task_id"]
            real_cm.update_task_status(task_id, TaskStatus.CALIBRATING)

            # ---- "只补校对" request: calibrate=True (real calibration
            # requested), summarize=False (llm_summary.txt already exists,
            # even if it's only the SKIPPED_SHORT fallback).
            llm_task = {
                "task_id": task_id,
                "url": "https://www.youtube.com/watch?v=short1",
                "display_url": "https://www.youtube.com/watch?v=short1",
                "platform": "youtube",
                "media_id": "short1",
                "video_title": "cached title",
                "author": "cached author",
                "description": "cached desc",
                "transcript": "RAW short transcript",
                "use_speaker_recognition": False,
                "transcription_data": None,
                "is_generic": False,
                "wechat_webhook": None,
                "notification_channel": None,
                "notification_webhooks": {},
                "processing_options": {"calibrate": True, "summarize": False},
            }

            coordinator = MagicMock()
            coordinator.process.return_value = {
                "calibrated_text": "REAL calibrated text from this round",
                "summary_text": None,
                "stats": {
                    "calibration_status": CalibrationStatus.FULL,
                    "calibration_stats": {
                        "total_segments": 1, "calibrated_segments": 1,
                        "fallback_segments": 0, "low_quality_segments": 0,
                    },
                    "summary_status": None,
                },
                "models_used": {},
                "structured_data": None,
            }

            notification_router = MagicMock()
            notification_router.send_long_text = MagicMock()
            notification_router.send_text = MagicMock()

            ctxs = [
                patch.object(llm_ops, "cache_manager", real_cm),
                patch.object(llm_ops, "llm_coordinator", coordinator),
                patch.object(llm_ops, "llm_task_queue", MagicMock()),
                patch.object(llm_ops, "get_notification_router", lambda: notification_router),
                patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
            ]
            for c in ctxs:
                c.start()
            try:
                llm_ops._handle_llm_task(llm_task)
            finally:
                for c in ctxs:
                    c.stop()

            # ---- Task row and cache: SKIPPED_SHORT must be preserved as-is
            # (not silently promoted to GENERATED by the notification path).
            row = real_cm.get_task_by_id(task_id)
            assert row["status"] == "success"
            assert row["calibration_status"] == CalibrationStatus.FULL
            assert row["summary_status"] == SummaryStatus.SKIPPED_SHORT

            cache_data = real_cm.get_cache(
                "youtube", "short1", use_speaker_recognition=False
            )
            assert cache_data["llm_calibrated"] == "REAL calibrated text from this round"
            assert cache_data["llm_status"]["calibration_status"] == CalibrationStatus.FULL
            assert cache_data["llm_status"]["summary_status"] == SummaryStatus.SKIPPED_SHORT

            # ---- Notification: must take the skip_summary branch (fresh
            # calibrated text, "未生成" wording, 5000-char threshold logic
            # in play) and must NOT leak the stale SKIPPED_SHORT fallback
            # content as if it were a real "总结" -- this is the
            # codex-review R9 P2 regression this test locks down.
            assert notification_router.send_long_text.called
            call_kwargs = notification_router.send_long_text.call_args.kwargs
            sent_text = call_kwargs["text"]
            assert "## 校对文本" in sent_text
            assert "REAL calibrated text from this round" in sent_text
            assert "STALE calibrated-as-summary fallback text" not in sent_text
            assert "总结 未生成" in sent_text
            assert call_kwargs["is_summary"] is False
        finally:
            real_cm.close()


class TestFullHitCasLossSuppressesNotification:
    """H2 (local codex review round 7): process_transcription()'s cache
    full-hit branch used to send its content notification
    (router.send_long_text) and its "任务完成" completion notification
    (task_notifier.send_text) BEFORE calling cache_manager
    .update_task_status(..., TaskStatus.SUCCESS, ...), and it silently
    ignored that call's compare-and-set return value. If the task had
    already been closed to a terminal state by another path (e.g. shutdown
    liquidation marking it failed on a timeout) before this branch's own
    write landed, the CAS write loses -- but the user had already received
    a "success" notification for a task the database actually recorded as
    failed. Fixed by writing the CAS first and gating both notifications on
    a genuine win; a loss now logs a warning with the real terminal status
    instead.
    """

    def test_cas_loss_suppresses_notifications(self, monkeypatch, patch_runtime):
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "real calibrated text",
            "llm_summary": "real summary",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }

        class CasLosingCacheManager(DummyCacheManager):
            def update_task_status(self, task_id, status, **kwargs):
                # Simulate the task already having been closed elsewhere
                # (e.g. shutdown liquidation) as a terminal failure --
                # update_task_status()'s real terminal stickiness would
                # reject this SUCCESS write and return False.
                self.status_updates.append((task_id, status, kwargs))
                return False

        cache_manager = CasLosingCacheManager(cache_data=cache_data)
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)

        notification_router = MagicMock()
        monkeypatch.setattr(transcription, "get_notification_router", lambda: notification_router)

        result = transcription.process_transcription(
            task_id="task-cas-loss",
            url="https://www.youtube.com/watch?v=abc123",
            use_speaker_recognition=False,
            wechat_webhook=None,
            download_url=None,
            metadata_override=None,
            processing_options={"calibrate": False, "summarize": False},
        )

        # The HTTP-level response still reports the cached content was
        # served successfully -- that part is unrelated to this fix, which
        # is only about the async notification + terminal-status logging.
        assert result["status"] == "success"

        # The actual bug this test pins down: once the CAS write loses, no
        # notification of any kind (content or "task complete") may be sent.
        notification_router.send_long_text.assert_not_called()
        notification_router.send_text.assert_not_called()

        # The CAS write was genuinely attempted with SUCCESS (and lost) --
        # this isn't a case of the write being skipped outright.
        assert cache_manager.status_updates[-1][1] == TaskStatus.SUCCESS

    def test_cas_win_still_sends_notifications(self, monkeypatch, patch_runtime):
        """Sanity/regression companion: the normal path (CAS wins) must
        still notify -- the fix must not accidentally suppress real
        completions."""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "real calibrated text",
            "llm_summary": "real summary",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }
        cache_manager = DummyCacheManager(cache_data=cache_data)
        # The completion-message ("任务完成") send is guarded by
        # `task_info and task_info.get("view_token")` -- populate it so this
        # test can actually observe that branch being reached, matching a
        # real task row (which always has a view_token, assigned at
        # create_task time).
        cache_manager.tasks["task-cas-win"] = {"view_token": "vt-cas-win"}
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)

        notification_router = MagicMock()
        monkeypatch.setattr(transcription, "get_notification_router", lambda: notification_router)

        result = transcription.process_transcription(
            task_id="task-cas-win",
            url="https://www.youtube.com/watch?v=abc123",
            use_speaker_recognition=False,
            wechat_webhook=None,
            download_url=None,
            metadata_override=None,
            processing_options={"calibrate": False, "summarize": False},
        )

        assert result["status"] == "success"
        notification_router.send_long_text.assert_called_once()
        notification_router.send_text.assert_called_once()


class TestFullHitNotificationExceptionDoesNotFailTask:
    """K3（本地 codex review 第 8 轮）：H2 把 CAS 提到通知之前是对的，但
    通知本身此前仍在最外层通用失败处理的 try/except 覆盖范围内——success
    已经落库后，通知调用（router.send_long_text / task_notifier.send_text）
    抛出的任何异常都会被那个 except 当成"转录处理异常"：函数返回值改成
    failed、发一条误导性的"转录异常"通知、且无条件尝试把 task_status 覆盖
    成 failed（即便这次覆盖会被终态黏性拒绝）。修复后通知逻辑有自己独立
    的 try/except，异常只记日志，不影响已经写定的 success 结果。
    """

    def test_content_notification_exception_after_success_cas_does_not_fail_task(
        self, monkeypatch, patch_runtime,
    ):
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "real calibrated text",
            "llm_summary": "real summary",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }
        cache_manager = DummyCacheManager(cache_data=cache_data)
        cache_manager.tasks["task-notify-boom"] = {"view_token": "vt-notify-boom"}
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)

        notification_router = MagicMock()
        notification_router.send_long_text.side_effect = RuntimeError("webhook timeout")
        monkeypatch.setattr(transcription, "get_notification_router", lambda: notification_router)

        result = transcription.process_transcription(
            task_id="task-notify-boom",
            url="https://www.youtube.com/watch?v=abc123",
            use_speaker_recognition=False,
            wechat_webhook=None,
            download_url=None,
            metadata_override=None,
            processing_options={"calibrate": False, "summarize": False},
        )

        # 任务结果如实反映 success 已经落库——不能因为通知失败就整体报告
        # failed。
        assert result["status"] == "success"

        # CAS 只被真正尝试过一次（SUCCESS）：outer except 从未被触发，
        # 因此没有第二次试图把状态覆盖成 FAILED 的写入。
        assert len(cache_manager.status_updates) == 1
        assert cache_manager.status_updates[0][1] == TaskStatus.SUCCESS

        # 没有误导性的"转录异常"通知（notify_task_status 在正常流程里也会
        # 被调用几次做进度提示——"开始处理"/"使用已有缓存" 等，这里只需要
        # 确认其中不存在 outer except 那条 status="转录异常" 的调用）。
        error_calls = [
            call for call in notification_router.notify_task_status.call_args_list
            if call.kwargs.get("status") == "转录异常"
        ]
        assert error_calls == []

    def test_completion_notification_exception_after_success_cas_does_not_fail_task(
        self, monkeypatch, patch_runtime,
    ):
        """同上，但异常发生在"任务完成"通知（task_notifier.send_text，独立
        try/except 包住的第二个调用点）而不是正文通知。"""
        cache_data = {
            **BASE_CACHE_DATA,
            "llm_calibrated": "real calibrated text",
            "llm_summary": "real summary",
            "llm_status": {"calibration_status": CalibrationStatus.FULL},
        }
        cache_manager = DummyCacheManager(cache_data=cache_data)
        cache_manager.tasks["task-notify-boom-2"] = {"view_token": "vt-notify-boom-2"}
        monkeypatch.setattr(transcription, "cache_manager", cache_manager)

        notification_router = MagicMock()
        notification_router.send_text.side_effect = RuntimeError("webhook timeout")
        monkeypatch.setattr(transcription, "get_notification_router", lambda: notification_router)

        result = transcription.process_transcription(
            task_id="task-notify-boom-2",
            url="https://www.youtube.com/watch?v=abc123",
            use_speaker_recognition=False,
            wechat_webhook=None,
            download_url=None,
            metadata_override=None,
            processing_options={"calibrate": False, "summarize": False},
        )

        assert result["status"] == "success"
        assert len(cache_manager.status_updates) == 1
        assert cache_manager.status_updates[0][1] == TaskStatus.SUCCESS
        error_calls = [
            call for call in notification_router.notify_task_status.call_args_list
            if call.kwargs.get("status") == "转录异常"
        ]
        assert error_calls == []
        # 正文通知本身必须已经真正发出（异常发生在它之后的完成通知阶段）。
        notification_router.send_long_text.assert_called_once()
