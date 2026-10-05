"""Unit tests for finalize_presentation_metadata."""

from unittest.mock import MagicMock

import pytest
import requests

from video_transcript_api.api.services.transcription import (
    finalize_presentation_metadata,
)
from video_transcript_api.downloaders.models import VideoMetadata


class _StubDownloader:
    def __init__(self, metadata=None, exc=None):
        self._metadata = metadata
        self._exc = exc
        self.calls = []

    def get_metadata(self, url):
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        return self._metadata


def test_finalize_applies_metadata_override_over_existing():
    downloader = _StubDownloader(
        metadata=VideoMetadata(
            video_id="id1",
            platform="test",
            title="Fetched Title",
            author="Fetched Author",
            description="Fetched Desc",
        )
    )
    title, author, description = finalize_presentation_metadata(
        downloader=downloader,
        url="https://example.com/episode/abc",
        metadata_override={"title": "Override Title", "author": "Override Author"},
        title="Existing Title",
        author="Existing Author",
        description="",
    )
    assert title == "Override Title"
    assert author == "Override Author"
    assert downloader.calls == []


def test_finalize_retries_get_metadata_for_blank_title_and_author():
    downloader = _StubDownloader(
        metadata=VideoMetadata(
            video_id="id1",
            platform="xiaoyuzhou",
            title="Real Episode Title",
            author="Real Author",
            description="Real description",
        )
    )
    url = "https://www.xiaoyuzhoufm.com/episode/6a89b9b7008ed7314d3acdbe"
    title, author, description = finalize_presentation_metadata(
        downloader=downloader,
        url=url,
        metadata_override=None,
        title="",
        author="",
        description="",
    )
    assert title == "Real Episode Title"
    assert author == "Real Author"
    assert description == "Real description"
    assert downloader.calls == [url]


def test_finalize_does_not_overwrite_existing_non_empty_title():
    downloader = _StubDownloader(
        metadata=VideoMetadata(
            video_id="id1",
            platform="test",
            title="Retry Title",
            author="Retry Author",
            description="",
        )
    )
    title, author, description = finalize_presentation_metadata(
        downloader=downloader,
        url="https://example.com/watch?v=abc",
        metadata_override=None,
        title="Already Known Title",
        author="",
        description="",
    )
    assert title == "Already Known Title"
    assert author == "Retry Author"
    assert downloader.calls == ["https://example.com/watch?v=abc"]


def test_finalize_falls_back_to_basename_and_unknown_when_retry_fails():
    downloader = _StubDownloader(exc=requests.exceptions.ConnectTimeout("timed out"))
    url = "https://www.xiaoyuzhoufm.com/episode/6a89b9b7008ed7314d3acdbe"
    title, author, description = finalize_presentation_metadata(
        downloader=downloader,
        url=url,
        metadata_override=None,
        title="",
        author="",
        description="",
    )
    assert title == "6a89b9b7008ed7314d3acdbe"
    assert author == "Unknown"
    assert description == ""
    assert downloader.calls == [url]


def test_finalize_swallows_get_metadata_exception():
    downloader = MagicMock()
    downloader.get_metadata.side_effect = RuntimeError("boom")
    url = "https://example.com/podcast/1"
    title, author, description = finalize_presentation_metadata(
        downloader=downloader,
        url=url,
        metadata_override=None,
        title="",
        author="",
        description="",
    )
    assert title == "1"
    assert author == "Unknown"
    downloader.get_metadata.assert_called_once_with(url)


def test_finalize_ignores_non_string_override_values():
    url = "https://www.xiaoyuzhoufm.com/episode/6a89b9b7008ed7314d3acdbe"
    title, author, description = finalize_presentation_metadata(
        downloader=None,
        url=url,
        metadata_override={"title": 123, "author": None},
        title="",
        author="",
        description="",
    )
    assert title == "6a89b9b7008ed7314d3acdbe"
    assert author == "Unknown"
    assert description == ""


def test_finalize_preserves_str_title_against_non_string_override():
    title, author, description = finalize_presentation_metadata(
        downloader=None,
        url="https://example.com/episode/abc",
        metadata_override={"title": 123, "author": 999},
        title="Already Known Title",
        author="Existing Author",
        description="",
    )
    assert title == "Already Known Title"
    assert author == "Existing Author"
    assert description == ""
