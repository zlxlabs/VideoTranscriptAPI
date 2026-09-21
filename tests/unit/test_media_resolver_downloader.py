"""Unit tests for MediaResolverDownloader (T2 + P0-2 SSRF + P0-3/FORK4 403 re-resolve).

Console output English only.
"""

from unittest.mock import Mock
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

    def fetch_wechat_direct(self, sph_code):
        raise AssertionError("fetch_wechat_direct should not be called in this test")


def make_downloader(responses, base_url="http://resolver.local:8000", api_key="secret-key"):
    dl = MediaResolverDownloader()
    dl._resolver_netloc = "resolver.local:8000"
    dl._resolver_api_key = api_key
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

WECHAT_DATA = {
    "platform": "wechat_channels",
    "video_id": "AOzokRxWHz",
    "title": "视频号测试",
    "author_name": "bob",
    "description": "desc",
    "duration": 45.0,
    "video_url": "http://resolver.local:8000/api/stream/wechat_channels/AOzokRxWHz",
    "provider": "tikhub",
}

TWITTER_DATA = {
    "platform": "twitter",
    "video_id": "1234567890",
    "title": "Elon Musk tweet",
    "author_name": "elonmusk",
    "description": "desc",
    "duration": 30.0,
    "video_url": "https://video.twimg.com/ext_tw_video/1234567890/pu/vid/720x1280/xyz.mp4",
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
        "https://xhslink.cn/o/abc",
        "https://weixin.qq.com/sph/AOzokRxWHz",
        "https://x.com/someuser/status/1234567890",
        "https://twitter.com/someuser/status/1234567890",
        "https://mobile.twitter.com/someuser/status/1234567890",
        "https://www.x.com/someuser/status/1234567890",
    ])
    def test_supported(self, url):
        assert make_downloader([DOUYIN_DATA]).can_handle(url) is True

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=x",
        "https://www.bilibili.com/video/BV1",
        "https://example.com/x",
        "https://notx.com/u/status/1",
        "https://x.com.evil.com/u/status/1",
        "https://example.com/?q=x.com",
        "https://example.com/notx.com/status/1",
        "https://example.com/x.com/status/1",
        "",
    ])
    def test_unsupported(self, url):
        assert make_downloader([DOUYIN_DATA]).can_handle(url) is False

    @pytest.mark.parametrize("url,expected_id", [
        ("https://x.com/someuser/status/1234567890", "1234567890"),
        ("https://twitter.com/someuser/status/1234567890?s=20", "1234567890"),
        ("https://twitter.com/someuser/statuses/9876543210", "9876543210"),
    ])
    def test_extract_video_id_twitter(self, url, expected_id):
        dl = make_downloader([TWITTER_DATA])
        assert dl.extract_video_id(url) == expected_id


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

    def test_twitter_metadata_and_download_info(self):
        dl = make_downloader([TWITTER_DATA])
        url = "https://x.com/elonmusk/status/1234567890"
        md = dl.get_metadata(url)
        di = dl.get_download_info(url)
        assert len(dl.client.calls) == 1
        assert md.platform == "twitter"
        assert md.video_id == "1234567890"
        assert md.author == "elonmusk"
        assert md.title == "Elon Musk tweet"
        assert md.duration == 30.0
        assert di.download_url == TWITTER_DATA["video_url"]
        assert di.file_ext == "mp4"
        assert di.filename == "twitter_1234567890.mp4"


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

    def test_string_overflow_duration_degrades_to_none(self):
        """A string duration like "1e309" does NOT raise OverflowError --
        Python's float() str-parsing silently overflows to inf instead of
        throwing -- so the existing try/except (TypeError, ValueError,
        OverflowError) around float() never even fires. Without an explicit
        isfinite check afterwards, VideoMetadata.duration ends up holding
        inf, a non-finite value exposed straight to downstream callers."""
        data = dict(DOUYIN_DATA, duration="1e309")
        dl = make_downloader([data])
        md = dl.get_metadata("https://www.douyin.com/video/7123")
        assert md.duration is None

    def test_numeric_overflow_duration_degrades_to_none(self):
        """A JSON-decoded float duration that is already inf (e.g. the
        literal 1e309, which overflows to inf at parse time since it exceeds
        the double range) must degrade to None just like the string case."""
        data = dict(DOUYIN_DATA, duration=1e309)
        dl = make_downloader([data])
        md = dl.get_metadata("https://www.douyin.com/video/7123")
        assert md.duration is None

    def test_negative_duration_degrades_to_none(self):
        """Duration can never be negative -- a negative value from a
        malformed/adversarial resolver response must be treated as invalid,
        same as an unparseable or non-finite one."""
        data = dict(DOUYIN_DATA, duration=-10.0)
        dl = make_downloader([data])
        md = dl.get_metadata("https://www.douyin.com/video/7123")
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


