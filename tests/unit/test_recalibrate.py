"""Unit tests for recalibrate feature.

Tests cover:
- UserManager.check_permission: permission granted / denied / legacy user
"""

import json
import tempfile

import pytest


class TestCheckPermission:
    """Test UserManager.check_permission method."""

    def _make_manager(self):
        """Create a UserManager without loading real config."""
        from video_transcript_api.utils.accounts.user_manager import UserManager

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"users": {}}, f)
            config_path = f.name

        return UserManager(users_config_path=config_path)

    def test_legacy_user_has_all_permissions(self):
        """Legacy single-token user should have all permissions."""
        mgr = self._make_manager()
        user_info = {"user_id": "legacy_user", "is_legacy": True}
        assert mgr.check_permission(user_info, "recalibrate") is True
        assert mgr.check_permission(user_info, "anything_else") is True

    def test_user_with_permission(self):
        """Multi-user with recalibrate in permissions should pass."""
        mgr = self._make_manager()
        user_info = {"user_id": "admin", "permissions": ["recalibrate", "other"]}
        assert mgr.check_permission(user_info, "recalibrate") is True

    def test_user_without_permission(self):
        """Multi-user without recalibrate should fail."""
        mgr = self._make_manager()
        user_info = {"user_id": "reader", "permissions": ["read"]}
        assert mgr.check_permission(user_info, "recalibrate") is False

    def test_user_no_permissions_field(self):
        """Multi-user with no permissions field should fail."""
        mgr = self._make_manager()
        user_info = {"user_id": "basic_user"}
        assert mgr.check_permission(user_info, "recalibrate") is False
