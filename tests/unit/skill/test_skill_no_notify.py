import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


_SKILL_SCRIPT = (
    Path(__file__).resolve().parents[3] / "skill" / "scripts" / "videotranscript.py"
)


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def getcode(self):
        return 200

    def read(self):
        return json.dumps(
            {"code": 202, "data": {"task_id": "task-fixture", "view_token": "view-fixture"}}
        ).encode()


def test_submit_no_notify_sends_none_without_environment_webhooks(monkeypatch):
    spec = importlib.util.spec_from_file_location("skill_no_notify", _SKILL_SCRIPT)
    skill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(skill)
    monkeypatch.setenv("VIDEO_TRANSCRIPT_API_BASE_URL", "http://skill-test.invalid")
    monkeypatch.setenv("VIDEO_TRANSCRIPT_API_TOKEN", "fixture-token")
    monkeypatch.setenv("VIDEO_TRANSCRIPT_API_WECHAT_WEBHOOK", "fixture-wechat")
    monkeypatch.setenv("VIDEO_TRANSCRIPT_API_FEISHU_WEBHOOK", "fixture-feishu")
    sent = []

    def fake_urlopen(request, timeout):
        sent.append((request, timeout))
        return _Response()

    monkeypatch.setattr(skill.urlrequest, "urlopen", fake_urlopen)
    args = skill._build_parser().parse_args(
        ["submit", "https://example.invalid/video", "--no-notify", "--format", "json"]
    )

    assert skill.cmd_submit(args) == 0
    request, _timeout = sent[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert payload == {"url": "https://example.invalid/video", "notify": "none"}
    assert "wechat_webhook" not in payload
    assert "feishu_webhook" not in payload
