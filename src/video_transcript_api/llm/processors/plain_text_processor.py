"""无说话人文本处理器"""

from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
import re

from ...utils.logging import setup_logger
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

        # 合并校对结果（分段级检查已完成，无需全局检查）
        calibrated_text = "\n\n".join(calibrated_segments)

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
            """校对单个分段（含长度检查 + 二次校对）"""
            try:
                original_length = len(segment)
                logger.info(f"Calibrating segment {index + 1}/{len(segments)}, length: {original_length}")

                # 第一次校对
                user_prompt = build_calibrate_user_prompt(
                    transcript=segment,
                    video_title=title,
                    description=description,
                    key_info=key_info_text,
                )

                response = self.llm_client.call(
                    model=model,
                    system_prompt=CALIBRATE_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    reasoning_effort=reasoning_effort,
                    task_type="calibrate_segment",
                )

                calibrated_text = response.text
                calibrated_length = len(calibrated_text)

                # 分段级别长度检查
                min_length = int(original_length * self.config.min_calibrate_ratio)

                if calibrated_length >= min_length:
                    # 长度合格
                    calibrated_segments[index] = calibrated_text
                    logger.info(
                        f"Segment {index + 1} calibration passed: "
                        f"{original_length} -> {calibrated_length} (>= {min_length})"
                    )
                else:
                    # 长度不足，二次校对
                    logger.warning(
                        f"Segment {index + 1} too short: {calibrated_length} < {min_length}, "
                        f"retrying with hint..."
                    )

                    retry_hint = (
                        f"上一次校对结果过短（{calibrated_length} 字符），"
                        f"而原文有 {original_length} 字符。"
                        f"请确保保留所有实质性内容，不要大段删减。"
                    )

                    user_prompt_retry = build_calibrate_user_prompt(
                        transcript=segment,
                        video_title=title,
                        description=description,
                        key_info=key_info_text,
                        retry_hint=retry_hint,
                    )

                    response_retry = self.llm_client.call(
                        model=model,
                        system_prompt=CALIBRATE_SYSTEM_PROMPT,
                        user_prompt=user_prompt_retry,
                        reasoning_effort=reasoning_effort,
                        task_type="calibrate_segment_retry",
                    )

                    calibrated_text_retry = response_retry.text
                    calibrated_length_retry = len(calibrated_text_retry)

                    if calibrated_length_retry >= min_length:
                        # 二次校对通过
                        calibrated_segments[index] = calibrated_text_retry
                        logger.info(
                            f"Segment {index + 1} retry passed: "
                            f"{original_length} -> {calibrated_length_retry} (>= {min_length})"
                        )
                    else:
                        # 二次校对仍不通过，降级到原文（格式化处理）
                        formatted_segment = self._format_plain_text(segment)
                        calibrated_segments[index] = formatted_segment
                        logger.warning(
                            f"Segment {index + 1} retry still too short: "
                            f"{calibrated_length_retry} < {min_length}, falling back to formatted original"
                        )

            except Exception as e:
                logger.error(f"Segment {index + 1} calibration failed: {e}")
                # 降级到原文（格式化处理）
                formatted_segment = self._format_plain_text(segment)
                calibrated_segments[index] = formatted_segment

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

    def _format_plain_text(self, text: str) -> str:
        """格式化纯文本，通过标点符号分段提高可读性

        当校对失败降级到原始文本时，如果原文是一长串没有换行符的文本，
        本方法会在句子结束标点后添加换行符，提高可读性。

        Args:
            text: 原始文本

        Returns:
            格式化后的文本
        """
        # 如果文本已经有足够的换行符，直接返回
        # 条件：文本长度 >= 300 字符 且 平均每200字符至少有一个换行
        lines = text.split('\n')
        if len(text) >= 300 and len(lines) > len(text) / 200:
            logger.debug("Text already has sufficient line breaks, skipping formatting")
            return text

        logger.info(f"Formatting plain text with {len(text)} characters and {len(lines)} lines")

        # 定义句子结束标点的正则模式
        # 匹配中文句号、问号、感叹号、分号或英文对应标点
        pattern = r'([。！？；.!?;]+)(\s*)'

        # 在句子结束标点后添加换行符
        formatted_text = re.sub(pattern, r'\1\n', text)

        # 清理多余的空行（超过2个连续换行符的情况）
        formatted_text = re.sub(r'\n{3,}', '\n\n', formatted_text)

        # 清理首尾空白
        formatted_text = formatted_text.strip()

        formatted_lines = formatted_text.split('\n')
        logger.info(f"Formatting completed: {len(formatted_lines)} lines")

        return formatted_text
