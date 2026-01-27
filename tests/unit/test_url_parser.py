"""
Unit tests for URLParser module

Test coverage:
- URL parsing for all platforms (YouTube, Bilibili, Douyin, Xiaohongshu, Xiaoyuzhou)
- Short URL resolution
- Generic URL handling
- Error handling
"""

import pytest
from unittest.mock import patch, Mock
from src.video_transcript_api.utils.url_parser import URLParser, ParsedURL, parse_url, extract_platform


class TestURLParserBasic:
    """Test basic URL parsing functionality"""

    def test_youtube_standard_url(self):
        """Test standard YouTube URL parsing"""
        parser = URLParser()
        result = parser.parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        assert result.platform == "youtube"
        assert result.video_id == "dQw4w9WgXcQ"
        assert not result.is_short_url
        assert result.original_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_youtube_short_url(self):
        """Test YouTube short URL (youtu.be)"""
        parser = URLParser()
        # Mock the HTTP HEAD request
        with patch('requests.head') as mock_head:
            mock_response = Mock()
            mock_response.url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            mock_head.return_value = mock_response

            result = parser.parse("https://youtu.be/dQw4w9WgXcQ")

            assert result.platform == "youtube"
            assert result.video_id == "dQw4w9WgXcQ"
            assert result.is_short_url

    def test_youtube_shorts_url(self):
        """Test YouTube Shorts URL"""
        parser = URLParser()
        result = parser.parse("https://www.youtube.com/shorts/dQw4w9WgXcQ")

        assert result.platform == "youtube"
        assert result.video_id == "dQw4w9WgXcQ"

    def test_youtube_live_url(self):
        """Test YouTube Live URL"""
        parser = URLParser()
        result = parser.parse("https://www.youtube.com/live/dQw4w9WgXcQ")

        assert result.platform == "youtube"
        assert result.video_id == "dQw4w9WgXcQ"

    def test_bilibili_bv_url(self):
        """Test Bilibili BV URL"""
        parser = URLParser()
        result = parser.parse("https://www.bilibili.com/video/BV1xx411c7mD")

        assert result.platform == "bilibili"
        assert result.video_id == "BV1xx411c7mD"
        assert not result.is_short_url

    def test_bilibili_av_url(self):
        """Test Bilibili AV URL"""
        parser = URLParser()
        result = parser.parse("https://www.bilibili.com/video/av12345678")

        assert result.platform == "bilibili"
        assert result.video_id == "av12345678"

    def test_bilibili_short_url(self):
        """Test Bilibili short URL (b23.tv)"""
        parser = URLParser()
        with patch('requests.head') as mock_head:
            mock_response = Mock()
            mock_response.url = "https://www.bilibili.com/video/BV1xx411c7mD"
            mock_head.return_value = mock_response

            result = parser.parse("https://b23.tv/abc123")

            assert result.platform == "bilibili"
            assert result.video_id == "BV1xx411c7mD"
            assert result.is_short_url

    def test_douyin_url(self):
        """Test Douyin URL"""
        parser = URLParser()
        result = parser.parse("https://www.douyin.com/video/7123456789012345678")

        assert result.platform == "douyin"
        assert result.video_id == "7123456789012345678"

    def test_xiaohongshu_url(self):
        """Test Xiaohongshu URL"""
        parser = URLParser()
        result = parser.parse("https://www.xiaohongshu.com/explore/64abc123def456gh7890ijkl")

        assert result.platform == "xiaohongshu"
        assert result.video_id == "64abc123def456gh7890ijkl"

    def test_xiaoyuzhou_url(self):
        """Test Xiaoyuzhou URL"""
        parser = URLParser()
        result = parser.parse("https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597")

        assert result.platform == "xiaoyuzhou"
        assert result.video_id == "687893e0a12f9ff06a98a597"

    def test_generic_url(self):
        """Test generic URL (no platform matched)"""
        parser = URLParser()
        result = parser.parse("https://example.com/video/123")

        assert result.platform == "generic"
        assert len(result.video_id) == 16  # MD5 hash (16 chars)
        assert not result.is_short_url


