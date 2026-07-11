"""SSRF regression tests for GenericDownloader.

GenericDownloader is the catch-all downloader (can_handle() always returns
True), so any URL that no platform-specific downloader recognizes lands
here. Before this fix it called requests.head/requests.get directly,
bypassing utils.url_validator.validate_url_safe entirely -- a URL pointing
at loopback/private/link-local/cloud-metadata addresses (or a public URL
that 302-redirects to one) would be requested without any safety check.

Covers:
- get_video_info / download_file reject unsafe URLs before ANY network call
- redirect hops are validated individually (requests' automatic redirect
  following is disabled; a public URL that 302s into the internal network
  must be blocked, not silently followed)
- the redirect chain is capped so a malicious/broken server cannot loop
  the downloader forever
- the normal (public URL, no redirect) path still works end to end

Console output English only, no emoji.
"""

import os
import socket
from unittest.mock import MagicMock, patch

import pytest

from video_transcript_api.downloaders.generic import GenericDownloader
from video_transcript_api.errors import InvalidURLError

# Module path where GenericDownloader looks up `requests` -- patch here so
# we can assert on call counts without touching the real network stack.
REQUESTS_PATH = "video_transcript_api.downloaders.generic.requests"
# validate_url_safe's own DNS resolution call, patched at its source module
# so it affects every caller (generic.py imports validate_url_safe by name).
GETADDRINFO_PATH = "video_transcript_api.utils.url_validator.socket.getaddrinfo"

BLOCKED_URLS = [
    "http://127.0.0.1/x",
    "http://192.168.1.10/x",
    "http://169.254.169.254/latest/meta-data",
    "file:///etc/passwd",
]


class _StubTempManager:
    """Minimal temp manager stub so download_file has somewhere to write."""

    def __init__(self, task_dir):
        self._task_dir = task_dir

    def get_current_task_dir(self):
        return self._task_dir


def _make_downloader(tmp_path):
    downloader = GenericDownloader()
    downloader.temp_manager = _StubTempManager(str(tmp_path / "task"))
    return downloader


def _public_addrinfo(*args, **kwargs):
    """Fake socket.getaddrinfo returning a single public IPv4 address."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


# ---------------------------------------------------------------------------
# 1. Unsafe URLs must be rejected before any network call is made.
# ---------------------------------------------------------------------------


class TestGetVideoInfoBlocksUnsafeUrls:
    @pytest.mark.parametrize("url", BLOCKED_URLS)
    def test_rejects_without_network_call(self, url):
        downloader = GenericDownloader()

        with patch(f"{REQUESTS_PATH}.head") as mock_head, patch(
            f"{REQUESTS_PATH}.get"
        ) as mock_get:
            with pytest.raises(InvalidURLError):
                downloader.get_video_info(url)

            mock_head.assert_not_called()
            mock_get.assert_not_called()


class TestDownloadFileBlocksUnsafeUrls:
    @pytest.mark.parametrize("url", BLOCKED_URLS)
    def test_rejects_without_network_call(self, url, tmp_path):
        downloader = _make_downloader(tmp_path)

        with patch(f"{REQUESTS_PATH}.head") as mock_head, patch(
            f"{REQUESTS_PATH}.get"
        ) as mock_get:
            with pytest.raises(InvalidURLError):
                downloader.download_file(url, "x.mp4")

            mock_head.assert_not_called()
            mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Redirect hops must be validated individually.
# ---------------------------------------------------------------------------


def _redirect_response(location):
    resp = MagicMock()
    resp.is_redirect = True
    resp.headers = {"Location": location}
    return resp


class TestRedirectHopsAreValidated:
    def test_get_video_info_blocks_redirect_to_internal_ip(self):
        """No file extension -> _is_media_url falls back to a HEAD probe,
        which must not follow a redirect into the internal network."""
        downloader = GenericDownloader()
        url = "http://public.example.com/media-file"

        with patch(GETADDRINFO_PATH, side_effect=_public_addrinfo), patch(
            f"{REQUESTS_PATH}.head",
            return_value=_redirect_response("http://10.0.0.1/"),
        ) as mock_head:
            with pytest.raises(InvalidURLError):
                downloader.get_video_info(url)

        # Only the original public URL was ever requested; the internal
        # redirect target must never be dereferenced.
        assert mock_head.call_count == 1
        assert mock_head.call_args[0][0] == url

    def test_download_file_blocks_redirect_to_internal_ip(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        url = "http://public.example.com/video.mp4"

        with patch(GETADDRINFO_PATH, side_effect=_public_addrinfo), patch(
            f"{REQUESTS_PATH}.get",
            return_value=_redirect_response("http://10.0.0.1/"),
        ) as mock_get:
            with pytest.raises(InvalidURLError):
                downloader.download_file(url, "video.mp4")

        assert mock_get.call_count == 1
        assert mock_get.call_args[0][0] == url


# ---------------------------------------------------------------------------
# 3. Redirect chain is capped (5 hops).
# ---------------------------------------------------------------------------


class TestRedirectLimitExceeded:
    def test_download_file_raises_after_too_many_redirects(self, tmp_path):
        """A server that always redirects to a fresh public URL must be cut
        off after 5 hops instead of being followed forever."""
        downloader = _make_downloader(tmp_path)
        url = "https://public.example.com/start"

        call_count = {"n": 0}

        def fake_get(request_url, **kwargs):
            call_count["n"] += 1
            return _redirect_response(f"https://public.example.com/hop{call_count['n']}")

        with patch(GETADDRINFO_PATH, side_effect=_public_addrinfo), patch(
            f"{REQUESTS_PATH}.get", side_effect=fake_get
        ):
            with pytest.raises(InvalidURLError):
                downloader.download_file(url, "video.mp4")

        # initial request + 5 allowed redirect hops = 6 requests; the 6th
        # redirect response (7th would-be hop) trips the limit and aborts
        # before a 7th request is ever made.
        assert call_count["n"] == 6


# ---------------------------------------------------------------------------
# 4. Normal (safe, non-redirecting) path must not regress.
# ---------------------------------------------------------------------------


class TestNormalPathNotRegressed:
    def test_get_video_info_direct_media_link(self):
        downloader = GenericDownloader()
        url = "https://public.example.com/audio.mp3"

        with patch(GETADDRINFO_PATH, side_effect=_public_addrinfo):
            info = downloader.get_video_info(url)

        assert info["is_generic"] is True
        assert info["download_url"] == url
        assert info["platform"] == "generic"
        assert info["filename"] == "audio.mp3"

    def test_download_file_success(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        url = "https://public.example.com/audio.mp3"

        ok_response = MagicMock()
        ok_response.is_redirect = False
        ok_response.status_code = 200
        ok_response.headers = {"content-length": "4"}
        ok_response.iter_content = MagicMock(return_value=[b"data"])

        with patch(GETADDRINFO_PATH, side_effect=_public_addrinfo), patch(
            f"{REQUESTS_PATH}.get", return_value=ok_response
        ):
            local_path = downloader.download_file(url, "audio.mp3")

        assert local_path is not None
        assert os.path.exists(local_path)
        with open(local_path, "rb") as f:
            assert f.read() == b"data"
