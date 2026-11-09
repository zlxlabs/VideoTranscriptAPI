"""Output-language contract tests for chapter generation.

All model responses are mocked at the LLMClient.call() boundary; no real
network calls are made. Console-visible test output uses English only.
"""

from io import StringIO
from unittest.mock import Mock, patch

from video_transcript_api.llm.core.config import LLMConfig
from video_transcript_api.llm.processors import chapters_processor
from video_transcript_api.utils.llm_status import ChaptersStatus


def _processor_for_response(chapters):
    response = Mock()
    response.structured_output = {"chapters": chapters}
    client = Mock()
    client.call.return_value = response
    config = LLMConfig(
        api_key="test-key",
        base_url="https://llm.invalid",
        calibrate_model="test-model",
        summary_model="test-model",
        chapters_model="test-model",
        min_chapters_threshold=1,
    )
    return chapters_processor.ChaptersProcessor(client, config), client


def _segments():
    return [
        {"text": "Transcript segment one.", "start_time": 0, "end_time": 120},
        {"text": "Transcript segment two.", "start_time": 120, "end_time": 240},
    ]


def test_english_request_rejects_chinese_chapters_without_retry():
    chapters = [
        {
            "title": "项目背景与目标",
            "gist": "本章节介绍项目启动的背景，并说明团队希望解决的核心问题。",
            "start_seg": 0,
        },
        {
            "title": "方案设计与实施",
            "gist": "本章节说明团队采用的实施方案，以及各阶段的具体安排。",
            "start_seg": 1,
        },
    ]
    processor, client = _processor_for_response(chapters)
    captured_log = StringIO()
    sink_id = chapters_processor.logger.add(captured_log, format="{message}")
    try:
        with patch(
            "video_transcript_api.llm.prompts.localized.get_language",
            return_value="en",
        ):
            result = processor.process(_segments(), title="Video")
    finally:
        chapters_processor.logger.remove(sink_id)

    assert result.status is ChaptersStatus.FAILED
    assert result.chapters == []
    assert "CHAPTERS-LANGUAGE-MISMATCH" in result.error
    client.call.assert_called_once()
    assert "Write every chapter title and gist in English" in client.call.call_args.kwargs[
        "system_prompt"
    ]
    assert "CHAPTERS-LANGUAGE-MISMATCH" in captured_log.getvalue()


def test_english_request_accepts_english_chapters():
    chapters = [
        {
            "title": "Project goals",
            "gist": "This chapter explains the project's goals and the problem the team aims to solve.",
            "start_seg": 0,
        },
        {
            "title": "Implementation plan",
            "gist": "This chapter describes the proposed approach and the steps for carrying it out.",
            "start_seg": 1,
        },
    ]
    processor, client = _processor_for_response(chapters)

    with patch(
        "video_transcript_api.llm.prompts.localized.get_language",
        return_value="en",
    ):
        result = processor.process(_segments(), title="Video")

    assert result.status is ChaptersStatus.GENERATED
    assert [chapter.title for chapter in result.chapters] == [
        "Project goals",
        "Implementation plan",
    ]
    client.call.assert_called_once()
