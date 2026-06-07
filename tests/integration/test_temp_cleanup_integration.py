"""Integration tests for task temp cleanup wiring in process_transcription.

Verifies the spec acceptance criteria (D7):
- a successful task cleans its temp files
- a task that throws during transcription still cleans its temp files (finally)

The whole pipeline (downloader / transcriber / cache / notifier / LLM queue) is
mocked; only the temp-file lifecycle wiring is exercised.
"""
import os
from pathlib import Path

import pytest

from src.video_transcript_api.utils.tempfile_manager import TempFileManager
import src.video_transcript_api.api.services.transcription as tx


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Meta:
    def __init__(self):
        self.video_id = "vid123"
        self.title = "Test Video"
        self.author = "Tester"
        self.description = ""
        self.platform = "generic"


class _DownloadInfo:
    def __init__(self):
        self.downloaded = False
        self.local_file = None
        self.download_url = "https://example.com/video.mp4"
        self.filename = "video.mp4"


class FakeDownloader:
    """Writes a fake media file into the current task dir, like a real download."""

    def __init__(self, temp_manager, should_raise_on_transcribe=False):
        self._tm = temp_manager
        self.written_path = None

    def get_metadata(self, url):
        return _Meta()

    def get_download_info(self, url):
        return _DownloadInfo()

    def get_subtitle(self, url):
        return None

    def download_file(self, url, filename):
        task_dir = self._tm.get_current_task_dir()
        p = Path(task_dir) / filename
        p.write_bytes(b"fake-video-bytes" * 1000)
        self.written_path = p
        return str(p)


class FakeTranscriber:
    raise_it = False

    def transcribe(self, local_file, output_base=None):
        if FakeTranscriber.raise_it:
            raise RuntimeError("transcription boom")
        return {"transcript": "hello world"}


class FakeRouter:
    def notify_task_status(self, *a, **k):
        return None

    def send_text(self, *a, **k):
        return None

    def send_long_text(self, *a, **k):
        return None


class FakeCache:
    def get_cache(self, *a, **k):
        return None

    def save_cache(self, *a, **k):
        return True

    def update_task_status(self, *a, **k):
        return None

    def get_task_by_id(self, *a, **k):
        return {}


class FakeQueue:
    def put(self, *a, **k):
        return None


# ---------------------------------------------------------------------------
# Fixture: wire all collaborators to fakes, temp manager to tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture
def wired(tmp_path, monkeypatch):
    tm = TempFileManager(str(tmp_path / "temp"), retention_hours=24)
    downloader = FakeDownloader(tm)

    monkeypatch.setattr(tx, "get_temp_manager", lambda: tm)
    monkeypatch.setattr(tx, "create_downloader", lambda url: downloader)
    monkeypatch.setattr(tx, "Transcriber", FakeTranscriber)
    monkeypatch.setattr(tx, "cache_manager", FakeCache())
    monkeypatch.setattr(tx, "llm_task_queue", FakeQueue())
    monkeypatch.setattr(tx, "get_notification_router", lambda: FakeRouter())

    FakeTranscriber.raise_it = False
    yield tm, downloader
    FakeTranscriber.raise_it = False


def test_success_cleans_task_temp(wired):
    tm, downloader = wired
    result = tx.process_transcription(
        task_id="t-success",
        url="https://example.com/video.mp4",
    )
    assert result["status"] == "success"
    # the downloaded file landed in the task dir and was removed
    assert downloader.written_path is not None
    assert not downloader.written_path.exists()
    # task dir is gone and no longer tracked / active
    assert tm.get_task_dir("t-success") is None
    assert not tm.is_active("t-success")


def test_exception_still_cleans_task_temp(wired):
    tm, downloader = wired
    FakeTranscriber.raise_it = True
    result = tx.process_transcription(
        task_id="t-fail",
        url="https://example.com/video.mp4",
    )
    assert result["status"] == "failed"
    # even though transcription threw, the temp file was cleaned by finally
    assert downloader.written_path is not None
    assert not downloader.written_path.exists()
    assert tm.get_task_dir("t-fail") is None
    assert not tm.is_active("t-fail")


# ---------------------------------------------------------------------------
# Regression: base.py download error path must not delete OTHER tasks' files
# (previously called the global clean_up() which wiped every tracked file).
# ---------------------------------------------------------------------------

def test_discard_temp_does_not_touch_other_tasks(tmp_path, monkeypatch):
    from src.video_transcript_api.utils import tempfile_manager as tfm

    mgr = TempFileManager(str(tmp_path / "temp"))
    monkeypatch.setattr(tfm, "_shared_manager", mgr)

    from src.video_transcript_api.downloaders.generic import GenericDownloader

    dl = GenericDownloader()
    assert dl.temp_manager is mgr

    # task A: another concurrent task's in-flight file
    da = mgr.create_task_dir("A")
    fa = da / "a.mp4"
    fa.write_bytes(b"x" * 100)
    # task B: the file whose download just failed
    db = mgr.create_task_dir("B")
    fb = db / "b.mp4"
    fb.write_bytes(b"y" * 100)

    dl._discard_temp(fb)

    assert not fb.exists()        # its own failed file removed
    assert fa.exists()            # other task's in-flight file untouched (regression)


# ---------------------------------------------------------------------------
# Regression: YouTube-API download without target_dir must land under the
# current task dir (data/temp/task_<id>/...), not system /tmp.
# ---------------------------------------------------------------------------

class _FakeResp:
    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=8192):
        yield b"audio-bytes"


def test_youtube_api_download_lands_in_task_dir(tmp_path, monkeypatch):
    from src.video_transcript_api.utils import tempfile_manager as tfm
    import src.video_transcript_api.downloaders.youtube_api_client as yac

    mgr = TempFileManager(str(tmp_path / "temp"))
    monkeypatch.setattr(tfm, "_shared_manager", mgr)
    monkeypatch.setattr(yac.requests, "get", lambda *a, **k: _FakeResp())

    mgr.create_task_dir("YT")
    mgr.set_current_task("YT")

    client = yac.YouTubeApiClient({"base_url": "http://example.com", "api_key": "k"})
    path = client.download_to_local("/api/v1/files/audio.m4a")

    task_dir = mgr.get_task_dir("YT")
    assert task_dir is not None
    assert Path(path).resolve().is_relative_to(task_dir.resolve())
    # and cleaning the task removes the downloaded file
    mgr.clean_up_task("YT")
    assert not Path(path).exists()
