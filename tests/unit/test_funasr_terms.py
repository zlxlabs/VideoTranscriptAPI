"""FunASR terms capability gate and upload contract tests."""

import asyncio
import json

import pytest
import requests
from loguru import logger

from video_transcript_api.transcriber import funasr_client as fc
from video_transcript_api.transcriber.funasr_client import FunASRSpeakerClient


CAPS = {
    "schema_version": 1,
    "features": {"terms": True},
    "capability_id": "runtime-a",
}
RESULT = {
    "segments": [{"speaker": "A", "text": "done"}],
    "speakers": ["A"],
    "metadata": {"context_applied": True, "terms_count": 1},
}


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    async def sleep(self, duration):
        self.sleeps.append(duration)
        self.t += max(duration, 0)


class FakeWS:
    def __init__(self, incoming, events):
        self.incoming = list(incoming)
        self.events = events
        self.sent = []

    async def send(self, message):
        parsed = json.loads(message)
        self.sent.append(parsed)
        self.events.append(f"send:{parsed['type']}")

    async def recv(self):
        item = self.incoming.pop(0)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)

    async def close(self):
        pass

    @property
    def sent_types(self):
        return [message["type"] for message in self.sent]


class FakeHTTPResponse:
    def __init__(self, payload=None, status_error=None, json_error=False):
        self.payload = payload
        self.status_error = status_error
        self.json_error = json_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def json(self):
        if self.json_error:
            raise ValueError("not json")
        return self.payload


@pytest.fixture
def log_messages():
    messages = []
    sink_id = logger.add(lambda message: messages.append(str(message)), format="{level} {message}")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def welcome(capabilities=CAPS, include_capabilities=True):
    data = {"message": "ok"}
    if include_capabilities:
        data["capabilities"] = capabilities
    return {"type": "connected", "data": data}


def batch(task_id="t1", result=RESULT):
    return {
        "type": "task_status_batch",
        "data": {"items": [{"task_id": task_id, "status": "completed", "result": result}]},
    }


def uploaded_session(events, task_id="t1", result=RESULT, welcome_message=None):
    return FakeWS([
        welcome_message or welcome(),
        {"type": "upload_ready", "data": {"task_id": task_id}},
        {"type": "upload_complete", "data": {}},
        batch(task_id, result),
    ], events)


def make_client(monkeypatch, tmp_path, terms, sessions, http_responses=None, events=None):
    clock = FakeClock()
    events = events if events is not None else []
    http_calls = []
    responses = list(http_responses or [FakeHTTPResponse(CAPS)])
    monkeypatch.setattr(fc, "load_config", lambda: {
        "funasr_spk_server": {
            "server_url": "ws://fake:8767",
            "max_retries": 3,
            "retry_delay": 1,
            "poll_interval": 1,
            "poll_recv_timeout": 60,
            "total_timeout": 3600,
            "first_delay_fallback": 1,
        }
    })
    monkeypatch.setattr(
        fc,
        "get_terminology_db",
        lambda: type("Terms", (), {"export_correct_terms": lambda self: terms})(),
    )
    session_queue = list(sessions)

    async def fake_connect(*args, **kwargs):
        return session_queue.pop(0)

    monkeypatch.setattr(fc.websockets, "connect", fake_connect)
    monkeypatch.setattr(fc.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(fc.time, "time", clock.now)

    def fake_get(url, timeout):
        http_calls.append((url, timeout))
        events.append("get:capabilities")
        response = responses.pop(0) if responses else FakeHTTPResponse(CAPS)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(fc.requests, "get", fake_get)
    client = FunASRSpeakerClient()
    client._test_http_calls = http_calls
    client._test_events = events
    return client


def run(client, path):
    return asyncio.run(client.transcribe_with_speaker_recognition(str(path)))


def audio_file(tmp_path, size=1024):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"a" * size)
    return path


