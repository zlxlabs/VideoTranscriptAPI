"""Unit tests for XiaohongshuDownloader.

Covers:
- Note ID extraction from various URL formats
- Multi-endpoint fallback (first success, fallback to second, all fail)
- Instance cache hit skips API calls
- Response parsing (flat format, app format, missing field tolerance)
- _extract_by_path navigation (including list index)
- Interface compatibility (returned dict contains all required keys)
"""

import pytest
from unittest.mock import patch, MagicMock

from video_transcript_api.downloaders.xiaohongshu import (
    XiaohongshuDownloader,
    _ENDPOINT_CONFIGS,
    _VIDEO_URL_PATHS,
    _TITLE_PATHS,
    _AUTHOR_PATHS,
    _DESC_PATHS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def downloader():
    """Create a XiaohongshuDownloader with mocked config."""
    with patch(
        "video_transcript_api.downloaders.base.load_config",
        return_value={"tikhub": {"api_key": "test_key"}},
    ):
        dl = XiaohongshuDownloader()
    return dl


def _make_api_response(data: dict, code: int = 200) -> dict:
    """Build a standard TikHub API response envelope."""
    return {"code": code, "message": "success", "data": data}


# A realistic V3/V4-style response data section
SAMPLE_V3_DATA = {
    "title": "Test Video Title",
    "desc": "A sample description",
    "user": {"nickname": "TestAuthor"},
    "video": {
        "media": {
            "stream": {
                "h264": [
                    {
                        "backup_urls": ["https://cdn.example.com/video.mp4"],
                        "master_url": "https://cdn.example.com/master.mp4",
                    }
                ]
            }
        }
    },
}

# An App API-style response data section
SAMPLE_APP_DATA = {
    "title": "App Video",
    "description": "App desc",
    "user": {"nick_name": "AppUser"},
    "video_info": {
        "url": "https://cdn.example.com/app_video.mp4",
    },
}

# Minimal data: only video.url path available
SAMPLE_MINIMAL_DATA = {
    "video": {"url": "https://cdn.example.com/minimal.mp4"},
}


# ===========================================================================
# 1. Note ID extraction
# ===========================================================================

class TestExtractNoteId:
    """Test _extract_note_id with various URL formats."""

    def test_explore_url(self, downloader):
        url = "https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234"
        assert downloader._extract_note_id(url) == "6501a1234b5c6d7e8f901234"

    def test_discovery_item_url(self, downloader):
        url = "https://www.xiaohongshu.com/discovery/item/6501a1234b5c6d7e8f901234"
        assert downloader._extract_note_id(url) == "6501a1234b5c6d7e8f901234"

    def test_items_url(self, downloader):
        url = "https://www.xiaohongshu.com/items/6501a1234b5c6d7e8f901234"
        assert downloader._extract_note_id(url) == "6501a1234b5c6d7e8f901234"

    def test_raw_24char_id(self, downloader):
        raw_id = "6501a1234b5c6d7e8f901234"
        assert downloader._extract_note_id(raw_id) == raw_id

    def test_short_link_resolved(self, downloader):
        """Short link should be resolved then parsed."""
        with patch.object(
            downloader,
            "resolve_short_url",
            return_value="https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234",
        ):
            result = downloader._extract_note_id("https://xhslink.com/abc123")
            assert result == "6501a1234b5c6d7e8f901234"

    def test_invalid_url_raises(self, downloader):
        with pytest.raises(ValueError, match="Failed to extract note ID"):
            downloader._extract_note_id("https://example.com/nothing")

    def test_extract_video_id_delegates(self, downloader):
        """extract_video_id should delegate to _extract_note_id."""
        url = "https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234"
        assert downloader.extract_video_id(url) == "6501a1234b5c6d7e8f901234"


# ===========================================================================
# 2. _extract_by_path
# ===========================================================================

class TestExtractByPath:
    """Test the static _extract_by_path helper."""

    def test_simple_dict_path(self):
        data = {"a": {"b": "value"}}
        assert XiaohongshuDownloader._extract_by_path(data, ("a", "b")) == "value"

    def test_list_index_path(self):
        data = {"items": [{"name": "first"}, {"name": "second"}]}
        assert XiaohongshuDownloader._extract_by_path(data, ("items", 1, "name")) == "second"

    def test_missing_key_returns_none(self):
        data = {"a": {"c": 1}}
        assert XiaohongshuDownloader._extract_by_path(data, ("a", "b")) is None

    def test_index_out_of_range_returns_none(self):
        data = {"items": [1, 2]}
        assert XiaohongshuDownloader._extract_by_path(data, ("items", 5)) is None

    def test_none_data_returns_none(self):
        assert XiaohongshuDownloader._extract_by_path(None, ("a",)) is None

    def test_empty_path_returns_data(self):
        data = {"a": 1}
        assert XiaohongshuDownloader._extract_by_path(data, ()) == data

    def test_deep_nested_path(self):
        """Simulates the real h264 backup_urls path."""
        data = {
            "video": {
                "media": {
                    "stream": {
                        "h264": [
                            {"backup_urls": ["https://cdn.example.com/v.mp4"]}
                        ]
                    }
                }
            }
        }
        path = ("video", "media", "stream", "h264", 0, "backup_urls", 0)
        assert XiaohongshuDownloader._extract_by_path(data, path) == "https://cdn.example.com/v.mp4"


# ===========================================================================
# 3. _extract_first_match
# ===========================================================================

class TestExtractFirstMatch:
    """Test multi-path extraction priority."""

    def test_first_path_wins(self):
        data = {"title": "First", "note_info": {"title": "Second"}}
        result = XiaohongshuDownloader._extract_first_match(data, _TITLE_PATHS)
        assert result == "First"

    def test_fallback_to_second_path(self):
        data = {"note_info": {"title": "Fallback"}}
        result = XiaohongshuDownloader._extract_first_match(data, _TITLE_PATHS)
        assert result == "Fallback"

    def test_all_paths_miss_returns_none(self):
        data = {"unrelated": 1}
        result = XiaohongshuDownloader._extract_first_match(data, _TITLE_PATHS)
        assert result is None

    def test_empty_string_skipped(self):
        data = {"title": "", "note_info": {"title": "NonEmpty"}}
        result = XiaohongshuDownloader._extract_first_match(data, _TITLE_PATHS)
        assert result == "NonEmpty"


# ===========================================================================
# 4. _validate_response
# ===========================================================================

class TestValidateResponse:
    """Test API response validation."""

    def test_valid_response_passes(self):
        resp = _make_api_response({"key": "val"})
        # Should not raise
        XiaohongshuDownloader._validate_response(resp, "test")

    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="not a dict"):
            XiaohongshuDownloader._validate_response("string", "test")

    def test_bad_code_raises(self):
        resp = {"code": 404, "message": "not found", "data": {}}
        with pytest.raises(ValueError, match="code=404"):
            XiaohongshuDownloader._validate_response(resp, "test")

    def test_missing_data_raises(self):
        resp = {"code": 200, "message": "ok"}
        with pytest.raises(ValueError, match="'data' field missing"):
            XiaohongshuDownloader._validate_response(resp, "test")


