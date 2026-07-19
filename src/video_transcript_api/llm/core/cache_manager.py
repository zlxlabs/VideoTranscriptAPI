"""缓存管理器（关键信息和说话人映射）"""

import json
import datetime
import os
import uuid
from pathlib import Path
from typing import Dict, Optional

from ...utils.logging import setup_logger

logger = setup_logger(__name__)


class CacheManager:
    """缓存管理器（关键信息和说话人映射）"""

    def __init__(self, cache_dir: str):
        """初始化缓存管理器

        Args:
            cache_dir: 缓存目录路径（与现有系统一致）
        """
        self.cache_dir = Path(cache_dir)

    def _get_video_cache_dir(self, platform: str, media_id: str) -> Path:
        """获取视频缓存目录（复用现有逻辑）

        目录结构: cache_dir/platform/YYYY/YYYYMM/media_id

        Args:
            platform: 平台名称（如 youtube, bilibili）
            media_id: 媒体 ID

        Returns:
            视频缓存目录路径
        """
        date = datetime.datetime.now()
        year = date.strftime("%Y")
        year_month = date.strftime("%Y%m")

        # 构建路径：cache_dir/platform/YYYY/YYYYMM/media_id
        return self.cache_dir / platform / year / year_month / media_id

    # 关键信息缓存

    def get_key_info(self, platform: str, media_id: str) -> Optional[Dict]:
        """获取关键信息缓存

        Args:
            platform: 平台名称
            media_id: 媒体 ID

        Returns:
            关键信息字典，如果不存在则返回 None
        """
        cache_dir = self._get_video_cache_dir(platform, media_id)
        key_info_file = cache_dir / "key_info.json"

        if key_info_file.exists():
            try:
                with open(key_info_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load key_info cache {key_info_file}: {e}")
                return None
        return None

    def save_key_info(self, platform: str, media_id: str, key_info: Dict):
        """保存关键信息缓存

        Args:
            platform: 平台名称
            media_id: 媒体 ID
            key_info: 关键信息字典
        """
        cache_dir = self._get_video_cache_dir(platform, media_id)
        cache_dir.mkdir(parents=True, exist_ok=True)

        key_info_file = cache_dir / "key_info.json"
        try:
            with open(key_info_file, "w", encoding="utf-8") as f:
                json.dump(key_info, f, ensure_ascii=False, indent=2)
            logger.debug(f"Key info cache saved: {key_info_file}")
        except Exception as e:
            logger.error(f"Failed to save key_info cache {key_info_file}: {e}")

    # 说话人映射缓存

    def get_speaker_mapping(
        self,
        platform: str,
        media_id: str,
        *,
        input_fingerprint: Optional[str] = None,
        speakers: Optional[list[str]] = None,
    ) -> Optional[Dict]:
        """获取说话人映射缓存

        Args:
            platform: 平台名称
            media_id: 媒体 ID

        Returns:
            说话人映射字典，如果不存在则返回 None
        """
        cache_dir = self._get_video_cache_dir(platform, media_id)
        mapping_file = cache_dir / "speaker_mapping.json"

        if mapping_file.exists():
            try:
                with open(mapping_file, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                # Old callers can still read legacy payloads. New speaker
                # inference always supplies validation inputs and only accepts
                # the versioned artifact envelope.
                if input_fingerprint is None or speakers is None:
                    if payload.get("schema_version") == 1:
                        result = payload.get("result")
                        return result if isinstance(result, dict) else None
                    return payload
                if (
                    payload.get("schema_version") != 1
                    or payload.get("input_fingerprint") != input_fingerprint
                    or set(payload.get("speakers") or []) != set(speakers)
                    or payload.get("source") != "llm"
                ):
                    return None
                return payload.get("result")
            except Exception as e:
                logger.warning(f"Failed to load speaker_mapping cache {mapping_file}: {e}")
                return None
        return None

    def save_speaker_mapping(
        self,
        platform: str,
        media_id: str,
        speaker_mapping: Dict,
        *,
        input_fingerprint: Optional[str] = None,
        speakers: Optional[list[str]] = None,
        source: str = "llm",
    ):
        """保存说话人映射缓存

        Args:
            platform: 平台名称
            media_id: 媒体 ID
            speaker_mapping: 说话人映射字典
        """
        cache_dir = self._get_video_cache_dir(platform, media_id)
        cache_dir.mkdir(parents=True, exist_ok=True)

        mapping_file = cache_dir / "speaker_mapping.json"
        try:
            payload = speaker_mapping
            if input_fingerprint is not None and speakers is not None:
                payload = {
                    "schema_version": 1,
                    "input_fingerprint": input_fingerprint,
                    "speakers": list(speakers),
                    "source": source,
                    "result": speaker_mapping,
                }
            temp_file = mapping_file.with_name(
                f".{mapping_file.name}.{uuid.uuid4().hex}.tmp"
            )
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(temp_file, mapping_file)
            logger.debug(f"Speaker mapping cache saved: {mapping_file}")
        except Exception as e:
            logger.error(f"Failed to save speaker_mapping cache {mapping_file}: {e}")
            raise
        finally:
            if "temp_file" in locals():
                temp_file.unlink(missing_ok=True)
