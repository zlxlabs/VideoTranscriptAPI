#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Unit tests for the FunASR async-polling client (long-queue adaptation).

These tests pin the new client contract against the funASR server's
"async polling contract + admission control" update:

  upload --> task_id --> task_status_batch polling (drop tolerant)
  queue_full       -> back off per retry_after, full resubmit (no fatal)
  task_expired/... -> poll-miss, resubmit by file_hash
  connection drop  -> reconnect & resume polling, NO re-upload
  terminal set     -> completed/failed/timed_out/cancelled all recognized
  total deadline   -> hard cap across all phases/retries

The websocket layer is faked (FakeWS) and the clock is faked (FakeClock)
so no real sleeping or network happens. All console logging is English only.
"""

import json
import asyncio

import pytest

from video_transcript_api.transcriber import funasr_client as fc
from video_transcript_api.transcriber.funasr_client import (
    FunASRSpeakerClient,
    FunASRFatal,
    FunASRTimeout,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
WELCOME = {"type": "connected", "data": {"message": "ok"}}


class FakeWS:
    """Scripted websocket. recv() pops the next scripted item.

    A scripted item that is an Exception (instance or class) is raised,
    modelling a mid-stream connection drop. Anything else is JSON-encoded.
    """

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if not self.incoming:
            raise ConnectionError("FakeWS: no more scripted messages")
        item = self.incoming.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        return json.dumps(item)

    async def close(self):
        self.closed = True

    @property
    def sent_types(self):
        return [m.get("type") for m in self.sent]


class FakeClock:
    """Deterministic clock; sleeping advances virtual time instead of waiting."""

    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps = []

    def now(self):
        return self.t

    async def sleep(self, duration):
        self.sleeps.append(duration)
        self.t += max(duration, 0)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
DEFAULT_CFG = {
    "funasr_spk_server": {
        "server_url": "ws://fake:8767",
        "max_retries": 3,
        "retry_delay": 5,
        "connection_timeout": 30,
        "poll_interval": 8,
        "poll_recv_timeout": 60,
        "total_timeout": 3600,
        "first_delay_fallback": 5,
    }
}


def make_client(monkeypatch, sessions, clock, cfg=None):
    """Build a client wired to scripted FakeWS sessions and a fake clock.

    `sessions` is either a list of FakeWS (returned in order on each connect)
    or a zero-arg factory returning a fresh FakeWS on each connect.
    """
    monkeypatch.setattr(fc, "load_config", lambda: cfg or DEFAULT_CFG)

    if callable(sessions):
        factory = sessions
    else:
        seq = list(sessions)

        def factory():
            return seq.pop(0)

    created = []

    async def fake_connect(*args, **kwargs):
        ws = factory()
        created.append(ws)
        return ws

    monkeypatch.setattr(fc.websockets, "connect", fake_connect)
    monkeypatch.setattr(fc.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(fc.time, "time", clock.now)

    client = FunASRSpeakerClient()
    return client, created


def run(client, audio_path):
    return asyncio.run(client.transcribe_with_speaker_recognition(str(audio_path)))


def small_file(tmp_path):
    p = tmp_path / "audio.mp3"
    p.write_bytes(b"x" * 1024)  # < 5MB -> single upload
    return p


def big_file(tmp_path):
    p = tmp_path / "audio_big.mp3"
    p.write_bytes(b"y" * (6 * 1024 * 1024))  # > 5MB -> chunked upload (6 chunks)
    return p


def result_payload(text="hi"):
    return {"segments": [{"speaker": "A", "text": text}], "speakers": ["A"]}


def batch(items):
    return {"type": "task_status_batch", "data": {"items": items}}


# --------------------------------------------------------------------------- #
# T1 - cache hit at upload_request
# --------------------------------------------------------------------------- #
def test_cache_hit_returns_immediately(monkeypatch, tmp_path):
    clock = FakeClock()
    ws = FakeWS([WELCOME, {"type": "task_complete", "data": {"result": result_payload()}}])
    client, _ = make_client(monkeypatch, [ws], clock)

    out = run(client, small_file(tmp_path))

    assert out == result_payload()
    assert ws.sent_types == ["upload_request"]  # no upload_data sent


# --------------------------------------------------------------------------- #
# T2 - single upload happy path -> poll -> completed
# --------------------------------------------------------------------------- #
def test_single_upload_then_poll_completed(monkeypatch, tmp_path):
    clock = FakeClock()
    ws = FakeWS([
        WELCOME,
        {"type": "upload_ready", "data": {"task_id": "t1"}},
        {"type": "upload_complete", "data": {}},
        batch([{"task_id": "t1", "status": "completed", "result": result_payload("done")}]),
    ])
    client, _ = make_client(monkeypatch, [ws], clock)

    out = run(client, small_file(tmp_path))

    assert out == result_payload("done")
    assert "upload_request" in ws.sent_types
    assert "upload_data" in ws.sent_types
    assert "task_status_batch" in ws.sent_types
    # the batch request carried our task_id
    batch_req = next(m for m in ws.sent if m["type"] == "task_status_batch")
    assert batch_req["data"]["task_ids"] == ["t1"]


# --------------------------------------------------------------------------- #
# T3 - chunked upload happy path
# --------------------------------------------------------------------------- #
def test_chunked_upload_then_poll_completed(monkeypatch, tmp_path):
    clock = FakeClock()
    chunk_acks = [
        {"type": "chunk_received", "data": {"progress": (i + 1) / 6 * 100}}
        for i in range(6)
    ]
    ws = FakeWS([
        WELCOME,
        {"type": "upload_ready", "data": {"task_id": "tc"}},
        *chunk_acks,
        {"type": "upload_complete", "data": {}},
        batch([{"task_id": "tc", "status": "completed", "result": result_payload("chunk")}]),
    ])
    client, _ = make_client(monkeypatch, [ws], clock)

    out = run(client, big_file(tmp_path))

    assert out == result_payload("chunk")
    assert ws.sent_types.count("upload_chunk") == 6


# --------------------------------------------------------------------------- #
# T4 - queue_full -> back off per retry_after -> full resubmit -> success
# --------------------------------------------------------------------------- #
def test_queue_full_backoff_and_resubmit(monkeypatch, tmp_path):
    clock = FakeClock()
    s1 = FakeWS([
        WELCOME,
        {"type": "upload_ready", "data": {"task_id": "t1"}},
        {"type": "queue_full", "data": {"retry_after": 30, "queue_size": 50, "max_queue_size": 50}},
    ])
    s2 = FakeWS([
        WELCOME,
        {"type": "upload_ready", "data": {"task_id": "t2"}},
        {"type": "upload_complete", "data": {}},
        batch([{"task_id": "t2", "status": "completed", "result": result_payload("ok")}]),
    ])
    client, _ = make_client(monkeypatch, [s1, s2], clock)

    out = run(client, small_file(tmp_path))

    assert out == result_payload("ok")
    assert 30 in clock.sleeps  # honored retry_after
    # full resubmit happened on the second connection
    assert "upload_request" in s2.sent_types
    assert "upload_data" in s2.sent_types


# --------------------------------------------------------------------------- #
# T5 - task_queued estimated_wait_seconds drives first poll delay (clamped)
# --------------------------------------------------------------------------- #
def test_estimated_wait_seconds_drives_first_delay(monkeypatch, tmp_path):
    clock = FakeClock()
    ws = FakeWS([
        WELCOME,
        {"type": "upload_ready", "data": {"task_id": "tq"}},
        {"type": "task_queued", "data": {"task_id": "tq", "queue_position": 3,
                                         "estimated_wait_seconds": 50}},
        batch([{"task_id": "tq", "status": "completed", "result": result_payload()}]),
    ])
    client, _ = make_client(monkeypatch, [ws], clock)

    run(client, small_file(tmp_path))

    assert 50 in clock.sleeps  # first_delay taken from estimated_wait_seconds


# --------------------------------------------------------------------------- #
# T6 - terminal failure states raise (no infinite polling)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("terminal", ["failed", "timed_out", "cancelled"])
def test_terminal_states_raise(monkeypatch, tmp_path, terminal):
    clock = FakeClock()
    ws = FakeWS([
        WELCOME,
        {"type": "upload_ready", "data": {"task_id": "tf"}},
        {"type": "upload_complete", "data": {}},
        batch([{"task_id": "tf", "status": terminal, "error": "boom"}]),
    ])
    client, _ = make_client(monkeypatch, [ws], clock)

    with pytest.raises(FunASRFatal):
        run(client, small_file(tmp_path))


# --------------------------------------------------------------------------- #
# T7 - poll-miss (task_expired) -> resubmit by file_hash -> cache hit
# --------------------------------------------------------------------------- #
def test_poll_miss_resubmits_by_hash(monkeypatch, tmp_path):
    clock = FakeClock()
    s1 = FakeWS([
        WELCOME,
        {"type": "upload_ready", "data": {"task_id": "t1"}},
        {"type": "upload_complete", "data": {}},
        batch([{"task_id": "t1", "status": None, "error": "task_expired"}]),
    ])
    s2 = FakeWS([
        WELCOME,
        {"type": "task_complete", "data": {"result": result_payload("cached")}},
    ])
    client, _ = make_client(monkeypatch, [s1, s2], clock)

    out = run(client, small_file(tmp_path))

    assert out == result_payload("cached")
    assert s2.sent_types[0] == "upload_request"  # resubmitted by hash


# --------------------------------------------------------------------------- #
# T8 - connection drop mid-poll -> reconnect & resume poll, NO re-upload
# --------------------------------------------------------------------------- #
def test_drop_mid_poll_resumes_without_reupload(monkeypatch, tmp_path):
    clock = FakeClock()
    s1 = FakeWS([
        WELCOME,
        {"type": "upload_ready", "data": {"task_id": "t1"}},
        {"type": "upload_complete", "data": {}},
        ConnectionError("dropped during poll"),
    ])
    s2 = FakeWS([
        WELCOME,
        batch([{"task_id": "t1", "status": "completed", "result": result_payload("resumed")}]),
    ])
    client, _ = make_client(monkeypatch, [s1, s2], clock)

    out = run(client, small_file(tmp_path))

    assert out == result_payload("resumed")
    # crucial: second session only polled, never re-uploaded
    assert "upload_request" not in s2.sent_types
    assert "upload_data" not in s2.sent_types
    assert s2.sent_types == ["task_status_batch"]


# --------------------------------------------------------------------------- #
# T9 - total_timeout is a hard cap across queue_full retries
# --------------------------------------------------------------------------- #
def test_total_timeout_raises(monkeypatch, tmp_path):
    clock = FakeClock()
    cfg = json_cfg(total_timeout=100)

    def factory():
        return FakeWS([
            WELCOME,
            {"type": "upload_ready", "data": {"task_id": "t"}},
            {"type": "queue_full", "data": {"retry_after": 30}},
        ])

    client, _ = make_client(monkeypatch, factory, clock, cfg=cfg)

    with pytest.raises(FunASRTimeout):
        run(client, small_file(tmp_path))


# --------------------------------------------------------------------------- #
# T10 - stray pushes during poll are tolerated
# --------------------------------------------------------------------------- #
def test_stray_push_during_poll_tolerated(monkeypatch, tmp_path):
    clock = FakeClock()
    ws = FakeWS([
        WELCOME,
        {"type": "upload_ready", "data": {"task_id": "t1"}},
        {"type": "upload_complete", "data": {}},
        {"type": "task_progress", "data": {"progress": 42, "status": "processing"}},
        batch([{"task_id": "t1", "status": "completed", "result": result_payload("late")}]),
    ])
    client, _ = make_client(monkeypatch, [ws], clock)

    out = run(client, small_file(tmp_path))

    assert out == result_payload("late")
    assert ws.sent_types.count("task_status_batch") >= 2


# --------------------------------------------------------------------------- #
# T11 - regression for the :272 KeyError (estimated_wait_minutes legacy/missing)
# --------------------------------------------------------------------------- #
def test_legacy_estimated_wait_minutes_no_keyerror(monkeypatch, tmp_path):
    clock = FakeClock()
    ws = FakeWS([
        WELCOME,
        {"type": "upload_ready", "data": {"task_id": "t1"}},
        # only the OLD field present, in seconds-less form -> must not KeyError
        {"type": "task_queued", "data": {"task_id": "t1", "queue_position": 2,
                                         "estimated_wait_minutes": 5}},
        batch([{"task_id": "t1", "status": "completed", "result": result_payload()}]),
    ])
    client, _ = make_client(monkeypatch, [ws], clock)

    out = run(client, small_file(tmp_path))

    assert out == result_payload()
    # 5 minutes -> 300s, clamped to the 120s ceiling
    assert 120 in clock.sleeps


def test_queue_ack_without_any_wait_field(monkeypatch, tmp_path):
    clock = FakeClock()
    ws = FakeWS([
        WELCOME,
        {"type": "upload_ready", "data": {"task_id": "t1"}},
        {"type": "task_queued", "data": {"task_id": "t1"}},  # no wait field at all
        batch([{"task_id": "t1", "status": "completed", "result": result_payload()}]),
    ])
    client, _ = make_client(monkeypatch, [ws], clock)

    out = run(client, small_file(tmp_path))
    assert out == result_payload()
    assert 5 in clock.sleeps  # first_delay_fallback


# --------------------------------------------------------------------------- #
# T12 - sparse completed item (defensive .get, no KeyError)
# --------------------------------------------------------------------------- #
def test_sparse_completed_item_no_keyerror(monkeypatch, tmp_path):
    clock = FakeClock()
    ws = FakeWS([
        WELCOME,
        {"type": "upload_ready", "data": {"task_id": "t1"}},
        {"type": "upload_complete", "data": {}},
        # completed item missing progress/srt_content/error keys
        batch([{"task_id": "t1", "status": "completed", "result": result_payload()}]),
    ])
    client, _ = make_client(monkeypatch, [ws], clock)

    out = run(client, small_file(tmp_path))
    assert out == result_payload()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def json_cfg(**overrides):
    cfg = {"funasr_spk_server": dict(DEFAULT_CFG["funasr_spk_server"])}
    cfg["funasr_spk_server"].update(overrides)
    return cfg
