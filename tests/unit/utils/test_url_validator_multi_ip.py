"""Unit tests for utils/url_validator.py's multi-candidate DNS pinning API
(validate_url_safe_with_ips), added for codex-review R8 #2.

Background: validate_url_safe_with_ip (singular) only ever exposed the
FIRST address a DNS resolution returned. Dual-stack / multi-node domains
routinely resolve to several public addresses (e.g. an AAAA record listed
before a reachable A record); when the first one happens to be unreachable
from the current network, a caller that only ever pins that single address
has no way to fall back to another candidate from the SAME resolution --
even though a plain, unpinned socket connection would have tried the next
getaddrinfo() candidate automatically.

validate_url_safe_with_ips exposes the full (deduped, order-preserving,
capped) candidate list so callers (see downloaders/generic.py) can retry
against the next already-validated address instead of hammering the same
dead one.

These tests cover the validator layer only (candidate list shape, cap,
dedup, backward-compatible singular wrapper, and -- most importantly --
that the existing strict "any private/reserved address anywhere in the
resolution result rejects the whole hostname" SSRF semantics is completely
unchanged by returning more than one address). The retry-across-candidates
behavior itself is covered end-to-end in
tests/unit/downloaders/test_generic_ssrf.py.

Note on test fixture IPs: the RFC 5737 documentation ranges (192.0.2.0/24,
198.51.100.0/24, 203.0.113.0/24) are classified `is_private=True` by
Python's ipaddress module, so they can't stand in for "public" addresses
here -- this file uses addresses under 93.184.216.0/24 (the historical
example.com block, already used the same way in test_generic_ssrf.py) and
other well-known public IPs instead.

Console output English only, no emoji.
"""

import socket
from unittest.mock import patch

import pytest

from video_transcript_api.utils.url_validator import (
    URLValidationError,
    validate_url_safe_with_ip,
    validate_url_safe_with_ips,
)

GETADDRINFO_PATH = "video_transcript_api.utils.url_validator.socket.getaddrinfo"


def _addrinfo(*ips):
    """Build a fake socket.getaddrinfo() result for one or more IPv4 addresses,
    in the given order -- mirrors what a real dual-stack/multi-node DNS
    answer looks like."""
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
        for ip in ips
    ]


class TestValidateUrlSafeWithIpsReturnsCandidateList:
    def test_returns_all_public_candidates_in_resolution_order(self):
        with patch(GETADDRINFO_PATH, side_effect=lambda *a, **k: _addrinfo(
            "93.184.216.1", "93.184.216.2", "93.184.216.3"
        )):
            url, ips = validate_url_safe_with_ips("https://multi.example.com/x")

        assert url == "https://multi.example.com/x"
        assert ips == ["93.184.216.1", "93.184.216.2", "93.184.216.3"]

    def test_default_cap_is_three_even_with_more_resolved_addresses(self):
        five_ips = [f"93.184.216.{i}" for i in range(1, 6)]
        with patch(GETADDRINFO_PATH, side_effect=lambda *a, **k: _addrinfo(*five_ips)):
            _, ips = validate_url_safe_with_ips("https://multi.example.com/x")

        assert ips == five_ips[:3]

    def test_custom_max_candidates_is_honored(self):
        five_ips = [f"93.184.216.{i}" for i in range(1, 6)]
        with patch(GETADDRINFO_PATH, side_effect=lambda *a, **k: _addrinfo(*five_ips)):
            _, ips = validate_url_safe_with_ips(
                "https://multi.example.com/x", max_candidates=2
            )

        assert ips == five_ips[:2]

    def test_duplicate_addresses_across_socktypes_are_deduped_preserving_order(self):
        # A real getaddrinfo() call can list the same IP twice (once per
        # socktype/protocol combination) -- the candidate list must not
        # expose the same address twice.
        raw = _addrinfo("93.184.216.1", "93.184.216.2") + _addrinfo("93.184.216.1")
        with patch(GETADDRINFO_PATH, side_effect=lambda *a, **k: raw):
            _, ips = validate_url_safe_with_ips("https://multi.example.com/x")

        assert ips == ["93.184.216.1", "93.184.216.2"]


class TestPrivateIpRejectionSemanticsUnchanged:
    """The existing strict rule -- ANY private/reserved address found among
    the resolution results rejects the whole hostname, regardless of how
    many public addresses are also present -- must survive returning a
    candidate list instead of a single IP. This is a deliberate design
    choice being preserved, not relaxed."""

    def test_private_ip_after_a_public_one_still_rejects_the_whole_hostname(self):
        with patch(GETADDRINFO_PATH, side_effect=lambda *a, **k: _addrinfo(
            "93.184.216.1", "10.0.0.5"
        )):
            with pytest.raises(URLValidationError):
                validate_url_safe_with_ips("https://mixed.example.com/x")

    def test_private_ip_before_a_public_one_still_rejects_the_whole_hostname(self):
        """Order must not matter -- a private candidate listed FIRST (which
        would have been the one pinned under the old single-IP behavior)
        rejects the hostname just as completely as one listed later."""
        with patch(GETADDRINFO_PATH, side_effect=lambda *a, **k: _addrinfo(
            "10.0.0.5", "93.184.216.1"
        )):
            with pytest.raises(URLValidationError):
                validate_url_safe_with_ips("https://mixed.example.com/x")


class TestDnsFailureFallback:
    def test_dns_resolution_failure_returns_empty_candidate_list(self):
        def _gaierror(*args, **kwargs):
            raise socket.gaierror("Name or service not known")

        with patch(GETADDRINFO_PATH, side_effect=_gaierror):
            url, ips = validate_url_safe_with_ips("https://unresolvable.example.com/x")

        assert url == "https://unresolvable.example.com/x"
        assert ips == []


class TestLiteralIpHostname:
    def test_literal_public_ip_hostname_returns_single_element_list(self):
        # No DNS lookup needed -- getaddrinfo on a literal IP just echoes it
        # back, so no patch is required here.
        url, ips = validate_url_safe_with_ips("http://93.184.216.7/x")

        assert ips == ["93.184.216.7"]


class TestSingularWrapperBackwardCompatibility:
    """validate_url_safe_with_ip (singular) is kept for existing callers
    (e.g. tests/integration/test_failure_status_persistence.py's stub) --
    it must keep returning exactly the first candidate, not the whole list."""

    def test_returns_only_the_first_candidate(self):
        with patch(GETADDRINFO_PATH, side_effect=lambda *a, **k: _addrinfo(
            "93.184.216.1", "93.184.216.2", "93.184.216.3"
        )):
            url, ip = validate_url_safe_with_ip("https://multi.example.com/x")

        assert url == "https://multi.example.com/x"
        assert ip == "93.184.216.1"

    def test_dns_failure_returns_none(self):
        def _gaierror(*args, **kwargs):
            raise socket.gaierror("Name or service not known")

        with patch(GETADDRINFO_PATH, side_effect=_gaierror):
            _, ip = validate_url_safe_with_ip("https://unresolvable.example.com/x")

        assert ip is None