# --------------------------------------------------------------------------- #
# extract_video_id
# --------------------------------------------------------------------------- #

class TestExtractVideoId:
    def test_extract_video_id_wechat_channels(self):
        dl = make_downloader([])
        assert dl.extract_video_id("https://weixin.qq.com/sph/AOzokRxWHz") == "AOzokRxWHz"


# --------------------------------------------------------------------------- #
# conditional download headers (X-API-Key only for resolver domain)
# --------------------------------------------------------------------------- #

class TestConditionalDownloadHeaders:
    def test_cdn_domain_does_not_include_api_key_header(self, monkeypatch):
        monkeypatch.setattr(BaseDownloader, "_validate_media_file", lambda self, path: True)
        dl = make_downloader([DOUYIN_DATA], api_key="my-secret-key")
        di = dl.get_download_info("https://www.douyin.com/video/7123")

        captured_headers = []

        def fake_get(url, headers=None, stream=True, timeout=60):
            captured_headers.append((url, headers))
            mock_resp = Mock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.headers = {"Content-Length": "100"}
            mock_resp.iter_content.return_value = [b"fake_mp4_bytes"]
            return mock_resp

        import requests
        monkeypatch.setattr(requests, "get", fake_get)
        out = dl.download_file(di.download_url, di.filename)
        assert out is not None
        assert len(captured_headers) == 1
        url, headers = captured_headers[0]
        assert url == "https://cdn.example.com/v/7123.mp4"
        assert headers is None or "X-API-Key" not in headers

    def test_reresolve_applies_correct_headers_on_retry(self, monkeypatch):
        monkeypatch.setattr(BaseDownloader, "_validate_media_file", lambda self, path: True)
        stale = dict(DOUYIN_DATA, video_url="https://cdn.example.com/v/7123-stale.mp4")
        fresh = dict(DOUYIN_DATA, video_url="https://cdn.example.com/v/7123-fresh.mp4")
        dl = make_downloader([stale, fresh], api_key="my-secret-key")
        di = dl.get_download_info("https://www.douyin.com/video/7123")

        captured_headers = []
        attempt = {"count": 0}

        def fake_get(url, headers=None, stream=True, timeout=60):
            attempt["count"] += 1
            captured_headers.append((url, headers))
            if "stale" in url:
                import requests
                raise requests.exceptions.HTTPError("403 Forbidden")
            mock_resp = Mock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.headers = {"Content-Length": "100"}
            mock_resp.iter_content.return_value = [b"fake_mp4_bytes"]
            return mock_resp

        import requests
        monkeypatch.setattr(requests, "get", fake_get)
        out = dl.download_file(di.download_url, di.filename, max_retries=1)
        assert out is not None
        assert len(captured_headers) >= 2
        fresh_call_headers = [h for u, h in captured_headers if "fresh" in u]
        assert fresh_call_headers
        assert fresh_call_headers[0] is None or "X-API-Key" not in (fresh_call_headers[0] or {})


# --------------------------------------------------------------------------- #
# variants lowest bitrate selection (ASR optimization)
# --------------------------------------------------------------------------- #

TWITTER_VARIANTS = [
    {
        "url": "https://video.twimg.com/ext_tw_video/1234567890/pu/vid/avc1/480x270/low.mp4",
        "bitrate": 256000,
        "width": 480,
        "height": 270,
        "quality": "270p",
    },
    {
        "url": "https://video.twimg.com/ext_tw_video/1234567890/pu/vid/avc1/1280x720/mid.mp4",
        "bitrate": 832000,
        "width": 1280,
        "height": 720,
        "quality": "720p",
    },
    {
        "url": "https://video.twimg.com/ext_tw_video/1234567890/pu/vid/avc1/1920x1080/high.mp4",
        "bitrate": 2176000,
        "width": 1920,
        "height": 1080,
        "quality": "1080p",
    },
]

TWITTER_DATA_WITH_VARIANTS = dict(
    TWITTER_DATA,
    variants=TWITTER_VARIANTS,
)


