"""View-token business logic coordinated through an existing CacheManager."""

import datetime
import json
import os
from typing import TYPE_CHECKING, Any, Dict, Optional

from ...i18n import t
from ...transcriber.segments import normalize_segments
from ...utils.llm_status import NotesStatus, SummaryStatus
from ...utils.logging import setup_logger

if TYPE_CHECKING:
    from ...cache.cache_manager import CacheManager


logger = setup_logger("view_token_resolver")
UPLOAD_DISABLED = "UPLOAD_DISABLED"

#: 瞬时阶段：经同一入口计时发布，但页面上不单独成步，归入"准备"。
#: 其余发布名按 web.processing.stage.<name> 查词表，无词表条目的新阶段
#: 直接以内部名上页。
_TRANSIENT_STAGES = ("url_parse", "cache_check", "metadata")


def _stage_display_name(stage: str) -> str:
    """Map a published stage name to its display text.

    Stages without a catalog entry (new stages added later via the same
    entry) surface under their internal name so they still appear on the
    page without any presentation-layer change.
    """
    if stage in _TRANSIENT_STAGES:
        return t("web.processing.stage.preparing")
    try:
        return t(f"web.processing.stage.{stage}")
    except KeyError:
        return stage


def _format_elapsed_text(elapsed_ms: Any) -> str:
    """Render a duration in milliseconds with the configured language."""
    if not isinstance(elapsed_ms, (int, float)) or isinstance(elapsed_ms, bool):
        return ""
    total_seconds = max(0, int(round(elapsed_ms / 1000)))
    if total_seconds < 60:
        return t("web.processing.duration.seconds", seconds=total_seconds)
    if total_seconds < 3600:
        minutes, seconds = divmod(total_seconds, 60)
        return t(
            "web.processing.duration.minutes", minutes=minutes, seconds=seconds
        )
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    return t("web.processing.duration.hours", hours=hours, minutes=minutes)


def _running_stage_elapsed_text(progress: Dict[str, Any]) -> str:
    """Elapsed time of the running stage, computed at render time."""
    started_at = progress.get("started_at")
    if not isinstance(started_at, str) or not started_at:
        return ""
    try:
        started = datetime.datetime.fromisoformat(started_at)
    except ValueError:
        return ""
    now = datetime.datetime.now(datetime.timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=datetime.timezone.utc)
    elapsed_ms = (now - started).total_seconds() * 1000
    return _format_elapsed_text(elapsed_ms)


