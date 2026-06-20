"""Unit tests for MediaResolverClient (T1).

Covers HTTP/response -> exception mapping per the Error & Rescue Registry.
All console output is English only.
"""

import pytest
import requests

from video_transcript_api.downloaders.media_resolver_client import MediaResolverClient
from video_transcript_api.errors import (
    NetworkError,
    ResolverAuthError,
    ResolverServerError,
    InvalidURLError,
    NonVideoContentError,
    ResolverResolveError,
    ResolverResponseError,
)


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code=200, json_data=None, text="", raise_json=False):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (str(json_data) if json_data is not None else "")
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("no json")
        return self._json_data


def make_client(**kwargs):
    defaults = dict(base_url="http://resolver:8000", api_key="k", max_retries=2, retry_delay=0)
    defaults.update(kwargs)
    return MediaResolverClient(**defaults)


def patch_post(monkeypatch, *responses_or_exc):
    """Patch requests.post to yield given responses/exceptions in sequence."""
    seq = list(responses_or_exc)
    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        idx = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        item = seq[idx]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


# --------------------------------------------------------------------------- #
# construction
# --------------------------------------------------------------------------- #

class TestConstruction:
    def test_requires_base_url(self):
        with pytest.raises(ValueError):
            MediaResolverClient(base_url="", api_key="k")

    def test_requires_api_key(self):
        with pytest.raises(ValueError):
            MediaResolverClient(base_url="http://x", api_key="")

    def test_endpoint_strips_trailing_slash(self):
        c = MediaResolverClient(base_url="http://x:8000/", api_key="k")
        assert c.resolve_endpoint == "http://x:8000/api/resolve"


# --------------------------------------------------------------------------- #
# success
# --------------------------------------------------------------------------- #

class TestSuccess:
    def test_returns_data_on_success(self, monkeypatch):
        data = {"platform": "douyin", "video_id": "1", "video_url": "http://cdn/v.mp4"}
        patch_post(monkeypatch, FakeResponse(200, {"success": True, "data": data}))
        out = make_client().resolve("http://v.douyin.com/abc")
        assert out["video_url"] == "http://cdn/v.mp4"

    def test_sends_api_key_and_payload(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse(200, {"success": True, "data": {"video_url": "http://x/v.mp4"}})

        monkeypatch.setattr(requests, "post", fake_post)
        make_client().resolve("http://page", force_refresh=True)
        assert captured["url"].endswith("/api/resolve")
        assert captured["headers"]["X-API-Key"] == "k"
        assert captured["json"] == {"url": "http://page", "translate": False, "force_refresh": True}


# --------------------------------------------------------------------------- #
# HTTP-layer errors
# --------------------------------------------------------------------------- #

class TestHttpErrors:
    def test_401_auth(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(401, text="unauthorized"))
        with pytest.raises(ResolverAuthError):
            make_client().resolve("http://page")

    def test_400_invalid_url(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(400, text="bad url"))
        with pytest.raises(InvalidURLError):
            make_client().resolve("http://page")

    def test_500_retries_then_server_error(self, monkeypatch):
        calls = patch_post(monkeypatch, FakeResponse(500, text="boom"), FakeResponse(500, text="boom"))
        with pytest.raises(ResolverServerError):
            make_client(max_retries=2).resolve("http://page")
        assert calls["n"] == 2  # retried

    def test_500_then_success_recovers(self, monkeypatch):
        patch_post(
            monkeypatch,
            FakeResponse(500, text="boom"),
            FakeResponse(200, {"success": True, "data": {"video_url": "http://x/v.mp4"}}),
        )
        out = make_client(max_retries=2).resolve("http://page")
        assert out["video_url"] == "http://x/v.mp4"

    def test_unexpected_status_is_response_error(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(404, text="nope"))
        with pytest.raises(ResolverResponseError):
            make_client().resolve("http://page")


# --------------------------------------------------------------------------- #
# network errors
# --------------------------------------------------------------------------- #

class TestNetworkErrors:
    def test_timeout_retries_then_network_error(self, monkeypatch):
        calls = patch_post(
            monkeypatch,
            requests.exceptions.Timeout("t"),
            requests.exceptions.Timeout("t"),
        )
        with pytest.raises(NetworkError):
            make_client(max_retries=2).resolve("http://page")
        assert calls["n"] == 2

    def test_connection_error_recovers(self, monkeypatch):
        patch_post(
            monkeypatch,
            requests.exceptions.ConnectionError("refused"),
            FakeResponse(200, {"success": True, "data": {"video_url": "http://x/v.mp4"}}),
        )
        out = make_client(max_retries=2).resolve("http://page")
        assert out["video_url"] == "http://x/v.mp4"


# --------------------------------------------------------------------------- #
# success=false classification (T8 contract)
# --------------------------------------------------------------------------- #

class TestFailureClassification:
    @pytest.mark.parametrize("code", ["NON_VIDEO_CONTENT", "IMAGE_TEXT", "DELETED", "PRIVATE"])
    def test_terminal_codes_non_video(self, monkeypatch, code):
        patch_post(monkeypatch, FakeResponse(200, {"success": False, "error": {"code": code, "message": "x"}}))
        with pytest.raises(NonVideoContentError):
            make_client().resolve("http://page")

    @pytest.mark.parametrize("code", ["ALL_SOURCES_FAILED", "RESOLVE_FAILED"])
    def test_all_source_fail_codes(self, monkeypatch, code):
        patch_post(monkeypatch, FakeResponse(200, {"success": False, "error": {"code": code, "message": "x"}}))
        with pytest.raises(ResolverResolveError):
            make_client().resolve("http://page")

    def test_text_fallback_non_video(self, monkeypatch):
        # no code, message indicates image-text post
        patch_post(monkeypatch, FakeResponse(200, {"success": False, "error": {"message": "该内容为图文笔记"}}))
        with pytest.raises(NonVideoContentError):
            make_client().resolve("http://page")

    def test_unknown_failure_defaults_resolve_error(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(200, {"success": False, "error": {"message": "weird"}}))
        with pytest.raises(ResolverResolveError):
            make_client().resolve("http://page")

    def test_error_as_plain_string(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(200, {"success": False, "error": "deleted"}))
        with pytest.raises(NonVideoContentError):
            make_client().resolve("http://page")


# --------------------------------------------------------------------------- #
# malformed responses
# --------------------------------------------------------------------------- #

class TestMalformed:
    def test_non_json_body(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(200, raise_json=True, text="<html>"))
        with pytest.raises(ResolverResponseError):
            make_client().resolve("http://page")

    def test_success_missing_video_url(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(200, {"success": True, "data": {"platform": "douyin"}}))
        with pytest.raises(ResolverResponseError):
            make_client().resolve("http://page")

    def test_top_level_not_object(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(200, ["a", "b"]))
        with pytest.raises(ResolverResponseError):
            make_client().resolve("http://page")