def test_supported_terms_are_sent_as_correct_spellings_with_metadata(monkeypatch, tmp_path):
    events = []
    result = {
        "segments": [{"speaker": "A", "text": "done"}],
        "speakers": ["A"],
        "metadata": {"context_applied": True, "terms_count": 2},
    }
    ws = uploaded_session(events, result=result)
    client = make_client(
        monkeypatch,
        tmp_path,
        ["Ａlpha   Beta", "Alpha Beta", "Gamma"],
        [ws],
        events=events,
    )

    output = run(client, audio_file(tmp_path))

    request = next(message for message in ws.sent if message["type"] == "upload_request")
    assert request["data"]["terms"] == ["Alpha Beta", "Gamma"]
    assert "incorrect" not in request["data"]
    assert client._test_http_calls == [("http://fake:8767/capabilities", 5)]
    assert output["metadata"] == {"context_applied": True, "terms_count": 2}


@pytest.mark.parametrize(
    "side,defect,reason",
    [
        ("http", "missing", "capabilities_missing"),
        ("ws", "missing", "capabilities_missing"),
        ("http", "404", "capabilities_http_error"),
        ("http", "timeout", "capabilities_http_error"),
        ("http", "non_json", "capabilities_http_non_json"),
        ("ws", "non_json", "capabilities_missing"),
        ("http", "schema", "schema_version_unsupported"),
        ("ws", "schema", "schema_version_unsupported"),
        ("http", "terms_false", "terms_disabled"),
        ("ws", "terms_false", "terms_disabled"),
        ("http", "id_mismatch", "capability_id_mismatch"),
        ("ws", "id_mismatch", "capability_id_mismatch"),
    ],
)
def test_capability_failures_omit_terms_and_complete(
    monkeypatch, tmp_path, log_messages, side, defect, reason
):
    http_caps = dict(CAPS)
    http_caps["features"] = dict(CAPS["features"])
    ws_caps = dict(CAPS)
    ws_caps["features"] = dict(CAPS["features"])
    response = FakeHTTPResponse(http_caps)
    include_ws = True

    target = http_caps if side == "http" else ws_caps
    if defect == "missing":
        if side == "http":
            target = {}
            response = FakeHTTPResponse(target)
        else:
            include_ws = False
    elif defect == "404":
        response = FakeHTTPResponse(status_error=requests.HTTPError("404"))
    elif defect == "timeout":
        response = requests.Timeout("timeout")
    elif defect == "non_json":
        if side == "http":
            response = FakeHTTPResponse(json_error=True)
        else:
            ws_caps = "not-a-capability-object"
    elif defect == "schema":
        target["schema_version"] = 9
    elif defect == "terms_false":
        target["features"]["terms"] = False
    elif defect == "id_mismatch":
        target["capability_id"] = "different-runtime"

    events = []
    ws = uploaded_session(
        events,
        result={"segments": [{"speaker": "A", "text": "done"}], "speakers": ["A"]},
        welcome_message=welcome(ws_caps, include_capabilities=include_ws),
    )
    client = make_client(
        monkeypatch,
        tmp_path,
        ["SecretProperNoun"],
        [ws],
        http_responses=[response],
        events=events,
    )

    output = run(client, audio_file(tmp_path))

    request = next(message for message in ws.sent if message["type"] == "upload_request")
    assert "terms" not in request["data"]
    assert output["segments"][0]["text"] == "done"
    assert any(f"terms_not_supported reason={reason}" in line for line in log_messages)
    assert "SecretProperNoun" not in "".join(log_messages)


@pytest.mark.parametrize(
    "raw_terms,expected_count,expected_dropped",
    [
        ([f"SecretTerm{i}" for i in range(51)], 50, 1),
        (["SecretLong" * 8, "SecretValid"], 1, 1),
        ([f"Term{i:02d}" + "x" * 34 for i in range(30)], 25, 5),
    ],
)
def test_terms_are_clipped_before_upload_and_log_counts_only(
    monkeypatch, tmp_path, log_messages, raw_terms, expected_count, expected_dropped
):
    result = {
        "segments": [{"speaker": "A", "text": "done"}],
        "speakers": ["A"],
        "metadata": {"context_applied": True, "terms_count": expected_count},
    }
    events = []
    ws = uploaded_session(events, result=result)
    client = make_client(monkeypatch, tmp_path, raw_terms, [ws], events=events)

    run(client, audio_file(tmp_path))

    terms = next(message for message in ws.sent if message["type"] == "upload_request")["data"]["terms"]
    assert len(terms) == expected_count
    assert len(terms) <= 50
    assert all(len(term) <= 64 for term in terms)
    assert sum(map(len, terms)) <= 1024
    assert any(
        f"terms_clipped dropped_count={expected_dropped} " in line
        for line in log_messages
    )
    assert not any(term in "".join(log_messages) for term in raw_terms)


