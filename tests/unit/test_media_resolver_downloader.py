"""Unit tests for MediaResolverDownloader (T2 + P0-2 SSRF + P0-3/FORK4 403 re-resolve).

Console output English only.
"""

import pytest

from video_transcript_api.downloaders.base import BaseDownloader
from video_transcript_api.downloaders.media_resolver import MediaResolverDownloader
from video_transcript_api.errors import DownloadFailedError, ResolverResponseError


class FakeClient:
    """Counts resolve() calls; returns queued payloads (or raises)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def resolve(self, url, translate=False, force_refresh=False):
        self.calls.append({"url": url, "force_refresh": force_refresh})
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        item = self._responses[idx]
        if isinstance(item, Exception):
            raise item
        return item


def make_downloader(responses):
    dl = MediaResolverDownloader()
    dl.client = FakeClient(responses)
    return dl


DOUYIN_DATA = {
    "platform": "douyin",
    "video_id": "7123",
    "title": "hello",
    "author_name": "alice",
    "description": "desc",
    "duration": 12.5,
    "video_url": "https://cdn.example.com/v/7123.mp4",
    "provider": "tikhub",
}


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #

class TestCanHandle:
    @pytest.mark.parametrize("url", [
        "https://www.douyin.com/video/7123",
        "https://v.douyin.com/abc/",
        "https://www.xiaohongshu.com/explore/abc",
        "https://xhslink.com/abc",
    ])
    def test_supported(self, url):
        assert make_downloader([DOUYIN_DATA]).can_handle(url) is True

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=x",
        "https://www.bilibili.com/video/BV1",
        "https://example.com/x",
        "",
    ])
    def test_unsupported(self, url):
        assert make_downloader([DOUYIN_DATA]).can_handle(url) is False


# --------------------------------------------------------------------------- #
# FORK1-A: one resolve feeds both metadata + download_info
# --------------------------------------------------------------------------- #

class TestSharedResolveCache:
    def test_single_resolve_for_metadata_and_download(self):
        dl = make_downloader([DOUYIN_DATA])
        url = "https://www.douyin.com/video/7123"
        md = dl.get_metadata(url)
        di = dl.get_download_info(url)
        assert dl.client.calls and len(dl.client.calls) == 1  # only one network call
        assert md.video_id == "7123"
        assert md.platform == "douyin"
        assert md.author == "alice"
        assert md.duration == 12.5
        assert di.download_url == DOUYIN_DATA["video_url"]

    def test_normalized_url_cache_hit_across_variants(self):
        dl = make_downloader([DOUYIN_DATA])
        dl.get_metadata("https://www.douyin.com/video/7123/")
        dl.get_metadata("https://www.douyin.com/video/7123")  # trailing slash variant
        assert len(dl.client.calls) == 1


# --------------------------------------------------------------------------- #
# file_ext inference + get_subtitle
# --------------------------------------------------------------------------- #

class TestDownloadInfoMapping:
    def test_file_ext_from_url(self):
        dl = make_downloader([DOUYIN_DATA])
        di = dl.get_download_info("https://www.douyin.com/video/7123")
        assert di.file_ext == "mp4"
        assert di.filename == "douyin_7123.mp4"

    def test_file_ext_defaults_mp4_when_no_suffix(self):
        data = dict(DOUYIN_DATA, video_url="https://cdn.example.com/stream?id=7123")
        dl = make_downloader([data])
        di = dl.get_download_info("https://www.douyin.com/video/7123")
        assert di.file_ext == "mp4"

    def test_get_subtitle_none(self):
        dl = make_downloader([DOUYIN_DATA])
        assert dl.get_subtitle("https://www.douyin.com/video/7123") is None

    def test_missing_video_url_raises_response_error(self):
        data = dict(DOUYIN_DATA)
        data.pop("video_url")
        dl = make_downloader([data])
        with pytest.raises(ResolverResponseError):
            dl.get_download_info("https://www.douyin.com/video/7123")

    def test_astronomically_large_duration_degrades_to_none_without_raising(self):
        """A JSON-legal but astronomically large integer duration (e.g.
        10**400, which can survive response.json() deserialization as a
        legit Python int -- json.loads has no size limit on integers) makes
        float() raise OverflowError instead of the TypeError/ValueError this
        call site already guards against. get_metadata() must not blow up on
        a malformed/adversarial resolver response -- it should degrade
        duration to None like any other unparseable value."""
        data = dict(DOUYIN_DATA, duration=10 ** 400)
        dl = make_downloader([data])
        md = dl.get_metadata("https://www.douyin.com/video/7123")  # must not raise
        assert md.duration is None


# --------------------------------------------------------------------------- #
# P0-2: SSRF validation on resolver-returned video_url
# --------------------------------------------------------------------------- #

class TestSSRF:
    def test_unsafe_video_url_blocked(self):
        # 169.254.169.254 cloud metadata endpoint must be blocked
        data = dict(DOUYIN_DATA, video_url="http://169.254.169.254/latest/meta-data")
        dl = make_downloader([data])
        with pytest.raises(ResolverResponseError):
            dl.get_download_info("https://www.douyin.com/video/7123")


# --------------------------------------------------------------------------- #
# P0-3 / FORK4: 403/expired -> force_refresh re-resolve -> retry
# --------------------------------------------------------------------------- #

class TestDownloadReResolve:
    def test_reresolve_on_download_failure(self, monkeypatch):
        fresh = dict(DOUYIN_DATA, video_url="https://cdn.example.com/v/7123-fresh.mp4")
        dl = make_downloader([DOUYIN_DATA, fresh])
        # populate reverse map
        di = dl.get_download_info("https://www.douyin.com/video/7123")

        sequence = {"n": 0}

        def fake_super(self, url, filename, max_retries=3):
            sequence["n"] += 1
            # first (stale) url fails, fresh url succeeds
            return "/tmp/ok.mp4" if "fresh" in url else None

        monkeypatch.setattr(BaseDownloader, "download_file", fake_super)
        out = dl.download_file(di.download_url, di.filename)
        assert out == "/tmp/ok.mp4"
        # second resolve was force_refresh
        assert dl.client.calls[-1]["force_refresh"] is True

    def test_reresolve_still_fails_raises(self, monkeypatch):
        fresh = dict(DOUYIN_DATA, video_url="https://cdn.example.com/v/7123-fresh.mp4")
        dl = make_downloader([DOUYIN_DATA, fresh])
        di = dl.get_download_info("https://www.douyin.com/video/7123")

        monkeypatch.setattr(BaseDownloader, "download_file", lambda self, u, f, max_retries=3: None)
        with pytest.raises(DownloadFailedError):
            dl.download_file(di.download_url, di.filename)

    def test_unknown_url_raises_without_reresolve(self, monkeypatch):
        dl = make_downloader([DOUYIN_DATA])
        monkeypatch.setattr(BaseDownloader, "download_file", lambda self, u, f, max_retries=3: None)
        with pytest.raises(DownloadFailedError):
            dl.download_file("https://cdn.example.com/unknown.mp4", "x.mp4")
        # no resolve happened (url not in reverse map)
        assert len(dl.client.calls) == 0
