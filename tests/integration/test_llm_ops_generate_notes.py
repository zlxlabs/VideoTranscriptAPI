"""Integration tests for the notes-only LLM queue branch.

All console output must stay ASCII-only.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest

from video_transcript_api.api.services import llm_ops
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.llm.processors.notes_processor import (
    compute_notes_anchor_fingerprint,
)
from video_transcript_api.utils.llm_status import (
    CalibrationStatus,
    ChaptersStatus,
    NotesStatus,
    SummaryStatus,
)
from video_transcript_api.utils.task_status import TaskStatus


@pytest.fixture
def notes_cache(tmp_path):
    manager = CacheManager(str(tmp_path / "cache"))
    yield manager
    manager.close()


def _seed_layered_notes_cache(cache_manager):
    source_segments = [
        {
            "start_time": 0,
            "end_time": 60,
            "speaker": "Host",
            "text": "raw transcript",
        }
    ]
    source_fingerprint = compute_notes_anchor_fingerprint(source_segments)
    cache_manager.save_cache(
        platform="youtube",
        url="https://example.com/notes",
        media_id="notes-media",
        use_speaker_recognition=False,
        transcript_data="raw transcript",
        transcript_type="capswriter",
        title="Notes demo",
        author="Owner",
    )
    cache_manager.save_llm_result(
        platform="youtube",
        media_id="notes-media",
        use_speaker_recognition=False,
        llm_type="structured",
        content={"dialogs": source_segments},
    )
    cache_manager.save_llm_result(
        platform="youtube",
        media_id="notes-media",
        use_speaker_recognition=False,
        llm_type="calibrated",
        content="calibrated artifact",
    )
    cache_manager.save_llm_result(
        platform="youtube",
        media_id="notes-media",
        use_speaker_recognition=False,
        llm_type="summary",
        content="summary artifact",
    )
    cache_manager.save_llm_result(
        platform="youtube",
        media_id="notes-media",
        use_speaker_recognition=False,
        llm_type="chapters",
        content={
            "format_version": "v1",
            "source": {
                "kind": "dialogs",
                "segment_count": 1,
                "fingerprint": source_fingerprint,
            },
            "chapters": [
                {
                    "title": "Chapter",
                    "gist": "Gist",
                    "start_seg": 0,
                    "end_seg": 0,
                }
            ],
        },
    )
    cache_manager.save_llm_status(
        platform="youtube",
        media_id="notes-media",
        use_speaker_recognition=False,
        calibration_status=CalibrationStatus.FULL,
        summary_status=SummaryStatus.GENERATED,
        chapters_status=ChaptersStatus.GENERATED,
    )
    return (
        cache_manager.get_cache(
            "youtube",
            "notes-media",
            use_speaker_recognition=False,
        ),
        source_fingerprint,
    )


def _notes_task(task_id, cache_dir):
    return {
        "task_id": task_id,
        "url": "https://example.com/notes",
        "display_url": "https://example.com/notes",
        "platform": "youtube",
        "media_id": "notes-media",
        "video_title": "Notes demo",
        "author": "Owner",
        "description": "",
        "transcript": "calibrated artifact",
        "use_speaker_recognition": False,
        "transcription_data": None,
        "cache_dir": cache_dir,
        "notification_webhooks": {},
        "processing_options": {
            "calibrate": False,
            "summarize": False,
            "infer_speaker_names": False,
            "chapters": False,
            "notes": True,
        },
    }


def _notes_patches(cache_manager, processor_result):
    coordinator = SimpleNamespace(
        llm_client=MagicMock(),
        config=SimpleNamespace(get_models=lambda: {}),
    )
    processor = MagicMock()
    processor.process.return_value = processor_result
    processor_type = MagicMock(return_value=processor)
    notification_router = MagicMock()
    return processor, notification_router, [
        patch.object(llm_ops, "cache_manager", cache_manager),
        patch.object(llm_ops, "llm_coordinator", coordinator),
        patch.object(llm_ops, "llm_task_queue", MagicMock()),
        patch.object(llm_ops, "NotesProcessor", processor_type),
        patch.object(
            llm_ops,
            "get_notification_router",
            lambda: notification_router,
        ),
    ]


def test_notes_task_only_adds_notes_layer(notes_cache):
    snapshot, source_fingerprint = _seed_layered_notes_cache(notes_cache)
    task_id = notes_cache.create_task(
        url="https://example.com/notes",
        platform="youtube",
        media_id="notes-media",
    )["task_id"]
    notes_cache.update_task_status(task_id, TaskStatus.CALIBRATING)

    protected_files = {
        name: os.path.join(snapshot["file_path"], name)
        for name in (
            "llm_calibrated.txt",
            "llm_summary.txt",
            "llm_chapters.json",
        )
    }
    protected_contents = {
        name: Path(path).read_bytes()
        for name, path in protected_files.items()
    }
    protected_mtimes = {
        name: os.path.getmtime(path)
        for name, path in protected_files.items()
    }

    result = SimpleNamespace(
        status=NotesStatus.GENERATED,
        text="## [00:00:00 - 00:01:00] Chapter\n- Notes",
        error=None,
        fingerprint=source_fingerprint,
        chapter_count=1,
    )
    processor, notification_router, contexts = _notes_patches(notes_cache, result)
    processor.process.side_effect = lambda **kwargs: (
        kwargs["progress_callback"](1, 1) or result
    )
    for context in contexts:
        context.start()
    try:
        llm_ops._handle_llm_task(_notes_task(task_id, snapshot["file_path"]))
    finally:
        for context in contexts:
            context.stop()

    updated = notes_cache.get_cache(
        "youtube",
        "notes-media",
        use_speaker_recognition=False,
    )
    assert updated["llm_notes"] == result.text
    assert updated["llm_status"]["notes_status"] == NotesStatus.GENERATED
    assert updated["llm_status"]["calibration_status"] == CalibrationStatus.FULL
    assert updated["llm_status"]["summary_status"] == SummaryStatus.GENERATED
    assert updated["llm_status"]["chapters_status"] == ChaptersStatus.GENERATED
    assert notes_cache.get_task_by_id(task_id)["status"] == TaskStatus.SUCCESS
    processor.process.assert_called_once_with(
        cache_dir=snapshot["file_path"],
        selected_models={},
        progress_callback=ANY,
    )
    assert notes_cache.get_task_by_id(task_id)["progress"] == {
        "stage": "notes", "done": 1, "total": 1
    }
    notification_router.send_long_text.assert_called_once()
    long_text_call = notification_router.send_long_text.call_args.kwargs
    assert long_text_call["title"] == "Notes demo"
    assert "详细笔记已生成" in long_text_call["text"]
    notification_router.notify_task_status.assert_called()
    terminal_kwargs = notification_router.notify_task_status.call_args.kwargs
    assert terminal_kwargs.get("status") == "【任务完成】"

    for name, path in protected_files.items():
        with open(path, "rb") as artifact_file:
            assert artifact_file.read() == protected_contents[name]
        assert os.path.getmtime(path) == protected_mtimes[name]


def test_notes_task_failure_writes_failed_status_without_artifact(notes_cache):
    snapshot, source_fingerprint = _seed_layered_notes_cache(notes_cache)
    task_id = notes_cache.create_task(
        url="https://example.com/notes",
        platform="youtube",
        media_id="notes-media",
    )["task_id"]
    notes_cache.update_task_status(task_id, TaskStatus.CALIBRATING)
    result = SimpleNamespace(
        status=NotesStatus.FAILED,
        text=None,
        error="chapter 1 notes generation failed",
        fingerprint=source_fingerprint,
        chapter_count=1,
    )
    _, notification_router, contexts = _notes_patches(notes_cache, result)
    for context in contexts:
        context.start()
    try:
        llm_ops._handle_llm_task(_notes_task(task_id, snapshot["file_path"]))
    finally:
        for context in contexts:
            context.stop()

    updated = notes_cache.get_cache(
        "youtube",
        "notes-media",
        use_speaker_recognition=False,
    )
    assert "llm_notes" not in updated
    assert updated["llm_status"]["notes_status"] == NotesStatus.FAILED
    assert notes_cache.get_task_by_id(task_id)["status"] == TaskStatus.FAILED
    assert (
        notes_cache.get_task_by_id(task_id)["terminal_snapshot"]["status"]
        == TaskStatus.FAILED
    )
    notification_router.send_long_text.assert_not_called()
    notification_router.notify_task_status.assert_called()
    fail_kwargs = notification_router.notify_task_status.call_args.kwargs
    assert fail_kwargs.get("status") == "【任务失败】"
    assert "【LLM API调用异常】" in (fail_kwargs.get("error") or "")


def test_notes_task_discards_result_when_chapters_change_before_persist(
    notes_cache,
):
    snapshot, source_fingerprint = _seed_layered_notes_cache(notes_cache)
    task_id = notes_cache.create_task(
        url="https://example.com/notes",
        platform="youtube",
        media_id="notes-media",
    )["task_id"]
    notes_cache.update_task_status(task_id, TaskStatus.CALIBRATING)
    result = SimpleNamespace(
        status=NotesStatus.GENERATED,
        text="## [00:00:00 - 00:01:00] Chapter\n- Stale notes",
        error=None,
        fingerprint=source_fingerprint,
        chapter_count=1,
    )
    processor, notification_router, contexts = _notes_patches(notes_cache, result)

    def replace_chapters_before_persist(**_kwargs):
        changed_segments = [
            {
                "start_time": 0,
                "end_time": 60,
                "speaker": "Host",
                "text": "changed transcript",
            }
        ]
        changed_fingerprint = compute_notes_anchor_fingerprint(changed_segments)
        assert changed_fingerprint != source_fingerprint
        assert notes_cache.save_llm_result(
            platform="youtube",
            media_id="notes-media",
            use_speaker_recognition=False,
            llm_type="structured",
            content={"dialogs": changed_segments},
        )
        assert notes_cache.save_llm_result(
            platform="youtube",
            media_id="notes-media",
            use_speaker_recognition=False,
            llm_type="chapters",
            content={
                "format_version": "v1",
                "source": {
                    "kind": "dialogs",
                    "segment_count": 1,
                    "fingerprint": changed_fingerprint,
                },
                "chapters": [
                    {
                        "title": "Changed chapter",
                        "gist": "Changed gist",
                        "start_seg": 0,
                        "end_seg": 0,
                    }
                ],
            },
        )
        return result

    processor.process.side_effect = replace_chapters_before_persist
    for context in contexts:
        context.start()
    try:
        llm_ops._handle_llm_task(_notes_task(task_id, snapshot["file_path"]))
    finally:
        for context in contexts:
            context.stop()

    updated = notes_cache.get_cache(
        "youtube",
        "notes-media",
        use_speaker_recognition=False,
    )
    failed_task = notes_cache.get_task_by_id(task_id)
    expected_error = (
        "chapters changed during notes generation, "
        "notes discarded to avoid misalignment"
    )
    assert "llm_notes" not in updated
    assert updated["llm_status"]["notes_status"] == NotesStatus.FAILED
    assert failed_task["status"] == TaskStatus.FAILED
    assert expected_error in failed_task["error_message"]
    assert expected_error in failed_task["terminal_snapshot"]["error_message"]
    notification_router.send_long_text.assert_not_called()
