"""Unit tests for POST /api/edit_metadata (vta-261031).

Strategy: real FastAPI TestClient + real CacheManager (tmp sqlite) + real
AuditLogger (tmp audit.db). Only the auth Depends (verify_token) and the
UserManager permission answer are faked per test; ownership and all media
constraints run against the real database rows.

Covered card scenarios:
 1. no edit_metadata permission -> 403 metadata_permission_denied, DB untouched
 2. ownership failure (someone else's token) -> 403 metadata_ownership_denied
 3. media shared with another submitted_by -> 403 metadata_shared_denied
 4. queued/processing/calibrating task on the media -> 409 metadata_in_flight
 5. title+author edit -> 200, view page payload carries new values
 6. title-only and author-only edits keep the other field
 7. both fields missing / blank after strip -> 400 metadata_invalid
 8. speaker=0 token with a speaker=1 cache row: the row the page actually
    reads (speaker=1, ORDER BY use_speaker_recognition DESC) must change
 9. "url" field in request body -> 422 (model forbids extra fields)
10. og_card.*.png files are removed after a successful edit
11. upload_ prefixed token is rejected (404 transcript_not_found)

All console output must be in English only (no emoji, no Chinese).
"""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.logging.audit_logger import AuditLogger


_USER_A = {
    "user_id": "user_a",
    "api_key": "sk-test-edit-a",
    "permissions": ["edit_metadata"],
    "is_legacy": False,
}
_USER_B = {
    "user_id": "user_b",
    "api_key": "sk-test-edit-b",
    "permissions": [],
    "is_legacy": False,
}

_URL = "https://www.youtube.com/watch?v=vta261031"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def cache_manager(tmp_path):
    cm = CacheManager(str(tmp_path / "cache"))
    cm.audit_logger = AuditLogger(str(tmp_path / "audit.db"))
    yield cm
    cm.close()


def _seed_task(
    cache_manager,
    *,
    url=_URL,
    platform="youtube",
    media_id="vta261031",
    speaker=False,
    submitted_by=None,
    status="success",
    title="Old Title",
    author="Old Author",
):
    """Create one task_status row + one video_cache row; force status."""
    task = cache_manager.create_task(
        url=url,
        platform=platform,
        media_id=media_id,
        use_speaker_recognition=speaker,
        submitted_by=submitted_by,
    )
    cache_manager.save_cache(
        platform=platform,
        url=url,
        media_id=media_id,
        use_speaker_recognition=speaker,
        transcript_data="hello transcript body",
        transcript_type="capswriter",
        title=title,
        author=author,
    )
    with cache_manager._get_cursor() as cursor:
        cursor.execute(
            "UPDATE task_status SET status = ?, title = ?, author = ? "
            "WHERE task_id = ?",
            (status, title, author, task["task_id"]),
        )
    return task["view_token"]


def _read_media_rows(cache_manager, platform="youtube", media_id="vta261031"):
    """Return [(title, author)] for video_cache and task_status of the media."""
    with cache_manager._get_cursor() as cursor:
        cursor.execute(
            "SELECT title, author FROM video_cache "
            "WHERE platform = ? AND media_id = ? "
            "ORDER BY use_speaker_recognition DESC",
            (platform, media_id),
        )
        cache_rows = [tuple(row) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT title, author FROM task_status "
            "WHERE platform = ? AND media_id = ?",
            (platform, media_id),
        )
        task_rows = [tuple(row) for row in cursor.fetchall()]
    return cache_rows, task_rows


def _build_client(monkeypatch, cache_manager, *, user_info, has_permission=True):
    from video_transcript_api.api.context import install_api_error_handlers
    from video_transcript_api.api.routes import metadata as metadata_route
    from video_transcript_api.api.services.transcription import verify_token

    fake_user_manager = MagicMock()
    fake_user_manager.check_permission.return_value = has_permission
    monkeypatch.setattr(metadata_route, "user_manager", fake_user_manager)
    monkeypatch.setattr(metadata_route, "cache_manager", cache_manager)
    monkeypatch.setattr(metadata_route, "audit_logger", cache_manager.audit_logger)

    app = FastAPI()
    app.include_router(metadata_route.router)
    install_api_error_handlers(app)

    async def _fake_verify_token():
        return user_info

    app.dependency_overrides[verify_token] = _fake_verify_token
    return TestClient(app)


