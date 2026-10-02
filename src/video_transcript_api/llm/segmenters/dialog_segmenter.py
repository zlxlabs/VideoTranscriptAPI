"""有说话人文本分段器

基于 structured_calibrator.py 的 _intelligent_chunking 逻辑重构
"""

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from ...transcriber.segments import interpolate_segment_times, parse_time_to_seconds
from ...utils.logging import setup_logger
# 长度兜底的唯一实现（issue #142）：上限不可放弃这条不变式只靠一份代码成立
from ...utils.text_split import split_oversized_text
from ..core.config import LLMConfig

logger = setup_logger(__name__)

class DialogSegmenter:
    """有说话人文本分段器"""

    def __init__(
        self,
        config: LLMConfig,
        preferred_chunk_length: Optional[int] = None,
        max_chunk_length: Optional[int] = None,
    ):
        """初始化对话分段器

        Args:
            config: LLM 配置
            preferred_chunk_length: 理想块长覆盖（None 时取
                config.preferred_chunk_length）。无说话人（plain 源）模式
                用独立分块参数覆盖，has_speaker=True 路径不传、行为不变。
            max_chunk_length: 最大块长覆盖（None 时取 config.max_chunk_length）
        """
        self.config = config
        self.min_chunk_length = config.min_chunk_length
        self.max_chunk_length = (
            max_chunk_length if max_chunk_length is not None else config.max_chunk_length
        )
        self.preferred_chunk_length = (
            preferred_chunk_length
            if preferred_chunk_length is not None
            else config.preferred_chunk_length
        )

    def segment(self, dialogs: List[Dict]) -> List[List[Dict]]:
        """对对话列表进行智能分块

        Args:
            dialogs: 对话列表（每项包含 speaker, text, start_time）

        Returns:
            分块后的对话列表
        """
        if not dialogs:
            return []

        chunks = []
        current_chunk = []
        current_length = 0

        for dialog in dialogs:
            dialog_length = len(dialog.get("text", ""))

            # 单个对话太长 → 拆分
            if dialog_length > self.max_chunk_length:
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = []
                    current_length = 0

                sub_dialogs = self._split_long_dialog(dialog)
                for sub_dialog in sub_dialogs:
                    chunks.append([sub_dialog])
                continue

            # 加入会超长 → 结束当前 chunk
            if current_length + dialog_length > self.max_chunk_length:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = [dialog]
                current_length = dialog_length
            else:
                current_chunk.append(dialog)
                current_length += dialog_length

                # 达到理想长度 → 结束 chunk
                if current_length >= self.preferred_chunk_length:
                    chunks.append(current_chunk)
                    current_chunk = []
                    current_length = 0

        # 处理剩余对话
        if current_chunk:
            # 如果最后一个 chunk 太短，合并到前一个
            if chunks and current_length < self.min_chunk_length:
                chunks[-1].extend(current_chunk)
            else:
                chunks.append(current_chunk)

        # 过滤空 chunk
        chunks = [chunk for chunk in chunks if chunk]

        logger.debug(
            f"Dialog chunking completed: {len(chunks)} chunks, "
            f"length distribution: {[sum(len(d.get('text', '')) for d in chunk) for chunk in chunks]}"
        )
        return chunks

    def _split_long_dialog(self, dialog: Dict[str, Any]) -> List[Dict[str, Any]]:
        """拆分过长的单个对话

        Args:
            dialog: 单个对话

        Returns:
            拆分后的对话片段列表
        """
        text = dialog.get("text", "")
        if len(text) <= self.max_chunk_length:
            return [dialog]

        # 按句子分割
        sentences = self._split_by_sentences(text)
        sub_dialogs = []
        current_text = ""

        for sentence in sentences:
            if len(current_text + sentence) > self.max_chunk_length and current_text:
                # 创建子对话
                sub_dialog = dialog.copy()
                sub_dialog["text"] = current_text.strip()
                sub_dialogs.append(sub_dialog)
                current_text = sentence
            else:
                current_text += sentence

        # 处理剩余文本
        if current_text.strip():
            sub_dialog = dialog.copy()
            sub_dialog["text"] = current_text.strip()
            sub_dialogs.append(sub_dialog)

        # 兜底（issue #142）：句末切分对「无句末标点」的长文本（整篇英文独白最常见）
        # 完全无能为力，超长「句」会整条进一个子 dialog，max_chunk_length 就退化成
        # 软目标。形态固定为空白优先 / 无空白按上限硬切，只移动切点。
        # 碎片沿用 dialog.copy() 的既有做法：不改 id（校准阶段按 id 锚点回填，
        # 碎片靠 chunk 内位置区分），时间仍由 _interpolate_dialog_times 插值。
        # 上限来源：self.max_chunk_length 来自 LLMConfig.max_chunk_length，
        # plain 路径用 plain_structured_max_chunk_length 覆盖，两者在配置加载处
        # 都没有取值校验，因此共享实现在 max_len <= 0 时 fail fast（不 clamp）。
        # 另注：本仓句末/切分定义有 4 套互不一致的实现（capswriter `。！？!?` /
        # paragraphize `。！？….!?` / DialogSegmenter `。！？` / TextSegmenter 另一套），
        # 本批明确不统一，见 issue #146；本兜底只管长度，不改句末定义。
        whitespace_cuts = 0
        hard_cuts = 0
        capped_dialogs: List[Dict[str, Any]] = []
        for sub_dialog in sub_dialogs:
            sub_text = sub_dialog.get("text", "")
            if len(sub_text) <= self.max_chunk_length:
                capped_dialogs.append(sub_dialog)
                continue
            pieces, item_whitespace_cuts, item_hard_cuts = split_oversized_text(
                sub_text, self.max_chunk_length
            )
            whitespace_cuts += item_whitespace_cuts
            hard_cuts += item_hard_cuts
            for piece in pieces:
                fragment = dialog.copy()
                fragment["text"] = piece
                capped_dialogs.append(fragment)
        sub_dialogs = capped_dialogs

        if whitespace_cuts or hard_cuts:
            # 越上限是可观测的质量降级（正文不丢、不崩），因此记 warning 而不是
            # fail fast：后者会把一次质量问题升级成整任务失败。
            logger.warning(
                "Long dialog exceeds max_chunk_length, fallback split applied: "
                f"text_length={len(text)} parts={len(sub_dialogs)} "
                f"max_chunk_length={self.max_chunk_length} "
                f"whitespace_cuts={whitespace_cuts} hard_cuts={hard_cuts}"
            )

        logger.debug(f"Long dialog split: length {len(text)} -> {len(sub_dialogs)} fragments")
        if len(sub_dialogs) > 1:
            self._interpolate_dialog_times(dialog, sub_dialogs)
        return sub_dialogs

    def _interpolate_dialog_times(
        self, dialog: Dict[str, Any], sub_dialogs: List[Dict[str, Any]]
    ) -> None:
        """为实际切出的子对话写回字符串时间，无法格式化时保留文本并置空时间。"""
        start_raw = dialog.get("start_time")
        end_raw = dialog.get("end_time")
        start_seconds = parse_time_to_seconds(start_raw)
        end_seconds = parse_time_to_seconds(end_raw)
        time_pairs = interpolate_segment_times(
            start_seconds,
            end_seconds,
            [sub_dialog.get("text", "") for sub_dialog in sub_dialogs],
        )

        # Format each shared boundary once with a single template so adjacent
        # fragment end/start strings stay identical by construction.
        formatted_pairs: List[Tuple[str, str]] = []
        template = self._pick_dialog_timestamp_template(start_raw, end_raw)
        if template is not None and time_pairs:
            boundaries = [time_pairs[0][0]]
            boundaries.extend(pair[1] for pair in time_pairs)
            formatted_boundaries = [
                self._format_dialog_timestamp(boundary, template)
                for boundary in boundaries
            ]
            if all(value is not None for value in formatted_boundaries):
                formatted_pairs = [
                    (formatted_boundaries[index], formatted_boundaries[index + 1])
                    for index in range(len(time_pairs))
                ]

        can_estimate = len(formatted_pairs) == len(sub_dialogs)
        if can_estimate:
            formatted_seconds: List[Tuple[Optional[float], Optional[float]]] = [
                (
                    parse_time_to_seconds(start_time),
                    parse_time_to_seconds(end_time),
                )
                for start_time, end_time in formatted_pairs
            ]
            can_estimate = all(
                start_time is not None
                and end_time is not None
                and end_time > start_time
                for start_time, end_time in formatted_seconds
            )

        for index, sub_dialog in enumerate(sub_dialogs):
            sub_dialog["time_estimated"] = True
            if not can_estimate:
                sub_dialog["start_time"] = None
                sub_dialog["end_time"] = None
                sub_dialog["duration"] = None
                continue

            start_time, end_time = formatted_pairs[index]
            start_value = parse_time_to_seconds(start_time)
            end_value = parse_time_to_seconds(end_time)
            sub_dialog["start_time"] = start_time
            sub_dialog["end_time"] = end_time
            sub_dialog["duration"] = end_value - start_value

        if can_estimate:
            logger.warning(
                "Estimated dialog timestamps after splitting segment: "
                f"span_seconds={end_seconds - start_seconds:.2f} "
                f"text_length={len(dialog.get('text', ''))} "
                f"parts={len(sub_dialogs)}"
            )

    @staticmethod
    def _timestamp_template_decimal_places(template: Any) -> Optional[int]:
        """Return second-field decimal places for a usable clock template, else None."""
        if not isinstance(template, str):
            return None

        parts = template.strip().split(":")
        if len(parts) not in (2, 3) or any(
            not re.fullmatch(r"\d+(?:\.\d+)?", part) for part in parts
        ):
            return None

        second_template = parts[-1]
        if "." not in second_template:
            return 0
        return len(second_template.split(".", 1)[1])

    @classmethod
    def _pick_dialog_timestamp_template(
        cls, start_raw: Any, end_raw: Any
    ) -> Optional[str]:
        """Pick a single format template: the usable end with more decimal places."""
        best_template: Optional[str] = None
        best_places = -1
        for raw in (start_raw, end_raw):
            places = cls._timestamp_template_decimal_places(raw)
            if places is None:
                continue
            # Prefer higher precision so both endpoints remain expressible.
            if places > best_places:
                best_places = places
                best_template = raw.strip()
        return best_template

    @staticmethod
    def _format_dialog_timestamp(
        seconds: Optional[float], template: Any
    ) -> Optional[str]:
        """Format an interpolated timestamp without changing the source field type."""
        if seconds is None or not math.isfinite(seconds) or not isinstance(template, str):
            return None

        parts = template.strip().split(":")
        if len(parts) not in (2, 3) or any(
            not re.fullmatch(r"\d+(?:\.\d+)?", part) for part in parts
        ):
            return None

        second_template = parts[-1]
        has_fraction = "." in second_template
        decimal_places = (
            len(second_template.split(".", 1)[1]) if has_fraction else 0
        )
        scale = 10**decimal_places
        total_units = int(round(seconds * scale)) if has_fraction else int(seconds)
        whole_seconds, fractional_units = divmod(total_units, scale)
        hours, remainder = divmod(whole_seconds, 3600)
        minutes, second_value = divmod(remainder, 60)

        if len(parts) == 3:
            hour_width = max(2, len(parts[0]))
            minute_width = max(2, len(parts[1]))
            formatted = (
                f"{hours:0{hour_width}d}:{minutes:0{minute_width}d}:"
                f"{second_value:02d}"
            )
        else:
            minute_total = hours * 60 + minutes
            minute_width = max(2, len(parts[0]))
            formatted = f"{minute_total:0{minute_width}d}:{second_value:02d}"

        if has_fraction:
            formatted += f".{fractional_units:0{decimal_places}d}"
        return formatted

    def _split_by_sentences(self, text: str) -> List[str]:
        """按句子分割文本"""
        # 按中文句号、问号、感叹号分割，保留标点
        sentences = re.split(r'([。！？])', text)

        # 重组句子（将标点符号合并回前一个句子）
        result = []
        for i in range(0, len(sentences), 2):
            sentence = sentences[i]
            if i + 1 < len(sentences):
                sentence += sentences[i + 1]
            if sentence.strip():
                result.append(sentence)

        return result
