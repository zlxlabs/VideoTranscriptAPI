"""Unit tests for chapter-detailed notes processing."""

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.core.usage_context import get_context, set_context
from video_transcript_api.llm.processors.notes_processor import (
    NotesProcessor,
    compute_notes_anchor_fingerprint,
    detect_notes_speaker_labels,
    load_notes_source_segments,
    sanitize_notes_chapter_output,
)
from video_transcript_api.llm.prompts import (
    NOTES_SYSTEM_PROMPT_BASE,
    NOTES_SYSTEM_PROMPT_NO_SPEAKER,
    NOTES_SYSTEM_PROMPT_SPEAKER,
    build_notes_user_prompt,
)
from video_transcript_api.utils.llm_status import NotesStatus


class FakeNotesClient:
    """Record calls and return scripted responses without network access."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _config(*, notes_concurrency=10):
    return LLMConfig(
        api_key="test-key",
        base_url="http://localhost",
        calibrate_model="calibrate-model",
        summary_model="summary-model",
        summary_reasoning_effort="low",
        notes_model="notes-model",
        notes_reasoning_effort="high",
        notes_concurrency=notes_concurrency,
    )


def _structured_segments():
    return [
        {
            "start_time": 0,
            "end_time": 5,
            "speaker": "Alice",
            "text": "第一句包含张三和数字 2024。",
        },
        {
            "start_time": 5,
            "end_time": 10,
            "speaker": "Bob",
            "text": "第二句保留原话。",
        },
        {
            "start_time": 10,
            "end_time": 15,
            "speaker": "Cara",
            "text": "第三句属于下一章。",
        },
    ]


def _chapter_payload(segments):
    return {
        "source": {
            "kind": "dialogs",
            "fingerprint": compute_notes_anchor_fingerprint(segments),
        },
        "chapters": [
            {
                "title": "第一章",
                "gist": "开场观点",
                "start_seg": 0,
                "end_seg": 1,
            },
            {
                "title": "第二章",
                "gist": "后续观点",
                "start_seg": 2,
                "end_seg": 2,
            },
        ],
    }


def _chapter_batch(count):
    segments = [
        {
            "start_time": index * 10,
            "end_time": index * 10 + 10,
            "speaker": "Speaker",
            "text": f"Chapter {index} source text with enough context to avoid density retry",
        }
        for index in range(count)
    ]
    return segments, {
        "source": {
            "kind": "dialogs",
            "fingerprint": compute_notes_anchor_fingerprint(segments),
        },
        "chapters": [
            {
                "title": f"Chapter {index}",
                "gist": f"Chapter {index} gist",
                "start_seg": index,
                "end_seg": index,
            }
            for index in range(count)
        ],
    }


def _single_chapter_payload(segments, title="单章"):
    return {
        "source": {
            "kind": "segments",
            "fingerprint": compute_notes_anchor_fingerprint(segments),
        },
        "chapters": [
            {
                "title": title,
                "gist": "单章概要",
                "start_seg": 0,
                "end_seg": len(segments) - 1,
            }
        ],
    }


def _chapter_title(user_prompt):
    marker = "本章标题："
    start = user_prompt.index(marker) + len(marker)
    return user_prompt[start:].splitlines()[0]


def _set_english_output_language(monkeypatch):
    monkeypatch.setattr(
        "video_transcript_api.llm.processors.notes_processor.get_language",
        lambda: "en",
    )
    monkeypatch.setattr(
        "video_transcript_api.llm.prompts.localized.get_language",
        lambda: "en",
    )


def _set_chinese_output_language(monkeypatch):
    monkeypatch.setattr(
        "video_transcript_api.llm.processors.notes_processor.get_language",
        lambda: "zh-CN",
    )
    monkeypatch.setattr(
        "video_transcript_api.llm.prompts.localized.get_language",
        lambda: "zh-CN",
    )


def test_structured_slice_is_closed_and_calls_notes_sequentially():
    segments = _structured_segments()
    client = FakeNotesClient(["- **第一章结论**", "- 第二章结论"])
    # FakeNotesClient 按队列顺序分发响应，只在串行下确定；本用例锁的是切片闭区间
    # 与相邻章上下文的提示词契约，并发行为由下面的 concurrency 用例覆盖。
    processor = NotesProcessor(client, _config(notes_concurrency=1))

    result = processor.process(
        chapters=_chapter_payload(segments),
        structured_data={"dialogs": segments},
    )

    assert result.status is NotesStatus.GENERATED
    assert result.error is None
    assert result.text == (
        "## [00:00:00 - 00:00:10] 第一章\n- **第一章结论**\n\n"
        "## [00:00:10 - 00:00:15] 第二章\n- 第二章结论"
    )
    assert len(client.calls) == 2
    assert [call["task_type"] for call in client.calls] == ["notes", "notes"]
    assert all(
        call["system_prompt"] == NOTES_SYSTEM_PROMPT_SPEAKER for call in client.calls
    )
    assert all(call["model"] == "notes-model" for call in client.calls)
    assert all(call["reasoning_effort"] == "high" for call in client.calls)
    assert "[00:00:00 - 00:00:05] Alice: 第一" in client.calls[0]["user_prompt"]
    assert "[00:00:05 - 00:00:10] Bob: 第二句" in client.calls[0]["user_prompt"]
    assert "Cara: 第三句" not in client.calls[0]["user_prompt"]
    assert "上一章标题：第一章" in client.calls[1]["user_prompt"]
    assert "下一章标题：无" in client.calls[1]["user_prompt"]


def test_plain_source_slice_uses_no_speaker_variant_without_unknown_speaker():
    segments = [
        {"start": 1, "end": 2, "text": "纯文本第一句"},
        {"start": 2, "end": 3, "text": "纯文本第二句"},
    ]
    chapters = {
        "source": {
            "kind": "segments",
            "fingerprint": compute_notes_anchor_fingerprint(segments),
        },
        "chapters": [
            {
                "title": "纯文本章",
                "gist": "纯文本概要",
                "start_seg": 0,
                "end_seg": 1,
            }
        ],
    }
    client = FakeNotesClient([SimpleNamespace(text="- 纯文本笔记")])

    result = NotesProcessor(client, _config()).process(
        chapters=chapters,
        source_segments=segments,
        source_kind="segments",
    )

    assert result.status is NotesStatus.GENERATED
    prompt = client.calls[0]["user_prompt"]
    assert client.calls[0]["system_prompt"] == NOTES_SYSTEM_PROMPT_NO_SPEAKER
    assert "[00:00:01 - 00:00:02] 纯文本第一句" in prompt
    assert "[00:00:02 - 00:00:03] 纯文本第二句" in prompt
    assert "Unknown" not in prompt


@pytest.mark.parametrize(
    ("segments", "expected_speaker"),
    [
        (
            [
                {"text": "有标签一这是一段足够长的内容", "speaker": "Alice"},
                {"text": "有标签二这是一段足够长的内容", "speaker": "Bob"},
            ],
            True,
        ),
        (
            [
                {"text": "无标签一这是一段足够长的内容"},
                {"text": "无标签二这是一段足够长的内容"},
            ],
            False,
        ),
        (
            [
                {"text": "混合一这是一段足够长的内容", "speaker": "Alice"},
                {"text": "混合二这是一段足够长的内容"},
            ],
            True,
        ),
    ],
)
def test_notes_speaker_variant_is_selected_from_full_source(
    segments, expected_speaker
):
    assert detect_notes_speaker_labels(segments) is expected_speaker
    client = FakeNotesClient(["- note"])

    result = NotesProcessor(client, _config()).process(
        chapters=_single_chapter_payload(segments),
        source_segments=segments,
    )

    assert result.status is NotesStatus.GENERATED
    expected_prompt = (
        NOTES_SYSTEM_PROMPT_SPEAKER
        if expected_speaker
        else NOTES_SYSTEM_PROMPT_NO_SPEAKER
    )
    assert client.calls[0]["system_prompt"] == expected_prompt
    if expected_speaker and "speaker" not in segments[1]:
        assert "Unknown: 混合二这是一段足够长的内容" in client.calls[0]["user_prompt"]


def test_notes_sanitizer_demotes_h1_h2_and_removes_duplicate_chapter_title():
    result = sanitize_notes_chapter_output(
        "# [00:00:00] 本章\n"
        "## [00:00:10 - 00:00:20] 另一个小节\n"
        "### [00:00:30] 已有三级标题\n"
        "- 内容",
        "本章",
    )

    assert result == (
        "### [00:00:10 - 00:00:20] 另一个小节\n"
        "### [00:00:30] 已有三级标题\n"
        "- 内容"
    )


def test_notes_process_removes_duplicate_model_chapter_heading():
    segments = [
        {
            "start": 0,
            "end": 1,
            "text": "这一章的正文内容足够长，用于验证章节标题拼接行为。" * 4,
        }
    ]
    client = FakeNotesClient(["## [00:00:00] 单章\n- 结论"])

    result = NotesProcessor(client, _config()).process(
        chapters=_single_chapter_payload(segments),
        source_segments=segments,
    )

    assert result.status is NotesStatus.GENERATED
    assert result.text == "## [00:00:00 - 00:00:01] 单章\n- 结论"


def test_notes_density_retry_uses_fixed_instruction_when_first_output_is_too_long():
    segments = [{"start": 0, "end": 1, "text": "abcdefghij"}]
    client = FakeNotesClient(["xxxxxxxxx", "- 合格笔记"])

    result = NotesProcessor(client, _config()).process(
        chapters=_single_chapter_payload(segments),
        source_segments=segments,
    )

    assert result.status is NotesStatus.GENERATED
    assert result.text.endswith("- 合格笔记")
    assert len(client.calls) == 2
    assert client.calls[1]["system_prompt"] == client.calls[0]["system_prompt"]
    assert client.calls[1]["model"] == client.calls[0]["model"]
    assert client.calls[1]["reasoning_effort"] == client.calls[0]["reasoning_effort"]
    assert client.calls[1]["task_type"] == client.calls[0]["task_type"]
    assert client.calls[1]["user_prompt"].endswith(
        "警告：你上一次的输出篇幅接近原文，这是誊抄不是笔记。请严格执行 40%-60% 篇幅预算，应用'必须丢弃'清单。"
    )


def test_notes_density_retry_warns_and_keeps_second_long_output(monkeypatch):
    _set_chinese_output_language(monkeypatch)
    segments = [{"start": 0, "end": 1, "text": "abcdefghij"}]
    client = FakeNotesClient(["中" * 9, "文" * 9])

    with patch(
        "video_transcript_api.llm.processors.notes_processor.logger.warning"
    ) as warning:
        result = NotesProcessor(client, _config()).process(
            chapters=_single_chapter_payload(segments),
            source_segments=segments,
        )

    assert result.status is NotesStatus.GENERATED
    assert result.text.endswith("文" * 9)
    warning.assert_called_once()
    warning_message = warning.call_args.args[0]
    assert "Notes chapter 0" in warning_message
    assert "ratio=0.900" in warning_message


def test_cache_source_loader_selects_structured_or_plain_path(tmp_path: Path):
    (tmp_path / "llm_processed.json").write_text(
        json.dumps({"dialogs": [{"text": "dialog"}]}), encoding="utf-8"
    )
    (tmp_path / "transcript_capswriter.json").write_text(
        json.dumps({"segments": [{"text": "plain"}]}), encoding="utf-8"
    )

    structured, structured_kind = load_notes_source_segments(tmp_path, "dialogs")
    plain, plain_kind = load_notes_source_segments(tmp_path, "segments")

    assert structured_kind == "dialogs"
    assert structured == [{"text": "dialog"}]
    assert plain_kind == "segments"
    assert plain == [{"start_time": None, "end_time": None, "text": "plain"}]


def test_fingerprint_mismatch_fails_without_llm_call():
    segments = _structured_segments()
    client = FakeNotesClient(["should not be used"])

    result = NotesProcessor(client, _config()).process(
        chapters=_chapter_payload(segments),
        source_segments=segments,
        stored_fingerprint="wrong-fingerprint",
    )

    assert result.status is NotesStatus.FAILED
    assert result.text is None
    assert result.error == "chapter anchor fingerprint mismatch"
    assert result.fingerprint == compute_notes_anchor_fingerprint(segments)
    assert client.calls == []


def test_one_chapter_failure_returns_no_partial_text():
    segments = _structured_segments()
    client = FakeNotesClient(["- first", RuntimeError("provider exploded")])
    progress = []

    # 同上：队列式响应分发只在串行下确定，这里锁的是"任一章失败即整体失败"。
    result = NotesProcessor(client, _config(notes_concurrency=1)).process(
        chapters=_chapter_payload(segments),
        source_segments=segments,
        progress_callback=lambda done, total: progress.append((done, total)),
    )

    assert result.status is NotesStatus.FAILED
    assert result.text is None
    assert result.error.startswith("chapter 1 notes generation failed: provider exploded")
    assert len(client.calls) == 2
    assert progress == [(1, 2)]


def test_notes_concurrency_keeps_chapter_order_when_completion_is_out_of_order(
    monkeypatch,
):
    _set_chinese_output_language(monkeypatch)
    segments, chapters = _chapter_batch(4)
    completed = []
    progress = []
    completion_events = [threading.Event() for _ in range(4)]
    notes_body = "中文笔记" * 8

    class ReverseCompletionNotesClient:
        def call(self, **kwargs):
            title = _chapter_title(kwargs["user_prompt"])
            chapter_index = int(title.rsplit(" ", 1)[1])
            if chapter_index + 1 < len(completion_events):
                assert completion_events[chapter_index + 1].wait(timeout=5), (
                    f"Timed out waiting for Chapter {chapter_index + 1}"
                )
            completed.append(title)
            completion_events[chapter_index].set()
            return f"- {title} notes {notes_body}"

    result = NotesProcessor(
        ReverseCompletionNotesClient(), _config(notes_concurrency=4)
    ).process(
        chapters=chapters,
        source_segments=segments,
        progress_callback=lambda done, total: progress.append((done, total)),
    )

    assert result.status is NotesStatus.GENERATED
    assert completed == ["Chapter 3", "Chapter 2", "Chapter 1", "Chapter 0"]
    assert progress == [(1, 4), (2, 4), (3, 4), (4, 4)]
    chapter_0 = f"Chapter 0\n- Chapter 0 notes {notes_body}"
    chapter_1 = f"Chapter 1\n- Chapter 1 notes {notes_body}"
    chapter_2 = f"Chapter 2\n- Chapter 2 notes {notes_body}"
    chapter_3 = f"Chapter 3\n- Chapter 3 notes {notes_body}"
    assert result.text.index(chapter_0) < result.text.index(chapter_1)
    assert result.text.index(chapter_1) < result.text.index(chapter_2)
    assert result.text.index(chapter_2) < result.text.index(chapter_3)


def test_notes_concurrency_does_not_exceed_configured_worker_limit(monkeypatch):
    _set_english_output_language(monkeypatch)
    notes_concurrency = 2
    segments, chapters = _chapter_batch(4)
    expected_concurrency = min(len(chapters["chapters"]), notes_concurrency)
    concurrent_barrier = threading.Barrier(expected_concurrency, timeout=5)
    active = 0
    peak = 0
    counter_lock = threading.Lock()

    class CountingNotesClient:
        def call(self, **kwargs):
            nonlocal active, peak
            with counter_lock:
                active += 1
                peak = max(peak, active)
            try:
                concurrent_barrier.wait()
                return "- notes"
            finally:
                with counter_lock:
                    active -= 1

    result = NotesProcessor(
        CountingNotesClient(), _config(notes_concurrency=notes_concurrency)
    ).process(chapters=chapters, source_segments=segments)

    assert result.status is NotesStatus.GENERATED
    assert peak <= notes_concurrency
    assert peak == expected_concurrency


def test_notes_concurrency_propagates_usage_context_to_workers(monkeypatch):
    _set_english_output_language(monkeypatch)
    segments, chapters = _chapter_batch(3)
    seen_contexts = []
    context_lock = threading.Lock()

    class ContextNotesClient:
        def call(self, **kwargs):
            with context_lock:
                seen_contexts.append(get_context())
            return "- notes"

    with set_context(task_id="task-123", stage="notes"):
        result = NotesProcessor(ContextNotesClient(), _config(notes_concurrency=3)).process(
            chapters=chapters,
            source_segments=segments,
        )

    assert result.status is NotesStatus.GENERATED
    assert seen_contexts == [
        {"task_id": "task-123", "stage": "notes"},
        {"task_id": "task-123", "stage": "notes"},
        {"task_id": "task-123", "stage": "notes"},
    ]


def test_empty_chapter_response_returns_failed_without_partial_text():
    segments, chapters = _chapter_batch(2)

    class EmptySecondNotesClient:
        def call(self, **kwargs):
            return "" if _chapter_title(kwargs["user_prompt"]) == "Chapter 1" else "- first"

    result = NotesProcessor(EmptySecondNotesClient(), _config(notes_concurrency=2)).process(
        chapters=chapters,
        source_segments=segments,
    )

    assert result.status is NotesStatus.FAILED
    assert result.text is None
    assert result.error == "chapter 1 notes response is empty"


# NOTE: 失败后 cancel 排队章节是 best-effort（见 notes_processor 的注释）：主线程
# cancel 与 worker 的 set_running_or_notify_cancel 本身就是竞态，任何断言"排队章节
# 未被执行"的测试都只是在赌调度顺序。失败语义由
# test_one_chapter_failure_returns_no_partial_text 锁定，这里不再断言取消行为。


def test_notes_concurrency_config_defaults_to_ten_and_parses_explicit_value():
    base = {
        "api_key": "k",
        "base_url": "u",
        "calibrate_model": "calibrate",
        "summary_model": "summary",
    }

    default_config = LLMConfig.from_dict({"llm": base})
    explicit_config = LLMConfig.from_dict(
        {"llm": {**base, "notes_concurrency": 3}}
    )

    assert default_config.notes_concurrency == 10
    assert explicit_config.notes_concurrency == 3


def test_notes_prompt_contract_contains_context_and_constraints():
    prompt = build_notes_user_prompt(
        chapter_title="本章",
        chapter_gist="本章概要",
        chapter_text="[00:00:01 - 00:00:02] Alice: 原话",
        previous_title="上一章",
        next_title="下一章",
    )

    assert "目标篇幅为输入正文的 40%-60%" in NOTES_SYSTEM_PROMPT_BASE
    assert "不使用 emoji" in NOTES_SYSTEM_PROMPT_BASE
    assert "输入每行格式为 `[时间戳] 说话人: 内容`" in NOTES_SYSTEM_PROMPT_SPEAKER
    assert "不得新增事实" in NOTES_SYSTEM_PROMPT_NO_SPEAKER
    assert "不得使用 \"Unknown\"" in NOTES_SYSTEM_PROMPT_NO_SPEAKER
    assert "生成本章精读笔记" in prompt
    assert "本章标题：本章" in prompt
    assert "本章概要：本章概要" in prompt
    assert "上一章标题：上一章" in prompt
    assert "下一章标题：下一章" in prompt
    assert "[00:00:01 - 00:00:02] Alice: 原话" in prompt