class TestURLParserShortURL:
    """Test short URL resolution"""

    def test_short_url_resolution_success(self):
        """Test successful short URL resolution"""
        parser = URLParser()
        with patch('requests.head') as mock_head:
            mock_response = Mock()
            mock_response.url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            mock_head.return_value = mock_response

            result = parser.parse("https://youtu.be/short123")

            assert result.is_short_url
            assert mock_head.called
            assert mock_head.call_args[0][0] == "https://youtu.be/short123"

    def test_short_url_resolution_timeout(self):
        """Test short URL resolution with timeout"""
        parser = URLParser()
        with patch('requests.head') as mock_head:
            import requests
            mock_head.side_effect = requests.exceptions.Timeout("Timeout")

            # Should fall back to original URL
            result = parser.parse("https://youtu.be/short123")

            assert result.is_short_url
            # Should use original URL since resolution failed
            assert result.normalized_url == "https://youtu.be/short123"

    def test_short_url_resolution_network_error(self):
        """Test short URL resolution with network error"""
        parser = URLParser()
        with patch('requests.head') as mock_head:
            import requests
            mock_head.side_effect = requests.exceptions.RequestException("Network error")

            result = parser.parse("https://youtu.be/short123")

            assert result.is_short_url
            assert result.normalized_url == "https://youtu.be/short123"


class TestURLParserErrorHandling:
    """Test error handling"""

    def test_empty_url(self):
        """Test empty URL raises ValueError"""
        parser = URLParser()
        with pytest.raises(ValueError, match="URL must be a non-empty string"):
            parser.parse("")

    def test_none_url(self):
        """Test None URL raises ValueError"""
        parser = URLParser()
        with pytest.raises(ValueError, match="URL must be a non-empty string"):
            parser.parse(None)

    def test_invalid_url_returns_generic(self):
        """Test invalid URL returns generic platform"""
        parser = URLParser()
        result = parser.parse("not-a-valid-url")

        assert result.platform == "generic"
        assert len(result.video_id) == 16


class TestURLParserConvenienceFunctions:
    """Test convenience functions"""

    def test_parse_url_function(self):
        """Test parse_url convenience function"""
        result = parse_url("https://www.youtube.com/watch?v=test123")

        assert isinstance(result, ParsedURL)
        assert result.platform == "youtube"
        assert result.video_id == "test123"

    def test_extract_platform_function(self):
        """Test extract_platform convenience function"""
        platform = extract_platform("https://www.youtube.com/watch?v=test123")
        assert platform == "youtube"

        platform = extract_platform("https://www.bilibili.com/video/BV1test")
        assert platform == "bilibili"

        platform = extract_platform("https://example.com/video/123")
        assert platform == "generic"


class TestURLParserEdgeCases:
    """Test edge cases and special scenarios"""

    def test_url_with_query_parameters(self):
        """Test URL with multiple query parameters"""
        parser = URLParser()
        result = parser.parse("https://www.youtube.com/watch?v=test123&t=30s&list=PLtest")

        assert result.platform == "youtube"
        assert result.video_id == "test123"

    def test_url_with_fragment(self):
        """Test URL with fragment"""
        parser = URLParser()
        result = parser.parse("https://www.youtube.com/watch?v=test123#comment")

        assert result.platform == "youtube"
        assert result.video_id == "test123"

    def test_url_case_sensitivity(self):
        """Test URL parsing is case-insensitive for domains"""
        parser = URLParser()
        result = parser.parse("https://WWW.YOUTUBE.COM/watch?v=test123")

        assert result.platform == "youtube"
        assert result.video_id == "test123"

    def test_duplicate_parsing(self):
        """Test parsing the same URL twice returns consistent results"""
        parser = URLParser()
        url = "https://www.youtube.com/watch?v=test123"

        result1 = parser.parse(url)
        result2 = parser.parse(url)

        assert result1.platform == result2.platform
        assert result1.video_id == result2.video_id

    def test_hash_id_consistency(self):
        """Test generic URL hash ID is consistent"""
        parser = URLParser()
        url = "https://example.com/video/123"

        result1 = parser.parse(url)
        result2 = parser.parse(url)

        assert result1.video_id == result2.video_id
        assert len(result1.video_id) == 16


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
