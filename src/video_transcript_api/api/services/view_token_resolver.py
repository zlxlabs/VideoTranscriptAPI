"""View-token business logic coordinated through an existing CacheManager."""

from typing import TYPE_CHECKING, Any, Dict, Optional

from ...utils.llm_status import SummaryStatus
from ...utils.logging import setup_logger

if TYPE_CHECKING:
    from ...cache.cache_manager import CacheManager


logger = setup_logger("view_token_resolver")


class ViewTokenResolver:
    """Resolve view-token data without owning cache connections or state."""

    def __init__(self, cache_manager: "CacheManager"):
        """Use the application's existing CacheManager instance."""
        self._cache_manager = cache_manager

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

    def get_view_data_by_token(self, view_token: str) -> Optional[Dict[str, Any]]:
        """Return the view-page data associated with a view token."""
        try:
            task_info = self._cache_manager.get_task_by_view_token(view_token)
            if not task_info:
                return None

            display_url = task_info.get("url") or task_info.get("download_url") or ""

            if task_info["status"] in ["queued", "processing", "calibrating"]:
                return {
                    "status": "processing",
                    "title": task_info.get("title", "转录处理中..."),
                    "url": display_url,
                    "created_at": task_info["created_at"],
                }

            if task_info["status"] == "failed":
                return {
                    "status": "failed",
                    "title": task_info.get("title", "转录失败"),
                    "url": display_url,
                    "message": task_info.get("error_message")
                    or "转录任务失败，请重新提交",
                }

            if task_info["platform"] and task_info["media_id"]:
                cache_data = self._cache_manager.get_cache(
                    platform=task_info["platform"],
                    media_id=task_info["media_id"],
                    use_speaker_recognition=task_info["use_speaker_recognition"],
                )

                if cache_data:
                    summary_state, summary = self._resolve_summary_state(
                        task_info, cache_data
                    )
                    transcript = cache_data.get("llm_calibrated") or cache_data.get(
                        "transcript_data", "转录文本获取中..."
                    )
                    if not isinstance(transcript, str):
                        transcript = (
                            str(transcript)
                            if transcript is not None
                            else "转录文本获取中..."
                        )

                    llm_config = self._cache_manager.get_task_llm_config(
                        task_info["task_id"]
                    )
                    if not llm_config:
                        llm_config = self._get_llm_config_by_view_token(
                            task_info["view_token"]
                        )

                    return {
                        "status": "success",
                        "title": cache_data.get("title", ""),
                        "author": cache_data.get("author", ""),
                        "description": cache_data.get("description", ""),
                        "url": display_url,
                        "summary": summary,
                        "summary_state": summary_state,
                        "transcript": transcript,
                        "use_speaker_recognition": cache_data.get(
                            "use_speaker_recognition", False
                        ),
                        "created_at": task_info["created_at"],
                        "cache_dir": cache_data.get("file_path"),
                        "llm_config": llm_config,
                        "platform": cache_data.get("platform", ""),
                    }

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
        try:
            task_info = self._cache_manager.get_task_by_view_token(view_token)
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

            cache_data = self._cache_manager.get_cache(
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
