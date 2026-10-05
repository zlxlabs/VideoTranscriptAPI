"""关键信息提取器"""

from typing import Dict, List, Optional
from dataclasses import dataclass

from ...utils.logging import setup_logger
from .llm_client import LLMClient
from .cache_manager import CacheManager
from ..prompts.schemas.key_info import (
    KEY_INFO_SCHEMA,
    KEY_INFO_SYSTEM_PROMPT,
    build_key_info_user_prompt,
)

logger = setup_logger(__name__)


@dataclass
class KeyInfo:
    """关键信息数据类"""
    names: List[str]           # 人名
    places: List[str]          # 地名
    technical_terms: List[str] # 技术术语
    brands: List[str]          # 品牌/产品
    abbreviations: List[str]   # 缩写
    foreign_terms: List[str]   # 外文术语
    other_entities: List[str]  # 其他实体

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "names": self.names,
            "places": self.places,
            "technical_terms": self.technical_terms,
            "brands": self.brands,
            "abbreviations": self.abbreviations,
            "foreign_terms": self.foreign_terms,
            "other_entities": self.other_entities,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "KeyInfo":
        """从字典创建"""
        return cls(
            names=data.get("names", []),
            places=data.get("places", []),
            technical_terms=data.get("technical_terms", []),
            brands=data.get("brands", []),
            abbreviations=data.get("abbreviations", []),
            foreign_terms=data.get("foreign_terms", []),
            other_entities=data.get("other_entities", []),
        )

    def format_for_prompt(self) -> str:
        """格式化为 prompt 可用的文本

        输出格式强调这些是"正确拼写"，引导 LLM 主动在转录文本中寻找并纠正 ASR 误识别。
        """
        parts = []

        if self.names:
            parts.append(f"- 人名（正确写法）: {', '.join(self.names)}")
        if self.places:
            parts.append(f"- 地名（正确写法）: {', '.join(self.places)}")
        if self.technical_terms:
            parts.append(f"- 技术术语（正确写法）: {', '.join(self.technical_terms)}")
        if self.brands:
            parts.append(f"- 品牌/产品（正确写法）: {', '.join(self.brands)}")
        if self.abbreviations:
            parts.append(f"- 缩写（正确写法）: {', '.join(self.abbreviations)}")
        if self.foreign_terms:
            parts.append(f"- 外文术语（正确写法）: {', '.join(self.foreign_terms)}")
        if self.other_entities:
            parts.append(f"- 其他实体（正确写法）: {', '.join(self.other_entities)}")

        return "\n".join(parts) if parts else "无特殊关键信息"


class KeyInfoExtractor:
    """关键信息提取器"""

    def __init__(
        self,
        llm_client: LLMClient,
        cache_manager: Optional[CacheManager] = None,
        model: str = "claude-3-5-sonnet",
        reasoning_effort: Optional[str] = None,
    ):
        """初始化关键信息提取器

        Args:
            llm_client: LLM 客户端
            cache_manager: 缓存管理器（可选）
            model: 使用的模型
            reasoning_effort: reasoning effort 参数
        """
        self.llm_client = llm_client
        self.cache_manager = cache_manager
        self.model = model
        self.reasoning_effort = reasoning_effort

    def extract(
        self,
        title: str,
        author: str = "",
        description: str = "",
        platform: str = "",
        media_id: str = "",
    ) -> KeyInfo:
        """提取关键信息

        Args:
            title: 视频标题
            author: 作者/频道
            description: 视频描述
            platform: 平台标识（用于缓存）
            media_id: 媒体 ID（用于缓存）

        Returns:
            KeyInfo 对象
        """
        # 尝试从缓存获取
        if self.cache_manager and platform and media_id:
            cached = self.cache_manager.get_key_info(platform, media_id)
            if cached:
                logger.info(f"Retrieved key_info from cache: {platform}/{media_id}")
                return KeyInfo.from_dict(cached)

        # LLM 提取
        logger.info(f"Extracting key_info using LLM: {title}")

        user_prompt = build_key_info_user_prompt(title, author, description)

        try:
            result = self.llm_client.call(
                model=self.model,
                system_prompt=KEY_INFO_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                response_schema=KEY_INFO_SCHEMA,
                reasoning_effort=self.reasoning_effort,
                task_type="key_info",
            )

            # 解析结果
            key_info = KeyInfo.from_dict(result.structured_output)

            # 缓存结果
            if self.cache_manager and platform and media_id:
                self.cache_manager.save_key_info(
                    platform, media_id, key_info.to_dict()
                )
                logger.info(f"Key_info cached: {platform}/{media_id}")

            logger.info(
                f"Key_info extraction completed: "
                f"{len(key_info.names)} names, "
                f"{len(key_info.technical_terms)} terms, "
                f"{len(key_info.brands)} brands"
            )

            # Debug: 显示详细的提取结果
            logger.debug(f"[KEY_INFO] Extracted details for {title}:")
            logger.debug(f"[KEY_INFO]   Names: {key_info.names}")
            logger.debug(f"[KEY_INFO]   Places: {key_info.places}")
            logger.debug(f"[KEY_INFO]   Technical terms: {key_info.technical_terms}")
            logger.debug(f"[KEY_INFO]   Brands: {key_info.brands}")
            logger.debug(f"[KEY_INFO]   Abbreviations: {key_info.abbreviations}")
            logger.debug(f"[KEY_INFO]   Foreign terms: {key_info.foreign_terms}")
            logger.debug(f"[KEY_INFO]   Other entities: {key_info.other_entities}")

            return key_info

        except Exception as e:
            logger.error(f"Key_info extraction failed: {e}")
            # 返回空的关键信息
            return KeyInfo([], [], [], [], [], [], [])
