import asyncio
import datetime
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from video_transcript_api.api.services import transcription
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.errors import InvalidMediaError
from video_transcript_api.utils.tempfile_manager import TempFileManager


def _key():
    now = datetime.datetime.now(datetime.timezone.utc)
    return f"{int(now.timestamp() * 1000)}-{uuid.uuid4()}"


def test_dispatcher_fails_stale_local_mapping_before_processing_or_executor_submit(
    tmp_path, monkeypatch
):
    cache = CacheManager(str(tmp_path / "cache"))
    intent = cache.register_local_upload(
        owner_user_id="alice", idempotency_key=_key(), retention="30d"
    )
    task_id = cache.generate_task_id()
    upload = cache.accept_local_upload(
        intent["upload_id"],
        media_id=f"upload_{uuid.uuid4().hex}",
        task_id=task_id,
        filename="clip.mp4",
        request_metadata={"filename": "clip.mp4"},
        media_path=str(tmp_path / "missing-upload.bin"),
        byte_size=12,
        sha256="f" * 64,
    )
    assert upload["root_task_id"] == task_id
    assert cache.get_admitted_local_upload_by_task(task_id)["media_path"].endswith(
        "missing-upload.bin"
    )

    queue = asyncio.Queue()
    queue.put_nowait({"id": task_id, "url": "https://example.invalid/display-only", "platform": "local_upload"})
    temp_manager = TempFileManager(str(tmp_path / "temp"))
    task_dir = temp_manager.create_task_dir(task_id)
    runtime = MagicMock()
    runtime.inflight_registry.release = MagicMock()
    runtime.release_upload_temp = MagicMock()
    executor = MagicMock()
    process_calls = []
    original_update = cache.update_task_status
    failed = asyncio.Event()

    def update_and_signal(*args, **kwargs):
        result = original_update(*args, **kwargs)
        if args[1] == "failed":
            failed.set()
        return result

    monkeypatch.setattr(transcription, "cache_manager", cache)
    monkeypatch.setattr(transcription, "task_queue", queue)
    monkeypatch.setattr(transcription, "executor", executor)
    monkeypatch.setattr(transcription, "get_runtime", lambda: runtime)
    monkeypatch.setattr(transcription, "get_temp_manager", lambda: temp_manager)
    monkeypatch.setattr(cache, "update_task_status", update_and_signal)
    monkeypatch.setattr(
        transcription,
        "process_transcription",
        lambda *args, **kwargs: process_calls.append((args, kwargs)),
    )

    async def run_dispatcher_once():
        dispatcher = asyncio.create_task(transcription.process_task_queue())
        await asyncio.wait_for(failed.wait(), timeout=2)
        dispatcher.cancel()
        try:
            await dispatcher
        except asyncio.CancelledError:
            pass

    asyncio.run(run_dispatcher_once())

    root = cache.get_task_by_id(task_id)
    assert root["status"] == "failed"
    assert process_calls == []
    executor.submit.assert_not_called()
    runtime.inflight_registry.release.assert_called_once_with("transcription", task_id)
    runtime.release_upload_temp.assert_called_once_with(task_id)
    assert not task_dir.exists()
    assert queue._unfinished_tasks == 0


@pytest.mark.parametrize(
    ("probe_result", "probe_error", "expected_code"),
    [
        (None, None, "media_duration_unknown"),
        (None, InvalidMediaError("no_audio_track"), "no_audio_track"),
        (3601.0, None, "media_duration_limit"),
    ],
)
def test_local_media_admission_rejects_before_asr_and_never_uses_url_downloader(
    tmp_path, monkeypatch, probe_result, probe_error, expected_code
):
    task_id = f"task_{uuid.uuid4().hex}"
    temp_manager = TempFileManager(str(tmp_path / "temp"))
    task_dir = temp_manager.create_task_dir(task_id)
    media_path = task_dir / "upload-source.bin"
    media_path.write_bytes(b"real local media fixture")
    transcriber = MagicMock()
    terminal_calls = []

    def probe(path):
        assert path == str(media_path)
        if probe_error is not None:
            raise probe_error
        return probe_result

    monkeypatch.setattr(transcription, "get_temp_manager", lambda: temp_manager)
    monkeypatch.setattr(transcription, "_resolve_view_url", lambda *args: None)
    monkeypatch.setattr(transcription, "get_notification_router", MagicMock)
    monkeypatch.setattr(transcription, "create_downloader", lambda *args: pytest.fail("local upload called URL downloader"))
    monkeypatch.setattr(transcription, "_ensure_audio_track", probe)
    monkeypatch.setattr(transcription, "Transcriber", lambda: transcriber)
    monkeypatch.setattr(transcription.slow_alert, "track_transcription", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        transcription,
        "finalize_terminal_status_and_notify",
        lambda *args, **kwargs: terminal_calls.append(kwargs["error_message"]) or True,
    )
    monkeypatch.setattr(
        transcription,
        "get_config",
        lambda: {"storage": {"upload_limits": {"max_media_hours": 1}}},
    )

    result = transcription.process_transcription(
        task_id,
        "https://display-only.example/source",
        metadata_override={"title": "本地媒体"},
        local_media_path=str(media_path),
        local_media_id=f"upload_{uuid.uuid4().hex}",
    )

    assert result["status"] == "failed"
    assert expected_code in result["message"] or expected_code in result.get("error", "")
    assert terminal_calls
    transcriber.transcribe.assert_not_called()
    assert not media_path.exists()
