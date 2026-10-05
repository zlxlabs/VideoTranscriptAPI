"""
Unit tests for YouTube downloader instance-level cache

Test coverage:
- Instance cache initialization
- get_video_info() caching behavior
- _get_subtitle_with_tikhub_api() cache reuse
- Cache isolation between instances

All console output must be in English only (no emoji, no Chinese).
"""

import os
import pytest
from unittest.mock import patch, Mock, MagicMock
from video_transcript_api.downloaders.youtube import YoutubeDownloader


@pytest.fixture(autouse=True)
def ensure_debug_dir():
    """Ensure debug directory exists for youtube downloader debug logging."""
    os.makedirs("./data/logs/debug", exist_ok=True)


class TestYoutubeDownloaderCache:
    """Test YouTube downloader instance-level cache"""

    @pytest.fixture
    def mock_config(self):
        """Mock configuration"""
        return {
            "tikhub": {
                "api_key": "test_api_key",
                "max_retries": 3,
                "retry_delay": 2,
                "timeout": 30
            },
            "youtube_api_server": {
                "enabled": False
            },
            "storage": {
                "temp_dir": "./data/temp"
            }
        }

    @pytest.fixture
    def downloader(self, mock_config):
        """Create a YouTube downloader instance with mocked dependencies"""
        # Mock load_config at the base downloader level
        with patch('video_transcript_api.downloaders.base.load_config', return_value=mock_config):
            with patch('video_transcript_api.downloaders.base.get_temp_manager'):
                downloader = YoutubeDownloader()
                return downloader

    def test_cache_initialization(self, downloader):
        """Test that cache is initialized in __init__"""
        assert hasattr(downloader, '_cached_video_info')
        assert isinstance(downloader._cached_video_info, dict)
        assert len(downloader._cached_video_info) == 0

    def test_get_video_info_first_call(self, downloader):
        """Test first call to get_video_info triggers API request and caches result"""
        url = "https://www.youtube.com/watch?v=test123"
        video_id = "test123"

        # Mock API response
        api_response = {
            "code": 200,
            "data": {
                "title": "Test Video",
                "channel": {"name": "Test Author"},
                "description": "Test description",
                "audios": {
                    "items": [{"url": "https://example.com/audio.m4a"}]
                },
                "subtitles": {}
            }
        }

        with patch.object(downloader, 'make_api_request', return_value=api_response):
            result = downloader.get_video_info(url)

            # Verify API was called
            downloader.make_api_request.assert_called_once()

            # Verify result structure
            assert result["video_id"] == video_id
            assert result["video_title"] == "Test Video"
            assert result["author"] == "Test Author"

            # Verify cache is populated
            assert video_id in downloader._cached_video_info
            assert downloader._cached_video_info[video_id] == result

    def test_get_video_info_second_call_uses_cache(self, downloader):
        """Test second call to get_video_info uses cache (no API request)"""
        url = "https://www.youtube.com/watch?v=test123"
        video_id = "test123"

        # Mock API response
        api_response = {
            "code": 200,
            "data": {
                "title": "Test Video",
                "channel": {"name": "Test Author"},
                "description": "Test description",
                "audios": {
                    "items": [{"url": "https://example.com/audio.m4a"}]
                },
                "subtitles": {}
            }
        }

        with patch.object(downloader, 'make_api_request', return_value=api_response):
            # First call
            result1 = downloader.get_video_info(url)
            first_call_count = downloader.make_api_request.call_count

            # Second call
            result2 = downloader.get_video_info(url)
            second_call_count = downloader.make_api_request.call_count

            # Verify API was only called once
            assert first_call_count == 1
            assert second_call_count == 1  # No additional call

            # Verify results are identical
            assert result1 == result2

    def test_get_subtitle_reuses_cache(self, downloader):
        """Test _get_subtitle_with_tikhub_api reuses cached video_info"""
        url = "https://www.youtube.com/watch?v=test123"
        video_id = "test123"

        # Pre-populate cache
        cached_video_info = {
            "video_id": video_id,
            "video_title": "Test Video",
            "author": "Test Author",
            "subtitle_info": {
                "url": "https://example.com/subtitle.xml",
                "code": "en"
            }
        }
        downloader._cached_video_info[video_id] = cached_video_info

        # Mock HTTP request for subtitle download
        mock_response = Mock()
        mock_response.text = """<?xml version="1.0" encoding="utf-8" ?>
<transcript><text start="0" dur="1">Test subtitle</text></transcript>"""
        mock_response.raise_for_status = Mock()

        with patch('requests.get', return_value=mock_response):
            with patch.object(downloader, 'get_video_info') as mock_get_video_info:
                result = downloader._get_subtitle_with_tikhub_api(url)

                # Verify get_video_info was NOT called (cache reused)
                mock_get_video_info.assert_not_called()

                # Verify subtitle was parsed
                assert result is not None
                assert "Test subtitle" in result

    def test_get_subtitle_calls_get_video_info_when_cache_miss(self, downloader):
        """Test _get_subtitle_with_tikhub_api calls get_video_info when cache is empty"""
        url = "https://www.youtube.com/watch?v=test123"
        video_id = "test123"

        # Cache is empty
        assert video_id not in downloader._cached_video_info

        # Mock get_video_info
        video_info_result = {
            "video_id": video_id,
            "subtitle_info": {
                "url": "https://example.com/subtitle.xml",
                "code": "en"
            }
        }

        mock_response = Mock()
        mock_response.text = """<?xml version="1.0" encoding="utf-8" ?>
<transcript><text start="0" dur="1">Test subtitle</text></transcript>"""
        mock_response.raise_for_status = Mock()

        with patch.object(downloader, 'get_video_info', return_value=video_info_result) as mock_get_video_info:
            with patch('requests.get', return_value=mock_response):
                result = downloader._get_subtitle_with_tikhub_api(url)

                # Verify get_video_info WAS called
                mock_get_video_info.assert_called_once_with(url)

                # Verify subtitle was parsed
                assert result is not None
                assert "Test subtitle" in result

    def test_cache_isolation_between_instances(self, mock_config):
        """Test that cache is isolated between different instances"""
        with patch('video_transcript_api.downloaders.base.load_config', return_value=mock_config):
            with patch('video_transcript_api.downloaders.base.get_temp_manager'):
                downloader1 = YoutubeDownloader()
                downloader2 = YoutubeDownloader()

                # Populate cache in downloader1
                video_id = "test123"
                downloader1._cached_video_info[video_id] = {"video_title": "Test Video 1"}

                # Verify downloader2's cache is empty
                assert video_id not in downloader2._cached_video_info

                # Populate cache in downloader2 with different value
                downloader2._cached_video_info[video_id] = {"video_title": "Test Video 2"}

                # Verify caches are independent
                assert downloader1._cached_video_info[video_id]["video_title"] == "Test Video 1"
                assert downloader2._cached_video_info[video_id]["video_title"] == "Test Video 2"

    def test_cache_lifecycle(self, mock_config):
        """Test cache lifecycle (created with instance, destroyed with instance)"""
        with patch('video_transcript_api.downloaders.base.load_config', return_value=mock_config):
            with patch('video_transcript_api.downloaders.base.get_temp_manager'):
                downloader = YoutubeDownloader()

                # Populate cache
                video_id = "test123"
                downloader._cached_video_info[video_id] = {"video_title": "Test Video"}

                # Verify cache exists
                assert video_id in downloader._cached_video_info

                # Delete instance
                del downloader

                # Create new instance
                new_downloader = YoutubeDownloader()

                # Verify cache is empty (new instance has clean cache)
                assert video_id not in new_downloader._cached_video_info

    def test_get_video_info_different_videos(self, downloader):
        """Test get_video_info caches multiple videos independently"""
        video1_url = "https://www.youtube.com/watch?v=video001"
        video2_url = "https://www.youtube.com/watch?v=video002"

        api_response_1 = {
            "code": 200,
            "data": {
                "title": "Video 1",
                "channel": {"name": "Author 1"},
                "description": "",
                "audios": {"items": [{"url": "https://example.com/audio1.m4a"}]},
                "subtitles": {}
            }
        }

        api_response_2 = {
            "code": 200,
            "data": {
                "title": "Video 2",
                "channel": {"name": "Author 2"},
                "description": "",
                "audios": {"items": [{"url": "https://example.com/audio2.m4a"}]},
                "subtitles": {}
            }
        }

        with patch.object(downloader, 'make_api_request') as mock_api:
            # Setup mock to return different responses
            mock_api.side_effect = [api_response_1, api_response_2]

            # Call get_video_info for both videos
            result1 = downloader.get_video_info(video1_url)
            result2 = downloader.get_video_info(video2_url)

            # Verify API was called twice
            assert mock_api.call_count == 2

            # Verify both are cached
            assert "video001" in downloader._cached_video_info
            assert "video002" in downloader._cached_video_info

            # Verify cached values are correct
            assert downloader._cached_video_info["video001"]["video_title"] == "Video 1"
            assert downloader._cached_video_info["video002"]["video_title"] == "Video 2"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
