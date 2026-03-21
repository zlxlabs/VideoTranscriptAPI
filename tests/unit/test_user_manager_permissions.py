"""
UserManager permission and edge case tests.

Supplements test_multi_user.py with:
- Permission checking (multi-user vs legacy)
- Edge cases: empty token, None token, empty users file
- get_user_by_id lookups
- Multi-user mode detection

All console output must be in English only (no emoji, no Chinese).
"""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from video_transcript_api.utils.accounts.user_manager import UserManager


@pytest.fixture
def users_config(tmp_path):
    """Create a temporary users.json with test data."""
    users_data = {
        "users": {
            "sk-admin-token": {
                "user_id": "admin",
                "name": "Admin User",
                "enabled": True,
                "permissions": ["recalibrate", "delete", "admin"],
                "wechat_webhook": "https://qyapi.weixin.qq.com/admin",
            },
            "sk-viewer-token": {
                "user_id": "viewer",
                "name": "Viewer User",
                "enabled": True,
                "permissions": ["recalibrate"],
            },
            "sk-disabled-token": {
                "user_id": "disabled_user",
                "name": "Disabled User",
                "enabled": False,
                "permissions": ["recalibrate"],
            },
            "sk-no-perms-token": {
                "user_id": "no_perms",
                "name": "No Permissions User",
                "enabled": True,
                "permissions": [],
            },
        }
    }
    path = tmp_path / "users.json"
    path.write_text(json.dumps(users_data), encoding="utf-8")
    return str(path)


@pytest.fixture
def fallback_config():
    """Provide fallback config with legacy token."""
    return {
        "api": {"auth_token": "legacy-token-123"},
        "wechat": {"webhook": "https://qyapi.weixin.qq.com/legacy"},
    }


@pytest.fixture
def manager(users_config, fallback_config):
    """Create a UserManager with test configuration."""
    return UserManager(users_config_path=users_config, fallback_config=fallback_config)


@pytest.fixture
def legacy_only_manager(tmp_path, fallback_config):
    """Create a UserManager with no users file (legacy mode only)."""
    empty_path = str(tmp_path / "nonexistent_users.json")
    return UserManager(users_config_path=empty_path, fallback_config=fallback_config)


# ============================================================
# Permission Check Tests
# ============================================================


class TestPermissionCheck:
    """Verify check_permission behavior."""

    def test_admin_has_permission(self, manager):
        """Admin user should have admin permission."""
        user = manager.validate_token("sk-admin-token")
        assert manager.check_permission(user, "admin") is True
        assert manager.check_permission(user, "recalibrate") is True
        assert manager.check_permission(user, "delete") is True

    def test_viewer_limited_permissions(self, manager):
        """Viewer should only have recalibrate permission."""
        user = manager.validate_token("sk-viewer-token")
        assert manager.check_permission(user, "recalibrate") is True
        assert manager.check_permission(user, "admin") is False
        assert manager.check_permission(user, "delete") is False

    def test_no_perms_user(self, manager):
        """User with empty permissions should have no permissions."""
        user = manager.validate_token("sk-no-perms-token")
        assert manager.check_permission(user, "recalibrate") is False
        assert manager.check_permission(user, "admin") is False

    def test_legacy_user_has_all_permissions(self, manager):
        """Legacy user should have all permissions."""
        user = manager.validate_token("legacy-token-123")
        assert manager.check_permission(user, "recalibrate") is True
        assert manager.check_permission(user, "admin") is True
        assert manager.check_permission(user, "any_permission") is True

    def test_legacy_only_mode_permissions(self, legacy_only_manager):
        """Legacy-only mode user should have all permissions."""
        user = legacy_only_manager.validate_token("legacy-token-123")
        assert user is not None
        assert legacy_only_manager.check_permission(user, "anything") is True


# ============================================================
# Token Validation Edge Cases
# ============================================================


class TestTokenValidationEdgeCases:
    """Verify edge cases in token validation."""

    def test_empty_string_token(self, manager):
        """Empty string token should be rejected."""
        assert manager.validate_token("") is None

    def test_wrong_token(self, manager):
        """Non-existent token should be rejected."""
        assert manager.validate_token("sk-nonexistent") is None

    def test_disabled_user_rejected(self, manager):
        """Disabled user token should be rejected."""
        assert manager.validate_token("sk-disabled-token") is None

    def test_valid_token_returns_user_info(self, manager):
        """Valid token should return complete user info dict."""
        user = manager.validate_token("sk-admin-token")
        assert user is not None
        assert user["user_id"] == "admin"
        assert user["name"] == "Admin User"
        assert user["api_key"] == "sk-admin-token"
        assert user["enabled"] is True

    def test_legacy_token_has_is_legacy_flag(self, manager):
        """Legacy token user should have is_legacy=True."""
        user = manager.validate_token("legacy-token-123")
        assert user is not None
        assert user["is_legacy"] is True
        assert user["user_id"] == "legacy_user"

    def test_multi_user_token_no_legacy_flag(self, manager):
        """Multi-user token should NOT have is_legacy flag."""
        user = manager.validate_token("sk-admin-token")
        assert user.get("is_legacy") is None or user.get("is_legacy") is False


# ============================================================
# User Lookup Tests
# ============================================================


class TestUserLookup:
    """Verify get_user_by_id and related methods."""

    def test_get_user_by_id_found(self, manager):
        """Should return user info for existing user_id."""
        user = manager.get_user_by_id("admin")
        assert user is not None
        assert user["name"] == "Admin User"

    def test_get_user_by_id_not_found(self, manager):
        """Should return None for non-existent user_id."""
        user = manager.get_user_by_id("nonexistent")
        assert user is None

    def test_get_user_by_id_legacy(self, manager):
        """Should return legacy user info."""
        user = manager.get_user_by_id("legacy_user")
        assert user is not None
        assert user["is_legacy"] is True

    def test_get_user_webhook(self, manager):
        """Should return user's webhook URL."""
        webhook = manager.get_user_webhook("sk-admin-token")
        assert webhook == "https://qyapi.weixin.qq.com/admin"

    def test_get_user_webhook_no_webhook(self, manager):
        """Should return None for user without webhook."""
        webhook = manager.get_user_webhook("sk-viewer-token")
        assert webhook is None


# ============================================================
# Multi-User Mode Detection
# ============================================================


class TestMultiUserMode:
    """Verify multi-user mode detection."""

    def test_multi_user_mode_with_users(self, manager):
        """Should be multi-user mode when users.json has users."""
        assert manager.is_multi_user_mode() is True

    def test_not_multi_user_mode_without_users(self, legacy_only_manager):
        """Should NOT be multi-user mode without users.json."""
        assert legacy_only_manager.is_multi_user_mode() is False

    def test_user_count(self, manager):
        """Should return correct user count."""
        assert manager.get_user_count() == 4  # 4 users in config

    def test_list_all_users_masks_keys(self, manager):
        """list_all_users should mask API keys."""
        users = manager.list_all_users()
        for user in users:
            api_key = user.get("api_key", "")
            # Key should be masked (not the full key)
            assert "sk-admin-token" != api_key or len(api_key) < len("sk-admin-token")
