"""SSRF regression tests for GenericDownloader.

GenericDownloader is the catch-all downloader (can_handle() always returns
True), so any URL that no platform-specific downloader recognizes lands
here. Before the first fix it called requests.head/requests.get directly,
bypassing utils.url_validator.validate_url_safe entirely -- a URL pointing
at loopback/private/link-local/cloud-metadata addresses (or a public URL
that 302-redirects to one) would be requested without any safety check.

A second, subtler gap (closed by this file's DNS-rebinding tests) remained
even after that fix: validate_url_safe() resolves+checks a hostname's IP
once, but the code then handed the *hostname* to requests, which resolves
DNS again, independently, when it actually connects. An attacker-controlled
domain can flip its DNS record between those two lookups (public IP for
validation, private/internal IP for the real connection) and slip past the
check -- a classic TOCTOU / DNS-rebinding window. The fix (generic.py's
_dispatch_pinned_request + utils/pinned_ip_adapter.PinnedIPHTTPAdapter) pins
the real connection to the exact IP validation already resolved and
checked, so requests/urllib3 never gets a chance to re-resolve the hostname
on its own.

Covers:
- get_video_info / download_file reject unsafe URLs before ANY network call
- redirect hops are validated individually (requests' automatic redirect
  following is disabled; a public URL that 302s into the internal network
  must be blocked, not silently followed)
- the redirect chain is capped so a malicious/broken server cannot loop
  the downloader forever
- the normal (public URL, no redirect) path still works end to end
- the outgoing connection is pinned to the IP validation already resolved
  and checked -- a second, independent resolution of the same hostname
  (simulating DNS rebinding) is never used to connect
- each redirect hop is pinned to *its own* freshly validated IP
- generic.py wires the real hostname (not the pinned IP) into the adapter
  it constructs, so HTTPS SNI/certificate hostname checks stay correct
  (the adapter's own TLS-parameter mechanics are unit-tested in
  tests/unit/utils/test_pinned_ip_adapter.py)

Console output English only, no emoji.
"""

import os
import socket
from unittest.mock import MagicMock, patch

import pytest

from video_transcript_api.downloaders.generic import GenericDownloader
from video_transcript_api.errors import InvalidURLError
from video_transcript_api.utils.pinned_ip_adapter import PinnedIPHTTPAdapter

# validate_url_safe's own DNS resolution call, patched at its source module
# so it affects every caller (generic.py imports validate_url_safe by name).
GETADDRINFO_PATH = "video_transcript_api.utils.url_validator.socket.getaddrinfo"
# The one seam every real network dispatch passes through, pinned or not:
# requests.adapters.HTTPAdapter.send. Patching here (rather than the old
# module-level requests.head/requests.get) lets tests inspect the exact
# PreparedRequest that would have gone out on the wire -- including the
# IP-pinned URL and the restored Host header -- without touching the
# network.
BASE_SEND_PATH = "requests.adapters.HTTPAdapter.send"

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


def _addrinfo(ip):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


def _public_addrinfo(*args, **kwargs):
    """Fake socket.getaddrinfo returning a single public IPv4 address."""
    return _addrinfo("93.184.216.34")


def _ok_response():
    resp = MagicMock()
    resp.is_redirect = False
    resp.status_code = 200
    resp.headers = {"content-length": "4"}
    resp.iter_content = MagicMock(return_value=[b"data"])
    return resp


def _redirect_response(location):
    resp = MagicMock()
    resp.is_redirect = True
    resp.headers = {"Location": location}
    return resp


# ---------------------------------------------------------------------------
# 1. Unsafe URLs must be rejected before any network call is made.
# ---------------------------------------------------------------------------


class TestGetVideoInfoBlocksUnsafeUrls:
    @pytest.mark.parametrize("url", BLOCKED_URLS)
    def test_rejects_without_network_call(self, url):
        downloader = GenericDownloader()

        with patch(BASE_SEND_PATH) as mock_send:
            with pytest.raises(InvalidURLError):
                downloader.get_video_info(url)

            mock_send.assert_not_called()


class TestDownloadFileBlocksUnsafeUrls:
    @pytest.mark.parametrize("url", BLOCKED_URLS)
    def test_rejects_without_network_call(self, url, tmp_path):
        downloader = _make_downloader(tmp_path)

        with patch(BASE_SEND_PATH) as mock_send:
            with pytest.raises(InvalidURLError):
                downloader.download_file(url, "x.mp4")

            mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Redirect hops must be validated individually.
# ---------------------------------------------------------------------------


