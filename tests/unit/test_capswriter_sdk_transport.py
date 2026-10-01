import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from capswriter_asr import Transcript

from video_transcript_api.transcriber.capswriter_client import CapsWriterClient, Config


def _make_client(output_dir: Path) -> CapsWriterClient:
    """Build a client without loading project configuration."""
    with patch.object(CapsWriterClient, "__init__", lambda self: None):
        client = CapsWriterClient()
    client.output_dir = str(output_dir)
    client.max_retries = 1
    client.retry_delay = 0
    client.log = MagicMock()
    return client


def test_sdk_transport_passes_config_and_writes_transcript_sidecars(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    media_path = tmp_path / "audio.mp4"
    media_path.write_bytes(b"fixture")
    client = _make_client(output_dir)

    monkeypatch.setattr(Config, "server_addr", "test-server")
    monkeypatch.setattr(Config, "server_port", 6010)
    monkeypatch.setattr(Config, "file_seg_duration", 37)
    monkeypatch.setattr(Config, "file_seg_overlap", 5)
    monkeypatch.setattr(Config, "generate_txt", True)
    monkeypatch.setattr(Config, "generate_merge_txt", False)
    monkeypatch.setattr(Config, "generate_json", False)
    monkeypatch.setattr(Config, "generate_funasr_compat", True)

    # text_accu is the body; the echo draft in transcript.text deliberately
    # differs from it, and the products must follow text_accu.
    transcript = Transcript(
        text="hallo",
        tokens=list("hello!"),
        timestamps=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5],
        duration=1.5,
        raw={
            "task_id": "task-1",
            "time_start": 10.0,
            "time_complete": 12.5,
            "text_accu": "hello!",
        },
    )
    with patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        return_value=transcript,
    ) as sdk_call:
        success, generated_files = client.transcribe_file(str(media_path))

    assert success is True
    assert set(generated_files) == {
        output_dir / "audio.txt",
        output_dir / "audio_funasr.json",
    }

    path, url = sdk_call.call_args.args
    keywords = sdk_call.call_args.kwargs
    assert path == media_path
    assert url.startswith("ws://")
    assert keywords["encoding"] == "flac"
    assert keywords["seg_duration"] == 37
    assert keywords["seg_overlap"] == 5
    assert "model" not in keywords

    assert (output_dir / "audio.txt").read_text(encoding="utf-8") == "hello!"
    sidecar = json.loads(
        (output_dir / "audio_funasr.json").read_text(encoding="utf-8")
    )
    assert sidecar["task_id"] == "task-1"
    assert sidecar["duration"] == 1.5
    assert sidecar["processing_time"] == 2.5
    assert sidecar["segments"] == [
        {"start_time": 0.0, "end_time": 0.5, "text": "hello!"}
    ]