# ===========================================================================
# 5. Multi-endpoint fallback
# ===========================================================================

class TestMultiEndpointFallback:
    """Test get_video_info multi-endpoint retry logic."""

    def test_first_endpoint_succeeds(self, downloader):
        """First endpoint returns valid data, no further endpoints called."""
        response = _make_api_response(SAMPLE_V3_DATA)

        with patch.object(downloader, "make_api_request", return_value=response) as mock_api:
            result = downloader.get_video_info(
                "https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234"
            )

        assert result["video_title"] == "Test Video Title"
        assert result["author"] == "TestAuthor"
        assert result["download_url"] == "https://cdn.example.com/video.mp4"
        assert result["platform"] == "xiaohongshu"
        # Only the first endpoint should be called
        assert mock_api.call_count == 1
        call_endpoint = mock_api.call_args_list[0][0][0]
        assert call_endpoint == _ENDPOINT_CONFIGS[0]["path"]

    def test_fallback_to_second_endpoint(self, downloader):
        """First endpoint fails, second succeeds."""
        fail_response = {"code": 404, "message": "not found", "data": {}}
        success_response = _make_api_response(SAMPLE_APP_DATA)

        call_count = 0

        def mock_api(endpoint, params):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return fail_response
            return success_response

        with patch.object(downloader, "make_api_request", side_effect=mock_api):
            result = downloader.get_video_info(
                "https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234"
            )

        assert result["video_title"] == "App Video"
        assert result["author"] == "AppUser"
        assert result["download_url"] == "https://cdn.example.com/app_video.mp4"
        assert call_count == 2

    def test_all_endpoints_fail(self, downloader):
        """All endpoints fail, raises ValueError with combined errors."""
        fail_response = {"code": 500, "message": "server error", "data": {}}

        with patch.object(downloader, "make_api_request", return_value=fail_response):
            with pytest.raises(ValueError, match="All xiaohongshu API endpoints failed"):
                downloader.get_video_info(
                    "https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234"
                )

    def test_api_exception_triggers_fallback(self, downloader):
        """make_api_request raising an exception should fallback to next endpoint."""
        success_response = _make_api_response(SAMPLE_V3_DATA)
        call_count = 0

        def mock_api(endpoint, params):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ValueError("API request failed")
            return success_response

        with patch.object(downloader, "make_api_request", side_effect=mock_api):
            result = downloader.get_video_info(
                "https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234"
            )

        assert result["video_title"] == "Test Video Title"
        assert call_count == 3  # first two failed, third succeeded


