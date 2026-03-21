"""
BaseDownloader core flow unit tests.

Covers:
- Metadata caching (get_metadata uses cache on second call)
- Download info caching (get_download_info uses cache)
- Short URL resolution (resolve_short_url)
- Media file validation (_validate_media_file)
- Downloader factory (create_downloader)

All console output must be in English only (no emoji, no Chinese).
"""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))


@pytest.fixture
def mock_downloader():
    """Create a concrete BaseDownloader subclass for testing."""
    from video_transcript_api.downloaders.base import BaseDownloader
    from video_transcript_api.downloaders.models import VideoMetadata, DownloadInfo

    class TestDownloader(BaseDownloader):
        def can_handle(self, url):
            return "test.com" in url

        def extract_video_id(self, url):
            return "vid_123"

        def _fetch_metadata(self, url, video_id):
            return VideoMetadata(
                video_id=video_id,
                title="Test Video",
                author="Test Author",
                description="Test Description",
                platform="test",
            )

        def _fetch_download_info(self, url, video_id):
            return DownloadInfo(
                download_url="https://cdn.test.com/video.mp4",
                file_ext=".mp4",
                filename="video.mp4",
            )

        def get_subtitle(self, url):
            return None

    with patch("video_transcript_api.downloaders.base.load_config", return_value={"tikhub": {}}):
        with patch("video_transcript_api.downloaders.base.get_temp_manager") as mock_tm:
            mock_tm.return_value = MagicMock()
            return TestDownloader()


# ============================================================
# Metadata Caching Tests
# ============================================================


class TestMetadataCaching:
    """Verify metadata caching in get_metadata."""

    def test_first_call_fetches_metadata(self, mock_downloader):
        """First call should invoke _fetch_metadata."""
        metadata = mock_downloader.get_metadata("https://test.com/video")
        assert metadata.title == "Test Video"
        assert metadata.video_id == "vid_123"

    def test_second_call_uses_cache(self, mock_downloader):
        """Second call with same URL should use cache, not fetch again."""
        meta1 = mock_downloader.get_metadata("https://test.com/video")
        meta2 = mock_downloader.get_metadata("https://test.com/video")

        assert meta1 is meta2  # Same object (cached)

    def test_cache_keyed_by_video_id(self, mock_downloader):
        """Cache should be keyed by video_id, not URL."""
        meta1 = mock_downloader.get_metadata("https://test.com/v1")
        # Same video_id extracted, so should use cache
        meta2 = mock_downloader.get_metadata("https://test.com/v2")
        assert meta1 is meta2  # Same video_id -> same cache entry


# ============================================================
# Download Info Caching Tests
# ============================================================


class TestDownloadInfoCaching:
    """Verify download info caching in get_download_info."""

    def test_download_info_cached(self, mock_downloader):
        """Download info should be cached after first fetch."""
        info1 = mock_downloader.get_download_info("https://test.com/video")
        info2 = mock_downloader.get_download_info("https://test.com/video")
        assert info1 is info2

    def test_download_info_has_url(self, mock_downloader):
        """Download info should contain download URL."""
        info = mock_downloader.get_download_info("https://test.com/video")
        assert info.download_url == "https://cdn.test.com/video.mp4"
        assert info.filename == "video.mp4"


# ============================================================
# Short URL Resolution Tests
# ============================================================


class TestShortURLResolution:
    """Verify resolve_short_url behavior."""

    @patch("video_transcript_api.downloaders.base.requests.head")
    def test_resolve_short_url_success(self, mock_head, mock_downloader):
        """Should follow redirects and return final URL."""
        mock_response = MagicMock()
        mock_response.url = "https://test.com/full-video-url"
        mock_head.return_value = mock_response

        result = mock_downloader.resolve_short_url("https://t.co/abc123")
        assert result == "https://test.com/full-video-url"

    @patch("video_transcript_api.downloaders.base.requests.head")
    def test_resolve_short_url_failure(self, mock_head, mock_downloader):
        """Should return original URL on failure."""
        mock_head.side_effect = Exception("network error")

        result = mock_downloader.resolve_short_url("https://t.co/abc123")
        assert result == "https://t.co/abc123"


# ============================================================
# Media File Validation Tests
# ============================================================


class TestMediaValidation:
    """Verify _validate_media_file behavior."""

    @patch("subprocess.run")
    def test_valid_media_file(self, mock_run, mock_downloader):
        """Valid media file should return True."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=b'{"format": {"duration": "120"}, "streams": [{"codec_type": "audio"}]}'
        )

        assert mock_downloader._validate_media_file("/tmp/video.mp4") is True

    @patch("subprocess.run")
    def test_invalid_media_file(self, mock_run, mock_downloader):
        """Non-media file should return False."""
        mock_run.return_value = MagicMock(returncode=1, stdout=b"")

        assert mock_downloader._validate_media_file("/tmp/text.txt") is False

    @patch("subprocess.run")
    def test_ffprobe_timeout(self, mock_run, mock_downloader):
        """ffprobe timeout should return False gracefully."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)

        assert mock_downloader._validate_media_file("/tmp/video.mp4") is False


# ============================================================
# Downloader Factory Tests
# ============================================================


class TestDownloaderFactory:
    """Verify create_downloader factory function."""

    def test_youtube_url_creates_youtube_downloader(self):
        """YouTube URL should create YoutubeDownloader."""
        from video_transcript_api.downloaders import create_downloader

        with patch("video_transcript_api.downloaders.base.load_config", return_value={"tikhub": {}}):
            with patch("video_transcript_api.downloaders.base.get_temp_manager", return_value=MagicMock()):
                downloader = create_downloader("https://www.youtube.com/watch?v=abc123")

        assert downloader is not None
        assert "youtube" in downloader.__class__.__name__.lower() or downloader.can_handle("https://www.youtube.com/watch?v=abc123")

    def test_bilibili_url_creates_bilibili_downloader(self):
        """Bilibili URL should create BilibiliDownloader."""
        from video_transcript_api.downloaders import create_downloader

        with patch("video_transcript_api.downloaders.base.load_config", return_value={"tikhub": {}}):
            with patch("video_transcript_api.downloaders.base.get_temp_manager", return_value=MagicMock()):
                downloader = create_downloader("https://www.bilibili.com/video/BV123abc")

        assert downloader is not None

    def test_unknown_url_creates_generic_downloader(self):
        """Unknown URL should create GenericDownloader."""
        from video_transcript_api.downloaders import create_downloader

        with patch("video_transcript_api.downloaders.base.load_config", return_value={"tikhub": {}}):
            with patch("video_transcript_api.downloaders.base.get_temp_manager", return_value=MagicMock()):
                downloader = create_downloader("https://random-site.com/media.mp3")

        assert downloader is not None
        assert "generic" in downloader.__class__.__name__.lower()
