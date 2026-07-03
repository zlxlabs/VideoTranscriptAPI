"""
Apple Podcast downloader unit tests.

Covers:
- URL id extraction (show id + episode id)
- get_video_info via mocked iTunes Lookup API (hit / deep-catalog miss)
- Metadata and download info mapping
- Audio extension detection
- Show-only URL rejection (missing ?i= episode param)

All console output must be in English only (no emoji, no Chinese).
"""

import pytest

from video_transcript_api.downloaders import apple_podcast
from video_transcript_api.downloaders.apple_podcast import ApplePodcastDownloader


EPISODE_URL = "https://podcasts.apple.com/us/podcast/lex-fridman-podcast/id1434243584?i=1000774912806"
SHOW_URL = "https://podcasts.apple.com/us/podcast/lex-fridman-podcast/id1434243584"

LOOKUP_RESPONSE = {
    "resultCount": 3,
    "results": [
        {
            "wrapperType": "track",
            "kind": "podcast",
            "collectionId": 1434243584,
            "collectionName": "Lex Fridman Podcast",
            "artistName": "Lex Fridman",
            "feedUrl": "https://lexfridman.com/feed/podcast/",
        },
        {
            "wrapperType": "podcastEpisode",
            "kind": "podcast-episode",
            "trackId": 1000774912806,
            "trackName": "#498 - Anthony Kaldellis: Roman Empire",
            "collectionName": "Lex Fridman Podcast",
            "episodeUrl": "https://media.example.com/lex_ai_anthony_kaldellis.mp3",
            "episodeFileExtension": "mp3",
            "trackTimeMillis": 7200000,
            "releaseDate": "2026-06-30T21:33:40Z",
            "description": "Anthony Kaldellis is a historian of the Roman Empire.",
        },
        {
            "wrapperType": "podcastEpisode",
            "kind": "podcast-episode",
            "trackId": 1000774900000,
            "trackName": "#497 - Some Other Episode",
            "collectionName": "Lex Fridman Podcast",
            "episodeUrl": "https://media.example.com/other.m4a",
            "episodeFileExtension": "m4a",
        },
    ],
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture
def mock_lookup(monkeypatch):
    """Patch requests.get in the apple_podcast module to return LOOKUP_RESPONSE."""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params})
        return FakeResponse(LOOKUP_RESPONSE)

    monkeypatch.setattr(apple_podcast.requests, "get", fake_get)
    return calls


class TestIdExtraction:
    def test_extract_video_id_from_episode_url(self):
        downloader = ApplePodcastDownloader()
        assert downloader.extract_video_id(EPISODE_URL) == "1000774912806"

    def test_extract_ids_returns_show_and_episode(self):
        downloader = ApplePodcastDownloader()
        show_id, episode_id = downloader._extract_ids(EPISODE_URL)
        assert show_id == "1434243584"
        assert episode_id == "1000774912806"

    def test_show_url_without_episode_param_raises(self):
        downloader = ApplePodcastDownloader()
        with pytest.raises(ValueError):
            downloader.extract_video_id(SHOW_URL)

    def test_invalid_url_without_show_id_raises(self):
        downloader = ApplePodcastDownloader()
        with pytest.raises(ValueError):
            downloader.extract_video_id("https://podcasts.apple.com/us/browse")

    @pytest.mark.parametrize("url,expected", [
        (EPISODE_URL, "us"),
        ("https://podcasts.apple.com/cn/podcast/show/id123?i=456", "cn"),
        ("https://podcasts.apple.com/podcast/show/id123?i=456", None),
    ])
    def test_extract_country(self, url, expected):
        assert ApplePodcastDownloader._extract_country(url) == expected


class TestDownloadHeaders:
    def test_browser_ua_set_for_media_download(self):
        """CDNs that 403 bot UAs need the browser UA on the media request too."""
        downloader = ApplePodcastDownloader()
        assert downloader.download_headers.get("User-Agent", "").startswith("Mozilla/5.0")


