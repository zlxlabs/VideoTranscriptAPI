import types

import pytest

from video_transcript_api.downloaders.base import BaseDownloader
from video_transcript_api.downloaders.models import VideoMetadata, DownloadInfo
from video_transcript_api.downloaders import (
    DouyinDownloader,
    BilibiliDownloader,
    XiaohongshuDownloader,
    XiaoyuzhouDownloader,
    ApplePodcastDownloader,
    YoutubeDownloader,
    GenericDownloader,
)


class DummyDownloader(BaseDownloader):
    def __init__(self):
        super().__init__()
        self.meta_calls = 0
        self.download_calls = 0

    def can_handle(self, url):
        return True

    def extract_video_id(self, url: str) -> str:
        return "dummy_id"

    def _fetch_metadata(self, url: str, video_id: str) -> VideoMetadata:
        self.meta_calls += 1
        return VideoMetadata(
            video_id=video_id,
            platform="dummy",
            title="dummy title",
            author="dummy author",
            description="dummy desc",
        )

    def _fetch_download_info(self, url: str, video_id: str) -> DownloadInfo:
        self.download_calls += 1
        return DownloadInfo(
            download_url="http://example.com/dummy.mp3",
            file_ext="mp3",
            filename="dummy.mp3",
        )

    def get_subtitle(self, url):
        return None


def test_base_downloader_caching_and_compat():
    downloader = DummyDownloader()

    meta1 = downloader.get_metadata("http://example.com")
    meta2 = downloader.get_metadata("http://example.com")
    assert meta1.video_id == "dummy_id"
    assert meta2.video_id == "dummy_id"
    assert downloader.meta_calls == 1

    info1 = downloader.get_download_info("http://example.com")
    info2 = downloader.get_download_info("http://example.com")
    assert info1.download_url == "http://example.com/dummy.mp3"
    assert info2.filename == "dummy.mp3"
    assert downloader.download_calls == 1

    legacy = downloader.get_video_info("http://example.com")
    assert legacy["video_id"] == "dummy_id"
    assert legacy["video_title"] == "dummy title"
    assert legacy["author"] == "dummy author"
    assert legacy["download_url"] == "http://example.com/dummy.mp3"
    assert legacy["filename"] == "dummy.mp3"
    assert legacy["platform"] == "dummy"


@pytest.mark.parametrize(
    "downloader_cls, fake_info, expected_ext",
    [
        (
            DouyinDownloader,
            {
                "video_id": "123",
                "video_title": "douyin title",
                "author": "author",
                "description": "desc",
                "download_url": "http://example.com/douyin.mp3",
                "filename": "douyin_123.mp3",
                "platform": "douyin",
            },
            "mp3",
        ),
        (
            BilibiliDownloader,
            {
                "video_id": "BV123",
                "video_title": "bili title",
                "author": "author",
                "description": "desc",
                "download_url": "http://example.com/bili.m4s",
                "filename": "bili_BV123.m4s",
                "platform": "bilibili",
                "downloaded": True,
                "local_file": "C:/tmp/bili.m4s",
                "cid": "987",
            },
            "m4s",
        ),
        (
            XiaohongshuDownloader,
            {
                "video_id": "xhs123",
                "video_title": "xhs title",
                "author": "author",
                "description": "desc",
                "download_url": "http://example.com/xhs.mp4",
                "filename": "xhs_xhs123.mp4",
                "platform": "xiaohongshu",
            },
            "mp4",
        ),
        (
            XiaoyuzhouDownloader,
            {
                "video_id": "xyz123",
                "video_title": "xyz title",
                "author": "author",
                "description": "desc",
                "download_url": "http://example.com/xyz.m4a",
                "filename": "xyz_xyz123.m4a",
                "platform": "xiaoyuzhou",
            },
            "m4a",
        ),
        (
            ApplePodcastDownloader,
            {
                "video_id": "1000774912806",
                "video_title": "apple podcast title",
                "author": "show name",
                "description": "desc",
                "download_url": "http://example.com/episode.mp3",
                "filename": "apple_podcast_1000774912806.mp3",
                "platform": "apple_podcast",
            },
            "mp3",
        ),
        (
            YoutubeDownloader,
            {
                "video_id": "yt123",
                "video_title": "yt title",
                "author": "author",
                "description": "desc",
                "download_url": "http://example.com/yt.m4a",
                "filename": "yt_yt123.m4a",
                "platform": "youtube",
                "subtitle_info": {"code": "en", "url": "http://example.com/sub.vtt"},
            },
            "m4a",
        ),
        (
            GenericDownloader,
            {
                "video_id": "gen123",
                "video_title": "",
                "author": "",
                "description": "",
                "download_url": "http://example.com/gen.mp4",
                "filename": "gen_gen123.mp4",
                "platform": "generic",
                "is_generic": True,
            },
            "mp4",
        ),
    ],
)
def test_downloader_new_interface_mapping(downloader_cls, fake_info, expected_ext):
    downloader = downloader_cls()

    downloader.extract_video_id = types.MethodType(lambda self, url: fake_info["video_id"], downloader)

    # Mock _fetch_metadata and _fetch_download_info to avoid real network calls
    def _fake_fetch_metadata(self, url, video_id):
        return VideoMetadata(
            video_id=video_id,
            platform=fake_info["platform"],
            title=fake_info.get("video_title", ""),
            author=fake_info.get("author", ""),
            description=fake_info.get("description", ""),
        )

    def _fake_fetch_download_info(self, url, video_id):
        return DownloadInfo(
            download_url=fake_info.get("download_url"),
            file_ext=expected_ext,
            filename=fake_info.get("filename"),
            downloaded=fake_info.get("downloaded", False),
            local_file=fake_info.get("local_file"),
        )

    downloader._fetch_metadata = types.MethodType(_fake_fetch_metadata, downloader)
    downloader._fetch_download_info = types.MethodType(_fake_fetch_download_info, downloader)

    metadata = downloader.get_metadata("http://example.com")
    download_info = downloader.get_download_info("http://example.com")

    assert metadata.video_id == fake_info["video_id"]
    assert metadata.platform == fake_info["platform"]
    assert metadata.title == fake_info.get("video_title", "")
    assert metadata.author == fake_info.get("author", "")
    assert metadata.description == fake_info.get("description", "")

    assert download_info.download_url == fake_info.get("download_url")
    assert download_info.file_ext == expected_ext
    assert download_info.filename == fake_info.get("filename")

    if fake_info.get("downloaded"):
        assert download_info.downloaded is True
        assert download_info.local_file == fake_info.get("local_file")
