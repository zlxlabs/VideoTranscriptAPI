"""说话人推断器"""

from typing import Dict, List, Optional

from ...logging import setup_logger
from .llm_client import LLMClient
from .cache_manager import CacheManager
from .key_info_extractor import KeyInfo
from ..prompts import (
    SPEAKER_INFERENCE_SYSTEM_PROMPT,
    build_speaker_inference_user_prompt,
)
from ..prompts.schemas.speaker_mapping import SPEAKER_MAPPING_SCHEMA

logger = setup_logger(__name__)


class SpeakerInferencer:
    """说话人推断器"""

    def __init__(
        self,
        llm_client: LLMClient,
        cache_manager: Optional[CacheManager] = None,
        model: str = "claude-3-5-sonnet",
        reasoning_effort: Optional[str] = None,
        sample_length: int = 1000,
    ):
        """初始化说话人推断器

        Args:
            llm_client: LLM 客户端
            cache_manager: 缓存管理器（可选）
            model: 使用的模型
            reasoning_effort: reasoning effort 参数
            sample_length: 采样对话的字符长度（默认 1000）
        """
        self.llm_client = llm_client
        self.cache_manager = cache_manager
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.sample_length = sample_length

    def infer(
        self,
        speakers: List[str],
        dialogs: List[Dict[str, str]],
        title: str,
        author: str = "",
        description: str = "",
        key_info: Optional[KeyInfo] = None,
        platform: str = "",
        media_id: str = "",
    ) -> Dict[str, str]:
        """推断说话人真实姓名

        Args:
            speakers: 说话人 ID 列表（如 ["Speaker1", "Speaker2"]）
            dialogs: 对话列表（每项包含 speaker, text, start_time）
            title: 视频标题
            author: 作者/频道
            description: 视频描述
            key_info: 关键信息（可选，用于辅助推断）
            platform: 平台标识（用于缓存）
            media_id: 媒体 ID（用于缓存）

        Returns:
            说话人映射字典 {"Speaker1": "张三", "Speaker2": "李四"}
        """
        if not speakers:
            logger.warning("Speaker list is empty, skipping inference")
            return {}

        # 尝试从缓存获取
        if self.cache_manager and platform and media_id:
            cached = self.cache_manager.get_speaker_mapping(platform, media_id)
            if cached:
                logger.info(f"Retrieved speaker_mapping from cache: {platform}/{media_id}")
                return cached

        # 提取前 N 字符的对话样本
        sample_dialogs = self._extract_sample_dialogs(dialogs, speakers)

        if not sample_dialogs:
            logger.warning("No valid dialog samples, cannot infer speakers")
            return {speaker: speaker for speaker in speakers}

        # LLM 推断
        logger.info(f"Inferring speakers using LLM: {speakers}")

        # 构建对话样本文本
        context_snippets = self._format_sample_dialogs(sample_dialogs)

        user_prompt = build_speaker_inference_user_prompt(
            context_snippets=context_snippets,
            original_speakers=speakers,
            video_title=title,
            author=author,
            description=description
        )

        try:
            result = self.llm_client.call(
                model=self.model,
                system_prompt=SPEAKER_INFERENCE_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                response_schema=SPEAKER_MAPPING_SCHEMA,
                reasoning_effort=self.reasoning_effort,
            )

            # 解析结果
            speaker_mapping = result.structured_output.get("speaker_mapping", {})

            # 验证：确保所有 speaker 都有映射
            for speaker in speakers:
                if speaker not in speaker_mapping:
                    speaker_mapping[speaker] = speaker

            # 缓存结果
            if self.cache_manager and platform and media_id:
                self.cache_manager.save_speaker_mapping(
                    platform, media_id, speaker_mapping
                )
                logger.info(f"Speaker_mapping cached: {platform}/{media_id}")

            logger.info(f"Speaker inference completed: {speaker_mapping}")

            return speaker_mapping

        except Exception as e:
            logger.error(f"Speaker inference failed: {e}")
            # 返回原始映射
            return {speaker: speaker for speaker in speakers}

    def _extract_sample_dialogs(
        self, dialogs: List[Dict[str, str]], speakers: List[str]
    ) -> Dict[str, List[str]]:
        """提取前 N 字符的对话样本（按说话人分组）

        Args:
            dialogs: 完整对话列表
            speakers: 说话人列表

        Returns:
            {speaker: [text1, text2, ...]}
        """
        sample_by_speaker = {speaker: [] for speaker in speakers}
        total_chars = 0

        for dialog in dialogs:
            if total_chars >= self.sample_length:
                break

            speaker = dialog.get("speaker", "")
            text = dialog.get("text", "").strip()

            if speaker in sample_by_speaker and text:
                sample_by_speaker[speaker].append(text)
                total_chars += len(text)

        # 过滤空列表
        sample_by_speaker = {
            speaker: texts
            for speaker, texts in sample_by_speaker.items()
            if texts
        }

        return sample_by_speaker

    def _format_sample_dialogs(self, sample_by_speaker: Dict[str, List[str]]) -> str:
        """格式化对话样本为字符串

        Args:
            sample_by_speaker: {speaker: [text1, text2, ...]}

        Returns:
            格式化的对话文本
        """
        parts = []

        for speaker, texts in sample_by_speaker.items():
            sample_text = " ".join(texts[:5])  # 最多 5 条
            parts.append(f"\n[{speaker}]:")
            parts.append(f"{sample_text}")

        return "\n".join(parts)
