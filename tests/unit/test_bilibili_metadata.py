"""Unit tests for BilibiliDownloader metadata resilience.

Background (the bug this guards against)
----------------------------------------
In BBDown mode, ``_fetch_metadata`` used to call ``get_video_info`` (a full
BBDown audio download) inside the metadata path. When BBDown timed out, the
exception propagated and discarded the title/author already fetched from the
official Bilibili ``x/web-interface/view`` API, leaving the task with a
garbage title (the short-link code) and ``author="Unknown"``.

The fix (L1 + L2):
- L1: metadata phase relies only on the official API; in BBDown mode it never
  triggers a download. A downloader-info failure can no longer break metadata.
- L2: the official API is hardened with a buvid3 cookie + retry/backoff so
  risk-control responses (code -412 / -799 / -509) and transient timeouts
  self-heal instead of falling straight back to the BV id.

All network calls are mocked. No emoji / Chinese in console output per repo rule.
"""

import pytest
from unittest.mock import patch, MagicMock

from video_transcript_api.downloaders.bilibili import BilibiliDownloader


BV_ID = "BV1AoEg6SEW4"
URL = f"https://www.bilibili.com/video/{BV_ID}"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_downloader(use_bbdown: bool) -> BilibiliDownloader:
    """Build a downloader with config controlled (bbdown vs tikhub mode)."""
    cfg = {
        "tikhub": {"api_key": "test_key"},
        "bbdown": {"use_bbdown": use_bbdown},
    }
    with patch(
        "video_transcript_api.downloaders.base.load_config",
        return_value=cfg,
    ):
        dl = BilibiliDownloader()
    # Ensure instance config matches even if load_config patch missed
    dl.config = cfg
    return dl


def _official_response(code: int = 0, title: str = "Real Title",
                       author: str = "Real Author") -> MagicMock:
    """Build a fake requests.Response for the official view API."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "code": code,
        "message": "0" if code == 0 else "err",
        "data": {
            "title": title,
            "desc": "a description",
            "owner": {"name": author, "mid": 123},
            "duration": 100,
            "pubdate": 1700000000,
        } if code == 0 else None,
    }
    return resp


# ---------------------------------------------------------------------------
# L1: decoupling
# ---------------------------------------------------------------------------

def test_official_ok_bbdown_mode_no_download():
    """BBDown mode: official API success -> metadata correct AND BBDown
    (get_video_info) is never invoked during the metadata phase."""
    dl = _make_downloader(use_bbdown=True)
    with patch(
        "video_transcript_api.downloaders.bilibili.requests.get",
        return_value=_official_response(),
    ), patch.object(
        dl, "get_video_info",
        side_effect=AssertionError("get_video_info must not run in metadata phase (bbdown mode)"),
    ):
        meta = dl.get_metadata(URL)

    assert meta.title == "Real Title"
    assert meta.author == "Real Author"
    assert meta.description == "a description"


def test_official_ok_bbdown_would_fail_regression():
    """REGRESSION: reproduces the reported bug. Official API succeeds but the
    downloader (BBDown) would fail. Metadata must still be correct, never the
    BV id / Unknown fallback."""
    dl = _make_downloader(use_bbdown=True)
    with patch(
        "video_transcript_api.downloaders.bilibili.requests.get",
        return_value=_official_response(title="69_guide", author="white_rabbit"),
    ), patch.object(
        dl, "get_video_info",
        side_effect=ValueError("BBDown failed: net_http_request_timedout, 120"),
    ):
        meta = dl.get_metadata(URL)

    assert meta.title == "69_guide"
    assert meta.author == "white_rabbit"
    assert meta.author != "Unknown"


# ---------------------------------------------------------------------------
# L2: official API hardening (retry + cookie + fallback)
# ---------------------------------------------------------------------------

def test_official_412_then_ok_retries():
    """Risk-control code -412 on first call, success on retry -> correct
    metadata, and requests.get is called more than once."""
    dl = _make_downloader(use_bbdown=True)
    responses = [_official_response(code=-412), _official_response(title="After Retry")]
    with patch(
        "video_transcript_api.downloaders.bilibili.requests.get",
        side_effect=responses,
    ) as mock_get, patch(
        "video_transcript_api.downloaders.bilibili.time.sleep", return_value=None
    ), patch.object(dl, "get_video_info", side_effect=AssertionError("no download")):
        meta = dl.get_metadata(URL)

    assert meta.title == "After Retry"
    assert mock_get.call_count >= 2


def test_official_all_fail_falls_back_to_bvid():
    """All official API attempts fail -> title is the BV id (stable), author
    is empty. Never a garbage short-link code."""
    dl = _make_downloader(use_bbdown=True)
    with patch(
        "video_transcript_api.downloaders.bilibili.requests.get",
        side_effect=__import__("requests").exceptions.Timeout("timeout"),
    ), patch(
        "video_transcript_api.downloaders.bilibili.time.sleep", return_value=None
    ), patch.object(dl, "get_video_info", side_effect=AssertionError("no download")):
        meta = dl.get_metadata(URL)

    assert meta.title == BV_ID
    assert meta.author == ""


def test_buvid3_cookie_attached():
    """The official API request carries a buvid3 cookie to dodge IP-level
    risk control."""
    dl = _make_downloader(use_bbdown=True)
    with patch(
        "video_transcript_api.downloaders.bilibili.requests.get",
        return_value=_official_response(),
    ) as mock_get, patch.object(
        dl, "get_video_info", side_effect=AssertionError("no download")
    ):
        dl.get_metadata(URL)

    _, kwargs = mock_get.call_args
    cookies = kwargs.get("cookies") or {}
    headers = kwargs.get("headers") or {}
    cookie_header = headers.get("Cookie", "")
    assert "buvid3" in cookies or "buvid3" in cookie_header


# ---------------------------------------------------------------------------
# TikHub mode safety net
# ---------------------------------------------------------------------------

def test_tikhub_mode_get_info_raises_metadata_survives():
    """TikHub mode: get_video_info raising must not break metadata; official
    API result is still used."""
    dl = _make_downloader(use_bbdown=False)
    with patch(
        "video_transcript_api.downloaders.bilibili.requests.get",
        return_value=_official_response(title="Official TikHub", author="Owner"),
    ), patch.object(
        dl, "get_video_info", side_effect=RuntimeError("tikhub api error")
    ):
        meta = dl.get_metadata(URL)

    assert meta.title == "Official TikHub"
    assert meta.author == "Owner"


def test_tikhub_mode_cid_into_extra():
    """TikHub mode: get_video_info contributes cid into metadata.extra."""
    dl = _make_downloader(use_bbdown=False)
    with patch(
        "video_transcript_api.downloaders.bilibili.requests.get",
        return_value=_official_response(),
    ), patch.object(
        dl, "get_video_info",
        return_value={
            "video_id": BV_ID,
            "video_title": "t",
            "author": "a",
            "cid": 999,
            "platform": "bilibili",
        },
    ):
        meta = dl.get_metadata(URL)

    assert meta.extra.get("cid") == 999
