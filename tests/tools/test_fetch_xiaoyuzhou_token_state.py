"""Atomic token-state persistence tests for scripts/fetch_xiaoyuzhou_episodes.py.

The script lives outside the package tree, so it is loaded via importlib.
save_identity() must publish the token state atomically (tmp file + flush +
fsync + os.replace) and propagate write failures instead of printing a
persisted success line. Console output is English-only.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "fetch_xiaoyuzhou_episodes.py"
)

_OLD_STATE = {
    "accessToken": "at-old",
    "refreshToken": "rt-old",
    "deviceId": "device-old",
}
_NEW_STATE = {
    "accessToken": "at-new",
    "refreshToken": "rt-new",
    "deviceId": "device-new",
}


def _load_script(monkeypatch, tmp_path):
    """Import the script as a module and point its state file at tmp_path."""
    spec = importlib.util.spec_from_file_location(
        "fetch_xiaoyuzhou_under_test", _SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state_file = tmp_path / "xiaoyuzhou" / ".token_state.json"
    monkeypatch.setattr(module, "TOKEN_STATE_FILE", state_file)
    module._ACCESS_TOKEN = _NEW_STATE["accessToken"]
    module._REFRESH_TOKEN = _NEW_STATE["refreshToken"]
    module._DEVICE_ID = _NEW_STATE["deviceId"]
    return module, state_file


def _seed_old_state(state_file: Path) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(_OLD_STATE), encoding="utf-8")


def _break_fsync(monkeypatch):
    """Make os.fsync raise OSError to simulate a disk-level write failure."""
    def broken_fsync(fd):
        raise OSError("disk full while fsyncing token state")

    monkeypatch.setattr(os, "fsync", broken_fsync)


def test_save_failure_propagates_and_keeps_old_state(
    monkeypatch, tmp_path, capsys
):
    """Write failure must propagate (no swallowed except), must not print
    'persisted', and must leave the previously persisted state intact.
    """
    module, state_file = _load_script(monkeypatch, tmp_path)
    _seed_old_state(state_file)
    _break_fsync(monkeypatch)

    with pytest.raises(OSError, match="disk full"):
        module.save_identity()

    captured = capsys.readouterr()
    assert "persisted" not in captured.out
    # The old state survives: the failed write never touched the target file.
    assert json.loads(state_file.read_text(encoding="utf-8")) == _OLD_STATE


def test_refresh_failure_propagates_without_persisted_print(
    monkeypatch, tmp_path, capsys
):
    """refresh_access_token() must not report success when the state save
    fails: the exception propagates before any 'persisted' line is printed
    and the function never returns True.
    """
    module, state_file = _load_script(monkeypatch, tmp_path)
    _seed_old_state(state_file)
    _break_fsync(monkeypatch)

    def fake_post(url, headers=None, json=None, timeout=None):
        return SimpleNamespace(
            status_code=200,
            text="{}",
            json=lambda: {
                "x-jike-access-token": _NEW_STATE["accessToken"],
                "x-jike-refresh-token": _NEW_STATE["refreshToken"],
            },
        )

    monkeypatch.setattr(module.requests, "post", fake_post)

    with pytest.raises(OSError, match="disk full"):
        module.refresh_access_token()

    captured = capsys.readouterr()
    assert "persisted" not in captured.out


def test_save_success_publishes_complete_json_after_fsync(
    monkeypatch, tmp_path, capsys
):
    """Successful save must publish a complete JSON document at the target
    path, durably (flush + fsync before publish), with no tmp leftovers.
    """
    module, state_file = _load_script(monkeypatch, tmp_path)
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def tracking_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)

    module.save_identity()

    assert fsync_calls, "save_identity must fsync before publishing"
    published = json.loads(state_file.read_text(encoding="utf-8"))
    assert published == _NEW_STATE
    assert not list(state_file.parent.glob("*.tmp*"))
    assert "persisted" not in capsys.readouterr().out
