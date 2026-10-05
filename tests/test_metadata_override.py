"""
Test metadata override functionality
"""
import pytest
from src.video_transcript_api.api.services.transcription import (
    extract_filename_from_url,
    generate_media_id_from_url,
    merge_metadata,
)


def test_extract_filename_from_url():
    """Test filename extraction from URL"""
    # Test with normal URL
    url1 = "http://localhost:8080/videos/my_video.mp4"
    assert extract_filename_from_url(url1) == "my_video"

    # Test with URL encoded filename
    url2 = "http://localhost:8080/videos/%E8%A7%86%E9%A2%91.mp4"
    assert extract_filename_from_url(url2) == "视频"

    # Test with no filename
    url3 = "http://localhost:8080/"
    assert extract_filename_from_url(url3) == ""


def test_generate_media_id_from_url():
    """Test media ID generation from URL"""
    url = "http://localhost:8080/video.mp4"
    media_id = generate_media_id_from_url(url)

    # Should return a 16-character hex string
    assert len(media_id) == 16
    assert all(c in "0123456789abcdef" for c in media_id)

    # Same URL should generate same ID
    assert generate_media_id_from_url(url) == media_id


def test_merge_metadata_with_parsed_success():
    """Test metadata merge when parsing succeeds"""
    parsed_metadata = {
        "title": "Parsed Title",
        "description": "Parsed Description",
        "author": "Parsed Author",
        "platform": "youtube",
        "video_id": "abc123"
    }

    metadata_override = {
        "title": "Override Title",
        "description": "Override Description"
    }

    url = "http://localhost:8080/video.mp4"

    result = merge_metadata(parsed_metadata, metadata_override, url)

    # Override should supplement parsed metadata
    assert result["title"] == "Override Title"
    assert result["description"] == "Override Description"
    assert result["author"] == "Parsed Author"  # Not overridden
    assert result["platform"] == "youtube"
    assert result["video_id"] == "abc123"


def test_merge_metadata_with_parsed_failure():
    """Test metadata merge when parsing fails"""
    metadata_override = {
        "title": "Override Title",
        "author": "Override Author"
    }

    url = "http://localhost:8080/video.mp4"

    result = merge_metadata(None, metadata_override, url)

    # Override should be used as fallback
    assert result["title"] == "Override Title"
    assert result["author"] == "Override Author"
    assert result["description"] == ""  # Default value
    assert result["platform"] == "generic"  # Default value
    assert len(result["video_id"]) == 16  # Generated from URL


def test_merge_metadata_with_defaults():
    """Test metadata merge with default values"""
    url = "http://localhost:8080/my_video.mp4"

    result = merge_metadata(None, None, url)

    # Should use all default values
    assert result["title"] == "my_video"  # Extracted from URL
    assert result["description"] == ""
    assert result["author"] == "Unknown"
    assert result["platform"] == "generic"
    assert len(result["video_id"]) == 16


def test_merge_metadata_partial_override():
    """Test metadata merge with partial override"""
    parsed_metadata = {
        "title": "Parsed Title",
        "description": "Parsed Description",
        "author": "Parsed Author",
        "platform": "youtube",
        "video_id": "abc123"
    }

    # Only override title
    metadata_override = {
        "title": "New Title"
    }

    url = "http://localhost:8080/video.mp4"

    result = merge_metadata(parsed_metadata, metadata_override, url)

    assert result["title"] == "New Title"
    assert result["description"] == "Parsed Description"
    assert result["author"] == "Parsed Author"
    assert result["platform"] == "youtube"
    assert result["video_id"] == "abc123"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