# ===========================================================================
# 6. Instance cache
# ===========================================================================

class TestInstanceCache:
    """Test that instance cache prevents repeated API calls."""

    def test_cache_hit_skips_api(self, downloader):
        """Second call for the same note_id uses cache, no API call."""
        response = _make_api_response(SAMPLE_V3_DATA)

        with patch.object(downloader, "make_api_request", return_value=response) as mock_api:
            url = "https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234"
            result1 = downloader.get_video_info(url)
            result2 = downloader.get_video_info(url)

        assert mock_api.call_count == 1
        assert result1 is result2

    def test_different_urls_same_note_id_uses_cache(self, downloader):
        """Different URL formats resolving to the same note_id should hit cache."""
        response = _make_api_response(SAMPLE_V3_DATA)

        with patch.object(downloader, "make_api_request", return_value=response) as mock_api:
            url1 = "https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234"
            url2 = "https://www.xiaohongshu.com/discovery/item/6501a1234b5c6d7e8f901234"
            result1 = downloader.get_video_info(url1)
            result2 = downloader.get_video_info(url2)

        assert mock_api.call_count == 1
        assert result1 == result2


# ===========================================================================
# 7. Response parsing
# ===========================================================================

class TestResponseParsing:
    """Test _parse_video_info with different data formats."""

    def test_v3_flat_format(self, downloader):
        result = downloader._parse_video_info(
            SAMPLE_V3_DATA,
            "https://example.com",
            "note123",
            "web_v4",
        )
        assert result["video_title"] == "Test Video Title"
        assert result["author"] == "TestAuthor"
        assert result["description"] == "A sample description"
        assert result["download_url"] == "https://cdn.example.com/video.mp4"

    def test_app_format(self, downloader):
        result = downloader._parse_video_info(
            SAMPLE_APP_DATA,
            "https://example.com",
            "note456",
            "app_note",
        )
        assert result["video_title"] == "App Video"
        assert result["author"] == "AppUser"
        assert result["download_url"] == "https://cdn.example.com/app_video.mp4"

    def test_minimal_format(self, downloader):
        result = downloader._parse_video_info(
            SAMPLE_MINIMAL_DATA,
            "https://example.com",
            "note789",
            "web_v2",
        )
        assert result["download_url"] == "https://cdn.example.com/minimal.mp4"
        # Missing title should fallback to note_id
        assert "note789" in result["video_title"]

    def test_no_video_url_raises(self, downloader):
        data = {"title": "No Video", "user": {"nickname": "Author"}}
        with pytest.raises(ValueError, match="Cannot extract video URL"):
            downloader._parse_video_info(data, "https://example.com", "noid", "test")

    def test_master_url_fallback(self, downloader):
        """If backup_urls is empty, should fallback to master_url."""
        data = {
            "title": "Master URL Test",
            "user": {"nickname": "Author"},
            "video": {
                "media": {
                    "stream": {
                        "h264": [
                            {
                                "backup_urls": [],
                                "master_url": "https://cdn.example.com/master.mp4",
                            }
                        ]
                    }
                }
            },
        }
        result = downloader._parse_video_info(data, "https://example.com", "n1", "test")
        assert result["download_url"] == "https://cdn.example.com/master.mp4"


# ===========================================================================
# 8. Interface compatibility
# ===========================================================================

