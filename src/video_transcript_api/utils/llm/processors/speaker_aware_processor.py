"""有说话人文本处理器"""

from typing import Dict, List, Optional, Any
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures

from ...logging import setup_logger
from ..core.config import LLMConfig
from ..core.llm_client import LLMClient
from ..core.key_info_extractor import KeyInfoExtractor, KeyInfo
from ..core.speaker_inferencer import SpeakerInferencer
from ..core.quality_validator import QualityValidator
from ..segmenters.dialog_segmenter import DialogSegmenter
from ..prompts import (
    STRUCTURED_CALIBRATE_SYSTEM_PROMPT,
    build_structured_calibrate_user_prompt,
)
from ..schemas import CALIBRATION_RESULT_SCHEMA

logger = setup_logger(__name__)


class SpeakerAwareProcessor:
    """有说话人文本处理器"""

    def __init__(
        self,
        config: LLMConfig,
        llm_client: LLMClient,
        key_info_extractor: KeyInfoExtractor,
        speaker_inferencer: SpeakerInferencer,
        quality_validator: QualityValidator,
    ):
        """初始化有说话人文本处理器

        Args:
            config: LLM 配置
            llm_client: LLM 客户端
            key_info_extractor: 关键信息提取器
            speaker_inferencer: 说话人推断器
            quality_validator: 质量验证器
        """
        self.config = config
        self.llm_client = llm_client
        self.key_info_extractor = key_info_extractor
        self.speaker_inferencer = speaker_inferencer
        self.quality_validator = quality_validator
        self.segmenter = DialogSegmenter(config)

    def process(
        self,
        dialogs: List[Dict],
        title: str,
        author: str = "",
        description: str = "",
        platform: str = "",
        media_id: str = "",
        selected_models: Optional[Dict] = None,
    ) -> Dict:
        """处理有说话人文本

        Args:
            dialogs: 对话列表（每项包含 speaker, text, start_time）
            title: 视频标题
            author: 作者
            description: 描述
            platform: 平台标识
            media_id: 媒体 ID
            selected_models: 选定的模型

        Returns:
            处理结果字典
        """
        total_length = sum(len(d.get("text", "")) for d in dialogs)
        logger.info(f"Start processing speaker-aware text: {title}, dialog count: {len(dialogs)}, total length: {total_length}")

        # 步骤1: 提取关键信息
        key_info = self.key_info_extractor.extract(
            title=title,
            author=author,
            description=description,
            platform=platform,
            media_id=media_id,
        )

        # 步骤1.5: 说话人推断
        speakers = list(set(d.get("speaker", "") for d in dialogs if d.get("speaker")))
        speaker_mapping = self.speaker_inferencer.infer(
            speakers=speakers,
            dialogs=dialogs,
            title=title,
            author=author,
            description=description,
            key_info=key_info,
            platform=platform,
            media_id=media_id,
        )

        # 步骤2: 分段
        chunks = self.segmenter.segment(dialogs)
        logger.info(f"Dialogs segmented: {len(chunks)} chunks")

        # 步骤3: 分段校对（每段独立验证）
        calibrated_chunks = self._calibrate_chunks(
            chunks=chunks,
            original_chunks=chunks,  # 传入原始chunk用于验证
            key_info=key_info,
            speaker_mapping=speaker_mapping,
            title=title,
            description=description,
            selected_models=selected_models,
        )

        # 合并校对结果（不再进行整体验证）
        calibrated_dialogs = []
        for chunk in calibrated_chunks:
            calibrated_dialogs.extend(chunk)

        # 构建文本用于统计
        original_text = self._build_text_from_dialogs(dialogs)
        calibrated_text = self._build_text_from_dialogs(calibrated_dialogs)

        logger.info(
            f"Speaker-aware text processing completed: "
            f"original length {len(original_text)}, calibrated length {len(calibrated_text)}"
        )

        return {
            "calibrated_text": calibrated_text,
            "structured_data": {
                "dialogs": calibrated_dialogs,
                "speaker_mapping": speaker_mapping,
            },
            "key_info": key_info.to_dict(),
            "stats": {
                "original_length": len(original_text),
                "calibrated_length": len(calibrated_text),
                "dialog_count": len(dialogs),
                "chunk_count": len(chunks),
            }
        }

    def _calibrate_chunks(
        self,
        chunks: List[List[Dict]],
        original_chunks: List[List[Dict]],
        key_info: KeyInfo,
        speaker_mapping: Dict[str, str],
        title: str,
        description: str,
        selected_models: Optional[Dict],
    ) -> List[List[Dict]]:
        """校对分块对话（并发处理，每块独立验证）

        Args:
            chunks: 分块列表
            original_chunks: 原始分块列表（用于验证失败时降级）
            key_info: 关键信息
            speaker_mapping: 说话人映射
            title: 视频标题
            description: 描述
            selected_models: 选定的模型

        Returns:
            校对后的分块列表（包含成功+降级的混合结果）
        """
        model = selected_models["calibrate_model"] if selected_models else self.config.calibrate_model
        reasoning_effort = selected_models.get("calibrate_reasoning_effort") if selected_models else self.config.calibrate_reasoning_effort

        # 格式化关键信息
        key_info_text = key_info.format_for_prompt()

        calibrated_chunks = [None] * len(chunks)

        def calibrate_single_chunk(index: int, chunk: List[Dict]):
            """校对单个 chunk（含质量验证）"""
            try:
                chunk_length = sum(len(d.get("text", "")) for d in chunk)
                logger.info(f"Calibrating chunk {index + 1}/{len(chunks)}, dialog count: {len(chunk)}, length: {chunk_length}")

                # 构建 prompt（包含对话结构）
                chunk_text = self._format_chunk_for_prompt(chunk, speaker_mapping)

                user_prompt = build_structured_calibrate_user_prompt(
                    dialogs_text=chunk_text,
                    video_title=title,
                    description=description,
                    key_info=key_info_text,
                    dialog_count=len(chunk),
                    min_ratio=self.config.min_calibrate_ratio,
                )

                # 调用 LLM（结构化输出）
                response = self.llm_client.call(
                    model=model,
                    system_prompt=STRUCTURED_CALIBRATE_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    response_schema=CALIBRATION_RESULT_SCHEMA,
                    reasoning_effort=reasoning_effort,
                    task_type="calibrate_chunk",
                )

                # 解析结构化输出
                calibrated_dialogs = response.structured_output.get("calibrated_dialogs", [])

                # 确保数量一致
                if len(calibrated_dialogs) != len(chunk):
                    logger.warning(f"Chunk {index + 1} calibration result count mismatch, falling back to original")
                    calibrated_chunks[index] = chunk
                    return

                # 步骤4: 分段质量验证（可选）
                if self.config.enable_validation:
                    logger.info(f"Validating chunk {index + 1}/{len(chunks)}")

                    validation_result = self.quality_validator.validate_by_score(
                        original=chunk,
                        calibrated=calibrated_dialogs,
                        video_metadata={"title": title, "author": "", "description": description},
                        selected_models=selected_models,
                    )

                    if not validation_result["passed"]:
                        logger.warning(
                            f"Chunk {index + 1} validation failed "
                            f"(score: {validation_result.get('overall_score', 'N/A')}), "
                            f"falling back to original"
                        )
                        calibrated_chunks[index] = chunk
                        return

                    logger.info(f"Chunk {index + 1} validation passed (score: {validation_result.get('overall_score', 'N/A')})")

                calibrated_chunks[index] = calibrated_dialogs
                logger.info(f"Chunk {index + 1} calibration completed")

            except Exception as e:
                logger.error(f"Chunk {index + 1} calibration failed: {e}")
                calibrated_chunks[index] = chunk  # 降级到原文

        # 并发处理
        max_workers = min(len(chunks), self.config.calibration_concurrent_limit)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(calibrate_single_chunk, i, chunk)
                for i, chunk in enumerate(chunks)
            ]

            for future in concurrent.futures.as_completed(futures):
                future.result()  # 等待完成

        return calibrated_chunks

    def _format_chunk_for_prompt(self, chunk: List[Dict], speaker_mapping: Dict[str, str]) -> str:
        """格式化对话块为 prompt 文本

        Args:
            chunk: 对话块
            speaker_mapping: 说话人映射

        Returns:
            格式化的文本
        """
        parts = []
        for dialog in chunk:
            speaker = dialog.get("speaker", "")
            text = dialog.get("text", "")

            # 应用说话人映射
            if speaker_mapping and speaker in speaker_mapping:
                speaker = speaker_mapping[speaker]

            parts.append(f"[{speaker}]: {text}")

        return "\n".join(parts)

    def _build_text_from_dialogs(self, dialogs: List[Dict]) -> str:
        """从对话列表构建纯文本

        Args:
            dialogs: 对话列表

        Returns:
            纯文本字符串
        """
        parts = []
        for dialog in dialogs:
            speaker = dialog.get("speaker", "")
            text = dialog.get("text", "")
            parts.append(f"[{speaker}]: {text}")

        return "\n".join(parts)