class TestVariantsSelection:
    def test_select_lowest_bitrate_variant(self):
        dl = make_downloader([TWITTER_DATA_WITH_VARIANTS])
        url = "https://x.com/someuser/status/1234567890"
        di = dl.get_download_info(url)
        # 1. download_url equals variants[0].url rather than top-level video_url
        assert di.download_url == TWITTER_VARIANTS[0]["url"]
        assert di.download_url != TWITTER_DATA["video_url"]
        # 2. file_ext is inferred properly
        assert di.file_ext == "mp4"
        assert di.filename == "twitter_1234567890.mp4"
        # 3. extra does not add unexpected fields
        assert di.extra == {"provider": "tikhub"}

    @pytest.mark.parametrize("variants_val", [
        None,
        [],
    ])
    def test_variants_fallback_null_or_empty(self, variants_val):
        data = dict(TWITTER_DATA, variants=variants_val)
        dl = make_downloader([data])
        di = dl.get_download_info("https://x.com/someuser/status/1234567890")
        assert di.download_url == TWITTER_DATA["video_url"]

    def test_variants_fallback_missing_key(self):
        data = dict(TWITTER_DATA)
        data.pop("variants", None)
        dl = make_downloader([data])
        di = dl.get_download_info("https://x.com/someuser/status/1234567890")
        assert di.download_url == TWITTER_DATA["video_url"]

    @pytest.mark.parametrize("invalid_variants", [
        "not-a-list",
        123,
        {"url": "https://example.com/a.mp4"},
        [123],
        ["https://example.com/a.mp4"],
        [{}],
        [{"url": ""}],
        [{"url": "   "}],
        [{"url": None}],
        [{"url": 12345}],
    ])
    def test_malformed_variants_raises_resolver_response_error(self, invalid_variants):
        data = dict(TWITTER_DATA, variants=invalid_variants)
        dl = make_downloader([data])
        with pytest.raises(ResolverResponseError):
            dl.get_download_info("https://x.com/someuser/status/1234567890")

    def test_selected_variant_registered_in_reverse_map(self):
        dl = make_downloader([TWITTER_DATA_WITH_VARIANTS])
        url = "https://x.com/someuser/status/1234567890"
        di = dl.get_download_info(url)
        normalized_url = dl._normalize_url(url)
        assert dl._video_url_to_page.get(di.download_url) == normalized_url

    def test_unsafe_variant_url_blocked_by_ssrf(self):
        unsafe_variants = [
            {"url": "http://169.254.169.254/latest/meta-data", "bitrate": 100000},
            {"url": "https://video.twimg.com/safe.mp4", "bitrate": 500000},
        ]
        data = dict(TWITTER_DATA, variants=unsafe_variants)
        dl = make_downloader([data])
        with pytest.raises(ResolverResponseError):
            dl.get_download_info("https://x.com/someuser/status/1234567890")

    def test_download_reresolve_with_variants(self, monkeypatch):
        stale = dict(TWITTER_DATA_WITH_VARIANTS)
        fresh_variants = [
            {
                "url": "https://video.twimg.com/ext_tw_video/fresh/low.mp4",
                "bitrate": 256000,
                "quality": "270p",
            },
            {
                "url": "https://video.twimg.com/ext_tw_video/fresh/high.mp4",
                "bitrate": 2176000,
                "quality": "1080p",
            },
        ]
        fresh = dict(TWITTER_DATA, video_url="https://video.twimg.com/fresh-default.mp4", variants=fresh_variants)
        dl = make_downloader([stale, fresh])
        di = dl.get_download_info("https://x.com/someuser/status/1234567890")
        assert di.download_url == TWITTER_VARIANTS[0]["url"]

        downloaded_urls = []

        def fake_super(self, url, filename, max_retries=3):
            downloaded_urls.append(url)
            return "/tmp/ok.mp4" if "fresh" in url else None

        monkeypatch.setattr(BaseDownloader, "download_file", fake_super)
        out = dl.download_file(di.download_url, di.filename)
        assert out == "/tmp/ok.mp4"
        assert dl.client.calls[-1]["force_refresh"] is True
        # Verify force_refresh selected fresh variants[0] rather than fresh video_url
        assert fresh_variants[0]["url"] in downloaded_urls
        assert dl._video_url_to_page.get(fresh_variants[0]["url"]) == dl._normalize_url("https://x.com/someuser/status/1234567890")

