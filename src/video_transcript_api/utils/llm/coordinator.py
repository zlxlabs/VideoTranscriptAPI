"""协调器模块 - 场景路由和统一入口"""

from typing import Dict, List, Optional, Any, Union

from ..logging import setup_logger
from .core.config import LLMConfig
from .core.llm_client import LLMClient
from .core.cache_manager import CacheManager
from .core.key_info_extractor import KeyInfoExtractor
from .core.speaker_inferencer import SpeakerInferencer
from .core.quality_validator import QualityValidator
from .processors.plain_text_processor import PlainTextProcessor
from .processors.speaker_aware_processor import SpeakerAwareProcessor

logger = setup_logger(__name__)


class LLMCoordinator:
    """LLM 处理协调器

    负责场景路由，统一入口接口，集成两个处理器
    """

    def __init__(self, config_dict: dict, cache_dir: str):
        """初始化协调器

        Args:
            config_dict: 完整的配置字典
            cache_dir: 缓存目录路径
        """
        # 创建配置对象
        self.config = LLMConfig.from_dict(config_dict)

        # 创建核心组件
        self.llm_client = LLMClient(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            max_retries=self.config.max_retries,
            retry_delay=self.config.retry_delay,
        )

        self.cache_manager = CacheManager(cache_dir=cache_dir)

        self.key_info_extractor = KeyInfoExtractor(
            llm_client=self.llm_client,
            cache_manager=self.cache_manager,
            model=self.config.key_info_model or self.config.calibrate_model,
            reasoning_effort=self.config.key_info_reasoning_effort,
        )

        self.speaker_inferencer = SpeakerInferencer(
            llm_client=self.llm_client,
            cache_manager=self.cache_manager,
            model=self.config.speaker_model or self.config.calibrate_model,
            reasoning_effort=self.config.speaker_reasoning_effort,
        )

        self.quality_validator = QualityValidator(
            llm_client=self.llm_client,
            model=self.config.validator_model or self.config.calibrate_model,
            reasoning_effort=self.config.validator_reasoning_effort,
            overall_score_threshold=self.config.overall_score_threshold,
            minimum_single_score=self.config.minimum_single_score,
        )

        # 创建处理器
        self.plain_text_processor = PlainTextProcessor(
            config=self.config,
            llm_client=self.llm_client,
            key_info_extractor=self.key_info_extractor,
            quality_validator=self.quality_validator,
        )

        self.speaker_aware_processor = SpeakerAwareProcessor(
            config=self.config,
            llm_client=self.llm_client,
            key_info_extractor=self.key_info_extractor,
            speaker_inferencer=self.speaker_inferencer,
            quality_validator=self.quality_validator,
        )

        logger.info("LLM Coordinator initialized successfully")

    def process(
        self,
        content: Union[str, List[Dict]],
        title: str,
        author: str = "",
        description: str = "",
        platform: str = "",
        media_id: str = "",
        has_risk: bool = False,
    ) -> Dict:
        """处理文本（统一入口）

        Args:
            content: 文本内容（纯文本或对话列表）
            title: 视频标题
            author: 作者
            description: 描述
            platform: 平台标识
            media_id: 媒体 ID
            has_risk: 是否有风险内容

        Returns:
            处理结果字典
        """
        # 选择模型
        selected_models = self.config.select_models_for_task(has_risk)

        # 判断内容类型
        if isinstance(content, str):
            # 纯文本 - 使用 PlainTextProcessor
            logger.info("Routing to PlainTextProcessor")
            return self.plain_text_processor.process(
                text=content,
                title=title,
                author=author,
                description=description,
                platform=platform,
                media_id=media_id,
                selected_models=selected_models,
            )
        elif isinstance(content, list):
            # 对话列表 - 使用 SpeakerAwareProcessor
            logger.info("Routing to SpeakerAwareProcessor")
            return self.speaker_aware_processor.process(
                dialogs=content,
                title=title,
                author=author,
                description=description,
                platform=platform,
                media_id=media_id,
                selected_models=selected_models,
            )
        else:
            raise ValueError(f"Unsupported content type: {type(content)}")
