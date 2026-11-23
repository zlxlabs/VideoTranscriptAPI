"""
Downloader factory and platform handler tests.

Covers:
- Factory creates correct downloader for each platform URL
- Unknown URLs get GenericDownloader (fallback)
- Platform-specific can_handle and extract methods

All console output must be in English only (no emoji, no Chinese).
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


from video_transcript_api.downloaders import create_downloader
from video_transcript_api.downloaders.douyin import DouyinDownloader
from video_transcript_api.downloaders.bilibili import BilibiliDownloader
from video_transcript_api.downloaders.xiaohongshu import XiaohongshuDownloader
from video_transcript_api.downloaders.youtube import YoutubeDownloader
from video_transcript_api.downloaders.xiaoyuzhou import USER_AGENT, XiaoyuzhouDownloader
from video_transcript_api.downloaders.generic import GenericDownloader


class TestDownloaderFactory(unittest.TestCase):
    """Test downloader factory routing."""

    def test_create_downloader_douyin(self):
        """Douyin URL should create DouyinDownloader."""
        downloader = create_downloader("https://v.douyin.com/sample")
        self.assertIsInstance(downloader, DouyinDownloader)

    def test_create_downloader_bilibili(self):
        """Bilibili URL should create BilibiliDownloader."""
        downloader = create_downloader("https://www.bilibili.com/video/BV1234")
        self.assertIsInstance(downloader, BilibiliDownloader)

    def test_create_downloader_xiaohongshu(self):
        """Xiaohongshu URL should create XiaohongshuDownloader."""
        downloader = create_downloader("https://www.xiaohongshu.com/explore/12345")
        self.assertIsInstance(downloader, XiaohongshuDownloader)

    def test_create_downloader_youtube(self):
        """YouTube URL should create YoutubeDownloader."""
        downloader = create_downloader("https://www.youtube.com/watch?v=12345")
        self.assertIsInstance(downloader, YoutubeDownloader)

    def test_create_downloader_xiaoyuzhou(self):
        """Xiaoyuzhou URL should create XiaoyuzhouDownloader."""
        downloader = create_downloader("https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597")
        self.assertIsInstance(downloader, XiaoyuzhouDownloader)

    def test_create_downloader_unsupported_returns_generic(self):
        """Unknown URL should return GenericDownloader (not None)."""
        downloader = create_downloader("https://www.unsupported.com/video/12345")
        self.assertIsInstance(downloader, GenericDownloader)


class TestXiaoyuzhouDownloaderBasic(unittest.TestCase):
    """Test Xiaoyuzhou downloader basic methods (no network)."""

    def test_can_handle(self):
        """Should handle xiaoyuzhoufm.com URLs."""
        downloader = XiaoyuzhouDownloader()
        self.assertTrue(downloader.can_handle("https://www.xiaoyuzhoufm.com/episode/12345"))
        self.assertFalse(downloader.can_handle("https://www.youtube.com/watch?v=12345"))
        self.assertFalse(downloader.can_handle("https://www.bilibili.com/video/BV12345"))

    def test_extract_episode_id(self):
        """Should extract episode ID from URL."""
        downloader = XiaoyuzhouDownloader()
        episode_id = downloader._extract_episode_id(
            "https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597"
        )
        self.assertEqual(episode_id, "687893e0a12f9ff06a98a597")

    def test_extract_episode_id_invalid(self):
        """Invalid URL should raise ValueError."""
        downloader = XiaoyuzhouDownloader()
        with self.assertRaises(ValueError):
            downloader._extract_episode_id("https://www.example.com/invalid")

    def test_get_subtitle(self):
        """Xiaoyuzhou has no subtitle support."""
        downloader = XiaoyuzhouDownloader()
        result = downloader.get_subtitle("https://www.xiaoyuzhoufm.com/episode/12345")
        self.assertIsNone(result)

    def test_download_headers_browser_ua(self):
        """Audio CDN 403s bot UAs, so download headers must carry the browser UA."""
        downloader = XiaoyuzhouDownloader()
        self.assertEqual(downloader.download_headers.get("User-Agent"), USER_AGENT)
        self.assertTrue(USER_AGENT.startswith("Mozilla/5.0"))

    def test_download_file_passes_browser_ua_to_requests_get(self):
        """download_file must pass the browser UA to base.requests.get."""
        downloader = XiaoyuzhouDownloader()
        fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            mock_response = MagicMock()
            mock_response.headers = {}
            mock_response.iter_content.return_value = iter([b"fake audio bytes"])
            mock_tm = MagicMock()
            mock_tm.create_temp_file.return_value = tmp_path
            with patch(
                "video_transcript_api.downloaders.base.requests.get",
                return_value=mock_response,
            ) as mock_get, patch(
                "video_transcript_api.downloaders.base.get_temp_manager",
                return_value=mock_tm,
            ), patch.object(downloader, "_validate_media_file", return_value=True):
                result = downloader.download_file(
                    "https://tk.wavpub.com/WPTK_vAi0Ca5dsV7Dr5l5.mp3",
                    "WPTK_vAi0Ca5dsV7Dr5l5.mp3",
                )
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        self.assertIsNotNone(result)
        self.assertIsNotNone(mock_get.call_args)
        headers = mock_get.call_args.kwargs.get("headers")
        self.assertEqual(headers, {"User-Agent": USER_AGENT})


if __name__ == '__main__':
    unittest.main()