def test_terms_metadata_mismatch_logs_error_and_returns_result(monkeypatch, tmp_path, log_messages):
    bad_result = {
        "segments": [{"speaker": "A", "text": "done"}],
        "speakers": ["A"],
        "metadata": {"context_applied": False, "terms_count": 0},
    }
    ws = uploaded_session([], result=bad_result)
    client = make_client(monkeypatch, tmp_path, ["SecretTerm"], [ws])

    output = run(client, audio_file(tmp_path))

    assert output == bad_result
    assert any("ERROR terms_metadata_mismatch" in line for line in log_messages)
    assert "SecretTerm" not in "".join(log_messages)


def test_queue_full_chunk_finalize_keeps_terms_and_does_not_reupload_bytes(
    monkeypatch, tmp_path
):
    events = []
    chunk_acks = [
        {"type": "chunk_received", "data": {"progress": (index + 1) * 100 / 6}}
        for index in range(6)
    ]
    ws = FakeWS([
        welcome(),
        {"type": "upload_ready", "data": {"task_id": "tc"}},
        *chunk_acks,
        {"type": "queue_full", "data": {"retry_after": 2}},
        {"type": "upload_complete", "data": {}},
        batch("tc", {
            "segments": [{"speaker": "A", "text": "done"}],
            "speakers": ["A"],
            "metadata": {"context_applied": True, "terms_count": 1},
        }),
    ], events)
    client = make_client(monkeypatch, tmp_path, ["SecretTerm"], [ws], events=events)

    run(client, audio_file(tmp_path, 5 * 1024 * 1024 + 1))

    upload = next(message for message in ws.sent if message["type"] == "upload_request")
    assert upload["data"]["terms"] == ["SecretTerm"]
    assert ws.sent_types.count("finalize_upload") == 1
    assert ws.sent_types.count("upload_chunk") == 6
    assert ws.sent_types.count("upload_request") == 1


def test_queue_full_resubmit_reprobes_before_resending_same_terms(monkeypatch, tmp_path):
    events = []
    first = FakeWS([
        welcome(),
        {"type": "queue_full", "data": {"retry_after": 2}},
    ], events)
    second = uploaded_session(events, task_id="t2")
    client = make_client(
        monkeypatch,
        tmp_path,
        ["SecretTerm"],
        [first, second],
        http_responses=[FakeHTTPResponse(CAPS), FakeHTTPResponse(CAPS)],
        events=events,
    )

    run(client, audio_file(tmp_path))

    assert client._test_http_calls == [
        ("http://fake:8767/capabilities", 5),
        ("http://fake:8767/capabilities", 5),
    ]
    requests_sent = [
        message for message in first.sent + second.sent
        if message["type"] == "upload_request"
    ]
    assert [message["data"]["terms"] for message in requests_sent] == [
        ["SecretTerm"], ["SecretTerm"]
    ]
    get_indices = [i for i, event in enumerate(events) if event == "get:capabilities"]
    upload_indices = [i for i, event in enumerate(events) if event == "send:upload_request"]
    assert len(get_indices) == len(upload_indices) == 2
    assert get_indices[1] < upload_indices[1]


def test_existing_task_reconnect_only_polls_without_reprobe_or_reupload(monkeypatch, tmp_path):
    events = []
    first = FakeWS([
        welcome(),
        {"type": "upload_ready", "data": {"task_id": "t1"}},
        {"type": "upload_complete", "data": {}},
        ConnectionError("dropped"),
    ], events)
    second = FakeWS([
        welcome(include_capabilities=False),
        batch("t1", {
            "segments": [{"speaker": "A", "text": "resumed"}],
            "speakers": ["A"],
            "metadata": {"context_applied": True, "terms_count": 1},
        }),
    ], events)
    client = make_client(
        monkeypatch,
        tmp_path,
        ["SecretTerm"],
        [first, second],
        http_responses=[FakeHTTPResponse(CAPS)],
        events=events,
    )

    output = run(client, audio_file(tmp_path))

    assert output["segments"][0]["text"] == "resumed"
    assert len(client._test_http_calls) == 1
    assert second.sent_types == ["task_status_batch"]
