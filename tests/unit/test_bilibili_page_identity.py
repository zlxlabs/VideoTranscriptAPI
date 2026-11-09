"""Regression tests for Bilibili page-aware video identities."""

from unittest.mock import patch

import pytest

from video_transcript_api.downloaders.bilibili import BilibiliDownloader


BV_ID = "BV1AoEg6SEW4"
P1_URL = f"https://www.bilibili.com/video/{BV_ID}?p=1"
P2_URL = f"https://www.bilibili.com/video/{BV_ID}?p=2"


def _make_downloader(tmp_path):
    config = {
        "bbdown": {"use_bbdown": False},
        "tikhub": {"api_key": "test_key"},
    }
    with patch(
        "video_transcript_api.downloaders.base.load_config", return_value=config
    ):
        downloader = BilibiliDownloader()
    downloader.config = config
    return downloader


def _mock_tikhub(downloader, tmp_path, include_pages=True):
    selected_cids = []

    def make_api_request(endpoint, params):
        if endpoint.endswith("fetch_one_video"):
            video = {
                "title": "Video title",
                "owner": {"name": "Video owner"},
                "cid": 101,
            }
            if include_pages:
                video["pages"] = [
                    {"page": 1, "cid": 101},
                    {"page": 2, "cid": 202},
                ]
            return {"code": 200, "data": {"data": video}}
        cid = params["cid"]
        selected_cids.append(cid)
        return {
            "code": 200,
            "data": {
                "data": {"dash": {"audio": [{"baseUrl": f"https://audio/{cid}"}]}}
            },
        }

    downloader.make_api_request = make_api_request
    return selected_cids


def test_same_downloader_keeps_separate_page_cids(tmp_path):
    downloader = _make_downloader(tmp_path)
    selected_cids = _mock_tikhub(downloader, tmp_path)

    with patch("video_transcript_api.downloaders.bilibili.DEBUG_DIR", str(tmp_path)):
        first_page = downloader.get_video_info(P1_URL)
        second_page = downloader.get_video_info(P2_URL)

    assert first_page["video_id"] == BV_ID
    assert first_page["cid"] == 101
    assert second_page["video_id"] == f"{BV_ID}_p2"
    assert second_page["cid"] == 202
    assert selected_cids == [101, 202]


def test_new_downloader_resolves_requested_page_cid(tmp_path):
    downloader = _make_downloader(tmp_path)
    selected_cids = _mock_tikhub(downloader, tmp_path)

    with patch("video_transcript_api.downloaders.bilibili.DEBUG_DIR", str(tmp_path)):
        result = downloader.get_video_info(P2_URL)

    assert result["video_id"] == f"{BV_ID}_p2"
    assert result["cid"] == 202
    assert selected_cids == [202]


def test_tikhub_page_out_of_range_fails_with_page_identity_error(tmp_path):
    downloader = _make_downloader(tmp_path)
    _mock_tikhub(downloader, tmp_path)

    with patch("video_transcript_api.downloaders.bilibili.DEBUG_DIR", str(tmp_path)):
        with pytest.raises(ValueError, match="BILIBILI_PAGE_NOT_FOUND"):
            downloader.get_video_info(f"https://www.bilibili.com/video/{BV_ID}?p=3")


def test_tikhub_later_page_without_page_list_fails(tmp_path):
    downloader = _make_downloader(tmp_path)
    _mock_tikhub(downloader, tmp_path, include_pages=False)

    with patch("video_transcript_api.downloaders.bilibili.DEBUG_DIR", str(tmp_path)):
        with pytest.raises(ValueError, match="BILIBILI_PAGE_NOT_FOUND"):
            downloader.get_video_info(P2_URL)


def test_downloader_extracts_page_aware_identity(tmp_path):
    downloader = _make_downloader(tmp_path)

    assert downloader.extract_video_id(P1_URL) == BV_ID
    assert downloader.extract_video_id(P2_URL) == f"{BV_ID}_p2"
