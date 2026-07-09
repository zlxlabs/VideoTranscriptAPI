"""GenericDownloader.download_file retry policy: failed attempts must be
spaced by a backoff delay, so transient outages (e.g. the file server
restarting during a deploy) are survived instead of burning all retries
within the same second.

Regression for prod incident 2026-07-09: 3 connection-refused retries all
fired in the same second while the recorder was mid-redeploy.

Console output English only.
"""

import requests

from video_transcript_api.downloaders.generic import GenericDownloader


class _StubTempManager:
    def __init__(self, task_dir):
        self._task_dir = task_dir

    def get_current_task_dir(self):
        return self._task_dir


def _make_downloader(tmp_path):
    downloader = GenericDownloader()
    downloader.temp_manager = _StubTempManager(str(tmp_path / "task"))
    return downloader


class TestDownloadRetryBackoff:
    def test_connection_errors_are_retried_with_backoff(self, tmp_path, monkeypatch):
        """All attempts refused -> returns None, with a positive, increasing
        delay before each retry (attempts - 1 sleeps in total)."""
        downloader = _make_downloader(tmp_path)

        def refuse(*args, **kwargs):
            raise requests.exceptions.ConnectionError("connection refused")

        monkeypatch.setattr(
            "video_transcript_api.downloaders.generic.requests.get", refuse
        )
        sleeps = []
        monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))

        result = downloader.download_file("http://files.internal/rec.mp4", "rec.mp4")

        assert result is None
        assert len(sleeps) == 2, "expected one backoff gap between each retry"
        assert all(s > 0 for s in sleeps)
        assert sleeps == sorted(sleeps), "backoff should not shrink"