class TestInterfaceCompatibility:
    """Ensure returned dict contains all keys expected by downstream code."""

    REQUIRED_KEYS = {"video_id", "video_title", "author", "description",
                     "download_url", "filename", "platform"}

    def test_all_required_keys_present(self, downloader):
        response = _make_api_response(SAMPLE_V3_DATA)
        with patch.object(downloader, "make_api_request", return_value=response):
            result = downloader.get_video_info(
                "https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234"
            )
        assert self.REQUIRED_KEYS.issubset(result.keys())

    def test_platform_is_xiaohongshu(self, downloader):
        response = _make_api_response(SAMPLE_V3_DATA)
        with patch.object(downloader, "make_api_request", return_value=response):
            result = downloader.get_video_info(
                "https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234"
            )
        assert result["platform"] == "xiaohongshu"

    def test_filename_has_mp4_extension(self, downloader):
        response = _make_api_response(SAMPLE_V3_DATA)
        with patch.object(downloader, "make_api_request", return_value=response):
            result = downloader.get_video_info(
                "https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234"
            )
        assert result["filename"].endswith(".mp4")


# ===========================================================================
# 9. can_handle
# ===========================================================================

class TestCanHandle:
    """Test URL recognition."""

    def test_xiaohongshu_com(self, downloader):
        assert downloader.can_handle("https://www.xiaohongshu.com/explore/abc")

    def test_xhslink_com(self, downloader):
        assert downloader.can_handle("https://xhslink.com/abc")

    def test_unrelated_url(self, downloader):
        assert not downloader.can_handle("https://www.youtube.com/watch?v=abc")


# ===========================================================================
# 10. Endpoint configs sanity
# ===========================================================================

class TestEndpointConfigs:
    """Ensure endpoint configs are well-formed."""

    def test_at_least_two_endpoints(self):
        assert len(_ENDPOINT_CONFIGS) >= 2

    def test_configs_have_required_keys(self):
        for cfg in _ENDPOINT_CONFIGS:
            assert "name" in cfg
            assert "path" in cfg
            assert "params_builder" in cfg
            assert callable(cfg["params_builder"])

    def test_params_builders_return_dicts(self):
        test_url = "https://example.com/test"
        for cfg in _ENDPOINT_CONFIGS:
            params = cfg["params_builder"](test_url)
            assert isinstance(params, dict)
            assert "share_text" in params


# ===========================================================================
# 11. _unwrap_note_data
# ===========================================================================

class TestUnwrapNoteData:
    """Test nested response data unwrapping."""

    def test_flat_data_with_video_passthrough(self):
        """Data already at note level (has video key) passes through."""
        data = {"title": "T", "video": {"url": "http://example.com"}}
        result = XiaohongshuDownloader._unwrap_note_data(data, "test")
        assert result is data

    def test_unwrap_app_video_note_format(self):
        """data.data[0] with video_info_v2 → unwrap to note."""
        note = {
            "title": "Note Title",
            "video_info_v2": {"media": {"stream": {"h264": []}}},
            "user": {"nickname": "Author"},
        }
        data = {"code": 0, "data": [note], "success": True}
        result = XiaohongshuDownloader._unwrap_note_data(data, "test")
        assert result is note

    def test_unwrap_app_note_format(self):
        """data.data[0].note_list[0] → unwrap to inner note."""
        inner_note = {
            "title": "Inner Note",
            "type": "video",
            "video_info_v2": {"media": {}},
        }
        outer = {
            "note_list": [inner_note],
            "user": {"name": "OuterUser"},
        }
        data = {"code": 0, "data": [outer], "success": True}
        result = XiaohongshuDownloader._unwrap_note_data(data, "test")
        assert result is inner_note
        # User info should be merged from outer
        assert result["user"]["name"] == "OuterUser"

    def test_unwrap_note_list_without_video_key(self):
        """note_list[0] without video key still unwraps into inner note."""
        inner_note = {"title": "Text Note", "type": "normal"}
        outer = {"note_list": [inner_note], "user": {"name": "U"}}
        data = {"code": 0, "data": [outer], "success": True}
        result = XiaohongshuDownloader._unwrap_note_data(data, "test")
        # Unwraps into inner note with user merged from outer
        assert result["title"] == "Text Note"
        assert result["user"]["name"] == "U"

    def test_unwrap_empty_data_list_raises(self):
        """Empty data list should raise ValueError."""
        data = {"code": 0, "data": [], "success": True}
        with pytest.raises(ValueError, match="Cannot unwrap"):
            XiaohongshuDownloader._unwrap_note_data(data, "test")

    def test_unwrap_no_data_key_raises(self):
        """No nested 'data' and no video keys should raise."""
        data = {"code": 0, "success": True, "msg": "ok"}
        with pytest.raises(ValueError, match="Cannot unwrap"):
            XiaohongshuDownloader._unwrap_note_data(data, "test")


