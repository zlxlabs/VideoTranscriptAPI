import asyncio
import time

import pytest

from video_transcript_api.transcriber.funasr_client import (
    FunASRFatal,
    FunASRPollMiss,
    FunASRSpeakerClient,
)


def client():
    return object.__new__(FunASRSpeakerClient)


def batch(items):
    return {"type": "task_status_batch", "data": {"items": items}}


def test_matching_completed_item_returns_result():
    result = {"segments": [{"text": "bound result"}]}

    actual = client()._handle_poll_message(
        batch([
            {"task_id": "ours", "status": "completed", "result": result},
            {"task_id": "other", "status": "completed", "result": {"wrong": True}},
        ]),
        "ours",
    )

    assert actual == result


def test_matching_completed_item_preserves_srt_content():
    srt = "1\n00:00:00,000 --> 00:00:01,000\nhello\n"

    actual = client()._handle_poll_message(
        batch([{"task_id": "ours", "status": "completed", "result": {}, "srt_content": srt}]),
        "ours",
    )

    assert actual == srt


def test_matching_completed_item_with_empty_result_is_fatal():
    with pytest.raises(FunASRFatal, match="completed.*empty"):
        client()._handle_poll_message(
            batch([{"task_id": "ours", "status": "completed", "result": {}, "srt_content": ""}]),
            "ours",
        )


@pytest.mark.parametrize("error", ["task_not_found", "task_expired"])
def test_explicit_poll_miss_remains_retryable(error):
    with pytest.raises(FunASRPollMiss, match=error):
        client()._handle_poll_message(
            batch([{"task_id": "ours", "status": None, "error": error}]),
            "ours",
        )


@pytest.mark.parametrize("items", [[], [{"task_id": "other", "status": "completed", "result": {"wrong": True}}]])
def test_batch_without_our_task_is_protocol_error(items):
    with pytest.raises(FunASRFatal, match="FUNASR_PROTOCOL_ERROR"):
        client()._handle_poll_message(batch(items), "ours")


@pytest.mark.parametrize(
    ("status", "error"),
    [("queued_unknown", None), ("new_server_state", None), (None, None), (None, "unknown_error")],
)
def test_unknown_status_is_protocol_error(status, error):
    with pytest.raises(FunASRFatal, match="FUNASR_PROTOCOL_ERROR"):
        client()._handle_poll_message(
            batch([{"task_id": "ours", "status": status, "error": error}]), "ours"
        )


@pytest.mark.parametrize("status", ["pending", "processing"])
def test_pending_status_continues_polling(status):
    assert client()._handle_poll_message(
        batch([{"task_id": "ours", "status": status}]), "ours"
    ) is None


def test_task_complete_push_for_other_task_is_ignored_until_our_result():
    instance = client()
    instance.poll_interval = 0
    instance.poll_recv_timeout = 1
    result = {"segments": [{"text": "ours"}]}
    responses = [
        {"type": "task_complete", "data": {"task_id": "other", "result": {"wrong": True}}},
        {"type": "task_complete", "data": {"task_id": "ours", "result": result}},
    ]
    requests = []

    async def send_message(message):
        requests.append(message)

    async def receive_message(timeout):
        return responses.pop(0)

    instance.send_message = send_message
    instance.receive_message = receive_message

    actual = asyncio.run(instance._poll("ours", 0, time.time() + 5))

    assert actual == result
    assert len(requests) == 2


@pytest.mark.parametrize("data", [{"result": {"unbound": True}}, {"task_id": None}])
def test_task_complete_push_without_task_id_is_protocol_error(data):
    with pytest.raises(FunASRFatal, match="FUNASR_PROTOCOL_ERROR"):
        client()._handle_poll_message({"type": "task_complete", "data": data}, "ours")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"type": "task_queued", "data": {"task_id": "ours"}}, "task"),
        ({"type": "task_complete", "data": {"task_id": "ours", "result": {"ok": True}}}, "result"),
    ],
)
def test_upload_completion_accepts_matching_task_id(response, expected):
    instance = client()
    instance.first_delay_fallback = 5

    outcome = instance._post_upload_outcome(response, "ours")

    assert outcome["kind"] == expected


@pytest.mark.parametrize(
    "response",
    [
        {"type": "task_queued", "data": {"task_id": "other"}},
        {"type": "task_queued", "data": {}},
        {"type": "task_complete", "data": {"task_id": "other", "result": {"wrong": True}}},
        {"type": "task_complete", "data": {"result": {"unbound": True}}},
    ],
)
def test_upload_completion_with_unbound_or_wrong_task_id_is_protocol_error(response):
    instance = client()
    instance.first_delay_fallback = 5

    with pytest.raises(FunASRFatal, match="FUNASR_PROTOCOL_ERROR"):
        instance._post_upload_outcome(response, "ours")


def test_initial_upload_cache_hit_still_needs_no_task_id(tmp_path):
    instance = client()
    instance._add_terms = lambda _request: None
    instance.send_message = _async_noop
    result = {"segments": [{"text": "cached"}]}
    instance.receive_message = _async_result({"type": "task_complete", "data": {"result": result}})
    path = tmp_path / "cached.wav"
    path.write_bytes(b"audio")

    outcome = asyncio.run(instance._upload_single(path, 5, "hash", "json", False))

    assert outcome == {"kind": "result", "result": result}


async def _async_noop(_message):
    return None


def _async_result(value):
    async def receive_message(*_args, **_kwargs):
        return value

    return receive_message
