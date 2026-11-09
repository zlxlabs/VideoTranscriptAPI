import pytest

from video_transcript_api.downloaders.generic import GenericDownloader


@pytest.fixture(autouse=True)
def isolate_media_probe_for_ssrf_transport_tests(request, monkeypatch):
    if request.module.__name__.endswith("test_generic_ssrf"):
        monkeypatch.setattr(
            GenericDownloader, "_validate_media_file", lambda self, path: True
        )