# ===========================================================================
# 12. _enrich_from_widgets_context
# ===========================================================================

class TestEnrichFromWidgetsContext:
    """Test widgets_context JSON parsing and enrichment."""

    def test_extracts_audio_url(self):
        """Audio URL from note_sound_info should be injected."""
        note = {
            "title": "T",
            "widgets_context": '{"video": true, "note_sound_info": {"url": "http://cdn/audio.m4a"}}',
        }
        XiaohongshuDownloader._enrich_from_widgets_context(note, "test")
        assert note["_widgets_media_url"] == "http://cdn/audio.m4a"

    def test_no_widgets_context(self):
        """Missing widgets_context should not inject anything."""
        note = {"title": "T"}
        XiaohongshuDownloader._enrich_from_widgets_context(note, "test")
        assert "_widgets_media_url" not in note

    def test_invalid_json_no_crash(self):
        """Invalid JSON should not crash."""
        note = {"title": "T", "widgets_context": "not json"}
        XiaohongshuDownloader._enrich_from_widgets_context(note, "test")
        assert "_widgets_media_url" not in note

    def test_no_sound_info(self):
        """widgets_context without note_sound_info should not inject."""
        note = {"title": "T", "widgets_context": '{"video": true}'}
        XiaohongshuDownloader._enrich_from_widgets_context(note, "test")
        assert "_widgets_media_url" not in note

    def test_widgets_fallback_in_parse(self, downloader):
        """When no video URL exists, widgets audio URL should be used."""
        note = {
            "title": "Widgets Only",
            "user": {"nickname": "Author"},
            "_widgets_media_url": "http://cdn/audio.m4a",
        }
        result = downloader._parse_video_info(note, "http://x", "n1", "test")
        assert result["download_url"] == "http://cdn/audio.m4a"


# ===========================================================================
# 13. Full pipeline with nested real-world response format
# ===========================================================================

class TestRealWorldResponseFormats:
    """Test get_video_info with response structures matching real API."""

    def test_app_video_note_real_format(self, downloader):
        """Simulate real app_video_note response: data.data[0].video_info_v2."""
        inner_note = {
            "title": "Vibe Coding",
            "desc": "What is Vibe Coding?",
            "user": {"nickname": "TestUser", "name": "TestUser"},
            "type": "video",
            "video_info_v2": {
                "media": {
                    "stream": {
                        "h264": [{
                            "backup_urls": [
                                "http://sns-v8.bad/video.mp4",
                                "http://sns-v10.good/video.mp4",
                            ],
                            "master_url": "http://sns-v8.bad/video.mp4",
                            "quality_type": "HD",
                        }]
                    }
                }
            },
        }
        api_response = _make_api_response({
            "code": 0,
            "success": True,
            "data": [inner_note],
        })

        with patch.object(downloader, "make_api_request", return_value=api_response):
            result = downloader.get_video_info(
                "https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234"
            )

        assert result["video_title"] == "Vibe Coding"
        assert result["author"] == "TestUser"
        # backup_urls[1] should be preferred (index 1 before index 0 in paths)
        assert result["download_url"] == "http://sns-v10.good/video.mp4"

    def test_app_note_widgets_context_fallback(self, downloader):
        """Simulate app_note: no video_info_v2, fallback to widgets_context audio."""
        import json as json_module
        inner_note = {
            "title": "Audio Only Note",
            "desc": "Description here",
            "type": "video",
            "widgets_context": json_module.dumps({
                "video": True,
                "note_sound_info": {
                    "url": "http://cdn/audio_track.m4a",
                    "sound_id": "12345",
                },
            }),
        }
        outer = {
            "note_list": [inner_note],
            "user": {"name": "NoteAuthor"},
        }
        api_response = _make_api_response({
            "code": 0,
            "success": True,
            "data": [outer],
        })

        with patch.object(downloader, "make_api_request", return_value=api_response):
            result = downloader.get_video_info(
                "https://www.xiaohongshu.com/explore/6501a1234b5c6d7e8f901234"
            )

        assert result["video_title"] == "Audio Only Note"
        assert result["author"] == "NoteAuthor"
        assert result["download_url"] == "http://cdn/audio_track.m4a"
