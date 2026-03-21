"""
Downloader factory unit tests.

Covers:
- create_downloader returns correct downloader type for each platform URL
- Unknown URLs fall back to GenericDownloader
- Various URL formats (with/without www, https/http, path params)

All console output must be in English only (no emoji, no Chinese).
"""

import pytest

from video_transcript_api.downloaders.factory import create_downloader
from video_transcript_api.downloaders.youtube import YoutubeDownloader
from video_transcript_api.downloaders.bilibili import BilibiliDownloader
from video_transcript_api.downloaders.douyin import DouyinDownloader
from video_transcript_api.downloaders.xiaohongshu import XiaohongshuDownloader
from video_transcript_api.downloaders.xiaoyuzhou import XiaoyuzhouDownloader
from video_transcript_api.downloaders.generic import GenericDownloader


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

class TestYoutubeRouting:
    """Factory should return YoutubeDownloader for YouTube URLs."""

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=abc123",
        "http://www.youtube.com/watch?v=abc123",
        "https://youtube.com/watch?v=abc123",
        "https://www.youtube.com/shorts/abc123",
        "https://youtu.be/abc123",
        "http://youtu.be/abc123",
        "https://www.youtube.com/watch?v=abc123&list=PLxyz",
        "https://youtube.com/embed/abc123",
    ])
    def test_youtube_urls(self, url):
        downloader = create_downloader(url)
        assert isinstance(downloader, YoutubeDownloader), (
            f"Expected YoutubeDownloader for {url}, got {type(downloader).__name__}"
        )


# ---------------------------------------------------------------------------
# Bilibili
# ---------------------------------------------------------------------------

class TestBilibiliRouting:
    """Factory should return BilibiliDownloader for Bilibili URLs."""

    @pytest.mark.parametrize("url", [
        "https://www.bilibili.com/video/BV1xx411c7XW",
        "http://www.bilibili.com/video/BV1xx411c7XW",
        "https://bilibili.com/video/BV1xx411c7XW",
        "https://www.bilibili.com/video/av12345",
        "https://b23.tv/abc123",
        "http://b23.tv/abc123",
    ])
    def test_bilibili_urls(self, url):
        downloader = create_downloader(url)
        assert isinstance(downloader, BilibiliDownloader), (
            f"Expected BilibiliDownloader for {url}, got {type(downloader).__name__}"
        )


# ---------------------------------------------------------------------------
# Douyin
# ---------------------------------------------------------------------------

class TestDouyinRouting:
    """Factory should return DouyinDownloader for Douyin URLs."""

    @pytest.mark.parametrize("url", [
        "https://www.douyin.com/video/1234567890",
        "http://www.douyin.com/video/1234567890",
        "https://douyin.com/video/1234567890",
        "https://v.douyin.com/abc123/",
        "http://v.douyin.com/abc123/",
    ])
    def test_douyin_urls(self, url):
        downloader = create_downloader(url)
        assert isinstance(downloader, DouyinDownloader), (
            f"Expected DouyinDownloader for {url}, got {type(downloader).__name__}"
        )


# ---------------------------------------------------------------------------
# Xiaohongshu
# ---------------------------------------------------------------------------

class TestXiaohongshuRouting:
    """Factory should return XiaohongshuDownloader for Xiaohongshu URLs."""

    @pytest.mark.parametrize("url", [
        "https://www.xiaohongshu.com/explore/abc123",
        "http://www.xiaohongshu.com/explore/abc123",
        "https://xiaohongshu.com/explore/abc123",
        "https://xhslink.com/abc123",
        "http://xhslink.com/abc123",
    ])
    def test_xiaohongshu_urls(self, url):
        downloader = create_downloader(url)
        assert isinstance(downloader, XiaohongshuDownloader), (
            f"Expected XiaohongshuDownloader for {url}, got {type(downloader).__name__}"
        )


# ---------------------------------------------------------------------------
# Xiaoyuzhou
# ---------------------------------------------------------------------------

class TestXiaoyuzhouRouting:
    """Factory should return XiaoyuzhouDownloader for Xiaoyuzhou URLs."""

    @pytest.mark.parametrize("url", [
        "https://www.xiaoyuzhoufm.com/episode/abc123",
        "http://www.xiaoyuzhoufm.com/episode/abc123",
        "https://xiaoyuzhoufm.com/episode/abc123",
        "https://www.xiaoyuzhoufm.com/podcast/abc123",
    ])
    def test_xiaoyuzhou_urls(self, url):
        downloader = create_downloader(url)
        assert isinstance(downloader, XiaoyuzhouDownloader), (
            f"Expected XiaoyuzhouDownloader for {url}, got {type(downloader).__name__}"
        )


# ---------------------------------------------------------------------------
# Unknown / Generic fallback
# ---------------------------------------------------------------------------

class TestGenericFallback:
    """Unknown URLs should fall back to GenericDownloader."""

    @pytest.mark.parametrize("url", [
        "https://www.example.com/video/123",
        "https://vimeo.com/12345",
        "https://www.dailymotion.com/video/x7zzzzz",
        "https://unknown-site.org/watch?id=999",
        "http://some-random-site.net/media/clip.mp4",
    ])
    def test_unknown_urls(self, url):
        downloader = create_downloader(url)
        assert isinstance(downloader, GenericDownloader), (
            f"Expected GenericDownloader for {url}, got {type(downloader).__name__}"
        )
