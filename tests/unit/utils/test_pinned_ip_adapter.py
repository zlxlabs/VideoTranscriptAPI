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
BASE_PROXY_MANAGER_FOR_PATH = "requests.adapters.HTTPAdapter.proxy_manager_for"


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


class TestHostHeaderPortHandling:
    """codex-review R6 #3: the Host header must reflect the *original*
    request's port, not just the bare hostname. A previous version always
    wrote `Host: <hostname>` with no port at all, so a non-default-port
    request (e.g. `https://host:8443/...`) lost its port on the wire --
    Host+port-routed origins/reverse-proxies could misroute or reject it.
    Per RFC 7230 3.2.2, the default port for the scheme should be omitted;
    any other port must be included. IPv6 literal hosts must be
    bracketed."""

    def test_https_default_port_443_is_omitted(self):
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=True
        )
        req = _prepared_request("GET", "https://public.example.com:443/x")

        with patch(BASE_SEND_PATH) as mock_send:
            mock_send.return_value = MagicMock()
            adapter.send(req)

        sent_request = mock_send.call_args[0][0]
        assert sent_request.headers["Host"] == "public.example.com"

    def test_http_default_port_80_is_omitted(self):
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=False
        )
        req = _prepared_request("GET", "http://public.example.com:80/x")

        with patch(BASE_SEND_PATH) as mock_send:
            mock_send.return_value = MagicMock()
            adapter.send(req)

        sent_request = mock_send.call_args[0][0]
        assert sent_request.headers["Host"] == "public.example.com"

    def test_non_default_port_8443_is_included(self):
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=True
        )
        req = _prepared_request("GET", "https://public.example.com:8443/x")

        with patch(BASE_SEND_PATH) as mock_send:
            mock_send.return_value = MagicMock()
            adapter.send(req)

        sent_request = mock_send.call_args[0][0]
        assert sent_request.headers["Host"] == "public.example.com:8443"

    def test_custom_http_port_is_included(self):
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=False
        )
        req = _prepared_request("GET", "http://public.example.com:9000/x")

        with patch(BASE_SEND_PATH) as mock_send:
            mock_send.return_value = MagicMock()
            adapter.send(req)

        sent_request = mock_send.call_args[0][0]
        assert sent_request.headers["Host"] == "public.example.com:9000"

    def test_ipv6_host_header_is_bracketed_without_port(self):
        adapter = PinnedIPHTTPAdapter(
            hostname="2001:db8::1", pinned_ip="93.184.216.34", is_https=True
        )
        req = _prepared_request("GET", "https://[2001:db8::1]/x")

        with patch(BASE_SEND_PATH) as mock_send:
            mock_send.return_value = MagicMock()
            adapter.send(req)

        sent_request = mock_send.call_args[0][0]
        assert sent_request.headers["Host"] == "[2001:db8::1]"

    def test_ipv6_host_header_is_bracketed_with_port(self):
        adapter = PinnedIPHTTPAdapter(
            hostname="2001:db8::1", pinned_ip="93.184.216.34", is_https=True
        )
        req = _prepared_request("GET", "https://[2001:db8::1]:8443/x")

        with patch(BASE_SEND_PATH) as mock_send:
            mock_send.return_value = MagicMock()
            adapter.send(req)

        sent_request = mock_send.call_args[0][0]
        assert sent_request.headers["Host"] == "[2001:db8::1]:8443"


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


