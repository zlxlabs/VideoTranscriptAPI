"""有说话人文本处理器"""

from typing import Dict, List, Optional, Any
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures

from ...utils.logging import setup_logger
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
        base_dialogs = self._coerce_dialogs(dialogs)
        total_length = sum(len(d.get("text", "")) for d in base_dialogs)
        logger.info(
            f"Start processing speaker-aware text: {title}, dialog count: {len(base_dialogs)}, total length: {total_length}"
        )

        # 步骤1: 提取关键信息
        key_info = self.key_info_extractor.extract(
            title=title,
            author=author,
            description=description,
            platform=platform,
            media_id=media_id,
        )

        # 步骤1.5: 说话人推断
        speakers = list(
            dict.fromkeys(
                d.get("speaker", "") for d in base_dialogs if d.get("speaker")
            )
        )
        speaker_mapping = self.speaker_inferencer.infer(
            speakers=speakers,
            dialogs=base_dialogs,
            title=title,
            author=author,
            description=description,
            key_info=key_info,
            platform=platform,
            media_id=media_id,
        )

        # 结构化标准化（应用映射 + 合并连续同说话人 + 时间字段规范化）
        normalized_dialogs = self._normalize_and_merge_dialogs(
            base_dialogs, speaker_mapping
        )

        # 步骤2: 分段
        chunks = self.segmenter.segment(normalized_dialogs)
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
        original_text = self._build_text_from_dialogs(normalized_dialogs)
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
                "dialog_count": len(normalized_dialogs),
                "chunk_count": len(chunks),
            }
        }

    def _coerce_dialogs(self, dialogs: List[Dict]) -> List[Dict]:
        """将原始对话列表规范化为最小可用格式（speaker/text/start/end/duration）"""
        coerced = []
        for dialog in dialogs or []:
            if not isinstance(dialog, dict):
                continue

            speaker = dialog.get("speaker")
            if not speaker:
                speaker = dialog.get("spk") or dialog.get("speaker_id")

            text = dialog.get("text")
            if text is None:
                text = dialog.get("content")
            if text is None:
                text = dialog.get("transcript")

            if not text:
                continue

            coerced.append(
                {
                    "speaker": str(speaker) if speaker is not None else "unknown",
                    "text": str(text),
                    "start_time": dialog.get("start_time", dialog.get("start")),
                    "end_time": dialog.get("end_time", dialog.get("end")),
                    "duration": dialog.get("duration"),
                }
            )

        return coerced

    def _normalize_and_merge_dialogs(
        self, dialogs: List[Dict], speaker_mapping: Dict[str, str]
    ) -> List[Dict]:
        """应用说话人映射、规范化时间字段并合并连续同说话人对话"""
        normalized = []
        current = None
        current_start_seconds = None
        current_end_seconds = None

        for dialog in dialogs:
            normalized_dialog, start_seconds, end_seconds = self._normalize_dialog(
                dialog, speaker_mapping
            )
            if not normalized_dialog:
                continue

            if current and current.get("speaker") == normalized_dialog.get("speaker"):
                current["text"] = f"{current.get('text', '')} {normalized_dialog.get('text', '')}".strip()
                if normalized_dialog.get("end_time"):
                    current["end_time"] = normalized_dialog["end_time"]
                if end_seconds is not None:
                    current_end_seconds = end_seconds

                if current_start_seconds is not None and current_end_seconds is not None:
                    current["duration"] = current_end_seconds - current_start_seconds
                else:
                    current["duration"] = (
                        float(current.get("duration") or 0)
                        + float(normalized_dialog.get("duration") or 0)
                    )
            else:
                if current:
                    normalized.append(current)
                current = normalized_dialog
                current_start_seconds = start_seconds
                current_end_seconds = end_seconds

        if current:
            normalized.append(current)

        return normalized

    def _normalize_dialog(
        self, dialog: Dict, speaker_mapping: Dict[str, str]
    ) -> tuple:
        """规范化单条对话，返回(对话, start_seconds, end_seconds)"""
        speaker = dialog.get("speaker", "unknown")
        text = dialog.get("text", "")
        if not text:
            return None, None, None

        if speaker_mapping and speaker in speaker_mapping:
            speaker = speaker_mapping[speaker]

        start_raw = dialog.get("start_time")
        end_raw = dialog.get("end_time")
        duration_raw = dialog.get("duration")

        start_seconds = self._parse_time_value(start_raw)
        end_seconds = self._parse_time_value(end_raw)

        start_time = (
            self._format_timestamp(start_seconds)
            if start_seconds is not None
            else (str(start_raw) if start_raw else "00:00:00")
        )
        end_time = (
            self._format_timestamp(end_seconds)
            if end_seconds is not None
            else (str(end_raw) if end_raw else start_time)
        )

        if start_seconds is not None and end_seconds is not None:
            duration = end_seconds - start_seconds
        else:
            try:
                duration = float(duration_raw) if duration_raw is not None else 0.0
            except (TypeError, ValueError):
                duration = 0.0

        normalized_dialog = {
            "start_time": start_time,
            "end_time": end_time,
            "duration": duration,
            "speaker": speaker,
            "text": text,
        }

        return normalized_dialog, start_seconds, end_seconds

    @staticmethod
    def _parse_time_value(value: Any) -> Optional[float]:
        """解析时间值为秒数"""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            try:
                if ":" in value:
                    parts = value.split(":")
                    parts = [p.strip() for p in parts]
                    if len(parts) == 3:
                        hours, minutes, seconds = parts
                    elif len(parts) == 2:
                        hours = 0
                        minutes, seconds = parts
                    else:
                        return None
                    return float(hours) * 3600 + float(minutes) * 60 + float(seconds)
                return float(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _format_timestamp(seconds: Optional[float]) -> str:
        """将秒数转换为 HH:MM:SS 格式"""
        if seconds is None:
            return "00:00:00"
        total = int(seconds)
        hours = total // 3600
        minutes = (total % 3600) // 60
        secs = total % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

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
            chunk_length = sum(len(d.get("text", "")) for d in chunk)
            logger.info(
                f"Calibrating chunk {index + 1}/{len(chunks)}, dialog count: {len(chunk)}, length: {chunk_length}"
            )

            max_attempts = self.config.max_calibration_retries + 1
            for attempt in range(max_attempts):
                try:
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
                    calibrated_dialogs = response.structured_output.get(
                        "calibrated_dialogs", []
                    )

                    # 合并校对结果与原始对话（保留时间戳）
                    merged_dialogs = self._merge_calibrated_with_original(
                        calibrated_dialogs, chunk
                    )

                    # 若数量不一致，合并会降级为原始 chunk
                    if merged_dialogs is chunk:
                        logger.warning(
                            f"Chunk {index + 1} calibration result count mismatch, "
                            f"falling back to original"
                        )
                        calibrated_chunks[index] = chunk
                        return

                    # 步骤4: 分段质量验证（可选）
                    if self.config.enable_validation:
                        logger.info(f"Validating chunk {index + 1}/{len(chunks)}")

                        validation_result = self.quality_validator.validate_by_score(
                            original=chunk,
                            calibrated=merged_dialogs,
                            video_metadata={
                                "title": title,
                                "author": "",
                                "description": description,
                            },
                            selected_models=selected_models,
                        )

                        if not validation_result["passed"]:
                            if attempt < max_attempts - 1:
                                logger.warning(
                                    f"Chunk {index + 1} validation failed "
                                    f"(score: {validation_result.get('overall_score', 'N/A')}), "
                                    f"retrying ({attempt + 1}/{max_attempts - 1})"
                                )
                                continue

                            logger.warning(
                                f"Chunk {index + 1} validation failed "
                                f"(score: {validation_result.get('overall_score', 'N/A')}), "
                                f"falling back to original"
                            )
                            calibrated_chunks[index] = chunk
                            return

                        logger.info(
                            f"Chunk {index + 1} validation passed "
                            f"(score: {validation_result.get('overall_score', 'N/A')})"
                        )

                    calibrated_chunks[index] = merged_dialogs
                    logger.info(f"Chunk {index + 1} calibration completed")
                    return

                except Exception as e:
                    if attempt < max_attempts - 1:
                        logger.warning(
                            f"Chunk {index + 1} calibration failed: {e}, "
                            f"retrying ({attempt + 1}/{max_attempts - 1})"
                        )
                        continue

                    logger.error(f"Chunk {index + 1} calibration failed: {e}")
                    calibrated_chunks[index] = chunk  # 降级到原文
                    return

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
            start_time = dialog.get("start_time", "")

            # 应用说话人映射
            if speaker_mapping and speaker in speaker_mapping:
                speaker = speaker_mapping[speaker]

            time_tag = f"[{start_time}]" if start_time else ""
            parts.append(f"{time_tag}[{speaker}]: {text}")

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
            parts.append(f"{speaker}：{text}")

        return "\n\n".join(parts)

    def _merge_calibrated_with_original(
        self, calibrated_dialogs: List[Dict], original_chunk: List[Dict]
    ) -> List[Dict]:
        """合并校对结果与原始对话，保留时间戳和原文"""
        if len(calibrated_dialogs) != len(original_chunk):
            return original_chunk

        merged_dialogs = []
        for idx, calibrated in enumerate(calibrated_dialogs):
            original = original_chunk[idx]
            original_text = original.get("original_text", original.get("text", ""))

            merged_dialogs.append(
                {
                    "start_time": original.get("start_time", "00:00:00"),
                    "end_time": original.get("end_time", "00:00:00"),
                    "duration": original.get("duration", 0),
                    "speaker": calibrated.get("speaker", original.get("speaker", "unknown")),
                    "text": calibrated.get("text", original.get("text", "")),
                    "original_text": original_text,
                }
            )

        return merged_dialogs