class TestRedirectHopsAreValidated:
    def test_get_video_info_blocks_redirect_to_internal_ip(self):
        """No file extension -> _is_media_url falls back to a HEAD probe,
        which must not follow a redirect into the internal network."""
        downloader = GenericDownloader()
        url = "http://public.example.com/media-file"

        with patch(GETADDRINFO_PATH, side_effect=_public_addrinfo), patch(
            BASE_SEND_PATH,
            return_value=_redirect_response("http://10.0.0.1/"),
        ) as mock_send:
            with pytest.raises(InvalidURLError):
                downloader.get_video_info(url)

        # Only the original public URL was ever requested; the internal
        # redirect target must never be dereferenced.
        assert mock_send.call_count == 1
        sent_request = mock_send.call_args[0][0]
        # The dispatched request was pinned to the resolved public IP, not
        # the bare hostname -- proves the TOCTOU fix is engaged on this path.
        assert sent_request.url == "http://93.184.216.34/media-file"
        assert sent_request.headers["Host"] == "public.example.com"

    def test_download_file_blocks_redirect_to_internal_ip(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        url = "http://public.example.com/video.mp4"

        with patch(GETADDRINFO_PATH, side_effect=_public_addrinfo), patch(
            BASE_SEND_PATH,
            return_value=_redirect_response("http://10.0.0.1/"),
        ) as mock_send:
            with pytest.raises(InvalidURLError):
                downloader.download_file(url, "video.mp4")

        assert mock_send.call_count == 1
        sent_request = mock_send.call_args[0][0]
        assert sent_request.url == "http://93.184.216.34/video.mp4"


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

        def fake_send(request, **kwargs):
            call_count["n"] += 1
            return _redirect_response(f"https://public.example.com/hop{call_count['n']}")

        with patch(GETADDRINFO_PATH, side_effect=_public_addrinfo), patch(
            BASE_SEND_PATH, side_effect=fake_send
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

        with patch(GETADDRINFO_PATH, side_effect=_public_addrinfo), patch(
            BASE_SEND_PATH, return_value=_ok_response()
        ):
            local_path = downloader.download_file(url, "audio.mp3")

        assert local_path is not None
        assert os.path.exists(local_path)
        with open(local_path, "rb") as f:
            assert f.read() == b"data"


# ---------------------------------------------------------------------------
# 5. DNS-rebinding TOCTOU window must stay closed: the dispatched request
#    always uses the IP validation already resolved and checked, never a
#    second, independent resolution of the same hostname.
# ---------------------------------------------------------------------------


class TestDNSRebindingWindowClosed:
    def test_dispatch_uses_first_validated_ip_never_a_second_resolution(self):
        downloader = GenericDownloader()
        url = "https://rebinding.example.com/audio.mp3"

        addrinfo_calls = {"n": 0}

        def flipping_addrinfo(host, *args, **kwargs):
            addrinfo_calls["n"] += 1
            # 1st (and, if the fix holds, *only*) call: public IP, passes
            # validation. Any further call models what a rebinding domain's
            # DNS would hand back to an independent, uncontrolled second
            # resolution at connect time -- a private IP that must never be
            # the one actually connected to.
            ip = "93.184.216.34" if addrinfo_calls["n"] == 1 else "10.0.0.1"
            return _addrinfo(ip)

        with patch(GETADDRINFO_PATH, side_effect=flipping_addrinfo), patch(
            BASE_SEND_PATH, return_value=_ok_response()
        ) as mock_send:
            downloader._safe_request("get", url, timeout=5)

        # Exactly one DNS lookup: validate_url_safe_with_ip's own
        # resolution. If the fix regressed back to handing the hostname to
        # requests for it to resolve independently, this would be >= 2.
        assert addrinfo_calls["n"] == 1
        sent_request = mock_send.call_args[0][0]
        assert sent_request.url == "https://93.184.216.34/audio.mp3"
        assert "10.0.0.1" not in sent_request.url

    def test_download_file_end_to_end_never_connects_to_rebound_ip(self, tmp_path):
        """Same property, exercised through the public download_file() API
        end to end (download_file also runs an early fail-fast validation
        gate before _safe_request's own validate-then-pin call, so up to 2
        legitimate lookups are expected -- the property under test is that
        none of them, nor any further one, ever gets used to connect)."""
        downloader = _make_downloader(tmp_path)
        url = "https://rebinding.example.com/audio.mp3"

        addrinfo_calls = {"n": 0}

        def flipping_addrinfo(host, *args, **kwargs):
            addrinfo_calls["n"] += 1
            ip = "93.184.216.34" if addrinfo_calls["n"] <= 2 else "10.0.0.1"
            return _addrinfo(ip)

        with patch(GETADDRINFO_PATH, side_effect=flipping_addrinfo), patch(
            BASE_SEND_PATH, return_value=_ok_response()
        ) as mock_send:
            local_path = downloader.download_file(url, "audio.mp3")

        assert local_path is not None
        assert addrinfo_calls["n"] <= 2
        for call in mock_send.call_args_list:
            sent_request = call[0][0]
            assert sent_request.url.startswith("https://93.184.216.34")
            assert "10.0.0.1" not in sent_request.url


# ---------------------------------------------------------------------------
# 6. Each redirect hop is pinned to its own freshly validated IP.
# ---------------------------------------------------------------------------


class TestRedirectHopIsAlsoPinned:
    def test_second_hop_is_pinned_to_its_own_validated_ip(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        url = "https://first.example.com/start"

        ip_by_host = {
            "first.example.com": "93.184.216.34",
            "second.example.com": "104.16.1.1",
        }

        def addrinfo_by_host(host, *args, **kwargs):
            return _addrinfo(ip_by_host[host])

        send_calls = {"n": 0}

        def fake_send(request, **kwargs):
            send_calls["n"] += 1
            if send_calls["n"] == 1:
                return _redirect_response("https://second.example.com/final")
            return _ok_response()

        with patch(GETADDRINFO_PATH, side_effect=addrinfo_by_host), patch(
            BASE_SEND_PATH, side_effect=fake_send
        ) as mock_send:
            local_path = downloader.download_file(url, "final.mp3")

        assert local_path is not None
        first_hop_request = mock_send.call_args_list[0][0][0]
        second_hop_request = mock_send.call_args_list[1][0][0]
        assert first_hop_request.url == "https://93.184.216.34/start"
        assert first_hop_request.headers["Host"] == "first.example.com"
        assert second_hop_request.url == "https://104.16.1.1/final"
        assert second_hop_request.headers["Host"] == "second.example.com"


# ---------------------------------------------------------------------------
# 6b. DNS resolution failure at validation time must fail closed, never fall
#     back to an unpinned request (codex-review R6 #1).
# ---------------------------------------------------------------------------


class TestDNSResolutionFailureFailsClosed:
    """A previous version treated a resolver error (validate_url_safe_with_ip
    returning ip=None) as "transient, allow it through" and dispatched a
    plain, unpinned requests.get()/head() -- which also defaults to
    following redirects. An attacker who can make the validation-time
    lookup fail (e.g. a domain that answers SERVFAIL/times out on the first
    lookup) could ride that fallback straight past both the DNS-rebinding
    pin and the redirect-hop validation. Fix: no validated IP -> fail
    closed, raise InvalidURLError, never touch the network."""

    @staticmethod
    def _gaierror(*args, **kwargs):
        raise socket.gaierror("Name or service not known")

    def test_safe_request_raises_without_network_call(self):
        downloader = GenericDownloader()
        url = "https://unresolvable.example.com/audio.mp3"

        with patch(GETADDRINFO_PATH, side_effect=self._gaierror), patch(
            BASE_SEND_PATH
        ) as mock_send:
            with pytest.raises(InvalidURLError):
                downloader._safe_request("get", url, timeout=5)

        mock_send.assert_not_called()

    def test_get_video_info_raises_without_network_call(self):
        """No file extension -> _is_media_url falls back to a HEAD probe,
        which must fail closed rather than dispatch unpinned."""
        downloader = GenericDownloader()
        url = "https://unresolvable.example.com/media-file"

        with patch(GETADDRINFO_PATH, side_effect=self._gaierror), patch(
            BASE_SEND_PATH
        ) as mock_send:
            with pytest.raises(InvalidURLError):
                downloader.get_video_info(url)

        mock_send.assert_not_called()

    def test_download_file_raises_without_network_call(self, tmp_path):
        downloader = _make_downloader(tmp_path)
        url = "https://unresolvable.example.com/video.mp4"

        with patch(GETADDRINFO_PATH, side_effect=self._gaierror), patch(
            BASE_SEND_PATH
        ) as mock_send:
            with pytest.raises(InvalidURLError):
                downloader.download_file(url, "video.mp4")

        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# 7. generic.py must wire the *real* hostname (not the pinned IP) into the
#    adapter it builds, so HTTPS SNI / certificate hostname verification
#    stays correct. (The adapter's own TLS-parameter mechanics -- that
#    server_hostname/assert_hostname actually reach urllib3's pool manager
#    -- are unit-tested in tests/unit/utils/test_pinned_ip_adapter.py.)
# ---------------------------------------------------------------------------


class TestCertificateHostnamePinningWiredCorrectly:
    def test_https_dispatch_constructs_adapter_with_real_hostname(self):
        downloader = GenericDownloader()
        url = "https://public.example.com/audio.mp3"

        captured = {}

        class _SpyAdapter(PinnedIPHTTPAdapter):
            def __init__(self, hostname, pinned_ip, is_https, **kwargs):
                captured["hostname"] = hostname
                captured["pinned_ip"] = pinned_ip
                captured["is_https"] = is_https
                super().__init__(hostname, pinned_ip, is_https, **kwargs)

        with patch(GETADDRINFO_PATH, side_effect=_public_addrinfo), patch(
            "video_transcript_api.downloaders.generic.PinnedIPHTTPAdapter", _SpyAdapter
        ), patch(BASE_SEND_PATH, return_value=_ok_response()):
            downloader._safe_request("get", url, timeout=5)

        assert captured["hostname"] == "public.example.com"
        assert captured["pinned_ip"] == "93.184.216.34"
        assert captured["is_https"] is True