class TestProxyManagerForWiresTLSParameters:
    """ci-gate review (4th round): requests routes proxied requests through
    a *separate* urllib3.ProxyManager, not the direct PoolManager
    init_poolmanager() configures (see
    TestCertificateHostnameValidationStillApplies above). A prior revision
    sidestepped this by having GenericDownloader stop using environment
    proxy config entirely -- rejected as a correctness regression, since it
    silently drops requests' standard proxy support.

    The real fix overrides proxy_manager_for() to inject the exact same
    server_hostname/assert_hostname into the ProxyManager requests/urllib3
    build for the configured proxy, so the CONNECT-tunneled
    HTTPSConnectionPool used to actually reach the pinned IP *through* the
    proxy verifies TLS against the real hostname too -- symmetric with the
    direct-connection fix.

    Verified against this environment's installed versions (requests
    2.32.5, urllib3 2.6.2): requests.adapters.HTTPAdapter.
    get_connection_with_tls_context() calls `self.proxy_manager_for(proxy)`
    with NO extra kwargs -- so unlike init_poolmanager() (which receives
    caller-supplied pool_kwargs it can extend), this override cannot rely
    on the call site already carrying the right values and must inject them
    itself before delegating up the MRO.
    """

    def test_https_proxy_manager_for_injects_server_hostname_and_assert_hostname(self):
        """Spy on the parent HTTPAdapter.proxy_manager_for -- the seam
        where requests would otherwise hand off to urllib3 to build a
        ProxyManager with no idea which hostname the pinned IP belongs to
        -- and assert exactly what PinnedIPHTTPAdapter passes it."""
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=True
        )

        with patch(BASE_PROXY_MANAGER_FOR_PATH) as mock_super_proxy_manager_for:
            mock_super_proxy_manager_for.return_value = MagicMock()
            adapter.proxy_manager_for("http://proxy.internal:3128")

        mock_super_proxy_manager_for.assert_called_once()
        called_proxy = mock_super_proxy_manager_for.call_args[0][0]
        called_kwargs = mock_super_proxy_manager_for.call_args.kwargs
        assert called_proxy == "http://proxy.internal:3128"
        assert called_kwargs["server_hostname"] == "public.example.com"
        assert called_kwargs["assert_hostname"] == "public.example.com"

    def test_http_proxy_manager_for_does_not_inject_tls_kwargs(self):
        """HTTP (non-HTTPS) through a proxy has no TLS handshake -- SNI/
        certificate-hostname parameters are meaningless there. Mirrors
        init_poolmanager()'s existing is_https gate exactly."""
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=False
        )

        with patch(BASE_PROXY_MANAGER_FOR_PATH) as mock_super_proxy_manager_for:
            mock_super_proxy_manager_for.return_value = MagicMock()
            adapter.proxy_manager_for("http://proxy.internal:3128")

        mock_super_proxy_manager_for.assert_called_once()
        called_kwargs = mock_super_proxy_manager_for.call_args.kwargs
        assert "server_hostname" not in called_kwargs
        assert "assert_hostname" not in called_kwargs

    def test_https_proxy_manager_real_urllib3_propagates_to_tunnel_pool(self):
        """Deeper than the kwargs-spy test above: builds a REAL
        urllib3.ProxyManager (nothing mocked below the adapter) and
        inspects the actual CONNECT-tunneled HTTPSConnectionPool it
        constructs for the pinned IP, proving the server_hostname/
        assert_hostname kwargs genuinely reach the pool urllib3 uses to do
        the TLS handshake -- not just that PinnedIPHTTPAdapter *calls* the
        parent with the right arguments (which would still pass even if a
        urllib3/requests version mismatch silently broke the pass-through
        this override relies on)."""
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=True
        )

        proxy_manager = adapter.proxy_manager_for("http://proxy.internal:3128")

        # Mirrors adapter.poolmanager.connection_pool_kw checked for the
        # direct path in TestCertificateHostnameValidationStillApplies:
        # ProxyManager stores everything not in its own named __init__
        # params (proxy_headers, proxy_ssl_context, ...) as
        # connection_pool_kw.
        assert proxy_manager.connection_pool_kw["server_hostname"] == "public.example.com"
        assert proxy_manager.connection_pool_kw["assert_hostname"] == "public.example.com"

        # For an HTTPS target, ProxyManager.connection_from_host() delegates
        # to PoolManager.connection_from_host(), which merges
        # connection_pool_kw into the constructor args of the tunneled
        # HTTPSConnectionPool: host is the pinned IP (the CONNECT target,
        # so the proxy never re-resolves the original hostname itself),
        # assert_hostname is the real hostname (what TLS verification
        # checks the certificate against).
        tunneled_pool = proxy_manager.connection_from_host(
            "93.184.216.34", port=443, scheme="https"
        )
        assert tunneled_pool.host == "93.184.216.34"
        assert tunneled_pool.assert_hostname == "public.example.com"

    def test_http_proxy_manager_real_urllib3_no_tls_kwargs_pool_still_builds(self):
        """Plain HTTP through a proxy isn't tunneled -- the manager routes
        requests straight to the proxy itself, and building that pool with
        no TLS-only kwargs injected must not raise."""
        adapter = PinnedIPHTTPAdapter(
            hostname="public.example.com", pinned_ip="93.184.216.34", is_https=False
        )

        proxy_manager = adapter.proxy_manager_for("http://proxy.internal:3128")

        assert "server_hostname" not in proxy_manager.connection_pool_kw
        assert "assert_hostname" not in proxy_manager.connection_pool_kw

        plain_pool = proxy_manager.connection_from_host(
            "93.184.216.34", port=80, scheme="http"
        )
        assert plain_pool.host == "proxy.internal"