# ---------------------------------------------------------------------------
# Scenario 1: permission denied
# ---------------------------------------------------------------------------


class TestPermissionDenied:
    def test_no_permission_403_and_db_untouched(self, monkeypatch, cache_manager):
        view_token = _seed_task(cache_manager, submitted_by="user_a")
        client = _build_client(
            monkeypatch, cache_manager, user_info=_USER_A, has_permission=False
        )

        resp = client.post(
            "/api/edit_metadata",
            json={"view_token": view_token, "title": "New Title"},
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "metadata_permission_denied"
        cache_rows, task_rows = _read_media_rows(cache_manager)
        assert cache_rows == [("Old Title", "Old Author")]
        assert all(row == ("Old Title", "Old Author") for row in task_rows)


# ---------------------------------------------------------------------------
# Scenario 2: ownership denied
# ---------------------------------------------------------------------------


class TestOwnershipDenied:
    def test_other_users_token_403_and_db_untouched(
        self, monkeypatch, cache_manager
    ):
        view_token = _seed_task(cache_manager, submitted_by="user_b")
        client = _build_client(monkeypatch, cache_manager, user_info=_USER_A)

        resp = client.post(
            "/api/edit_metadata",
            json={"view_token": view_token, "title": "New Title"},
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "metadata_ownership_denied"
        cache_rows, task_rows = _read_media_rows(cache_manager)
        assert cache_rows == [("Old Title", "Old Author")]
        assert all(row == ("Old Title", "Old Author") for row in task_rows)


# ---------------------------------------------------------------------------
# Scenario 3: media shared with another submitter
# ---------------------------------------------------------------------------


class TestSharedMediaDenied:
    def test_other_submitted_by_on_same_media_403(
        self, monkeypatch, cache_manager
    ):
        # Two tasks on the same (platform, media_id) sharing one view_token:
        # row A belongs to the caller, row B to somebody else. Ownership
        # passes (caller owns row A), but exclusive-ownership must reject.
        view_token_a = _seed_task(cache_manager, submitted_by="user_a")
        view_token_b = _seed_task(cache_manager, submitted_by="user_b")
        assert view_token_a == view_token_b
        client = _build_client(monkeypatch, cache_manager, user_info=_USER_A)

        resp = client.post(
            "/api/edit_metadata",
            json={"view_token": view_token_a, "title": "New Title"},
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "metadata_shared_denied"
        cache_rows, task_rows = _read_media_rows(cache_manager)
        assert cache_rows == [("Old Title", "Old Author")]
        assert all(row == ("Old Title", "Old Author") for row in task_rows)


# ---------------------------------------------------------------------------
# Scenario 4: in-flight task on the media
# ---------------------------------------------------------------------------


class TestInFlightRejected:
    @pytest.mark.parametrize("status", ["queued", "processing", "calibrating"])
    def test_in_flight_status_409(self, monkeypatch, cache_manager, status):
        view_token = _seed_task(cache_manager, submitted_by="user_a")
        # A second task row on the same media, owned by the caller but still
        # in flight (create_task() defaults to 'queued'; force each status).
        _seed_task(
            cache_manager,
            url=_URL + "&variant=x",
            submitted_by="user_a",
            status=status,
        )
        client = _build_client(monkeypatch, cache_manager, user_info=_USER_A)

        resp = client.post(
            "/api/edit_metadata",
            json={"view_token": view_token, "title": "New Title"},
        )

        assert resp.status_code == 409
        assert resp.json()["error_code"] == "metadata_in_flight"
        cache_rows, task_rows = _read_media_rows(cache_manager)
        assert cache_rows == [("Old Title", "Old Author")]
        assert all(row == ("Old Title", "Old Author") for row in task_rows)


# ---------------------------------------------------------------------------
# Scenario 5 + E2E: successful title+author edit
# ---------------------------------------------------------------------------


class TestSuccessfulEdit:
    def test_edit_title_and_author_updates_view_payload(
        self, monkeypatch, cache_manager
    ):
        from video_transcript_api.api.services.view_token_resolver import (
            ViewTokenResolver,
        )

        view_token = _seed_task(cache_manager, submitted_by="user_a")
        client = _build_client(monkeypatch, cache_manager, user_info=_USER_A)

        resp = client.post(
            "/api/edit_metadata",
            json={
                "view_token": view_token,
                "title": "Fresh Title",
                "author": "Fresh Author",
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 200
        assert body["data"] == {
            "view_token": view_token,
            "title": "Fresh Title",
            "author": "Fresh Author",
        }

        # E2E assertion: the same CacheManager, through the exact public-page
        # resolution path, must now serve the new values.
        payload = ViewTokenResolver(cache_manager).get_view_data_by_token(
            view_token
        )
        assert payload is not None
        assert payload["title"] == "Fresh Title"
        assert payload["author"] == "Fresh Author"

        # The task_status display copies must be synced too (they feed the
        # processing/failed/file_cleaned page variants).
        _, task_rows = _read_media_rows(cache_manager)
        assert task_rows
        assert all(row == ("Fresh Title", "Fresh Author") for row in task_rows)


# ---------------------------------------------------------------------------
# Scenario 6: partial edits
# ---------------------------------------------------------------------------


class TestPartialEdit:
    def test_title_only_keeps_author(self, monkeypatch, cache_manager):
        from video_transcript_api.api.services.view_token_resolver import (
            ViewTokenResolver,
        )

        view_token = _seed_task(cache_manager, submitted_by="user_a")
        client = _build_client(monkeypatch, cache_manager, user_info=_USER_A)

        resp = client.post(
            "/api/edit_metadata",
            json={"view_token": view_token, "title": "Only Title"},
        )

        assert resp.status_code == 200
        payload = ViewTokenResolver(cache_manager).get_view_data_by_token(
            view_token
        )
        assert payload["title"] == "Only Title"
        assert payload["author"] == "Old Author"
        assert resp.json()["data"]["author"] == "Old Author"

    def test_author_only_keeps_title(self, monkeypatch, cache_manager):
        from video_transcript_api.api.services.view_token_resolver import (
            ViewTokenResolver,
        )

        view_token = _seed_task(cache_manager, submitted_by="user_a")
        client = _build_client(monkeypatch, cache_manager, user_info=_USER_A)

        resp = client.post(
            "/api/edit_metadata",
            json={"view_token": view_token, "author": "Only Author"},
        )

        assert resp.status_code == 200
        payload = ViewTokenResolver(cache_manager).get_view_data_by_token(
            view_token
        )
        assert payload["title"] == "Old Title"
        assert payload["author"] == "Only Author"
        assert resp.json()["data"]["title"] == "Old Title"


# ---------------------------------------------------------------------------
# Scenario 7: invalid bodies
# ---------------------------------------------------------------------------


class TestInvalidBody:
    def _client(self, monkeypatch, cache_manager):
        return _build_client(monkeypatch, cache_manager, user_info=_USER_A)

    def test_both_fields_missing_400(self, monkeypatch, cache_manager):
        view_token = _seed_task(cache_manager, submitted_by="user_a")
        client = self._client(monkeypatch, cache_manager)

        resp = client.post("/api/edit_metadata", json={"view_token": view_token})

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "metadata_invalid"

    def test_whitespace_title_400(self, monkeypatch, cache_manager):
        view_token = _seed_task(cache_manager, submitted_by="user_a")
        client = self._client(monkeypatch, cache_manager)

        resp = client.post(
            "/api/edit_metadata",
            json={"view_token": view_token, "title": "   \n\t "},
        )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "metadata_invalid"

    def test_empty_author_400(self, monkeypatch, cache_manager):
        view_token = _seed_task(cache_manager, submitted_by="user_a")
        client = self._client(monkeypatch, cache_manager)

        resp = client.post(
            "/api/edit_metadata",
            json={"view_token": view_token, "author": ""},
        )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "metadata_invalid"


# ---------------------------------------------------------------------------
# Scenario 8: speaker=0 token must update the row the page reads
# ---------------------------------------------------------------------------


class TestSpeakerVariantCoverage:
    def test_speaker0_token_updates_speaker1_row_the_page_reads(
        self, monkeypatch, cache_manager
    ):
        from video_transcript_api.api.services.view_token_resolver import (
            ViewTokenResolver,
        )

        # Speaker=0 task owns the view token; a speaker=1 sibling exists on
        # the same media. get_cache(use_speaker_recognition=False) orders by
        # use_speaker_recognition DESC, so the page actually reads the
        # speaker=1 video_cache row -- the edit must reach that row.
        view_token = _seed_task(
            cache_manager,
            speaker=False,
            submitted_by="user_a",
            title="Speaker0 Title",
            author="Speaker0 Author",
        )
        _seed_task(
            cache_manager,
            url=_URL + "&variant=sp1",
            speaker=True,
            submitted_by="user_a",
            title="Speaker1 Title",
            author="Speaker1 Author",
        )

        # Precondition: the page (speaker=0 token) currently displays the
        # speaker=1 row's values.
        before = ViewTokenResolver(cache_manager).get_view_data_by_token(
            view_token
        )
        assert before["title"] == "Speaker1 Title"
        assert before["author"] == "Speaker1 Author"

        client = _build_client(monkeypatch, cache_manager, user_info=_USER_A)
        resp = client.post(
            "/api/edit_metadata",
            json={"view_token": view_token, "title": "Both", "author": "Rows"},
        )

        assert resp.status_code == 200
        after = ViewTokenResolver(cache_manager).get_view_data_by_token(
            view_token
        )
        assert after["title"] == "Both"
        assert after["author"] == "Rows"


# ---------------------------------------------------------------------------
# Scenario 9: url field must not be writable
# ---------------------------------------------------------------------------


class TestUrlForbidden:
    def test_url_field_rejected_422(self, monkeypatch, cache_manager):
        view_token = _seed_task(cache_manager, submitted_by="user_a")
        client = _build_client(monkeypatch, cache_manager, user_info=_USER_A)

        resp = client.post(
            "/api/edit_metadata",
            json={
                "view_token": view_token,
                "title": "New Title",
                "url": "https://evil.example.com/watch?v=zzz",
            },
        )

        assert resp.status_code == 422
        cache_rows, _task_rows = _read_media_rows(cache_manager)
        assert cache_rows == [("Old Title", "Old Author")]


# ---------------------------------------------------------------------------
# Scenario 10: OG PNG invalidation
# ---------------------------------------------------------------------------


class TestOgPngInvalidation:
    def test_og_pngs_removed_after_successful_edit(
        self, monkeypatch, cache_manager
    ):
        from pathlib import Path

        view_token = _seed_task(cache_manager, submitted_by="user_a")
        cache_data = cache_manager.get_cache(
            platform="youtube",
            media_id="vta261031",
            use_speaker_recognition=False,
        )
        cache_dir = Path(cache_data["file_path"])
        assert cache_dir.is_dir()
        (cache_dir / "og_card.zh-CN.png").write_bytes(b"png-zh")
        (cache_dir / "og_card.en.png").write_bytes(b"png-en")

        client = _build_client(monkeypatch, cache_manager, user_info=_USER_A)
        resp = client.post(
            "/api/edit_metadata",
            json={"view_token": view_token, "author": "OG Fresh"},
        )

        assert resp.status_code == 200
        assert list(cache_dir.glob("og_card.*.png")) == []


# ---------------------------------------------------------------------------
# Scenario 11: upload_ tokens are never editable
# ---------------------------------------------------------------------------


class TestUploadTokenRejected:
    def test_upload_prefix_token_404(self, monkeypatch, cache_manager):
        client = _build_client(monkeypatch, cache_manager, user_info=_USER_A)

        resp = client.post(
            "/api/edit_metadata",
            json={"view_token": "upload_definitely-not-real", "title": "X"},
        )

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "transcript_not_found"
