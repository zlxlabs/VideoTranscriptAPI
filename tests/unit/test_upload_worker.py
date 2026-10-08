"""Contracts for local uploads entering the shared transcription dispatcher."""

import asyncio
import concurrent.futures
import datetime
import uuid
from unittest.mock import MagicMock

from video_transcript_api.api.services import transcription
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.tempfile_manager import TempFileManager


def _key():
    now = datetime.datetime.now(datetime.timezone.utc)
    return f"{int(now.timestamp() * 1000)}-{uuid.uuid4()}"


def test_dispatcher_consumes_persisted_media_mapping_and_processing_options(
    tmp_path, monkeypatch
):
    cache = CacheManager(str(tmp_path / "cache"))
    intent = cache.register_local_upload(
        owner_user_id="alice", idempotency_key=_key(), retention="30d"
    )
    task_id = cache.generate_task_id()
    temp_manager = TempFileManager(str(tmp_path / "temp"))
    task_dir = temp_manager.create_task_dir(task_id)
    media_path = task_dir / "upload-source.bin"
    media_bytes = b"actual upload media bytes"
    media_path.write_bytes(media_bytes)
    options = {
        "calibrate": True,
        "summarize": False,
        "infer_speaker_names": False,
        "chapters": False,
    }
    upload = cache.accept_local_upload(
        intent["upload_id"],
        media_id=f"upload_{uuid.uuid4().hex}",
        task_id=task_id,
        filename="clip.mp4",
        title="Owner title",
        source_url="",
        request_metadata={"filename": "clip.mp4", "title": "Owner title"},
        processing_options=options,
        media_path=str(media_path),
        byte_size=len(media_bytes),
        sha256="a" * 64,
    )
    queue = asyncio.Queue()
    queue.put_nowait(
        {
            "id": task_id,
            "url": "https://display-only.invalid/not-a-download-source",
            "platform": "local_upload",
            "processing_options": options,
        }
    )
    runtime = MagicMock()
    submitted = asyncio.Event()
    executor = MagicMock()
    executor.submit.side_effect = lambda function, *args: (
        submitted.set() or concurrent.futures.Future()
    )
    process_calls = []

    monkeypatch.setattr(transcription, "cache_manager", cache)
    monkeypatch.setattr(transcription, "task_queue", queue)
    monkeypatch.setattr(transcription, "executor", executor)
    monkeypatch.setattr(transcription, "get_runtime", lambda: runtime)
    monkeypatch.setattr(transcription, "get_temp_manager", lambda: temp_manager)
    monkeypatch.setattr(
        transcription,
        "process_transcription",
        lambda *args, **kwargs: process_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        transcription,
        "get_notification_router",
        lambda: MagicMock(),
    )

    async def dispatch_one():
        dispatcher = asyncio.create_task(transcription.process_task_queue())
        await asyncio.wait_for(submitted.wait(), timeout=2)
        submitted_args = executor.submit.call_args.args
        function, args = submitted_args[0], submitted_args[1:]
        function(*args)
        dispatcher.cancel()
        try:
            await dispatcher
        except asyncio.CancelledError:
            pass

    asyncio.run(dispatch_one())

    assert upload["root_task_id"] == task_id
    assert len(process_calls) == 1
    args, kwargs = process_calls[0]
    assert args[0] == task_id
    assert args[1] == ""
    assert kwargs["local_media_path"] == str(media_path.resolve())
    assert kwargs["local_media_id"] == upload["media_id"]
    assert kwargs["processing_options"] == options
    assert media_path.read_bytes() == media_bytes
    assert queue._unfinished_tasks == 0
