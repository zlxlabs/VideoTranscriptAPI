import json

import pytest

from video_transcript_api.utils.accounts.user_manager import UserConfigError, UserManager


def _write(path, users):
    path.write_text(json.dumps({"users": users}), encoding="utf-8")


@pytest.mark.parametrize(
    "user_info",
    [
        {"name": "Missing id", "permissions": []},
        {"user_id": "", "name": "Empty", "permissions": []},
        {"user_id": " padded ", "name": "Padded", "permissions": []},
        {"user_id": "legacy_user", "name": "Reserved", "permissions": []},
        {"user_id": "ok", "name": "Bad permission", "permissions": ["root"]},
        {"user_id": "ok", "name": "Wrong type", "permissions": "recalibrate"},
    ],
)
def test_existing_invalid_users_file_is_rejected(tmp_path, user_info):
    path = tmp_path / "users.json"
    _write(path, {"secret-token-value": user_info})
    with pytest.raises(UserConfigError) as exc_info:
        UserManager(str(path), fallback_config={})
    assert "secret-token-value" not in str(exc_info.value)


@pytest.mark.parametrize(
    "token",
    [
        "secret token",  # 内部空格
        " secret-token",  # 前导空格
        "secret-token ",  # 尾随空格
        "secret\ttoken",  # tab
        "secret\ntoken",  # 换行
    ],
)
def test_api_token_with_whitespace_is_rejected(tmp_path, token):
    """真实鉴权 transcription.py::verify_token 用
    `authorization.split()` 要求 `Bearer <token>` 恰好两段；token 键本身
    含空白会让该用户永久无法通过鉴权，此前的预检（仅查非空）对此完全没有
    察觉。这里必须在加载 users.json 时就拒绝，且错误信息不得回显 token 值。"""
    path = tmp_path / "users.json"
    _write(path, {token: {"user_id": "carol", "name": "Carol", "permissions": []}})
    with pytest.raises(UserConfigError) as exc_info:
        UserManager(str(path), fallback_config={})
    assert token not in str(exc_info.value)


def test_duplicate_user_id_including_disabled_user_is_rejected(tmp_path):
    path = tmp_path / "users.json"
    _write(
        path,
        {
            "token-one": {"user_id": "same", "name": "One", "permissions": []},
            "token-two": {
                "user_id": "same",
                "name": "Two",
                "enabled": False,
                "permissions": [],
            },
        },
    )
    with pytest.raises(UserConfigError, match="duplicate user_id"):
        UserManager(str(path), fallback_config={})


def test_invalid_reload_keeps_last_known_good(tmp_path):
    path = tmp_path / "users.json"
    _write(
        path,
        {"token-one": {"user_id": "stable", "name": "Stable", "permissions": []}},
    )
    manager = UserManager(str(path), fallback_config={})
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(UserConfigError):
        manager.reload_config()

    assert manager.validate_token("token-one")["user_id"] == "stable"


def test_missing_file_during_reload_keeps_last_known_good(tmp_path):
    path = tmp_path / "users.json"
    _write(
        path,
        {"token-one": {"user_id": "stable", "name": "Stable", "permissions": []}},
    )
    manager = UserManager(str(path), fallback_config={"api": {"auth_token": "legacy"}})
    path.unlink()

    with pytest.raises(UserConfigError, match="not found"):
        manager.reload_config()

    assert manager.validate_token("token-one")["user_id"] == "stable"
    assert manager.is_multi_user_mode() is True


def test_missing_file_is_the_only_legacy_fallback(tmp_path):
    missing = tmp_path / "missing.json"
    manager = UserManager(str(missing), fallback_config={"api": {"auth_token": "legacy"}})
    assert manager.validate_token("legacy")["user_id"] == "legacy_user"

    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(UserConfigError):
        UserManager(str(empty), fallback_config={"api": {"auth_token": "legacy"}})
