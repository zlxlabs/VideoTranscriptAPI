"""有说话人文本处理器"""

import contextvars
import re
import time
from typing import Dict, List, Optional, Any
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures

from ...utils.logging import setup_logger
from ..core.config import LLMConfig
from ..core.llm_client import LLMClient
from ..core.key_info_extractor import KeyInfoExtractor, KeyInfo
from ..core.speaker_inferencer import SpeakerInferencer
from ..core.usage_context import set_context
from ..validators.unified_quality_validator import UnifiedQualityValidator
from ..segmenters.dialog_segmenter import DialogSegmenter
from ..prompts import (
    STRUCTURED_CALIBRATE_SYSTEM_PROMPT,
    STRUCTURED_CALIBRATE_SYSTEM_PROMPT_EN,
    build_structured_calibrate_user_prompt,
)
from ..schemas import CALIBRATION_RESULT_SCHEMA
from ..utils.language_detector import detect_language

logger = setup_logger(__name__)


class SpeakerAwareProcessor:
    """有说话人文本处理器"""

    def __init__(
        self,
        config: LLMConfig,
        llm_client: LLMClient,
        key_info_extractor: KeyInfoExtractor,
        speaker_inferencer: SpeakerInferencer,
        quality_validator: UnifiedQualityValidator,
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

        # 步骤0: 检测语言
        dialog_text_sample = " ".join(d.get("text", "") for d in base_dialogs[:50])
        detected_language = detect_language(dialog_text_sample)
        logger.info(f"Detected language: {detected_language}")

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
        # 细化 stage=speaker_inference，仅覆盖本次说话人推断调用的审计标签，
        # 退出 with 块后自动恢复为外层的 calibration
        with set_context(stage="speaker_inference"):
            speaker_inference_result = self.speaker_inferencer.infer(
                speakers=speakers,
                dialogs=base_dialogs,
                title=title,
                author=author,
                description=description,
                key_info=key_info,
                platform=platform,
                media_id=media_id,
            )
        speaker_mapping = speaker_inference_result.get("mapping", {})
        speaker_inference_meta = speaker_inference_result.get("meta", {})

        # 结构化标准化（应用映射 + 合并连续同说话人 + 时间字段规范化）
        normalized_dialogs = self._normalize_and_merge_dialogs(
            base_dialogs, speaker_mapping
        )

        # 步骤2: 分段
        chunks = self.segmenter.segment(normalized_dialogs)
        logger.debug(f"Dialogs segmented: {len(chunks)} chunks")

        # 步骤3: 分段校对（每段独立验证）
        calibrated_chunks, calibration_stats = self._calibrate_chunks(
            chunks=chunks,
            original_chunks=chunks,  # 传入原始chunk用于验证
            key_info=key_info,
            speaker_mapping=speaker_mapping,
            title=title,
            description=description,
            selected_models=selected_models,
            language=detected_language,
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
                "calibration_stats": calibration_stats,
                "speaker_inference": speaker_inference_meta,
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
        language: str = "zh",
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
            language: 检测到的语言（"zh" 或 "en"）

        Returns:
            (calibrated_chunks, calibration_stats) 元组:
            - calibrated_chunks: 校对后的分块列表（包含成功+降级的混合结果）
            - calibration_stats: 校准统计 {total_chunks, success_count, fallback_count, failed_count}
        """
        model = selected_models["calibrate_model"] if selected_models else self.config.calibrate_model
        reasoning_effort = selected_models.get("calibrate_reasoning_effort") if selected_models else self.config.calibrate_reasoning_effort

        # 格式化关键信息
        key_info_text = key_info.format_for_prompt()

        # 注入专有名词库匹配结果
        try:
            from ...terminology import get_terminology_db
            term_db = get_terminology_db()
            # 从标题、描述和前几段对话中匹配专有名词
            sample_texts = [title, description]
            for chunk in chunks[:3]:
                for dialog in chunk[:5]:
                    sample_texts.append(dialog.get("text", "")[:200])
            sample_text = " ".join(sample_texts)
            term_prompt = term_db.format_matched_for_prompt(sample_text)
            if term_prompt:
                key_info_text += f"\n- 专有名词对照表（请使用正确写法）:\n{term_prompt}"
                logger.debug(f"Injected terminology corrections into prompt")
        except Exception as e:
            logger.warning(f"Failed to load terminology DB: {e}")

        # 根据语言选择 system prompt
        structured_system_prompt = (
            STRUCTURED_CALIBRATE_SYSTEM_PROMPT_EN if language == "en"
            else STRUCTURED_CALIBRATE_SYSTEM_PROMPT
        )

        calibrated_chunks = [None] * len(chunks)
        # 跟踪每个 chunk 的校准状态: "success" | "partial" | "fallback" | "failed"
        chunk_statuses = ["failed"] * len(chunks)
        # 每个 chunk 的 ID 锚点合并计数（applied/kept_original/unknown_id/duplicate_id/malformed），用于统计
        chunk_counts = [None] * len(chunks)

        def _fallback_counts(n: int) -> Dict:
            """异常/超时回退（整块用原文）时的计数：全部保留原文。"""
            return {
                "applied": 0,
                "kept_original": n,
                "unknown_id": 0,
                "duplicate_id": 0,
                "malformed": 0,
            }

        def calibrate_single_chunk(index: int, chunk: List[Dict]):
            """校对单个 chunk（ID 锚点合并 + 覆盖率重试 + 时间预算）"""
            n = len(chunk)
            chunk_length = sum(len(d.get("text", "")) for d in chunk)
            logger.debug(
                f"Calibrating chunk {index + 1}/{len(chunks)}, dialog count: {n}, length: {chunk_length}"
            )

            max_attempts = self.config.max_calibration_retries + 1
            fallback_strategy = self.config.structured_fallback_strategy
            chunk_budget = self.config.chunk_time_budget
            min_coverage = self.config.min_correction_coverage
            start_time = time.monotonic()

            # 追踪覆盖率最高的合并结果（重试耗尽时采用，永远不劣于原文）
            best_candidate = None
            best_counts = None
            best_coverage = -1.0
            for attempt in range(max_attempts):
                # 时间预算检查
                elapsed = time.monotonic() - start_time
                if elapsed > chunk_budget:
                    logger.warning(
                        f"Chunk {index + 1} time budget exhausted "
                        f"({elapsed:.0f}s > {chunk_budget}s), using best candidate"
                    )
                    if best_candidate is not None:
                        calibrated_chunks[index] = best_candidate
                        chunk_counts[index] = best_counts
                        chunk_statuses[index] = "partial"
                    else:
                        calibrated_chunks[index] = self._apply_structured_fallback(
                            original_chunk=chunk,
                            best_candidate=None,
                            last_candidate=None,
                            fallback_strategy=fallback_strategy,
                        )
                        chunk_counts[index] = _fallback_counts(n)
                        chunk_statuses[index] = "failed"
                    return
                try:
                    # 构建 prompt（每行带 [id] 锚点）
                    chunk_text = self._format_chunk_for_prompt(chunk, speaker_mapping)

                    user_prompt = build_structured_calibrate_user_prompt(
                        dialogs_text=chunk_text,
                        video_title=title,
                        description=description,
                        key_info=key_info_text,
                        dialog_count=n,
                        min_ratio=self.config.min_calibrate_ratio,
                        language=language,
                    )

                    # 调用 LLM（结构化输出）
                    response = self.llm_client.call(
                        model=model,
                        system_prompt=structured_system_prompt,
                        user_prompt=user_prompt,
                        response_schema=CALIBRATION_RESULT_SCHEMA,
                        reasoning_effort=reasoning_effort,
                        task_type="calibrate_chunk",
                    )

                    # 解析结构化输出（ID 锚点修正列表）
                    corrections = response.structured_output.get("corrections", [])

                    # 按 id 查表合并：结构永不失败，缺号自动保留原文
                    merged_dialogs, counts = self._apply_corrections_by_id(
                        corrections, chunk
                    )
                    coverage = (n - counts["kept_original"]) / n if n else 1.0

                    # 追踪最佳候选（覆盖率优先）
                    if coverage > best_coverage:
                        best_coverage = coverage
                        best_candidate = merged_dialogs
                        best_counts = counts

                    # 低覆盖（疑似截断/偷懒/空响应）→ 重试而非默默部分不改
                    if coverage < min_coverage and attempt < max_attempts - 1:
                        logger.warning(
                            f"Chunk {index + 1} low correction coverage "
                            f"({coverage:.0%} < {min_coverage:.0%}, applied={counts['applied']}/{n}, "
                            f"malformed={counts['malformed']}), retrying ({attempt + 1}/{max_attempts - 1})"
                        )
                        continue

                    # 步骤4: 分段质量验证（可选，默认关闭）
                    if self.config.structured_validation_enabled:
                        logger.debug(f"Validating chunk {index + 1}/{len(chunks)}")

                        # 细化 stage=validation，仅覆盖本次质量验证调用的审计标签，
                        # 退出 with 块后自动恢复为外层的 calibration
                        with set_context(stage="validation"):
                            validation_result = self.quality_validator.validate(
                                original=chunk,
                                calibrated=merged_dialogs,
                                context={
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
                                f"fallback strategy={fallback_strategy}"
                            )
                            calibrated_chunks[index] = self._apply_structured_fallback(
                                original_chunk=chunk,
                                best_candidate=best_candidate,
                                last_candidate=merged_dialogs,
                                fallback_strategy=fallback_strategy,
                            )
                            chunk_counts[index] = best_counts or counts
                            chunk_statuses[index] = "fallback"
                            return

                        logger.debug(
                            f"Chunk {index + 1} validation passed "
                            f"(score: {validation_result.get('overall_score', 'N/A')})"
                        )

                    # 接受：覆盖率达标为 success，否则 partial（仍保留已应用的修正，不退原文）
                    calibrated_chunks[index] = merged_dialogs
                    chunk_counts[index] = counts
                    chunk_statuses[index] = "success" if coverage >= min_coverage else "partial"
                    logger.debug(
                        f"Chunk {index + 1} calibration completed "
                        f"(applied={counts['applied']}/{n}, coverage={coverage:.0%})"
                    )
                    return

                except Exception as e:
                    if attempt < max_attempts - 1:
                        logger.warning(
                            f"Chunk {index + 1} calibration failed: {e}, "
                            f"retrying ({attempt + 1}/{max_attempts - 1})"
                        )
                        continue

                    logger.error(f"Chunk {index + 1} calibration failed: {e}")
                    # 异常耗尽：优先采用历史最佳候选，否则回退原文
                    if best_candidate is not None:
                        calibrated_chunks[index] = best_candidate
                        chunk_counts[index] = best_counts
                        chunk_statuses[index] = "partial"
                    else:
                        calibrated_chunks[index] = self._apply_structured_fallback(
                            original_chunk=chunk,
                            best_candidate=None,
                            last_candidate=None,
                            fallback_strategy=fallback_strategy,
                        )
                        chunk_counts[index] = _fallback_counts(n)
                        chunk_statuses[index] = "failed"
                    return

        # 并发处理
        # 注意：ThreadPoolExecutor worker 线程不会自动继承主线程的 contextvars
        # （task_id/stage 审计上下文），必须显式 contextvars.copy_context().run
        # 传播，否则 worker 线程内的 LLM 调用会被记成 task_id='unknown'
        max_workers = min(len(chunks), self.config.calibration_concurrent_limit)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    contextvars.copy_context().run, calibrate_single_chunk, i, chunk
                )
                for i, chunk in enumerate(chunks)
            ]

            for future in concurrent.futures.as_completed(futures):
                future.result()  # 等待完成

        # 统计校准质量（块级状态）
        success_count = chunk_statuses.count("success")
        partial_count = chunk_statuses.count("partial")
        fallback_count = chunk_statuses.count("fallback")
        failed_count = chunk_statuses.count("failed")
        total = len(chunks)

        # 聚合 ID 锚点合并的对话级计数（可观测：哪些 id 被改/保留/异常）
        dialog_counts = {
            "applied": 0,
            "kept_original": 0,
            "unknown_id": 0,
            "duplicate_id": 0,
            "malformed": 0,
        }
        for c in chunk_counts:
            if not c:
                continue
            for key in dialog_counts:
                dialog_counts[key] += c.get(key, 0)

        if failed_count == total:
            logger.warning(
                f"All {total} chunks calibration failed, "
                f"output is raw ASR text without calibration"
            )
        elif failed_count > 0 or fallback_count > 0 or partial_count > 0:
            logger.warning(
                f"Calibration partial: {success_count}/{total} succeeded, "
                f"{partial_count} partial, {fallback_count} fallback, {failed_count} failed; "
                f"dialogs applied={dialog_counts['applied']}, kept={dialog_counts['kept_original']}, "
                f"malformed={dialog_counts['malformed']}"
            )
        else:
            logger.info(
                f"All {total} chunks calibrated successfully "
                f"(dialogs applied={dialog_counts['applied']}, kept={dialog_counts['kept_original']})"
            )

        calibration_stats = {
            "total_chunks": total,
            "success_count": success_count,
            "partial_count": partial_count,
            "fallback_count": fallback_count,
            "failed_count": failed_count,
            "dialog_counts": dialog_counts,
        }

        return calibrated_chunks, calibration_stats

    def _format_chunk_for_prompt(self, chunk: List[Dict], speaker_mapping: Dict[str, str]) -> str:
        """格式化对话块为 prompt 文本

        Args:
            chunk: 对话块
            speaker_mapping: 说话人映射

        Returns:
            格式化的文本
        """
        parts = []
        for idx, dialog in enumerate(chunk):
            speaker = dialog.get("speaker", "")
            text = dialog.get("text", "")
            start_time = dialog.get("start_time", "")

            # 应用说话人映射
            if speaker_mapping and speaker in speaker_mapping:
                speaker = speaker_mapping[speaker]

            # [id] 为 ID 锚点（chunk 内 0 基下标），LLM 必须按此 id 回传修正；
            # 时间戳仅作上下文，不要求回传。
            time_tag = f"[{start_time}]" if start_time else ""
            parts.append(f"[{idx}]{time_tag}[{speaker}]: {text}")

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

    @staticmethod
    def _coerce_dialog_id(raw: Any) -> Optional[int]:
        """将 LLM 返回的 id 规范化为 int。

        接受 int、整数值的 float（3.0）、纯数字字符串（"3"）；
        其余（"abc"、1.5、None、列表等）一律返回 None 视为 malformed。
        """
        if isinstance(raw, bool):  # bool 是 int 子类，显式排除
            return None
        if isinstance(raw, int):
            return raw
        if isinstance(raw, float):
            return int(raw) if raw.is_integer() else None
        if isinstance(raw, str):
            s = raw.strip()
            if s.lstrip("-").isdigit():
                return int(s)
        return None

    @staticmethod
    def _valid_correction_text(text: Any) -> bool:
        """校验单条 correction 的 text 是否可用。

        拒绝：非字符串、空/纯空白、以及把 prompt 行格式 `[id][spk]:` 原样回吐的脏数据。
        """
        if not isinstance(text, str):
            return False
        stripped = text.strip()
        if not stripped:
            return False
        # 拒绝模型把输入行格式 `[0][S0]: ...` 原样抄回
        if re.match(r"^\[\d+\]\[", stripped):
            return False
        return True

    def _apply_corrections_by_id(
        self, corrections: List[Dict], original_chunk: List[Dict]
    ) -> tuple:
        """按 id 锚点合并校对结果。

        id == original_chunk 中的下标（0 基）。speaker/时间戳/对话数量全部取自原始，
        LLM 的输出物理上无法影响结构——结构不匹配这个故障类不再存在。

        合并语义：
        - id 命中且 text 合法 → 用校对 text（counts.applied）
        - id 缺失（LLM 没返回该条）→ 保留原文（counts.kept_original）
        - id 越界 → 忽略并计数（counts.unknown_id）
        - id 重复 → 取首条，其余计数（counts.duplicate_id）
        - id/text 非法 → 计为 malformed，该条保留原文

        Returns:
            (merged_dialogs, counts) — merged 长度恒等于 original_chunk
        """
        n = len(original_chunk)
        counts = {
            "applied": 0,
            "kept_original": 0,
            "unknown_id": 0,
            "duplicate_id": 0,
            "malformed": 0,
        }

        by_id: Dict[int, str] = {}
        for item in corrections or []:
            if not isinstance(item, dict):
                counts["malformed"] += 1
                continue
            cid = self._coerce_dialog_id(item.get("id"))
            text = item.get("text")
            if cid is None or not self._valid_correction_text(text):
                counts["malformed"] += 1
                continue
            if cid < 0 or cid >= n:
                counts["unknown_id"] += 1
                continue
            if cid in by_id:
                counts["duplicate_id"] += 1  # 首条已采用，丢弃后续
                continue
            by_id[cid] = text

        merged_dialogs = []
        for idx, original in enumerate(original_chunk):
            original_text = original.get("original_text", original.get("text", ""))
            if idx in by_id:
                text = by_id[idx]
                counts["applied"] += 1
            else:
                text = original.get("text", "")
                counts["kept_original"] += 1
            merged_dialogs.append(
                {
                    "start_time": original.get("start_time", "00:00:00"),
                    "end_time": original.get("end_time", "00:00:00"),
                    "duration": original.get("duration", 0),
                    "speaker": original.get("speaker", "unknown"),
                    "text": text,
                    "original_text": original_text,
                }
            )

        return merged_dialogs, counts

    def _apply_structured_fallback(
        self,
        original_chunk: List[Dict],
        best_candidate: Optional[List[Dict]],
        last_candidate: Optional[List[Dict]],
        fallback_strategy: str,
    ) -> List[Dict]:
        """结构化校对失败时的降级策略"""
        if fallback_strategy == "second_attempt" and last_candidate:
            return last_candidate

        if fallback_strategy == "best_quality" and best_candidate:
            return best_candidate

        return original_chunk
