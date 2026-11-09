"""Download integrity regressions using streamed fake HTTP responses."""

import gzip
import os

import requests

from video_transcript_api.downloaders.base import BaseDownloader
from video_transcript_api.downloaders.generic import GenericDownloader


class _FakeResponse:
    def __init__(self, body, headers=None):
        self.body = body
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.status_code = 200

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield self.body


class _GenericTempManager:
    def __init__(self, task_dir):
        self.task_dir = task_dir

    def get_current_task_dir(self):
        return self.task_dir


class _BaseTempManager:
    def __init__(self, temp_dir):
        self.temp_dir = temp_dir
        self.created = []

    def create_temp_file(self, suffix):
        path = os.path.join(self.temp_dir, f"download{suffix}")
        self.created.append(path)
        return path

    def untrack_file(self, path):
        return None


class _ConcreteBaseDownloader(BaseDownloader):
    def can_handle(self, url):
        return True

    def extract_video_id(self, url):
        return "test"

    def _fetch_metadata(self, url, video_id):
        raise NotImplementedError

    def _fetch_download_info(self, url, video_id):
        raise NotImplementedError

    def get_subtitle(self, url):
        return None


def _make_generic(tmp_path):
    downloader = GenericDownloader()
    task_dir = tmp_path / "task"
    task_dir.mkdir(exist_ok=True)
    downloader.temp_manager = _GenericTempManager(str(task_dir))
    downloader.max_download_bytes = 0
    return downloader, task_dir


def test_existing_partial_file_is_replaced_by_full_200_body(tmp_path, monkeypatch):
    downloader, task_dir = _make_generic(tmp_path)
    local_path = task_dir / "audio.mp3"
    local_path.write_bytes(b"old-partial")
    payload = b"complete-server-response"
    response = _FakeResponse(payload, {"content-length": str(len(payload))})
    requests_seen = []

    def fake_request(method, url, **kwargs):
        requests_seen.append((method, kwargs))
        return response

    monkeypatch.setattr(downloader, "_safe_request", fake_request)
    monkeypatch.setattr(downloader, "_validate_media_file", lambda path: True)
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    result = downloader.download_file("https://files.example/audio.mp3", "audio.mp3")

    assert result == str(local_path)
    assert local_path.read_bytes() == payload
    assert all("Range" not in kwargs.get("headers", {}) for _, kwargs in requests_seen)


def test_generic_empty_response_without_content_length_fails_and_leaves_no_file(
    tmp_path, monkeypatch
):
    downloader, task_dir = _make_generic(tmp_path)
    monkeypatch.setattr(
        downloader,
        "_safe_request",
        lambda *args, **kwargs: _FakeResponse(b""),
    )
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    result = downloader.download_file("https://files.example/audio.mp3", "audio.mp3")

    assert result is None
    assert not (task_dir / "audio.mp3").exists()


def test_generic_nonempty_body_without_content_length_runs_media_validation(
    tmp_path, monkeypatch
):
    downloader, task_dir = _make_generic(tmp_path)
    payload = b"media-payload"
    validations = []
    monkeypatch.setattr(
        downloader,
        "_safe_request",
        lambda *args, **kwargs: _FakeResponse(payload),
    )
    monkeypatch.setattr(
        downloader,
        "_validate_media_file",
        lambda path: validations.append(path) or True,
    )

    result = downloader.download_file("https://files.example/audio.mp3", "audio.mp3")

    assert result == str(task_dir / "audio.mp3")
    assert (task_dir / "audio.mp3").read_bytes() == payload
    assert validations == [str(task_dir / "audio.mp3")]


def test_generic_streaming_size_limit_discards_oversized_body(tmp_path, monkeypatch):
    downloader, task_dir = _make_generic(tmp_path)
    downloader.max_download_bytes = 3
    payload = b"too-large"
    monkeypatch.setattr(
        downloader,
        "_safe_request",
        lambda *args, **kwargs: _FakeResponse(payload),
    )

    result = downloader.download_file("https://files.example/audio.mp3", "audio.mp3")

    assert result is None
    assert not (task_dir / "audio.mp3").exists()


def test_base_content_length_mismatch_fails_and_discards_temp_file(
    tmp_path, monkeypatch
):
    downloader = _ConcreteBaseDownloader()
    downloader.temp_manager = _BaseTempManager(str(tmp_path))
    downloader.max_download_bytes = 0
    monkeypatch.setattr(
        "video_transcript_api.downloaders.base.requests.get",
        lambda *args, **kwargs: _FakeResponse(
            b"short", {"Content-Length": "12", "Content-Encoding": "IdEnTiTy"}
        ),
    )
    monkeypatch.setattr(downloader, "_validate_media_file", lambda path: True)
    error_logs = []
    monkeypatch.setattr(
        "video_transcript_api.downloaders.base.logger.error",
        lambda message, *args, **kwargs: error_logs.append(message),
    )

    result = downloader.download_file(
        "https://files.example/audio.mp4", "audio.mp4", max_retries=1
    )

    assert result is None
    assert downloader.temp_manager.created
    assert not os.path.exists(downloader.temp_manager.created[0])
    assert any("DOWNLOAD_LENGTH_MISMATCH" in str(message) for message in error_logs)


def test_base_gzip_length_is_not_compared_to_decoded_bytes(tmp_path, monkeypatch):
    downloader = _ConcreteBaseDownloader()
    downloader.temp_manager = _BaseTempManager(str(tmp_path))
    downloader.max_download_bytes = 0
    decoded_body = b"decoded payload is longer than its gzip representation"
    compressed_body = gzip.compress(decoded_body)
    response = _FakeResponse(
        decoded_body,
        {
            "Content-Encoding": "gzip",
            "Content-Length": str(len(compressed_body)),
        },
    )
    monkeypatch.setattr(
        "video_transcript_api.downloaders.base.requests.get",
        lambda *args, **kwargs: response,
    )
    monkeypatch.setattr(downloader, "_validate_media_file", lambda path: True)

    result = downloader.download_file(
        "https://files.example/audio.mp4", "audio.mp4", max_retries=1
    )

    assert result == downloader.temp_manager.created[0]
    with open(result, "rb") as downloaded_file:
        assert downloaded_file.read() == decoded_body


def test_generic_gzip_length_is_not_compared_to_decoded_bytes(tmp_path, monkeypatch):
    downloader, task_dir = _make_generic(tmp_path)
    decoded_body = b"decoded payload is longer than its gzip representation"
    compressed_body = gzip.compress(decoded_body)
    response = _FakeResponse(
        decoded_body,
        {
            "Content-Encoding": "gzip",
            "Content-Length": str(len(compressed_body)),
        },
    )
    monkeypatch.setattr(downloader, "_safe_request", lambda *args, **kwargs: response)
    monkeypatch.setattr(downloader, "_validate_media_file", lambda path: True)

    result = downloader.download_file("https://files.example/audio.mp3", "audio.mp3")

    assert result == str(task_dir / "audio.mp3")
    assert (task_dir / "audio.mp3").read_bytes() == decoded_body
