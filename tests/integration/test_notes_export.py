"""Integration coverage for detailed-notes view exports.

All console output must stay ASCII-only.
"""

import asyncio

from starlette.requests import Request

from video_transcript_api.api.routes import views
from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.llm_status import NotesStatus
from video_transcript_api.utils.task_status import TaskStatus


def test_view_raw_notes_exports_generated_artifact(tmp_path, monkeypatch):
    manager = CacheManager(str(tmp_path / "cache"))
    try:
        task = manager.create_task(
            url="https://example.com/notes-export",
            platform="youtube",
            media_id="notes-export",
        )
        manager.save_cache(
            platform="youtube",
            url="https://example.com/notes-export",
            media_id="notes-export",
            use_speaker_recognition=False,
            transcript_data="source transcript",
            transcript_type="capswriter",
            title="Notes export",
            author="Owner",
        )
        manager.save_llm_result(
            platform="youtube",
            media_id="notes-export",
            use_speaker_recognition=False,
            llm_type="notes",
            content="## Chapter\n- Detailed note.",
        )
        manager.save_llm_status(
            platform="youtube",
            media_id="notes-export",
            use_speaker_recognition=False,
            notes_status=NotesStatus.GENERATED,
        )
        manager.update_task_status(
            task["task_id"],
            TaskStatus.SUCCESS,
            platform="youtube",
            media_id="notes-export",
        )
        monkeypatch.setattr(views, "cache_manager", manager)

        async def _inline_to_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

        monkeypatch.setattr(views.asyncio, "to_thread", _inline_to_thread)

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/view/{task['view_token']}",
                "query_string": b"raw=notes",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            }
        )
        response = asyncio.run(
            views.view_transcript(
                view_token=task["view_token"],
                request=request,
                raw="notes",
            )
        )

        assert response.status_code == 200
        assert response.headers["x-content-type"] == "notes"
        assert response.headers["x-document-title"] == "Notes export"
        import h11
        h11.Response(
            status_code=200,
            headers=[
                (k.encode("latin-1"), v.encode("latin-1"))
                for k, v in response.headers.items()
            ],
        )
        body_text = response.body.decode("utf-8")
        assert "Title: Notes export" in body_text
        assert "## Chapter" in body_text
    finally:
        manager.close()
