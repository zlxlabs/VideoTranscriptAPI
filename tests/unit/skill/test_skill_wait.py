"""Tests for the skill CLI `wait` subcommand (notify-redesign C3).

These tests assert what the script actually puts on the wire: the HTTP
method, full URL (path + query), and the client-side timeout passed to
urlopen -- not internal helper functions. urllib is intercepted at the
module boundary the script itself uses (urllib.request.urlopen).

All console output must be in English only (no emoji, no Chinese).
"""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

_SKILL_SCRIPT = (
    Path(__file__).resolve().parents[3] / "skill" / "scripts" / "videotranscript.py"
)


def _load_skill_module():
    spec = importlib.util.spec_from_file_location(
        "videotranscript_skill_under_test", _SKILL_SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeResponse:
    """Minimal urllib response: context manager returning a JSON body."""

    def __init__(self, status_code, payload):
        self._status_code = status_code
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def getcode(self):
        return self._status_code

    def read(self):
        return self._body


def _resp_processing():
    """Real server shape: HTTP layer stays 200; business code lives in body.code."""
    return _FakeResponse(
        200,
        {"code": 202, "message": "任务处理中", "data": {"status": "processing"}},
    )


def _resp_success():
    return _FakeResponse(
        200,
        {
            "code": 200,
            "message": "查询成功",
            "data": {"status": "success", "view_token": "vt-1"},
        },
    )


def _resp_failed():
    return _FakeResponse(
        200,
        {
            "code": 500,
            "message": "任务失败",
            "data": {"status": "failed", "error": "ASR timeout"},
        },
    )


@pytest.fixture()
def skill(monkeypatch):
    monkeypatch.setenv("VIDEO_TRANSCRIPT_API_BASE_URL", "http://skill-test.local")
    monkeypatch.setenv("VIDEO_TRANSCRIPT_API_TOKEN", "sk-skill-test-token")
    return _load_skill_module()


@pytest.fixture()
def http(monkeypatch):
    """Intercept urlopen; record every request the script emits.

    state.calls -- list of {"url", "method", "timeout"} dicts.
    state.queue -- scripted responses; when empty, a 202 processing body is
    returned (so timeout tests can spin without an endless script).
    """
    state = SimpleNamespace(calls=[], queue=[])

    def fake_urlopen(req, timeout=None):
        state.calls.append(
            {"url": req.full_url, "method": req.get_method(), "timeout": timeout}
        )
        if state.queue:
            return state.queue.pop(0)
        return _resp_processing()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return state


def _query_of(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class TestWaitSendsLongPollRequests:
    def test_each_request_carries_wait_query_and_generous_timeout(self, skill, http):
        http.queue.append(_resp_processing())
        http.queue.append(_resp_success())
        exit_code = skill.main(["wait", "task-1", "--format", "json"])

        assert exit_code == 0
        assert len(http.calls) == 2
        for call in http.calls:
            parsed = urlparse(call["url"])
            assert parsed.path == "/api/task/task-1"
            assert call["method"] == "GET"
            # Server-side wait slice per request.
            assert _query_of(call["url"])["wait"] == "30"
            # Client HTTP timeout must exceed the per-request wait.
            assert call["timeout"] > int(_query_of(call["url"])["wait"])

    def test_bearer_token_sent_on_wait_requests(self, skill, http):
        http.queue.append(_resp_success())
        skill.main(["wait", "task-1", "--format", "json"])

        req_headers = skill._auth_headers()
        assert req_headers["Authorization"] == "Bearer sk-skill-test-token"
        assert http.calls[0]["url"].startswith("http://skill-test.local/api/task/")


class TestWaitTermination:
    def test_terminal_on_first_call_asks_server_once(self, skill, http):
        http.queue.append(_resp_success())
        exit_code = skill.main(["wait", "task-1", "--format", "json"])

        assert exit_code == 0
        assert len(http.calls) == 1

    def test_failed_task_exits_1(self, skill, http, capsys):
        http.queue.append(_resp_failed())
        exit_code = skill.main(["wait", "task-1", "--format", "json"])
        out = json.loads(capsys.readouterr().out)

        assert exit_code == 1
        assert out["task_status"] == "failed"

    def test_missing_task_raises_business_error(self, skill, http):
        http.queue.append(
            _FakeResponse(
                404,
                {"detail": "Task not found", "error_code": "task_id_not_found"},
            )
        )
        exit_code = skill.main(["wait", "task-1", "--format", "json"])

        assert exit_code == 1
        assert len(http.calls) == 1

    def test_invalid_timeout_is_config_error(self, skill, http):
        assert skill.main(["wait", "task-1", "--timeout", "0"]) == 3
        assert http.calls == []


class TestWaitTimeout:
    def test_timeout_reports_current_status_and_exit_1(self, skill, http, capsys):
        # No scripted responses: every call gets 202 processing until the
        # total timeout expires (kept small so the test stays fast).
        exit_code = skill.main(
            ["wait", "task-1", "--format", "json", "--timeout", "1"]
        )
        out = json.loads(capsys.readouterr().out)

        assert exit_code == 1
        assert out["task_status"] == "processing"
        assert out["timed_out"] is True
        assert len(http.calls) >= 1

    def test_success_before_timeout_is_not_timed_out(self, skill, http, capsys):
        http.queue.append(_resp_success())
        exit_code = skill.main(
            ["wait", "task-1", "--format", "json", "--timeout", "5"]
        )
        out = json.loads(capsys.readouterr().out)

        assert exit_code == 0
        assert out["task_status"] == "success"
        assert out["timed_out"] is False


class TestWaitMarkdownOutput:
    def test_markdown_mentions_final_status(self, skill, http, capsys):
        http.queue.append(_resp_success())
        exit_code = skill.main(["wait", "task-1"])
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "success" in out
