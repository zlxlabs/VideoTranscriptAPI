"""
Unit tests for platform recognition in transcription service

Tests the lightweight platform and video_id extraction logic
without triggering actual downloads or API calls.
"""
import re
import pytest


class TestPlatformRecognition:
    """Test platform recognition patterns"""

    def extract_platform_and_id(self, url):
        """
        Simulate the lightweight extraction logic from transcription.py

        Returns:
            tuple: (platform, video_id)
        """
        platform = None
        video_id = None

        # Bilibili
        if 'bilibili.com' in url or 'b23.tv' in url:
            match = re.search(r'BV[a-zA-Z0-9]+', url)
            if match:
                platform = 'bilibili'
                video_id = match.group(0)
            else:
                platform = 'bilibili'

        # YouTube
        elif 'youtube.com' in url or 'youtu.be' in url:
            match = re.search(r'(?:v=|/)([a-zA-Z0-9_-]{11})', url)
            if match:
                platform = 'youtube'
                video_id = match.group(1)
            else:
                platform = 'youtube'

        # Douyin
        elif 'douyin.com' in url or 'v.douyin.com' in url:
            match = re.search(r'(?:video/|note/)(\d+)', url)
            if match:
                platform = 'douyin'
                video_id = match.group(1)
            else:
                platform = 'douyin'

        # Xiaoyuzhou
        elif 'xiaoyuzhoufm.com' in url:
            match = re.search(r'/episode/([a-z0-9]+)', url)
            if match:
                platform = 'xiaoyuzhou'
                video_id = match.group(1)
            else:
                platform = 'xiaoyuzhou'

        # Xiaohongshu
        elif 'xiaohongshu.com' in url or 'xhslink.com' in url:
            match = re.search(r'(?:explore/|discovery/item/|items/)(\w+)', url)
            if not match:
                match = re.search(r'/(\w{24})', url)
            if match:
                platform = 'xiaohongshu'
                video_id = match.group(1)
            else:
                platform = 'xiaohongshu'

        return platform, video_id

    # ========== Bilibili Tests ==========

    def test_bilibili_standard_url(self):
        """Test Bilibili standard URL with BV number"""
        url = "https://www.bilibili.com/video/BV1234567890"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform == 'bilibili'
        assert video_id == 'BV1234567890'

    def test_bilibili_short_link(self):
        """Test Bilibili short link (b23.tv) - platform recognized, ID needs resolver"""
        url = "https://b23.tv/abc123"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform == 'bilibili'
        # Short link needs resolver, so video_id may be None

    # ========== YouTube Tests ==========

    def test_youtube_watch_url(self):
        """Test YouTube watch URL"""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform == 'youtube'
        assert video_id == 'dQw4w9WgXcQ'

    def test_youtube_short_url(self):
        """Test YouTube short URL (youtu.be)"""
        url = "https://youtu.be/dQw4w9WgXcQ"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform == 'youtube'
        assert video_id == 'dQw4w9WgXcQ'

    def test_youtube_embed_url(self):
        """Test YouTube embed URL"""
        url = "https://www.youtube.com/embed/dQw4w9WgXcQ"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform == 'youtube'
        assert video_id == 'dQw4w9WgXcQ'

    # ========== Douyin Tests ==========

    def test_douyin_video_url(self):
        """Test Douyin video URL"""
        url = "https://www.douyin.com/video/7234567890123456789"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform == 'douyin'
        assert video_id == '7234567890123456789'

    def test_douyin_note_url(self):
        """Test Douyin note URL"""
        url = "https://www.douyin.com/note/7234567890123456789"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform == 'douyin'
        assert video_id == '7234567890123456789'

    def test_douyin_short_link(self):
        """Test Douyin short link (v.douyin.com) - platform recognized"""
        url = "https://v.douyin.com/abc123"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform == 'douyin'
        # Short link needs resolver, so video_id may be None

    # ========== Xiaoyuzhou Tests ==========

    def test_xiaoyuzhou_episode_url(self):
        """Test Xiaoyuzhou episode URL"""
        url = "https://www.xiaoyuzhoufm.com/episode/68f7975f456ffec65ede5e47"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform == 'xiaoyuzhou'
        assert video_id == '68f7975f456ffec65ede5e47'

    def test_xiaoyuzhou_episode_url_with_params(self):
        """Test Xiaoyuzhou episode URL with query parameters"""
        url = "https://www.xiaoyuzhoufm.com/episode/68f7975f456ffec65ede5e47?source=share"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform == 'xiaoyuzhou'
        assert video_id == '68f7975f456ffec65ede5e47'

    # ========== Xiaohongshu Tests ==========

    def test_xiaohongshu_explore_url(self):
        """Test Xiaohongshu explore URL"""
        url = "https://www.xiaohongshu.com/explore/65a1b2c3d4e5f6789abcdef0"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform == 'xiaohongshu'
        assert video_id == '65a1b2c3d4e5f6789abcdef0'

    def test_xiaohongshu_discovery_url(self):
        """Test Xiaohongshu discovery/item URL"""
        url = "https://www.xiaohongshu.com/discovery/item/65a1b2c3d4e5f6789abcdef0"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform == 'xiaohongshu'
        assert video_id == '65a1b2c3d4e5f6789abcdef0'

    def test_xiaohongshu_short_link(self):
        """Test Xiaohongshu short link (xhslink.com) - platform recognized"""
        url = "https://xhslink.com/abc123"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform == 'xiaohongshu'
        # Short link needs resolver, so video_id may be None

    # ========== Edge Cases ==========

    def test_generic_url(self):
        """Test generic/unknown URL"""
        url = "https://example.com/video.mp4"
        platform, video_id = self.extract_platform_and_id(url)
        assert platform is None
        assert video_id is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
