"""无说话人文本处理器"""

from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures

from ...logging import setup_logger
from ..core.config import LLMConfig
from ..core.llm_client import LLMClient
from ..core.key_info_extractor import KeyInfoExtractor, KeyInfo
from ..core.quality_validator import QualityValidator
from ..segmenters.text_segmenter import TextSegmenter
from ..prompts import (
    CALIBRATE_SYSTEM_PROMPT,
    build_calibrate_user_prompt,
)

logger = setup_logger(__name__)


class PlainTextProcessor:
    """无说话人文本处理器"""

    def __init__(
        self,
        config: LLMConfig,
        llm_client: LLMClient,
        key_info_extractor: KeyInfoExtractor,
        quality_validator: QualityValidator,
    ):
        """初始化无说话人文本处理器

        Args:
            config: LLM 配置
            llm_client: LLM 客户端
            key_info_extractor: 关键信息提取器
            quality_validator: 质量验证器
        """
        self.config = config
        self.llm_client = llm_client
        self.key_info_extractor = key_info_extractor
        self.quality_validator = quality_validator
        self.segmenter = TextSegmenter(config)

    def process(
        self,
        text: str,
        title: str,
        author: str = "",
        description: str = "",
        platform: str = "",
        media_id: str = "",
        selected_models: Optional[Dict] = None,
    ) -> Dict:
        """处理无说话人文本

        Args:
            text: 原始文本
            title: 视频标题
            author: 作者
            description: 描述
            platform: 平台标识
            media_id: 媒体 ID
            selected_models: 选定的模型（可选）

        Returns:
            处理结果字典
        """
        logger.info(f"Start processing plain text: {title}, length: {len(text)}")

        # 步骤1: 提取关键信息
        key_info = self.key_info_extractor.extract(
            title=title,
            author=author,
            description=description,
            platform=platform,
            media_id=media_id,
        )

        # 步骤2: 分段
        need_segmentation = len(text) > self.config.enable_threshold

        if need_segmentation:
            segments = self.segmenter.segment(text)
            logger.info(f"Text segmented: {len(segments)} segments")
        else:
            segments = [text]
            logger.info("Text length below threshold, no segmentation")

        # 步骤3: 分段校对
        calibrated_segments = self._calibrate_segments(
            segments=segments,
            key_info=key_info,
            title=title,
            description=description,
            selected_models=selected_models,
        )

        # 合并校对结果
        calibrated_text = "\n\n".join(calibrated_segments)

        # 步骤4: 质量判断（长度检查）
        calibrated_text = self.quality_validator.validate_by_length(
            original=text,
            calibrated=calibrated_text,
            min_ratio=self.config.min_calibrate_ratio,
        )

        logger.info(
            f"Plain text processing completed: "
            f"original length {len(text)}, calibrated length {len(calibrated_text)}"
        )

        return {
            "calibrated_text": calibrated_text,
            "key_info": key_info.to_dict(),
            "stats": {
                "original_length": len(text),
                "calibrated_length": len(calibrated_text),
                "segment_count": len(segments),
            }
        }

    def _calibrate_segments(
        self,
        segments: List[str],
        key_info: KeyInfo,
        title: str,
        description: str,
        selected_models: Optional[Dict],
    ) -> List[str]:
        """校对分段文本（并发处理）

        Args:
            segments: 分段列表
            key_info: 关键信息
            title: 视频标题
            description: 描述
            selected_models: 选定的模型

        Returns:
            校对后的分段列表
        """
        model = selected_models["calibrate_model"] if selected_models else self.config.calibrate_model
        reasoning_effort = selected_models.get("calibrate_reasoning_effort") if selected_models else self.config.calibrate_reasoning_effort

        # 格式化关键信息
        key_info_text = key_info.format_for_prompt()

        calibrated_segments = [None] * len(segments)

        def calibrate_single_segment(index: int, segment: str):
            """校对单个分段"""
            try:
                logger.info(f"Calibrating segment {index + 1}/{len(segments)}, length: {len(segment)}")

                # 构建 prompt
                user_prompt = build_calibrate_user_prompt(
                    transcript=segment,
                    video_title=title,
                    description=description,
                    key_info=key_info_text,
                    min_ratio=self.config.min_calibrate_ratio,
                )

                # 调用 LLM
                response = self.llm_client.call(
                    model=model,
                    system_prompt=CALIBRATE_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    reasoning_effort=reasoning_effort,
                )

                calibrated_segments[index] = response.text
                logger.info(f"Segment {index + 1} calibration completed")

            except Exception as e:
                logger.error(f"Segment {index + 1} calibration failed: {e}")
                calibrated_segments[index] = segment  # 降级到原文

        # 并发处理
        max_workers = min(len(segments), self.config.concurrent_workers)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(calibrate_single_segment, i, seg)
                for i, seg in enumerate(segments)
            ]

            for future in concurrent.futures.as_completed(futures):
                future.result()  # 等待完成

        return calibrated_segments
