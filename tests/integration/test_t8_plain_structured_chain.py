"""Integration test: T8 stage-2 (S6) -- plain-source structured calibration
full chain, gated by llm.structured_calibration_for_plain.

Chain A (switch ON): a plain source task (CapsWriter-style cache with a
transcript_capswriter.json sidecar providing timeline segments) is driven
through llm_ops._handle_llm_task with a REAL LLMCoordinator /
SpeakerAwareProcessor / ChaptersProcessor and only the LLM client mocked
(deterministic echo calibration + deterministic chapters JSON). Assertions:

  1. llm_processed.json persisted, top-level mode == "plain_structured",
     serialized content contains no "unknown" (hard assert), dialogs carry
     no speaker key;
  2. rendering (views._prepare_success_view with the switch on) emits
     id="dlg-{i}" anchors;
  3. chapters fingerprint stored in llm_chapters.json matches the
     fingerprint recomputed from the persisted dialogs via the real views
     helpers -> the chapters data island marks every chapter jump_ok=True
     and the transcript carries inline chapter-anchor headers;
  4. llm_calibrated.txt exists and has no "speaker:" prefix lines.

Chain B (switch OFF, default): same task, same cache. Behavior must match
the legacy plain path: PlainTextProcessor text route, no llm_processed.json,
plain rendering (no dlg anchors), chapters generated but not jumpable
(jump_ok=False, no inline anchors) -- fingerprint matches yet the anchor
gate blocks jumps.

Chain C: legacy plain cache WITHOUT a segments sidecar honestly degrades to
the plain-text route even with the switch on (no llm_processed.json).

All external clients (LLM/CapsWriter/FunASR/TikHub/WeCom) are mocked or
never touched; caches live under pytest tmp_path. Console output is ASCII.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.video_transcript_api.cache.cache_manager import CacheManager
from src.video_transcript_api.llm.coordinator import LLMCoordinator
from src.video_transcript_api.llm.core.speaker_inferencer import SpeakerInferencer
from src.video_transcript_api.utils.llm_status import ChaptersStatus
from src.video_transcript_api.utils.task_status import TaskStatus
from src.video_transcript_api.api.services import llm_ops
from src.video_transcript_api.api.routes import views


PLATFORM = "youtube"
MEDIA_ID = "t8_plain_chain_vid"
URL = f"https://example.com/{MEDIA_ID}"

# Plain-source timeline segments (no speaker). Texts deliberately avoid the
# full-width colon so chain A can hard-assert "no speaker prefix" as
# "no colon at all" in llm_calibrated.txt. Gaps of >=2.0s plus sentence-final
# punctuation give the deterministic paragraphizer legal breakpoints.
SEGMENTS = [
    {"text": "大家好，欢迎来到今天的节目。我们先聊一聊最近的科技新闻。", "start": 0.0, "end": 4.5},
    {"text": "人工智能的发展速度超出了很多人的预期，尤其是在大模型领域。", "start": 4.8, "end": 9.2},
    {"text": "接下来看看开源社区的动态。本周有几个值得关注的项目发布了新版本。", "start": 12.5, "end": 17.0},
    {"text": "其中一个项目专注于视频转录，可以把字幕处理的流程完全自动化。", "start": 17.3, "end": 21.6},
    {"text": "最后我们讨论一个读者提问。如何判断一个工具是否值得长期使用。", "start": 25.0, "end": 29.4},
    {"text": "我的建议是看它能不能融入你的日常流程，并且持续节省时间。感谢收看。", "start": 29.7, "end": 34.0},
]
TRANSCRIPT = "\n".join(seg["text"] for seg in SEGMENTS)


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _seed_plain_cache(cm, *, with_segments=True):
    """Persist a real CapsWriter-style cache row; the sidecar JSON goes
    through the genuine save_cache -> get_cache -> load_segments path."""
    cm.save_cache(
        platform=PLATFORM,
        url=URL,
        media_id=MEDIA_ID,
        use_speaker_recognition=False,
        transcript_data=TRANSCRIPT,
        transcript_type="capswriter",
        title="T8 Chain Demo",
        author="Alice",
        extra_json_data=SEGMENTS if with_segments else None,
    )


def _make_coordinator(tmp_path):
    """Real coordinator with deterministic small thresholds; the shared
    llm_client.call is replaced by the test double (mutate .call, do NOT
    rebind llm_client -- processors captured the original reference)."""
    coordinator = LLMCoordinator(
        config_dict={
            "llm": {
                "api_key": "test-key",
                "base_url": "https://example.invalid",
                "calibrate_model": "test-model",
                "summary_model": "test-model",
                "min_chapters_threshold": 10,
                "paragraphization": {
                    "target_chars": 60,
                    "hard_max_chars": 200,
                    "pause_threshold_seconds": 2.0,
                },
            }
        },
        cache_dir=str(tmp_path / "llm_core_cache"),
    )
    return coordinator


def _install_llm_double(coordinator, seen_task_types):
    """Deterministic LLM double dispatched on task_type.

    - key_info: empty structured output (valid KeyInfo default).
    - calibrate_chunk: identity echo -- parse "[id][time]: text" lines and
      return {id, text} without the prefix (_valid_correction_text rejects
      text that still starts with "[id]..."), so every chunk passes as FULL.
    - calibrate_segment(_retry): identity echo of the whole transcript
      (plain-text route is a single segment at this length).
    - summary: fixed summary text.
    - chapters: two chapters anchored at the first/last "[i]" indices found
      in the prompt, which are valid surviving original indices for BOTH the
      paragraphized dialogs (chain A) and the raw segments (chain B).
    """

    def _fake_call(**kwargs):
        task_type = kwargs.get("task_type")
        seen_task_types.append(task_type)
        if task_type == "key_info":
            return SimpleNamespace(text="", structured_output={})
        if task_type == "calibrate_chunk":
            corrections = []
            for line in kwargs["user_prompt"].splitlines():
                m = re.match(r"^\[(\d+)\](?:\[[^\]]*\])?:\s*(.*)$", line.strip())
                if m:
                    corrections.append({"id": int(m.group(1)), "text": m.group(2)})
            return SimpleNamespace(text="", structured_output={"corrections": corrections})
        if task_type in ("calibrate_segment", "calibrate_segment_retry"):
            return SimpleNamespace(text=TRANSCRIPT, structured_output=None)
        if task_type == "summary":
            return SimpleNamespace(text="Deterministic summary.", structured_output=None)
        if task_type == "chapters":
            indices = [
                int(m.group(1))
                for line in kwargs["user_prompt"].splitlines()
                if (m := re.match(r"^\[(\d+)\]", line.strip()))
            ]
            assert len(indices) >= 2, f"chapters prompt lacks indices: {kwargs['user_prompt']!r}"
            return SimpleNamespace(
                text="",
                structured_output={
                    "chapters": [
                        {"title": "Opening", "gist": "first part", "start_seg": indices[0]},
                        {"title": "Deep dive", "gist": "second part", "start_seg": indices[-1]},
                    ]
                },
            )
        raise AssertionError(f"unexpected LLM call: task_type={task_type!r}")

    coordinator.llm_client.call = MagicMock(side_effect=_fake_call)


def _make_task(task_id):
    return {
        "task_id": task_id,
        "url": URL,
        "display_url": URL,
        "platform": PLATFORM,
        "media_id": MEDIA_ID,
        "video_title": "T8 Chain Demo",
        "author": "Alice",
        "description": "",
        "transcript": TRANSCRIPT,
        "use_speaker_recognition": False,
        "transcription_data": None,
        "is_generic": False,
        "wechat_webhook": None,
        "notification_channel": None,
        "notification_webhooks": {},
        "processing_options": {"calibrate": True, "summarize": True, "chapters": True},
    }


def _run_llm_task(cm, coordinator, task_id, *, switch_on, extra_patches=()):
    """Drive the real _handle_llm_task with only true external boundaries
    patched (queue, notifications, title LLM shortcut, the feature switch).
    cache_manager / coordinator / _prepare_llm_content / _save_llm_results
    all stay REAL -- that wiring is the object under test."""
    patches = [
        patch.object(llm_ops, "cache_manager", cm),
        patch.object(llm_ops, "llm_coordinator", coordinator),
        patch.object(llm_ops, "llm_task_queue", MagicMock()),
        patch.object(llm_ops, "_send_notification", MagicMock()),
        patch.object(llm_ops, "get_notification_router", lambda: MagicMock()),
        patch.object(llm_ops, "_generate_title_if_needed", lambda t, title, tr: title),
        patch.object(
            llm_ops, "config", {"llm": {"structured_calibration_for_plain": switch_on}}
        ),
        *extra_patches,
    ]
    for p in patches:
        p.start()
    try:
        llm_ops._handle_llm_task(_make_task(task_id))
    finally:
        for p in patches:
            p.stop()


def _prepare_view(cache_dir, *, switch_on):
    """Run the real success-view preparation (rendering strategy, chapters
    fingerprint re-check, anchor gate, chapters data island + inline chapter
    anchors) with the switch injected the same way views reads it in
    production."""
    view_data = {"cache_dir": str(cache_dir)}
    with patch.object(
        views,
        "get_config",
        lambda: {"llm": {"structured_calibration_for_plain": switch_on}},
    ):
        views._prepare_success_view(view_data)
    return view_data


def _create_running_task(cm):
    task_id = cm.create_task(url=URL, platform=PLATFORM, media_id=MEDIA_ID)["task_id"]
    cm.update_task_status(task_id, TaskStatus.CALIBRATING)
    return task_id


class TestPlainStructuredChainSwitchOn:
    """Chain A: switch on, plain source with segments -> full structured
    pipeline identical in shape to the FunASR product."""

    def test_full_chain(self, cm, tmp_path):
        _seed_plain_cache(cm, with_segments=True)
        task_id = _create_running_task(cm)
        coordinator = _make_coordinator(tmp_path)
        seen_task_types = []
        _install_llm_double(coordinator, seen_task_types)
        infer_spy = MagicMock()

        _run_llm_task(
            cm,
            coordinator,
            task_id,
            switch_on=True,
            extra_patches=(patch.object(SpeakerInferencer, "infer", infer_spy),),
        )

        row = cm.get_task_by_id(task_id)
        assert row["status"] == "success"

        cache_data = cm.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=False)
        cache_dir = Path(cache_data["file_path"])

        # ---- Assertion 1: structured artifact persisted with provenance,
        # no "unknown" speaker placeholder anywhere in the serialized file,
        # no speaker keys ----
        processed_file = cache_dir / "llm_processed.json"
        assert processed_file.exists(), "llm_processed.json must be persisted"
        raw_processed = processed_file.read_text(encoding="utf-8")
        processed = json.loads(raw_processed)
        # Hard assertion (spec: serialized content has no "unknown"). The only
        # tolerated carrier is the pre-existing calibration_stats merge-counter
        # key "unknown_id" (shared FunASR-era schema, injected by
        # _save_llm_results for every structured artifact) -- everything else
        # in the file must be completely free of the string.
        stats_blob = processed.get("calibration_stats") or {}
        stats_serialized = json.dumps(stats_blob, ensure_ascii=False)
        assert stats_serialized.count("unknown") == stats_serialized.count('"unknown_id":')
        serialized_without_stats = json.dumps(
            {k: v for k, v in processed.items() if k != "calibration_stats"},
            ensure_ascii=False,
        )
        assert "unknown" not in serialized_without_stats
        assert processed["mode"] == "plain_structured"
        dialogs = processed["dialogs"]
        assert isinstance(dialogs, list) and len(dialogs) >= 2
        # Deterministic paragraphization merged raw segments into paragraphs.
        assert len(dialogs) < len(SEGMENTS)
        for dialog in dialogs:
            assert "speaker" not in dialog
            assert "speaker_id" not in dialog

        # ---- SpeakerInferencer.infer ran zero times; key_info extraction
        # still happened (it feeds the calibration prompt) ----
        infer_spy.assert_not_called()
        assert "speaker_inference" not in seen_task_types
        assert "key_info" in seen_task_types

        # ---- Assertion 4: llm_calibrated.txt exists, no speaker prefix ----
        calibrated_file = cache_dir / "llm_calibrated.txt"
        assert calibrated_file.exists()
        calibrated_text = calibrated_file.read_text(encoding="utf-8")
        assert calibrated_text.strip()
        assert "unknown" not in calibrated_text
        assert "：" not in calibrated_text

        # ---- Chapters artifact generated from this-round dialogs ----
        chapters_file = cache_dir / "llm_chapters.json"
        assert chapters_file.exists()
        chapters_payload = json.loads(chapters_file.read_text(encoding="utf-8"))
        assert chapters_payload["source"]["kind"] == "dialogs"
        stored_fp = chapters_payload["source"]["fingerprint"]
        assert stored_fp
        assert cache_data["llm_status"]["chapters_status"] == ChaptersStatus.GENERATED

        # ---- Assertion 3: fingerprint recomputed from the PERSISTED
        # anchor source (real views helpers) matches the stored one ----
        anchor_source = views._load_chapters_anchor_source(cache_dir)
        assert [d.get("text") for d in anchor_source] == [d.get("text") for d in dialogs]
        current_fp = views._compute_anchor_fingerprint(anchor_source)
        assert current_fp == stored_fp

        # ---- Assertions 2+3 (view level): real _prepare_success_view emits
        # dlg anchors, a jump-enabled chapters data island, and inline
        # chapter anchors in the transcript ----
        view_data = _prepare_view(cache_dir, switch_on=True)
        calibrated_html = view_data["calibrated_html"]
        assert 'id="dlg-0"' in calibrated_html
        chapters = json.loads(view_data["chapters_data"])
        assert chapters
        assert all(ch["jump_ok"] is True for ch in chapters)

        # Every chapter start_seg must point at an anchor that actually
        # exists on the rendered page, with an inline chapter header before it.
        for chapter in chapters_payload["chapters"]:
            assert f'id="dlg-{chapter["start_seg"]}"' in calibrated_html
            assert (
                f'id="chapter-anchor-{chapter["index"]}"' in calibrated_html
            )


class TestPlainChainSwitchOffRegression:
    """Chain B: switch off (default) -> legacy plain behavior preserved."""

    def test_plain_regression(self, cm, tmp_path):
        _seed_plain_cache(cm, with_segments=True)
        task_id = _create_running_task(cm)
        coordinator = _make_coordinator(tmp_path)
        seen_task_types = []
        _install_llm_double(coordinator, seen_task_types)

        _run_llm_task(cm, coordinator, task_id, switch_on=False)

        row = cm.get_task_by_id(task_id)
        assert row["status"] == "success"

        cache_data = cm.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=False)
        cache_dir = Path(cache_data["file_path"])

        # Plain-text route: identity echo was used, no structured artifact.
        assert "calibrate_segment" in seen_task_types
        assert "calibrate_chunk" not in seen_task_types
        assert not (cache_dir / "llm_processed.json").exists()
        calibrated_text = (cache_dir / "llm_calibrated.txt").read_text(encoding="utf-8")
        assert calibrated_text == TRANSCRIPT

        # Chapters still generated from the cached timeline segments.
        chapters_file = cache_dir / "llm_chapters.json"
        assert chapters_file.exists()
        chapters_payload = json.loads(chapters_file.read_text(encoding="utf-8"))
        assert chapters_payload["source"]["kind"] == "segments"
        stored_fp = chapters_payload["source"]["fingerprint"]

        # Fingerprint DOES match (anchor source falls back to the same
        # segments) -- the nolink outcome comes from the missing dlg anchors
        # gate, not from a fingerprint mismatch.
        anchor_source = views._load_chapters_anchor_source(cache_dir)
        current_fp = views._compute_anchor_fingerprint(anchor_source)
        assert current_fp == stored_fp

        # View: plain rendering without dlg anchors, chapters not jumpable.
        view_data = _prepare_view(cache_dir, switch_on=False)
        assert 'id="dlg-' not in view_data["calibrated_html"]
        assert "chapter-anchor" not in view_data["calibrated_html"]
        chapters = json.loads(view_data["chapters_data"])
        assert chapters
        assert all(ch["jump_ok"] is False for ch in chapters)


class TestPlainStructuredHonestDegradation:
    """Chain C: legacy plain cache without a segments sidecar keeps the
    plain-text route even with the switch on."""

    def test_no_segments_degrades_to_plain_text(self, cm, tmp_path):
        _seed_plain_cache(cm, with_segments=False)
        task_id = _create_running_task(cm)
        coordinator = _make_coordinator(tmp_path)
        seen_task_types = []
        _install_llm_double(coordinator, seen_task_types)

        _run_llm_task(cm, coordinator, task_id, switch_on=True)

        row = cm.get_task_by_id(task_id)
        assert row["status"] == "success"

        cache_data = cm.get_cache(PLATFORM, MEDIA_ID, use_speaker_recognition=False)
        cache_dir = Path(cache_data["file_path"])

        assert "calibrate_chunk" not in seen_task_types
        assert "calibrate_segment" in seen_task_types
        assert not (cache_dir / "llm_processed.json").exists()
        calibrated_text = (cache_dir / "llm_calibrated.txt").read_text(encoding="utf-8")
        assert calibrated_text == TRANSCRIPT
        # No timeline anywhere -> chapters honestly skipped, no artifact.
        assert not (cache_dir / "llm_chapters.json").exists()
