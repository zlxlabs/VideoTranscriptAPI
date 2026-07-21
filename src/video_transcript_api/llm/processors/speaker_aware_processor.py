"""有说话人文本处理器"""

import contextvars
import re
import time
from typing import Dict, List, Optional, Any
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures

from ...utils.logging import setup_logger
from ...utils.llm_status import CalibrationStatus
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
    STRUCTURED_CALIBRATE_NO_SPEAKER_SYSTEM_PROMPT,
    STRUCTURED_CALIBRATE_NO_SPEAKER_SYSTEM_PROMPT_EN,
    build_structured_calibrate_user_prompt,
)
from ..schemas import CALIBRATION_RESULT_SCHEMA
from ...transcriber.paragraphize import paragraphize_segments
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
        skip_calibration: bool = False,
        infer_speaker_names: bool = True,
        has_speaker: Optional[bool] = None,
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
            skip_calibration: 是否跳过分块 LLM 校对调用（processing_options.calibrate=False
                时为 True）。说话人推断、说话人映射、对话规范化合并这些步骤仍会执行——
                它们属于"转录"交付物（谁在说话）而非"校对"（文字是否准确），跳过的只是
                逐块把文本喂给 LLM 做文字校正/纠错的那一步。
            has_speaker: 输入是否携带说话人标签。None（默认）时自动判定——
                任一原始输入段能解析出说话人标签即为 True（混合输入维持现状
                行为）；False 时走「无说话人逐段校对」模式：跳过说话人推断、
                不做同说话人合并、speaker/时间缺省保留缺省，最终产物是确定性
                段落化后的段落。

        Returns:
            处理结果字典
        """
        # has_speaker 自动判定（None 时）：任一原始输入段能解析出说话人标签
        # 即视为有说话人（混合输入维持现状 has_speaker=True 行为）。必须在
        # _coerce_dialogs 之前判定——coerce 会把缺省说话人规范掉。
        if has_speaker is None:
            has_speaker = any(
                isinstance(d, dict)
                and SpeakerInferencer.resolve_dialog_speaker(d) is not None
                for d in dialogs or []
            )

        base_dialogs = self._coerce_dialogs(dialogs, has_speaker=has_speaker)
        total_length = sum(len(d.get("text", "")) for d in base_dialogs)
        logger.info(
            f"Start processing speaker-aware text: {title}, dialog count: {len(base_dialogs)}, "
            f"total length: {total_length}, skip_calibration={skip_calibration}"
        )

        # 步骤0: 检测语言
        dialog_text_sample = " ".join(d.get("text", "") for d in base_dialogs[:50])
        detected_language = detect_language(dialog_text_sample)
        logger.info(f"Detected language: {detected_language}")

        # Key info only feeds calibration and speaker-name inference prompts.
        # Skipping both must not make a hidden LLM call.
        # 无说话人模式推广：说话人推断整步跳过时，key_info 的唯一剩余消费者
        # 是校准——need = not skip_calibration or (has_speaker and
        # infer_speaker_names)；has_speaker=True 时与原条件完全等价。
        need_key_info = (not skip_calibration) or (has_speaker and infer_speaker_names)
        if not need_key_info:
            key_info = KeyInfo([], [], [], [], [], [], [])
        else:
            key_info = self.key_info_extractor.extract(
                title=title,
                author=author,
                description=description,
                platform=platform,
                media_id=media_id,
            )

        # 步骤1.5: 说话人推断
        # 统一用 SpeakerInferencer.extract_speaker_labels（本地 codex review 第 7 轮 H7）：
        # 原本此处是独立手写的列表推导式，与下方 _coerce_dialogs 内部的别名链解析点同源但独立维护；
        # base_dialogs 是 _coerce_dialogs 的输出（只剩 "speaker"/"text" 两个规范字段且 text 已保证非空），
        # 在这份输入上与原来的列表推导式行为完全等价，但与
        # 读侧（transcription.py 的分层缓存预检）共用同一个实现，不再因两处各自手写悄悄漂移再次拉出
        # 经典读/写指纹不一致 bug。
        if has_speaker:
            speakers = SpeakerInferencer.extract_speaker_labels(base_dialogs)
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
                    allow_llm=infer_speaker_names,
                )
        else:
            # 无说话人模式：说话人推断整步跳过（零 LLM 调用）。
            # identity_fallback source 让下游 llm_ops 的刷新判断按既有语义
            # 安全跳过；meta 放说明性内容保持可观测。
            speaker_inference_result = {
                "mapping": {},
                "meta": {
                    "note": "input has no speaker labels; speaker inference skipped"
                },
                "source": "identity_fallback",
            }
        speaker_mapping = speaker_inference_result.get("mapping", {})
        speaker_inference_meta = speaker_inference_result.get("meta", {})
        # 本地 codex review 第 6 轮 G4：随 meta 一起把本轮映射的真实
        # 来源传给下游（llm_ops._save_llm_results 的补层刷新判断），
        # 区分 llm/cache_hit/identity_fallback——见
        # SpeakerInferencer.infer() 顶部 docstring 的 "source" 一节。
        speaker_inference_source = speaker_inference_result.get("source")

        # 结构化标准化（应用映射 + 合并连续同说话人 + 时间字段规范化）
        # 无说话人模式：normalize 前从 coerce 输出快照 float 秒时间轴。
        # normalize 会把时间 int() 截断成 HH:MM:SS 字符串，±1s 误差会污染
        # 段落化的停顿授权判定；不合并时 coerced/normalized/calibrated 三列表
        # 按下标 1:1 对齐（coerce 会丢空文本条，快照在 coerce 后做）。
        time_snapshot = None
        if not has_speaker:
            time_snapshot = [
                (
                    self._parse_time_value(d.get("start_time")),
                    self._parse_time_value(d.get("end_time")),
                )
                for d in base_dialogs
            ]

        normalized_dialogs = self._normalize_and_merge_dialogs(
            base_dialogs, speaker_mapping, has_speaker=has_speaker
        )

        if skip_calibration:
            # 校对已禁用：跳过分段与逐块 LLM 校对调用，直接使用规范化后的对话作为
            # 最终产物（说话人标注/合并已在上面完成，属于转录交付物，照常保留）。
            chunks: List[List[Dict]] = []
            calibrated_dialogs = normalized_dialogs
            calibration_stats = {
                "total_chunks": 0,
                "success_count": 0,
                "partial_count": 0,
                "fallback_count": 0,
                "failed_count": 0,
                "dialog_counts": {
                    "applied": 0,
                    "kept_original": len(normalized_dialogs),
                    "unknown_id": 0,
                    "duplicate_id": 0,
                    "malformed": 0,
                },
                "calibration_status": CalibrationStatus.DISABLED,
            }
            logger.info(
                f"Calibration disabled by request, using normalized dialogs as-is "
                f"({len(normalized_dialogs)} dialogs, no chunk-level LLM calls)"
            )
        else:
            # 步骤2: 分段
            # 无说话人模式用 plain 独立分块参数（段落比对话长，预算放大）；
            # has_speaker=True 路径沿用 __init__ 的 segmenter，行为不变。
            if has_speaker:
                segmenter = self.segmenter
            else:
                segmenter = DialogSegmenter(
                    self.config,
                    preferred_chunk_length=self.config.plain_structured_preferred_chunk_length,
                    max_chunk_length=self.config.plain_structured_max_chunk_length,
                )
            chunks = segmenter.segment(normalized_dialogs)
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
                has_speaker=has_speaker,
            )

            # 合并校对结果（不再进行整体验证）
            calibrated_dialogs = []
            for chunk in calibrated_chunks:
                calibrated_dialogs.extend(chunk)

        # 构建文本用于统计
        # 无说话人模式：确定性段落化（钉死在构造 structured_data 返回之前，
        # 校准/skip 两条分支共用）——llm_processed.json dialogs、章节输入、
        # 渲染锚点三者是同一个列表。
        if not has_speaker:
            calibrated_dialogs = self._paragraphize_no_speaker_dialogs(
                calibrated_dialogs, time_snapshot
            )

        original_text = self._build_text_from_dialogs(
            normalized_dialogs, has_speaker=has_speaker
        )
        calibrated_text = self._build_text_from_dialogs(
            calibrated_dialogs, has_speaker=has_speaker
        )

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
                "dialog_count": (
                    len(normalized_dialogs) if has_speaker else len(calibrated_dialogs)
                ),
                "chunk_count": len(chunks),
                "calibration_stats": calibration_stats,
                "speaker_inference": speaker_inference_meta,
                "speaker_inference_source": speaker_inference_source,
            }
        }

    def _coerce_dialogs(self, dialogs: List[Dict], has_speaker: bool = True) -> List[Dict]:
        """将原始对话列表规范化为最小可用格式（speaker/text/start/end/duration）。

        has_speaker=False（无说话人模式）时缺省说话人保留 None，不塞 "unknown"。
        """
        coerced = []
        for dialog in dialogs or []:
            if not isinstance(dialog, dict):
                continue

            # 别名链解析委托给 SpeakerInferencer.resolve_dialog_speaker/
            # resolve_dialog_text（本地 codex review 第 7 轮 H7），与
            # speaker_inferencer.input_fingerprint、transcription.py 的
            # 预检读侧共用同一份实现——三处此前各自手写同一段 if/for 链，
            # 任何一处改动都可能忘了同步另外两处（is not None 判定错写成
            # 别的判定条件会把数字 0 的说话人折叠成 "unknown"，空文本判定
            # 口径不一致会让两侧算出的说话人集合分叉），统一实现从根上
            # 避免这类漂移。
            speaker = SpeakerInferencer.resolve_dialog_speaker(dialog)
            text = SpeakerInferencer.resolve_dialog_text(dialog)

            if text in (None, ""):
                continue

            coerced.append(
                {
                    # 无说话人模式保留缺省（None），不塞 "unknown"。注意
                    # d.get("speaker", "unknown") 的默认值只在键缺失时生效，
                    # 键存在但值为 None 时不触发——所以这里必须显式写 None。
                    "speaker": (
                        str(speaker)
                        if speaker is not None
                        else ("unknown" if has_speaker else None)
                    ),
                    "text": str(text),
                    "start_time": dialog.get("start_time", dialog.get("start")),
                    "end_time": dialog.get("end_time", dialog.get("end")),
                    "duration": dialog.get("duration"),
                }
            )

        return coerced

    def _normalize_and_merge_dialogs(
        self, dialogs: List[Dict], speaker_mapping: Dict[str, str], has_speaker: bool = True
    ) -> List[Dict]:
        """应用说话人映射、规范化时间字段并合并连续同说话人对话（has_speaker=False 时仅逐条规范化，不合并）"""
        normalized = []
        if not has_speaker:
            for dialog in dialogs:
                normalized_dialog, _, _ = self._normalize_dialog(
                    dialog, speaker_mapping, has_speaker=False
                )
                if normalized_dialog:
                    normalized.append(normalized_dialog)
            return normalized

        current = None
        current_start_seconds = None
        current_end_seconds = None

        for dialog in dialogs:
            normalized_dialog, start_seconds, end_seconds = self._normalize_dialog(
                dialog, speaker_mapping
            )
            if not normalized_dialog:
                continue

            if current and current.get("speaker_id") == normalized_dialog.get("speaker_id"):
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
        self, dialog: Dict, speaker_mapping: Dict[str, str], has_speaker: bool = True
    ) -> tuple:
        """规范化单条对话，返回(对话, start_seconds, end_seconds)"""
        raw_speaker = dialog.get("speaker", "unknown" if has_speaker else None)
        speaker = raw_speaker
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
            else (str(start_raw) if start_raw else ("00:00:00" if has_speaker else None))
        )
        end_time = (
            self._format_timestamp(end_seconds)
            if end_seconds is not None
            else (str(end_raw) if end_raw else (start_time if has_speaker else None))
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
            "speaker_id": raw_speaker,
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
        has_speaker: bool = True,
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
            has_speaker: 是否含说话人（选择 prompt 变体与行格式/合并兜底）

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

        # 根据语言与说话人模式选择 system prompt（无说话人变体的行格式描述
        # 与 _format_chunk_for_prompt 的 has_speaker=False 输出一致）
        if has_speaker:
            structured_system_prompt = (
                STRUCTURED_CALIBRATE_SYSTEM_PROMPT_EN if language == "en"
                else STRUCTURED_CALIBRATE_SYSTEM_PROMPT
            )
        else:
            structured_system_prompt = (
                STRUCTURED_CALIBRATE_NO_SPEAKER_SYSTEM_PROMPT_EN if language == "en"
                else STRUCTURED_CALIBRATE_NO_SPEAKER_SYSTEM_PROMPT
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
                    chunk_text = self._format_chunk_for_prompt(chunk, speaker_mapping, has_speaker)

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
                        corrections, chunk, has_speaker
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

        # 诚实状态口径：failed_count==0 且 fallback_count==0 → full；
        # 全部 chunk 都是 fallback/failed（即没有任何 chunk 干净成功）→ none；否则 partial。
        # 注意 partial_count（覆盖率不足但仍应用了部分修正）不参与该判定——
        # 只要没有 chunk 整体降级或失败，仍视为 full，因为每个 chunk 都保留了真实的 LLM 修正。
        if failed_count == 0 and fallback_count == 0:
            calibration_status = CalibrationStatus.FULL
        elif (failed_count + fallback_count) == total:
            calibration_status = CalibrationStatus.NONE
        else:
            calibration_status = CalibrationStatus.PARTIAL

        calibration_stats = {
            "total_chunks": total,
            "success_count": success_count,
            "partial_count": partial_count,
            "fallback_count": fallback_count,
            "failed_count": failed_count,
            "dialog_counts": dialog_counts,
            "calibration_status": calibration_status,
        }

        return calibrated_chunks, calibration_stats

    def _format_chunk_for_prompt(self, chunk: List[Dict], speaker_mapping: Dict[str, str], has_speaker: bool = True) -> str:
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
            if has_speaker:
                parts.append(f"[{idx}]{time_tag}[{speaker}]: {text}")
            else:
                # 无说话人行格式变体：去掉 speaker 括号，避免 f-string 遇
                # None 输出字面 [None] 污染 prompt；time_tag 为空时为 [{idx}]: {text}
                parts.append(f"[{idx}]{time_tag}: {text}")

        return "\n".join(parts)

    def _build_text_from_dialogs(self, dialogs: List[Dict], has_speaker: bool = True) -> str:
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
            if has_speaker:
                parts.append(f"{speaker}：{text}")
            else:
                # 无 speaker 变体只输出文本，不输出 `speaker：` 前缀
                parts.append(text)

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
        # 拒绝模型把输入行格式原样抄回：`[0][S0]: ...`（有说话人）与 `[0]: ...`（无说话人变体）
        if re.match(r"^\[\d+\](\[|:)", stripped):
            return False
        return True

    def _apply_corrections_by_id(
        self, corrections: List[Dict], original_chunk: List[Dict], has_speaker: bool = True
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
                    "start_time": original.get("start_time", "00:00:00" if has_speaker else None),
                    "end_time": original.get("end_time", "00:00:00" if has_speaker else None),
                    "duration": original.get("duration", 0),
                    "speaker": original.get("speaker", "unknown" if has_speaker else None),
                    # speaker_id 是原始说话人标签（由 _normalize_dialog 构建），
                    # 之前这里重建 dialog dict 时漏掉了它——本地
                    # codex review 第 5 轮 F4：llm_ops.py::
                    # _refresh_speaker_names_in_existing_structured_artifact 的
                    # 姓名补层刷新依赖这个字段来精确
                    # 定位每条 dialog 对应的原始标签，丢掉后
                    # 会误判为旧 schema（无 speaker_id）不做刷新。
                    "speaker_id": original.get("speaker_id"),
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

    def _paragraphize_no_speaker_dialogs(
        self, calibrated_dialogs: List[Dict], time_snapshot: Optional[List[tuple]]
    ) -> List[Dict]:
        """无说话人模式：把逐段（校准后）结果确定性拼成阅读段落。

        段落化只选边界、不动文本。输入文本用校准后 text，时间用 normalize 前
        快照的 float 秒（不合并时 coerced/normalized/calibrated 按下标 1:1
        对齐）；条数不一致——如 DialogSegmenter 拆分超长单条——时回退解析
        条目自身的 HH:MM:SS，停顿授权精度降级但不中断。输出段落格式化回
        HH:MM:SS（None 保留，不编造），不带 speaker/speaker_id 键，与
        FunASR 产物同构，直接作为最终 dialogs 落盘 llm_processed.json。
        """
        use_snapshot = time_snapshot is not None and len(time_snapshot) == len(
            calibrated_dialogs
        )
        segments = []
        for idx, dialog in enumerate(calibrated_dialogs):
            if use_snapshot:
                start_seconds, end_seconds = time_snapshot[idx]
            else:
                start_seconds = self._parse_time_value(dialog.get("start_time"))
                end_seconds = self._parse_time_value(dialog.get("end_time"))
            segments.append(
                {
                    "text": dialog.get("text", ""),
                    "start_time": start_seconds,
                    "end_time": end_seconds,
                    "original_text": dialog.get("original_text"),
                    "duration": dialog.get("duration"),
                }
            )

        paragraphs = paragraphize_segments(
            segments,
            target_chars=self.config.paragraphization_target_chars,
            hard_max_chars=self.config.paragraphization_hard_max_chars,
            pause_threshold_seconds=self.config.paragraphization_pause_threshold_seconds,
        )

        final_dialogs = []
        for paragraph in paragraphs:
            dialog = {
                "start_time": (
                    self._format_timestamp(paragraph["start_time"])
                    if paragraph["start_time"] is not None
                    else None
                ),
                "end_time": (
                    self._format_timestamp(paragraph["end_time"])
                    if paragraph["end_time"] is not None
                    else None
                ),
                "text": paragraph["text"],
            }
            # duration / original_text 由 paragraphize 输出透传（有则带）
            if "duration" in paragraph:
                dialog["duration"] = paragraph["duration"]
            if "original_text" in paragraph:
                dialog["original_text"] = paragraph["original_text"]
            final_dialogs.append(dialog)
        return final_dialogs
