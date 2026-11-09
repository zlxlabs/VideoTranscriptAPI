"""Output names passed to shared CapsWriter workspaces must be task-unique."""

from tests.unit.test_transcription_audio_admission import RecordingTranscriber, wired
from tests.unit.test_media_duration_probe import _run as run_generic
from tests.unit.test_youtube_api_duration import _run as run_youtube_api


def test_youtube_api_output_base_contains_task_id(wired, monkeypatch, tmp_path):
    task_id = "task-youtube-output"
    result, _rec, _runner = run_youtube_api(
        wired, monkeypatch, tmp_path, task_id=task_id
    )

    assert result["status"] == "success", result
    assert len(RecordingTranscriber.calls) == 1
    output_base = RecordingTranscriber.calls[0][1]
    assert output_base.endswith(f"-{task_id}"), output_base


def test_generic_output_base_contains_task_id(wired, monkeypatch, tmp_path):
    task_id = "task-generic-output"
    result, _downloader, _runner = run_generic(
        wired, monkeypatch, tmp_path, task_id=task_id
    )

    assert result["status"] == "success", result
    assert len(RecordingTranscriber.calls) == 1
    output_base = RecordingTranscriber.calls[0][1]
    assert output_base.endswith(f"-{task_id}"), output_base
