"""Task deduplication coordinated through an existing CacheManager."""

from typing import TYPE_CHECKING, Any, Dict, Optional

from ...utils.logging import setup_logger

if TYPE_CHECKING:
    from ...cache.cache_manager import CacheManager


logger = setup_logger("task_dedup")


class TaskDedup:
    """Find existing tasks without owning database connections or state."""

    def __init__(self, cache_manager: "CacheManager"):
        """Use the application's existing CacheManager instance."""
        self._cache_manager = cache_manager

    def get_existing_task_by_url(
        self, url: str, use_speaker_recognition: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Find an existing task with the same URL and speaker setting."""
        try:
            with self._cache_manager._get_cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT task_id, view_token, url, download_url, use_speaker_recognition, status,
                           title, author, platform, media_id, cache_id, created_at
                    FROM task_status
                    WHERE url = ? AND use_speaker_recognition = ?
                    ORDER BY
                        {self._cache_manager._TASK_STATUS_PRIORITY_ORDER_BY}
                    LIMIT 1
                    """,
                    (url, use_speaker_recognition),
                )

                row = cursor.fetchone()
                if row:
                    task_info = {
                        "task_id": row[0],
                        "view_token": row[1],
                        "url": row[2],
                        "download_url": row[3],
                        "use_speaker_recognition": bool(row[4]),
                        "status": row[5],
                        "title": row[6],
                        "author": row[7],
                        "platform": row[8],
                        "media_id": row[9],
                        "cache_id": row[10],
                        "created_at": row[11],
                    }
                    logger.debug(
                        f"找到现有任务: {task_info['task_id']}, "
                        f"状态: {task_info['status']}, URL: {url}"
                    )
                    return task_info

                logger.debug(
                    f"未找到现有任务: URL={url}, "
                    f"use_speaker_recognition={use_speaker_recognition}"
                )
                return None
        except Exception as exc:
            logger.error(f"查找现有任务失败: {exc}")
            return None

    def get_existing_task_by_media(
        self,
        platform: str,
        media_id: str,
        use_speaker_recognition: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Find an existing task with the same platform, media ID and setting."""
        if not platform or not media_id:
            return None

        try:
            with self._cache_manager._get_cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT task_id, view_token, url, download_url, use_speaker_recognition, status,
                           title, author, platform, media_id, cache_id, created_at
                    FROM task_status
                    WHERE platform = ? AND media_id = ? AND use_speaker_recognition = ?
                    ORDER BY
                        {self._cache_manager._TASK_STATUS_PRIORITY_ORDER_BY}
                    LIMIT 1
                    """,
                    (platform, media_id, use_speaker_recognition),
                )

                row = cursor.fetchone()
                if row:
                    task_info = {
                        "task_id": row[0],
                        "view_token": row[1],
                        "url": row[2],
                        "download_url": row[3],
                        "use_speaker_recognition": bool(row[4]),
                        "status": row[5],
                        "title": row[6],
                        "author": row[7],
                        "platform": row[8],
                        "media_id": row[9],
                        "cache_id": row[10],
                        "created_at": row[11],
                    }
                    logger.debug(
                        f"通过平台+媒体ID找到现有任务: {task_info['task_id']}, "
                        f"状态: {task_info['status']}, platform={platform}, media_id={media_id}"
                    )
                    return task_info

                logger.debug(
                    "未通过平台+媒体ID找到现有任务: "
                    f"platform={platform}, media_id={media_id}"
                )
                return None
        except Exception as exc:
            logger.error(f"通过平台+媒体ID查找现有任务失败: {exc}")
            return None