class TestCanHandle:
    @pytest.mark.parametrize("url,expected", [
        (EPISODE_URL, True),
        (SHOW_URL, True),
        ("https://www.xiaoyuzhoufm.com/episode/abc123", False),
        ("https://www.youtube.com/watch?v=abc", False),
    ])
    def test_can_handle(self, url, expected):
        assert ApplePodcastDownloader().can_handle(url) is expected


class TestGetVideoInfo:
    def test_lookup_hit_returns_full_info(self, mock_lookup):
        downloader = ApplePodcastDownloader()
        info = downloader.get_video_info(EPISODE_URL)

        assert info["video_id"] == "1000774912806"
        assert info["video_title"] == "#498 - Anthony Kaldellis: Roman Empire"
        assert info["author"] == "Lex Fridman Podcast"
        assert info["description"] == "Anthony Kaldellis is a historian of the Roman Empire."
        assert info["duration"] == 7200.0
        assert info["download_url"] == "https://media.example.com/lex_ai_anthony_kaldellis.mp3"
        assert info["filename"].startswith("apple_podcast_1000774912806_")
        assert info["filename"].endswith(".mp3")
        assert info["platform"] == "apple_podcast"

        # lookup request params should target the show with episode entity
        assert mock_lookup[0]["params"]["id"] == "1434243584"
        assert mock_lookup[0]["params"]["entity"] == "podcastEpisode"
        # storefront country from the URL (/us/) must be forwarded to the API
        assert mock_lookup[0]["params"]["country"] == "us"

    def test_instance_cache_avoids_second_request(self, mock_lookup):
        downloader = ApplePodcastDownloader()
        downloader.get_video_info(EPISODE_URL)
        downloader.get_video_info(EPISODE_URL)
        assert len(mock_lookup) == 1

    def test_deep_catalog_miss_raises_with_feed_hint(self, mock_lookup):
        downloader = ApplePodcastDownloader()
        missing_url = SHOW_URL + "?i=999999"
        with pytest.raises(ValueError) as exc_info:
            downloader.get_video_info(missing_url)
        assert "lexfridman.com/feed/podcast" in str(exc_info.value)

    def test_empty_lookup_result_raises(self, monkeypatch):
        monkeypatch.setattr(
            apple_podcast.requests, "get",
            lambda *a, **k: FakeResponse({"resultCount": 0, "results": []}),
        )
        downloader = ApplePodcastDownloader()
        with pytest.raises(ValueError):
            downloader.get_video_info(EPISODE_URL)


class TestInterfaceMapping:
    def test_fetch_metadata_and_download_info(self, mock_lookup):
        downloader = ApplePodcastDownloader()

        metadata = downloader.get_metadata(EPISODE_URL)
        assert metadata.video_id == "1000774912806"
        assert metadata.platform == "apple_podcast"
        assert metadata.title == "#498 - Anthony Kaldellis: Roman Empire"
        assert metadata.duration == 7200.0

        download_info = downloader.get_download_info(EPISODE_URL)
        assert download_info.download_url == "https://media.example.com/lex_ai_anthony_kaldellis.mp3"
        assert download_info.file_ext == "mp3"

    def test_get_subtitle_returns_none(self):
        assert ApplePodcastDownloader().get_subtitle(EPISODE_URL) is None


class TestAudioExtDetection:
    @pytest.mark.parametrize("audio_url,itunes_ext,expected", [
        ("https://cdn.example.com/a.mp3", "mp3", ".mp3"),
        ("https://cdn.example.com/a.m4a?token=x", None, ".m4a"),
        ("https://cdn.example.com/a", "m4a", ".m4a"),
        ("https://cdn.example.com/stream", None, ".mp3"),
        ("https://cdn.example.com/a.exe", "exe", ".mp3"),
    ])
    def test_detect_audio_ext(self, audio_url, itunes_ext, expected):
        assert ApplePodcastDownloader._detect_audio_ext(audio_url, itunes_ext) == expected
