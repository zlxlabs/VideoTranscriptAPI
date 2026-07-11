"""Unit tests for PinnedIPHTTPAdapter (utils/pinned_ip_adapter.py).

This adapter exists to close a DNS-rebinding TOCTOU window: SSRF validation
resolves+checks a hostname's IP once, then (pre-fix) requests/urllib3 would
independently re-resolve the same hostname when actually connecting -- an
attacker-controlled domain could flip its DNS record between those two
resolutions and land the real connection on a private/internal address that
was never checked.

These tests exercise the adapter in isolation (no real network): they patch
requests.adapters.HTTPAdapter.send, the one seam where the base class would
otherwise hand off to urllib3, and inspect what request.url / request.headers
look like at that boundary plus what TLS parameters were configured on the
pool manager.

Console output English only, no emoji.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from video_transcript_api.utils.pinned_ip_adapter import PinnedIPHTTPAdapter

BASE_SEND_PATH = "requests.adapters.HTTPAdapter.send"


def _prepared_request(method: str, url: str) -> requests.PreparedRequest:
    return requests.Request(method, url).prepare()


class TestSendPinsConnectionToValidatedIP:
    """The actual outgoing connection must target the pre-validated IP, not
    a freshly (and independently) re-resolved hostname."""

    def test_https_request_rewrites_host_to_pinned_ip(self):
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=True
        )
        req = _prepared_request("GET", "https://public.example.com/audio.mp3?x=1")

        with patch(BASE_SEND_PATH) as mock_send:
            mock_send.return_value = MagicMock()
            adapter.send(req, timeout=10)

        sent_request = mock_send.call_args[0][0]
        assert sent_request.url == "https://93.184.216.34/audio.mp3?x=1"

    def test_http_request_rewrites_host_to_pinned_ip(self):
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=False
        )
        req = _prepared_request("HEAD", "http://public.example.com/video.mp4")

        with patch(BASE_SEND_PATH) as mock_send:
            mock_send.return_value = MagicMock()
            adapter.send(req)

        sent_request = mock_send.call_args[0][0]
        assert sent_request.url == "http://93.184.216.34/video.mp4"

    def test_preserves_explicit_port(self):
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=True
        )
        req = _prepared_request("GET", "https://public.example.com:8443/x")

        with patch(BASE_SEND_PATH) as mock_send:
            mock_send.return_value = MagicMock()
            adapter.send(req)

        sent_request = mock_send.call_args[0][0]
        assert sent_request.url == "https://93.184.216.34:8443/x"

    def test_ipv6_pinned_ip_is_bracketed(self):
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="2001:db8::1", is_https=True
        )
        req = _prepared_request("GET", "https://public.example.com/x")

        with patch(BASE_SEND_PATH) as mock_send:
            mock_send.return_value = MagicMock()
            adapter.send(req)

        sent_request = mock_send.call_args[0][0]
        assert sent_request.url == "https://[2001:db8::1]/x"

    def test_restores_original_host_header(self):
        """Without an explicit Host header, http.client would derive it from
        the connection pool's host -- which is now the pinned IP -- breaking
        virtual-hosted origins/CDNs. The adapter must force it back."""
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=True
        )
        req = _prepared_request("GET", "https://public.example.com/x")

        with patch(BASE_SEND_PATH) as mock_send:
            mock_send.return_value = MagicMock()
            adapter.send(req)

        sent_request = mock_send.call_args[0][0]
        assert sent_request.headers["Host"] == "public.example.com"

    def test_rejects_request_for_unpinned_host(self):
        """One adapter instance is scoped to exactly one validated target.
        A request for a different host must be refused, not silently sent
        to a host that was never SSRF-validated for this pin."""
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=True
        )
        req = _prepared_request("GET", "https://someone-else.example.com/x")

        with patch(BASE_SEND_PATH) as mock_send:
            mock_send.return_value = MagicMock()
            with pytest.raises(ValueError):
                adapter.send(req)

        mock_send.assert_not_called()


class TestCertificateHostnameValidationStillApplies:
    """Pinning the TCP connection to an IP must not silently disable or
    misdirect HTTPS certificate hostname verification / SNI."""

    def test_https_pool_manager_pins_sni_and_assert_hostname_to_real_host(self):
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=True
        )

        pool_kwargs = adapter.poolmanager.connection_pool_kw
        assert pool_kwargs["server_hostname"] == "public.example.com"
        assert pool_kwargs["assert_hostname"] == "public.example.com"

    def test_http_pool_manager_does_not_set_tls_only_kwargs(self):
        """server_hostname/assert_hostname are TLS-only urllib3 pool kwargs;
        setting them for a plain HTTP pool would blow up when urllib3
        constructs the (non-TLS) HTTPConnectionPool."""
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=False
        )

        pool_kwargs = adapter.poolmanager.connection_pool_kw
        assert "server_hostname" not in pool_kwargs
        assert "assert_hostname" not in pool_kwargs
