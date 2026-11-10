"""Output-language contracts for generated summaries and notes.

All model responses are mocked at the LLM boundary. Test diagnostics contain
only English; Chinese strings below are fixtures for language detection.
"""

from io import StringIO
from unittest.mock import Mock, patch

import pytest

from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.core.llm_client import LLMResponse
from video_transcript_api.llm.processors import notes_processor, summary_processor
from video_transcript_api.llm.processors.notes_processor import (
    NotesProcessor,
    compute_notes_anchor_fingerprint,
)
from video_transcript_api.llm.processors.summary_processor import SummaryProcessor
from video_transcript_api.utils.llm_status import NotesStatus, SummaryStatus


def _config() -> LLMConfig:
    return LLMConfig(
        api_key="test-key",
        base_url="https://llm.invalid",
        calibrate_model="test-model",
        summary_model="test-model",
        notes_model="test-model",
        min_summary_threshold=1,
        notes_concurrency=1,
    )


def _summary_processor(*responses: str) -> tuple[SummaryProcessor, Mock]:
    client = Mock()
    client.call.side_effect = [LLMResponse(text=response) for response in responses]
    return SummaryProcessor(client, _config()), client


def _with_language(language: str, processor_module):
    return (
        patch.object(processor_module, "get_language", return_value=language),
        patch(
            "video_transcript_api.llm.prompts.localized.get_language",
            return_value=language,
        ),
    )


def test_english_request_rejects_chinese_summary_and_logs_token():
    # Fixture length must stay above the English S-band floor (600 chars) so
    # the outcome is decided by language validation, not the floor guard.
    processor, client = _summary_processor("这是一段中文总结内容，包含主要观点和详细说明。" * 30)
    captured_log = StringIO()
    sink_id = summary_processor.logger.add(captured_log, format="{message}")
    language_patches = _with_language("en", summary_processor)
    try:
        with language_patches[0], language_patches[1]:
            result = processor.process("transcript " * 100, title="Video")
    finally:
        summary_processor.logger.remove(sink_id)

    assert result.status is SummaryStatus.FAILED
    assert result.text is None
    assert client.call.call_count == 1
    assert "SUMMARY-LANGUAGE-MISMATCH" in captured_log.getvalue()


def test_chinese_request_rejects_english_and_accepts_chinese_summary():
    language_patches = _with_language("zh-CN", summary_processor)
    processor, client = _summary_processor(
        "This is an English summary with enough detail. " * 6
    )
    with language_patches[0], language_patches[1]:
        result = processor.process("transcript " * 100, title="Video")

    assert result.status is SummaryStatus.FAILED
    assert result.text is None
    assert client.call.call_count == 1

    # Above the Chinese S-band floor (250 chars) so the floor guard stays out.
    chinese_summary = "这是一段中文总结内容，包含主要观点和详细说明。" * 12
    processor, _ = _summary_processor(chinese_summary)
    language_patches = _with_language("zh-CN", summary_processor)
    with language_patches[0], language_patches[1]:
        accepted_result = processor.process("transcript " * 100, title="Video")

    assert accepted_result.status is SummaryStatus.GENERATED
    assert accepted_result.text == chinese_summary


def test_compression_retry_final_text_is_language_validated():
    first_answer = "An English summary. " * 400
    retry_answer = "压缩后的中文总结内容，保留所有重要信息。" * 5
    processor, client = _summary_processor(first_answer, retry_answer)
    language_patches = _with_language("en", summary_processor)
    with language_patches[0], language_patches[1]:
        result = processor.process("t" * 1000, title="Video")

    assert result.status is SummaryStatus.FAILED
    assert result.text is None
    assert client.call.call_count == 2


def test_over_budget_accepted_final_text_is_language_validated():
    first_answer = "An English summary. " * 400
    shorter_over_budget_retry = "中" * 5000
    processor, client = _summary_processor(first_answer, shorter_over_budget_retry)
    captured_log = StringIO()
    sink_id = summary_processor.logger.add(captured_log, format="{message}")
    language_patches = _with_language("en", summary_processor)
    try:
        with language_patches[0], language_patches[1]:
            result = processor.process("t" * 1000, title="Video")
    finally:
        summary_processor.logger.remove(sink_id)

    assert result.status is SummaryStatus.FAILED
    assert result.text is None
    assert client.call.call_count == 2
    assert "summary_over_budget_accepted" in captured_log.getvalue()
    assert "SUMMARY-LANGUAGE-MISMATCH" in captured_log.getvalue()


@pytest.mark.parametrize(
    ("language", "response"),
    [
        ("en", "这是一段详细笔记内容，包含本章的主要信息和结论。" * 5),
        ("zh-CN", "This is detailed chapter notes with the main information and conclusions. " * 3),
    ],
)
def test_notes_language_mismatch_is_failed_and_logged(language, response):
    segments = [
        {"start_time": 0, "end_time": 10, "text": "source sentence " * 30}
    ]
    chapters = {
        "source": {
            "kind": "segments",
            "fingerprint": compute_notes_anchor_fingerprint(segments),
        },
        "chapters": [
            {
                "title": "Opening",
                "gist": "Opening topic",
                "start_seg": 0,
                "end_seg": 0,
            }
        ],
    }
    client = Mock()
    client.call.return_value = response
    captured_log = StringIO()
    sink_id = notes_processor.logger.add(captured_log, format="{message}")
    language_patches = _with_language(language, notes_processor)
    try:
        with language_patches[0], language_patches[1]:
            result = NotesProcessor(client, _config()).process(
                chapters=chapters,
                source_segments=segments,
                source_kind="segments",
            )
    finally:
        notes_processor.logger.remove(sink_id)

    assert result.status is NotesStatus.FAILED
    assert result.text is None
    assert "NOTES-LANGUAGE-MISMATCH" in result.error
    assert "NOTES-LANGUAGE-MISMATCH" in captured_log.getvalue()
    client.call.assert_called_once()
