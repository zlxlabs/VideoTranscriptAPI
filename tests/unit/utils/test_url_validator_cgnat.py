"""Regression tests for RFC 6598 Carrier-Grade NAT (CGNAT) shared address
space handling in utils/url_validator.py's SSRF protection (codex-review
R10 P1).

Background: ipaddress.IPv4Address.is_private / is_reserved do NOT classify
100.64.0.0/10 (RFC 6598 Shared Address Space -- used by carrier-grade NAT,
cloud NAT gateways, and container network overlays) as private/reserved on
the Python version this project actually runs (verified empirically:
ipaddress.ip_address("100.64.0.1").is_private == False,
.is_reserved == False, and even .is_global == False on Python 3.12.3 --
Python itself knows the address is not publicly routable, but does not
fold it into either of the two properties _is_private_ip() relied on).
Before this fix, a URL whose hostname resolved (via DNS, or was itself a
literal IP) into this range sailed straight through SSRF validation, and
GenericDownloader (the catch-all downloader used for any URL no
platform-specific downloader recognizes) would happily connect to it --
exactly the kind of internal-network target SSRF protection exists to
block.

The fix adds an explicit ipaddress.ip_network("100.64.0.0/10") membership
check inside _is_private_ip(), so the block does not depend on any
particular Python version's is_private/is_reserved behavior.

_is_private_ip() is the single judgment function shared by all three call
paths that need to reject this range; these tests cover all three
explicitly rather than assuming coverage:
- the literal-IP hostname path (_check_dangerous_hostname)
- the DNS-resolution path (_check_resolved_ip)
- the multi-candidate path introduced in R8 (validate_url_safe_with_ips),
  which must not have its own, divergent private-IP judgment

Range boundaries (100.64.0.0 - 100.127.255.255) are tested on both sides
so the fix does not spill over and start rejecting adjacent public
addresses (100.63.255.255 just below, 100.128.0.0 just above).

Console output English only, no emoji.
"""

import ipaddress
import socket
from unittest.mock import patch

import pytest

from video_transcript_api.utils.url_validator import (
    URLValidationError,
    _is_private_ip,
    validate_url_safe,
    validate_url_safe_with_ips,
)

# validate_url_safe's own DNS resolution call, patched at its source module
# (mirrors tests/unit/utils/test_url_validator_multi_ip.py's pattern).
GETADDRINFO_PATH = "video_transcript_api.utils.url_validator.socket.getaddrinfo"


def _addrinfo(*ips):
    """Build a fake socket.getaddrinfo() result for one or more IPv4
    addresses, in the given order."""
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
        for ip in ips
    ]


class TestIsPrivateIpCgnatBoundaries:
    """Direct unit tests of _is_private_ip() -- the shared judgment
    function all three call paths funnel through."""

    @pytest.mark.parametrize("addr", ["100.64.0.0", "100.64.0.1", "100.127.255.255"])
    def test_cgnat_range_is_flagged_private(self, addr):
        assert _is_private_ip(ipaddress.ip_address(addr)) is True

    @pytest.mark.parametrize("addr", ["100.63.255.255", "100.128.0.0"])
    def test_addresses_just_outside_cgnat_range_are_not_flagged_by_this_rule(self, addr):
        assert _is_private_ip(ipaddress.ip_address(addr)) is False


class TestLiteralIpHostnameCgnatPath:
    """hostname is itself an IP literal -- goes through
    _check_dangerous_hostname, never touches DNS (getaddrinfo on a literal
    IP is a local no-op, so no patch is needed here, matching
    test_url_validator_multi_ip.py's TestLiteralIpHostname)."""

    @pytest.mark.parametrize("addr", ["100.64.0.0", "100.64.0.1", "100.127.255.255"])
    def test_cgnat_literal_ip_is_blocked(self, addr):
        with pytest.raises(URLValidationError):
            validate_url_safe(f"http://{addr}/")

    @pytest.mark.parametrize("addr", ["100.63.255.255", "100.128.0.0"])
    def test_boundary_literal_ip_is_allowed(self, addr):
        result = validate_url_safe(f"http://{addr}/")
        assert result == f"http://{addr}/"


class TestDnsResolvedCgnatPath:
    """hostname is a domain name that resolves (via DNS) into the CGNAT
    range -- goes through _check_resolved_ip."""

    def test_domain_resolving_into_cgnat_range_is_blocked(self):
        with patch(GETADDRINFO_PATH, side_effect=lambda *a, **k: _addrinfo("100.64.0.1")):
            with pytest.raises(URLValidationError):
                validate_url_safe("https://cgnat.example.com/x")

    def test_domain_resolving_just_outside_cgnat_range_is_allowed(self):
        with patch(GETADDRINFO_PATH, side_effect=lambda *a, **k: _addrinfo("100.63.255.255")):
            result = validate_url_safe("https://public-boundary.example.com/x")
        assert result == "https://public-boundary.example.com/x"


class TestMultiCandidatePathCoversCgnat:
    """validate_url_safe_with_ips (R8) must reject a CGNAT candidate too --
    confirmed explicitly since it is its own public entry point, not
    assumed from the other two paths' coverage."""

    def test_cgnat_ip_among_candidates_rejects_whole_hostname(self):
        with patch(
            GETADDRINFO_PATH,
            side_effect=lambda *a, **k: _addrinfo("93.184.216.1", "100.64.5.5"),
        ):
            with pytest.raises(URLValidationError):
                validate_url_safe_with_ips("https://cgnat-multi.example.com/x")

    def test_boundary_public_candidates_are_returned_uncensored(self):
        with patch(
            GETADDRINFO_PATH,
            side_effect=lambda *a, **k: _addrinfo("100.63.255.255", "100.128.0.0"),
        ):
            _, ips = validate_url_safe_with_ips("https://boundary-multi.example.com/x")
        assert ips == ["100.63.255.255", "100.128.0.0"]
