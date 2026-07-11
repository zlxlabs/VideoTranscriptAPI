"""无说话人文本处理器"""

import contextvars
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
import re

from ...utils.logging import setup_logger
from ...utils.llm_status import CalibrationStatus
from ..core.config import LLMConfig
from ..core.llm_client import LLMClient
from ..core.key_info_extractor import KeyInfoExtractor, KeyInfo
from ..core.usage_context import set_context
from ..validators.unified_quality_validator import UnifiedQualityValidator
from ..segmenters.text_segmenter import TextSegmenter
from ..prompts import (
    CALIBRATE_SYSTEM_PROMPT,
    CALIBRATE_SYSTEM_PROMPT_EN,
    build_calibrate_user_prompt,
)
from ..utils.language_detector import detect_language

logger = setup_logger(__name__)


class PlainTextProcessor:
    """无说话人文本处理器"""

    def __init__(
        self,
        config: LLMConfig,
        llm_client: LLMClient,
        key_info_extractor: KeyInfoExtractor,
        quality_validator: UnifiedQualityValidator,
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

        # 步骤0: 检测语言
        detected_language = detect_language(text)
        logger.info(f"Detected language: {detected_language}")

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
            logger.debug(f"Text segmented: {len(segments)} segments")
        else:
            segments = [text]
            logger.debug("Text length below threshold, no segmentation")

        # 步骤3: 分段校对
        calibrated_segments, segment_statuses = self._calibrate_segments(
            segments=segments,
            key_info=key_info,
            title=title,
            description=description,
            selected_models=selected_models,
            language=detected_language,
        )

        # 合并校对结果（分段级检查已完成，无需全局检查）
        calibrated_text = "\n\n".join(calibrated_segments)

        logger.info(
            f"Plain text processing completed: "
            f"original length {len(text)}, calibrated length {len(calibrated_text)}"
        )

        # 统计"诚实状态"：区分干净通过(success) / 采用了LLM输出但质量存疑(low_quality) /
        # 完全降级为原文格式化(fallback)三类，避免把 low_quality 段谎报为成功。
        total_segments = len(segment_statuses)
        fallback_segments = segment_statuses.count("fallback")
        low_quality_segments = segment_statuses.count("low_quality")
        # calibrated_segments：采用了 LLM 输出的段数（success + low_quality），
        # 与本方法名 _calibrate_segments 呼应，但注意这里是统计字段，不要与
        # 上面局部变量 calibrated_segments（校对后文本列表）混淆——此处在赋值前先读取计数。
        calibrated_segment_count = total_segments - fallback_segments

        if fallback_segments == 0 and low_quality_segments == 0:
            calibration_status = CalibrationStatus.FULL
        elif calibrated_segment_count == 0:
            calibration_status = CalibrationStatus.NONE
        else:
            calibration_status = CalibrationStatus.PARTIAL

        return {
            "calibrated_text": calibrated_text,
            "key_info": key_info.to_dict(),
            "stats": {
                "original_length": len(text),
                "calibrated_length": len(calibrated_text),
                "segment_count": len(segments),
                "total_segments": total_segments,
                "calibrated_segments": calibrated_segment_count,
                "fallback_segments": fallback_segments,
                "low_quality_segments": low_quality_segments,
                "calibration_status": calibration_status,
            }
        }

    def _calibrate_segments(
        self,
        segments: List[str],
        key_info: KeyInfo,
        title: str,
        description: str,
        selected_models: Optional[Dict],
        language: str = "zh",
    ) -> tuple:
        """校对分段文本（并发处理）

        Args:
            segments: 分段列表
            key_info: 关键信息
            title: 视频标题
            description: 描述
            selected_models: 选定的模型
            language: 检测到的语言（"zh" 或 "en"）

        Returns:
            (calibrated_segments, segment_statuses) 元组:
            - calibrated_segments: 校对后的分段文本列表
            - segment_statuses: 每段的状态列表，取值 "success"/"low_quality"/"fallback"
              ("success": 干净通过 pass_ratio 或验证；"low_quality": 走了降级分支但最终
              仍采用 LLM 候选文本；"fallback": 最终采用了原文格式化，即
              _fallback_plain_text 或异常兜底路径返回了 _format_plain_text(original))
        """
        model = selected_models["calibrate_model"] if selected_models else self.config.calibrate_model
        reasoning_effort = selected_models.get("calibrate_reasoning_effort") if selected_models else self.config.calibrate_reasoning_effort

        # 格式化关键信息
        key_info_text = key_info.format_for_prompt()

        # 注入专有名词库匹配结果
        try:
            from ...terminology import get_terminology_db
            term_db = get_terminology_db()
            # 从标题、描述和前1000字转录文本中匹配专有名词
            sample_text = f"{title} {description} {' '.join(s[:200] for s in segments[:5])}"
            term_prompt = term_db.format_matched_for_prompt(sample_text)
            if term_prompt:
                key_info_text += f"\n- 专有名词对照表（请使用正确写法）:\n{term_prompt}"
                logger.debug(f"Injected terminology corrections into prompt")
        except Exception as e:
            logger.warning(f"Failed to load terminology DB: {e}")

        # 根据语言选择 system prompt
        system_prompt = CALIBRATE_SYSTEM_PROMPT_EN if language == "en" else CALIBRATE_SYSTEM_PROMPT

        calibrated_segments = [None] * len(segments)
        # 与 calibrated_segments 一一对应的状态标记，供 process() 汇总诚实状态统计
        segment_statuses = [None] * len(segments)

        def calibrate_single_segment(index: int, segment: str):
            """校对单个分段（含长度检查 + 质量验证 + 二次校对）"""
            try:
                original_length = len(segment)
                logger.debug(f"Calibrating segment {index + 1}/{len(segments)}, length: {original_length}")

                # 第一次校对
                user_prompt = build_calibrate_user_prompt(
                    transcript=segment,
                    video_title=title,
                    description=description,
                    key_info=key_info_text,
                    language=language,
                )

                response = self.llm_client.call(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    reasoning_effort=reasoning_effort,
                    task_type="calibrate_segment",
                )

                calibrated_text = response.text
                calibrated_length = len(calibrated_text)

                pass_ratio = self.config.segmentation_pass_ratio
                force_retry_ratio = self.config.segmentation_force_retry_ratio
                fallback_strategy = self.config.segmentation_fallback_strategy

                ratio = calibrated_length / original_length if original_length > 0 else 0.0

                # 绿灯区：直接通过
                if ratio >= pass_ratio:
                    calibrated_segments[index] = calibrated_text
                    segment_statuses[index] = "success"
                    logger.debug(
                        f"Segment {index + 1} passed length ratio: "
                        f"{ratio:.2f} >= {pass_ratio}"
                    )
                    return

                # 红灯区：触发重试
                if ratio < force_retry_ratio:
                    logger.warning(
                        f"Segment {index + 1} too short: ratio {ratio:.2f} < {force_retry_ratio}, retrying..."
                    )

                    if language == "en":
                        retry_hint = (
                            f"Previous proofread result was too short ({calibrated_length} chars), "
                            f"while the original has {original_length} chars. "
                            f"Please ensure all substantive content is preserved, do not condense."
                        )
                    else:
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
                        language=language,
                    )

                    response_retry = self.llm_client.call(
                        model=model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt_retry,
                        reasoning_effort=reasoning_effort,
                        task_type="calibrate_segment_retry",
                    )

                    calibrated_text_retry = response_retry.text
                    calibrated_length_retry = len(calibrated_text_retry)
                    retry_ratio = calibrated_length_retry / original_length if original_length > 0 else 0.0

                    if retry_ratio >= pass_ratio:
                        calibrated_segments[index] = calibrated_text_retry
                        segment_statuses[index] = "success"
                        logger.info(
                            f"Segment {index + 1} retry passed length ratio: "
                            f"{retry_ratio:.2f} >= {pass_ratio}"
                        )
                        return

                    if retry_ratio < force_retry_ratio:
                        # 仍在红灯区
                        fallback_text = self._fallback_plain_text(
                            segment,
                            calibrated_text,
                            calibrated_text_retry,
                            fallback_strategy,
                        )
                        calibrated_segments[index] = fallback_text
                        segment_statuses[index] = self._classify_fallback_result(
                            segment, fallback_text
                        )
                        logger.warning(
                            f"Segment {index + 1} retry still too short: "
                            f"{retry_ratio:.2f} < {force_retry_ratio}, fallback={fallback_strategy}, "
                            f"status={segment_statuses[index]}"
                        )
                        return

                    # 黄灯区：进入质量验证或直接接受
                    candidate = calibrated_text_retry
                    if self.config.segmentation_validation_enabled:
                        # 细化 stage=validation，仅覆盖本次质量验证调用的审计标签，
                        # 退出 with 块后自动恢复为外层的 calibration
                        with set_context(stage="validation"):
                            validation_result = self.quality_validator.validate(
                                original=segment,
                                calibrated=candidate,
                                context={"title": title, "author": "", "description": description},
                                selected_models=selected_models,
                            )
                        if validation_result.get("passed"):
                            calibrated_segments[index] = candidate
                            segment_statuses[index] = "success"
                            return

                        fallback_text = self._fallback_plain_text(
                            segment,
                            calibrated_text,
                            candidate,
                            fallback_strategy,
                            validation_result,
                        )
                        calibrated_segments[index] = fallback_text
                        segment_statuses[index] = self._classify_fallback_result(
                            segment, fallback_text
                        )
                        return

                    calibrated_segments[index] = candidate
                    segment_statuses[index] = "success"
                    return

                # 黄灯区：触发质量验证（或直接通过）
                if self.config.segmentation_validation_enabled:
                    # 细化 stage=validation，仅覆盖本次质量验证调用的审计标签，
                    # 退出 with 块后自动恢复为外层的 calibration
                    with set_context(stage="validation"):
                        validation_result = self.quality_validator.validate(
                            original=segment,
                            calibrated=calibrated_text,
                            context={"title": title, "author": "", "description": description},
                            selected_models=selected_models,
                        )
                    if validation_result.get("passed"):
                        calibrated_segments[index] = calibrated_text
                        segment_statuses[index] = "success"
                        return

                    fallback_text = self._fallback_plain_text(
                        segment,
                        calibrated_text,
                        None,
                        fallback_strategy,
                        validation_result,
                    )
                    calibrated_segments[index] = fallback_text
                    segment_statuses[index] = self._classify_fallback_result(
                        segment, fallback_text
                    )
                    return

                calibrated_segments[index] = calibrated_text
                segment_statuses[index] = "success"

            except Exception as e:
                logger.error(f"Segment {index + 1} calibration failed: {e}")
                # 降级到原文（格式化处理）——异常兜底路径必然使用原文格式化，直接记为 fallback
                formatted_segment = self._format_plain_text(segment)
                calibrated_segments[index] = formatted_segment
                segment_statuses[index] = "fallback"

        # 并发处理
        # 注意：ThreadPoolExecutor worker 线程不会自动继承主线程的 contextvars
        # （task_id/stage 审计上下文），必须显式 contextvars.copy_context().run
        # 传播，否则 worker 线程内的 LLM 调用会被记成 task_id='unknown'
        max_workers = min(len(segments), self.config.concurrent_workers)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    contextvars.copy_context().run, calibrate_single_segment, i, seg
                )
                for i, seg in enumerate(segments)
            ]

            for future in concurrent.futures.as_completed(futures):
                future.result()  # 等待完成

        return calibrated_segments, segment_statuses

    def _classify_fallback_result(self, original_segment: str, final_text: str) -> str:
        """判定一次降级处理的最终产物是"低质量但仍是LLM输出"还是"彻底原文格式化"。

        只在 _fallback_plain_text 的返回值处调用（另一个判定点是异常兜底路径，
        那里不需要调用本方法，因为异常路径必然是 fallback）。

        判定依据：将最终文本与 _format_plain_text(original_segment) 逐字比较——
        _fallback_plain_text 内部所有分支要么直接返回 _format_plain_text(original)
        （strategy=formatted_original 或无可用 LLM 候选时的兜底），要么返回某个
        LLM 候选文本（strategy=second_attempt / best_quality 选中的候选）。
        二者只要不完全相等，就说明最终展示的是 LLM 产出（哪怕质量存疑），
        应计为 low_quality 而不是谎报为 fallback（完全没有 LLM 贡献）。

        Args:
            original_segment: 该段原始文本
            final_text: _fallback_plain_text 的返回值

        Returns:
            "fallback": 最终文本等于原文格式化结果，没有 LLM 内容留存
            "low_quality": 最终文本是某个 LLM 候选（未清晰通过质量门槛，但保留了校对内容）
        """
        if final_text == self._format_plain_text(original_segment):
            return "fallback"
        return "low_quality"

    def _fallback_plain_text(
        self,
        original: str,
        first_attempt: Optional[str],
        second_attempt: Optional[str],
        fallback_strategy: str,
        validation_result: Optional[Dict] = None,
    ) -> str:
        """处理纯文本分段的降级策略"""
        if fallback_strategy == "formatted_original":
            return self._format_plain_text(original)

        if fallback_strategy == "second_attempt" and second_attempt:
            return second_attempt

        if fallback_strategy == "best_quality":
            if validation_result and validation_result.get("overall_score") is not None:
                return second_attempt or first_attempt or self._format_plain_text(original)

            # 无评分时采用长度更接近原文者
            candidates = [c for c in [first_attempt, second_attempt] if c]
            if candidates:
                return max(candidates, key=lambda c: len(c))

        return self._format_plain_text(original)

    def _format_plain_text(self, text: str) -> str:
        """格式化纯文本，智能调整段落长度以提升可读性

        核心目标：
        1. 段落不能太长（避免文字墙）
        2. 段落不能太短（避免每句一行，浪费屏幕空间）

        处理策略：
        - 类型A：长文本墙（行数很少，平均每行很长）→ 按句子分段
        - 类型B：过度分割（行数很多，平均每行很短）→ 合并成段落
        - 类型C：合理段落（段落长度适中）→ 保持原样

        Args:
            text: 原始文本

        Returns:
            格式化后的文本
        """
        if not text or len(text) < 100:
            return text

        lines = [line.strip() for line in text.split('\n') if line.strip()]
        line_count = len(lines)
        text_length = len(text)
        avg_line_length = text_length / line_count if line_count > 0 else 0

        # 检测是否有段落结构（双换行 \n\n）
        has_paragraph_breaks = '\n\n' in text
        paragraph_count = len(text.split('\n\n')) if has_paragraph_breaks else 0

        logger.info(
            f"Analyzing text structure: {text_length} chars, {line_count} lines, "
            f"avg {avg_line_length:.1f} chars/line, "
            f"paragraph_breaks={has_paragraph_breaks}, paragraphs={paragraph_count}"
        )

        # 类型判断：判断文本结构类型
        # 类型C1：已有段落结构（双换行分隔）
        if has_paragraph_breaks and paragraph_count >= 2:
            logger.debug("Text already has paragraph structure (\\n\\n), skipping formatting")
            return text

        # 类型C2：合理段落（5-50行 且 平均每行50-200字符）
        if 5 <= line_count <= 50 and 50 <= avg_line_length <= 200:
            logger.debug("Text has reasonable line structure, skipping formatting")
            return text

        # 类型B：过度分割（行数多 且 平均每行很短）
        if line_count > 10 and avg_line_length < 50:
            logger.info("Detected over-segmented text, merging into paragraphs")
            return self._merge_into_paragraphs(lines)

        # 类型A：长文本墙（行数很少 且 平均每行很长）
        if line_count <= 3 or avg_line_length > 200:
            logger.info("Detected text wall, splitting into paragraphs")
            return self._split_into_paragraphs(text)

        # 默认：保持原样
        logger.debug("Text structure is acceptable, keeping original")
        return text

    def _merge_into_paragraphs(self, lines: List[str]) -> str:
        """合并过度分割的短行为合理段落

        策略：每2-4句为一段，目标段落长度100-300字符

        Args:
            lines: 短行列表

        Returns:
            合并后的段落文本
        """
        paragraphs = []
        current_para = ""
        sentence_count = 0

        for line in lines:
            # 跳过空行
            if not line:
                continue

            # 累积句子
            current_para += line
            if not line.endswith(('。', '！', '？', '!', '?', '.', ';', '；')):
                current_para += '。'  # 补充标点
            sentence_count += 1

            # 判断是否形成段落：2-4句 或 长度达到100-300字符
            if sentence_count >= 2 and len(current_para) >= 100:
                paragraphs.append(current_para)
                current_para = ""
                sentence_count = 0
            elif sentence_count >= 4 or len(current_para) >= 300:
                # 强制换段（避免段落过长）
                paragraphs.append(current_para)
                current_para = ""
                sentence_count = 0

        # 处理剩余内容
        if current_para:
            paragraphs.append(current_para)

        result = '\n\n'.join(paragraphs)
        logger.info(f"Merged {len(lines)} lines into {len(paragraphs)} paragraphs")
        return result

    def _split_into_paragraphs(self, text: str) -> str:
        """拆分长文本墙为合理段落

        策略：按句子分割，每2-3句为一段

        Args:
            text: 长文本

        Returns:
            分段后的文本
        """
        # 按句子结束标点分割
        pattern = r'([。！？!?]+)'
        parts = re.split(pattern, text)

        # 重新组合句子（文本 + 标点）
        sentences = []
        for i in range(0, len(parts) - 1, 2):
            if parts[i].strip():
                sentence = parts[i].strip() + (parts[i + 1] if i + 1 < len(parts) else '')
                sentences.append(sentence)

        # 处理最后一个片段（可能没有标点）
        if len(parts) % 2 == 1 and parts[-1].strip():
            sentences.append(parts[-1].strip())

        # 按2-3句分组形成段落
        paragraphs = []
        current_para = ""
        sentence_count = 0

        for sentence in sentences:
            current_para += sentence
            sentence_count += 1

            # 每2-3句换段，或长度超过250字符
            if sentence_count >= 2 and len(current_para) >= 100:
                paragraphs.append(current_para)
                current_para = ""
                sentence_count = 0
            elif sentence_count >= 3 or len(current_para) >= 250:
                paragraphs.append(current_para)
                current_para = ""
                sentence_count = 0

        # 处理剩余内容
        if current_para:
            paragraphs.append(current_para)

        result = '\n\n'.join(paragraphs)
        logger.info(f"Split {len(sentences)} sentences into {len(paragraphs)} paragraphs")
        return result