def _parse_progress_document(raw: Any) -> Optional[Dict[str, Any]]:
    """Parse the raw ``task_status.progress`` TEXT into a dict.

    Mirrors CacheManager.get_task_by_id's tolerance: malformed or non-object
    payloads degrade to None instead of breaking the view.
    """
    if raw is None or isinstance(raw, dict):
        return raw if isinstance(raw, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        logger.warning("Invalid progress JSON on task row for view rendering")
        return None
    return parsed if isinstance(parsed, dict) else None


def build_stage_progress_display(
    progress: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Build the display-ready payload rendered by ``processing.html``.

    All strings are pre-rendered through the i18n catalog so the template
    and the browser fixture only deal with final text.
    """
    if not isinstance(progress, dict):
        return None
    stage = progress.get("stage")
    if not isinstance(stage, str) or not stage:
        return None

    label = _stage_display_name(stage)
    elapsed_text = (
        _running_stage_elapsed_text(progress)
        if progress.get("stage_state") == "running"
        else ""
    )
    if elapsed_text:
        current_line = t(
            "web.processing.progress.current", stage=label, elapsed=elapsed_text
        )
    else:
        current_line = t("web.processing.progress.current_plain", stage=label)

    completed_items = []
    stages = progress.get("stages")
    if isinstance(stages, list):
        for record in stages:
            if not isinstance(record, dict):
                continue
            record_stage = record.get("stage")
            if not isinstance(record_stage, str) or not record_stage:
                continue
            completed_items.append(
                t(
                    "web.processing.progress.completed_item",
                    stage=_stage_display_name(record_stage),
                    elapsed=_format_elapsed_text(record.get("elapsed_ms")),
                )
            )

    notes_line = None
    done = progress.get("done")
    total = progress.get("total")
    if (
        isinstance(done, int)
        and not isinstance(done, bool)
        and isinstance(total, int)
        and not isinstance(total, bool)
    ):
        notes_line = t(
            "web.processing.progress.notes_detail", done=done, total=total
        )

    return {
        "current_line": current_line,
        "notes_line": notes_line,
        "completed_header": t("web.processing.progress.completed_header"),
        "completed_items": completed_items,
    }


class ViewTokenResolver:
    """Resolve view-token data without owning cache connections or state."""

    def __init__(self, cache_manager: "CacheManager"):
        """Use the application's existing CacheManager instance."""
        self._cache_manager = cache_manager

    def _resolve_task_cache(
        self, platform: str, media_id: str, use_speaker_recognition: Any
    ) -> Optional[Dict[str, Any]]:
        """Resolve a task's own cache variant before legacy read-side reuse."""
        variant = bool(use_speaker_recognition)
        cache_data = self._cache_manager.get_cache(
            platform=platform,
            media_id=media_id,
            use_speaker_recognition=variant,
            exact_variant=True,
        )
        if cache_data is None and not variant:
            return self._cache_manager.get_cache(
                platform=platform,
                media_id=media_id,
                use_speaker_recognition=False,
            )
        return cache_data

    def _resolve_summary_state(
        self, task_info: Dict[str, Any], cache_data: Dict[str, Any]
    ) -> tuple:
        """Resolve the honest summary display state and its display text.

        The task-status column is authoritative, followed by llm_status.json.
        Legacy tasks without either source are conservatively inferred from the
        presence of a real summary file.
        """
        summary_status = task_info.get("summary_status")
        if not summary_status:
            llm_status = cache_data.get("llm_status") or {}
            summary_status = llm_status.get("summary_status")

        raw_summary = cache_data.get("llm_summary")
        if raw_summary is not None and not isinstance(raw_summary, str):
            raw_summary = str(raw_summary)
        has_summary_text = bool(raw_summary)

        if summary_status:
            if summary_status == SummaryStatus.GENERATED:
                return SummaryStatus.GENERATED, (
                    raw_summary if has_summary_text else None
                )
            return summary_status, None

        if has_summary_text:
            return SummaryStatus.GENERATED, raw_summary
        return SummaryStatus.SKIPPED_SHORT, None

    def _resolve_notes_state(
        self, cache_data: Dict[str, Any]
    ) -> tuple[Optional[str], Optional[str]]:
        """Resolve detailed-notes status and expose text only when generated."""
        llm_status = cache_data.get("llm_status") or {}
        notes_status = llm_status.get("notes_status")
        raw_notes = cache_data.get("llm_notes")
        if raw_notes is not None and not isinstance(raw_notes, str):
            raw_notes = str(raw_notes)
        has_notes_text = bool(raw_notes and raw_notes.strip())

        if notes_status == NotesStatus.GENERATED:
            return NotesStatus.GENERATED, raw_notes if has_notes_text else None
        if notes_status:
            return notes_status, None
        return None, None

    @staticmethod
    def _first_usable_text(*candidates: Any) -> Optional[str]:
        """Return the first string that is non-empty after strip."""
        for value in candidates:
            if isinstance(value, str) and value.strip():
                return value
        return None

    @staticmethod
    def _cache_has_usable_payload(cache_data: Dict[str, Any]) -> bool:
        """True when cache holds non-empty transcript text or FunASR segments.

        File existence is not enough: a directory may contain only
        key_info.json. Strings count when non-empty after strip; list/dict
        payloads count when normalize_segments finds at least one non-empty
        text.
        """
        if ViewTokenResolver._first_usable_text(
            cache_data.get("llm_calibrated"),
            cache_data.get("transcript_data"),
        ):
            return True
        transcript_data = cache_data.get("transcript_data")
        if isinstance(transcript_data, (list, dict)):
            return bool(normalize_segments(transcript_data))
        return False

    def _assemble_content_view(
        self,
        task_info: Dict[str, Any],
        cache_data: Dict[str, Any],
        display_url: str,
        *,
        status: str,
    ) -> Dict[str, Any]:
        """Assemble the transcript-page payload shared by success and interrupted.

        Interrupted is a presentation status: the task row stays failed, but
        usable cache text is shown instead of the failure page. Both statuses
        read the transcript body through the same normalize_segments path, so a
        structured FunASR payload never leaks its repr to the page.
        """
        summary_state, summary = self._resolve_summary_state(task_info, cache_data)
        notes_state, notes = self._resolve_notes_state(cache_data)
        transcript = self._first_usable_text(
            cache_data.get("llm_calibrated"),
            cache_data.get("transcript_data"),
        )
        if transcript is None:
            segments = normalize_segments(cache_data.get("transcript_data"))
            transcript = (
                "\n".join(seg["text"] for seg in segments)
                if segments
                else "转录文本获取中..."
            )

        llm_config = self._cache_manager.get_task_llm_config(task_info["task_id"])
        if not llm_config and task_info.get("platform") != "local_upload":
            llm_config = self._get_llm_config_by_view_token(task_info["view_token"])

        payload: Dict[str, Any] = {
            "status": status,
            "title": cache_data.get("title", ""),
            "author": cache_data.get("author", ""),
            "description": cache_data.get("description", ""),
            "url": display_url,
            "summary": summary,
            "summary_state": summary_state,
            "notes": notes,
            "notes_state": notes_state,
            "transcript": transcript,
            "use_speaker_recognition": cache_data.get(
                "use_speaker_recognition", False
            ),
            "created_at": task_info["created_at"],
            "cache_dir": cache_data.get("file_path"),
            "llm_config": llm_config,
            "platform": cache_data.get("platform", ""),
        }
        if status == "interrupted":
            payload["interrupted_reason"] = task_info.get("error_message")
            payload["interrupted_at"] = task_info.get("completed_at")
            logger.info(
                "view token resolved as interrupted: "
                f"task_id={task_info.get('task_id')} "
                f"reason={task_info.get('error_message')}"
            )
        return payload

    def _local_upload_task_info(self, view_token: str) -> Optional[Dict[str, Any]]:
        """Resolve only an independently recorded upload capability, never a legacy blank token."""
        upload = self._cache_manager.get_local_upload_by_view_token(view_token)
        if upload is None:
            return None
        if os.environ.get("VTA_UPLOADS_ENABLED", "").strip().lower() != "true":
            logger.warning("{}: local upload content read denied while disabled", UPLOAD_DISABLED)
            return None
        if not self._cache_manager.local_upload_share_is_active(upload):
            return None
        return {
            "task_id": upload["root_task_id"],
            "view_token": view_token,
            "url": upload.get("root_url") or "",
            "download_url": upload.get("root_download_url"),
            "platform": upload.get("root_platform"),
            "media_id": upload.get("root_media_id"),
            "use_speaker_recognition": bool(upload.get("root_speaker")),
            "status": upload.get("root_status"),
            "title": upload.get("root_title") or "本地上传",
            "created_at": upload.get("root_created_at"),
            "completed_at": upload.get("root_completed_at"),
            "error_message": upload.get("root_error"),
            "llm_config": upload.get("root_llm_config"),
        }

    def get_view_data_by_token(self, view_token: str) -> Optional[Dict[str, Any]]:
        """Return the view-page data associated with a view token."""
        if not view_token:
            return None
        try:
            task_info = (
                self._local_upload_task_info(view_token)
                if view_token.startswith("upload_")
                else self._cache_manager.get_task_by_view_token(view_token)
            )
            if not task_info:
                return None

            display_url = task_info.get("url") or task_info.get("download_url") or ""

            if task_info["status"] in ["queued", "processing", "calibrating"]:
                # 终态任务不展示残留 progress（阶段是从属元数据）；非终态
                # 时把统一阶段入口发布的进度转成展示结构带给 processing.html。
                progress_document = _parse_progress_document(
                    task_info.get("progress")
                )
                return {
                    "status": "processing",
                    "title": task_info.get("title", "转录处理中..."),
                    "url": display_url,
                    "created_at": task_info["created_at"],
                    "stage_progress": build_stage_progress_display(
                        progress_document
                    ),
                }

            if task_info["status"] == "failed":
                platform = task_info.get("platform")
                media_id = task_info.get("media_id")
                if platform and media_id:
                    cache_data = self._cache_manager.get_cache(
                        platform=platform,
                        media_id=media_id,
                        use_speaker_recognition=task_info["use_speaker_recognition"],
                    )
                    if cache_data and self._cache_has_usable_payload(cache_data):
                        return self._assemble_content_view(
                            task_info,
                            cache_data,
                            display_url,
                            status="interrupted",
                        )
                return {
                    "status": "failed",
                    "title": task_info.get("title", "转录失败"),
                    "url": display_url,
                    "message": task_info.get("error_message")
                    or "转录任务失败，请重新提交",
                }

            if task_info["platform"] and task_info["media_id"]:
                cache_data = self._resolve_task_cache(
                    platform=task_info["platform"],
                    media_id=task_info["media_id"],
                    use_speaker_recognition=task_info["use_speaker_recognition"],
                )

                if cache_data:
                    return self._assemble_content_view(
                        task_info, cache_data, display_url, status="success"
                    )

                return {
                    "status": "file_cleaned",
                    "title": task_info.get("title", "视频转录"),
                    "url": display_url,
                    "created_at": task_info["created_at"],
                }

            return {
                "status": "incomplete",
                "title": task_info.get("title", "任务信息不完整"),
                "url": display_url,
                "created_at": task_info["created_at"],
            }
        except Exception as exc:
            logger.error(f"获取查看页面数据失败: {exc}")
            return None

    def get_cache_by_view_token(self, view_token: str) -> Optional[Dict[str, Any]]:
        """Return complete cache data plus task information for a view token."""
        if not view_token:
            return None
        try:
            task_info = (
                self._local_upload_task_info(view_token)
                if view_token.startswith("upload_")
                else self._cache_manager.get_task_by_view_token(view_token)
            )
            if not task_info:
                logger.warning(f"未找到 view_token 对应的任务: {view_token}")
                return None

            platform = task_info.get("platform")
            media_id = task_info.get("media_id")
            use_speaker_recognition = task_info.get("use_speaker_recognition", False)
            if not platform or not media_id:
                logger.warning(
                    "任务信息不完整，缺少 platform 或 media_id: "
                    f"view_token={view_token}, platform={platform}, media_id={media_id}"
                )
                return None

            cache_data = self._resolve_task_cache(
                platform=platform,
                media_id=media_id,
                use_speaker_recognition=use_speaker_recognition,
            )
            if cache_data:
                cache_data["task_info"] = task_info
                logger.info(
                    f"通过 view_token 获取缓存成功: platform={platform}, media_id={media_id}"
                )
            return cache_data
        except Exception as exc:
            logger.error(f"通过 view_token 获取缓存失败: {exc}")
            return None

    def _get_llm_config_by_view_token(
        self, view_token: str
    ) -> Optional[Dict[str, Any]]:
        """Find the newest non-empty LLM config for tasks sharing a token."""
        if not view_token or view_token.startswith("upload_"):
            return None
        try:
            with self._cache_manager._get_cursor() as cursor:
                cursor.execute(
                    """SELECT llm_config FROM task_status
                       WHERE view_token = ?
                         AND llm_config IS NOT NULL
                         AND llm_config != ''
                       ORDER BY created_at DESC
                       LIMIT 1""",
                    (view_token,),
                )
                row = cursor.fetchone()
                if row and row["llm_config"]:
                    import json

                    return json.loads(row["llm_config"])
                return None
        except Exception as exc:
            logger.error(f"回退查找LLM配置失败: {exc}")
            return None
