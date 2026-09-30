#!/usr/bin/env python
# coding: utf-8

"""
CapsWriter 客户端重试逻辑测试

覆盖官方 SDK 的可重试错误、不可重试错误和确定性失败结果。
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

from capswriter_asr import AsrError, Transcript

from video_transcript_api.transcriber.capswriter_client import CapsWriterClient


def _make_client(max_retries=3, retry_delay=0):
    """Build a client without loading project configuration."""
    with patch.object(CapsWriterClient, "__init__", lambda self: None):
        client = CapsWriterClient()
    client.max_retries = max_retries
    client.retry_delay = retry_delay
    client.log = MagicMock()
    return client


def _successful_transcript():
    return Transcript(
        text="hello",
        tokens=["hello"],
        timestamps=[0.0],
        duration=1.0,
        raw={"task_id": "task-1"},
    )


def test_retryable_asr_error_reaches_max_retries():
    client = _make_client(max_retries=3, retry_delay=7)
    error = AsrError("overloaded", "try again", retryable=True)

    with patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        side_effect=error,
    ) as sdk_call, patch(
        "video_transcript_api.transcriber.capswriter_client.time.sleep"
    ) as sleep:
        success, files = client.transcribe_file("audio.mp4")

    assert (success, files) == (False, [])
    assert sdk_call.call_count == 3
    assert sleep.call_args_list == [call(7), call(7)]


def test_non_retryable_asr_error_calls_sdk_once_and_logs_code():
    client = _make_client(max_retries=5, retry_delay=7)
    error = AsrError("bad_request", "invalid request", retryable=False)

    with patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        side_effect=error,
    ) as sdk_call, patch(
        "video_transcript_api.transcriber.capswriter_client.time.sleep"
    ) as sleep:
        success, files = client.transcribe_file("audio.mp4")

    assert (success, files) == (False, [])
    assert sdk_call.call_count == 1
    sleep.assert_not_called()
    failure_logs = [
        entry.args[0]
        for entry in client.log.call_args_list
        if entry.args and "转录文件失败" in entry.args[0]
    ]
    assert failure_logs == [
        "转录文件失败: audio.mp4, code=bad_request, 原因: invalid request"
    ]


def test_non_asr_error_calls_sdk_once():
    client = _make_client(max_retries=5, retry_delay=7)

    with patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        side_effect=RuntimeError("unexpected failure"),
    ) as sdk_call, patch(
        "video_transcript_api.transcriber.capswriter_client.time.sleep"
    ) as sleep:
        success, files = client.transcribe_file("audio.mp4")

    assert (success, files) == (False, [])
    assert sdk_call.call_count == 1
    sleep.assert_not_called()


def test_success_returns_immediately_after_saving_results():
    client = _make_client(max_retries=3)
    client._save_results = AsyncMock(return_value=[Path("result.txt")])

    with patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        return_value=_successful_transcript(),
    ) as sdk_call, patch(
        "video_transcript_api.transcriber.capswriter_client.time.sleep"
    ) as sleep:
        success, files = client.transcribe_file("audio.mp4")

    assert (success, files) == (True, [Path("result.txt")])
    assert sdk_call.call_count == 1
    sleep.assert_not_called()


def test_missing_output_is_deterministic_failure_without_retry():
    client = _make_client(max_retries=5, retry_delay=7)
    client._save_results = AsyncMock(return_value=[])

    with patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        return_value=_successful_transcript(),
    ) as sdk_call, patch(
        "video_transcript_api.transcriber.capswriter_client.time.sleep"
    ) as sleep:
        success, files = client.transcribe_file("audio.mp4")

    assert (success, files) == (False, [])
    assert sdk_call.call_count == 1
    sleep.assert_not_called()
