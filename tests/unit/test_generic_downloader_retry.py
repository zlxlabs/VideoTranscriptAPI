"""GenericDownloader.download_file retry policy: failed attempts must be
spaced by a backoff delay, so transient outages (e.g. the file server
restarting during a deploy) are survived instead of burning all retries
within the same second.

Regression for prod incident 2026-07-09: 3 connection-refused retries all
fired in the same second while the recorder was mid-redeploy.

Console output English only.
"""

import socket

import requests

from video_transcript_api.downloaders.generic import GenericDownloader

# GenericDownloader routes every request through validate_url_safe_with_ip's
# IP-pinned dispatch (see downloaders/generic.py::_dispatch_pinned_request).
# Since codex-review R6 #1, a validation-time DNS failure fails closed
# (raises InvalidURLError) instead of falling back to a plain requests.get()
# call -- so "files.internal" (not a real resolvable domain) must have its
# DNS lookup faked to succeed, and the simulated "connection refused" must
# be injected at the transport layer the pinned dispatch actually reaches
# (requests.adapters.HTTPAdapter.send), matching the pattern used in
# tests/unit/downloaders/test_generic_ssrf.py.
GETADDRINFO_PATH = "video_transcript_api.utils.url_validator.socket.getaddrinfo"
BASE_SEND_PATH = "requests.adapters.HTTPAdapter.send"


def _public_addrinfo(*args, **kwargs):
    """Fake socket.getaddrinfo returning a single public IPv4 address."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


class _StubTempManager:
    def __init__(self, task_dir):
        self._task_dir = task_dir

    def get_current_task_dir(self):
        return self._task_dir


def _make_downloader(tmp_path):
    downloader = GenericDownloader()
    downloader.temp_manager = _StubTempManager(str(tmp_path / "task"))
    return downloader


class TestDownloadRetryBackoff:
    def test_connection_errors_are_retried_with_backoff(self, tmp_path, monkeypatch):
        """All attempts refused -> returns None, with a positive, increasing
        delay before each retry (attempts - 1 sleeps in total)."""
        downloader = _make_downloader(tmp_path)

        def refuse(*args, **kwargs):
            raise requests.exceptions.ConnectionError("connection refused")

        monkeypatch.setattr(GETADDRINFO_PATH, _public_addrinfo)
        monkeypatch.setattr(BASE_SEND_PATH, refuse)
        sleeps = []
        monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))

        result = downloader.download_file("http://files.internal/rec.mp4", "rec.mp4")

        assert result is None
        assert len(sleeps) == 2, "expected one backoff gap between each retry"
        assert all(s > 0 for s in sleeps)
        assert sleeps == sorted(sleeps), "backoff should not shrink"
