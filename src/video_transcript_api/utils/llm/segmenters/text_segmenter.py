"""无说话人文本分段器

基于现有 text_segmentation.py 的分段逻辑重构
"""

import re
from typing import List

from ...logging import setup_logger
from ..core.config import LLMConfig

logger = setup_logger(__name__)


class TextSegmenter:
    """无说话人文本分段器"""

    def __init__(self, config: LLMConfig):
        """初始化文本分段器

        Args:
            config: LLM 配置
        """
        self.config = config
        self.segment_size = config.segment_size
        self.max_segment_size = config.max_segment_size

    def segment(self, content: str) -> List[str]:
        """对纯文本内容进行分段

        Args:
            content: 文本内容

        Returns:
            分段后的文本列表
        """
        segments = []

        # 检测文本格式：判断是否为 CapsWriter 格式（短句换行，无标点符号）
        # 统计标点符号密度：每1000字符中的句号、问号、感叹号数量
        text_length = len(content)
        if text_length > 0:
            punctuation_count = (
                content.count('。') + content.count('！') + content.count('？') +
                content.count('!') + content.count('?')
            )
            punctuation_density = (punctuation_count / text_length) * 1000  # 每1000字符的标点数

            # 如果标点密度小于5（即每1000字符少于5个标点），认为是 CapsWriter 格式
            is_capswriter_format = punctuation_density < 5
        else:
            is_capswriter_format = False

        if is_capswriter_format:
            logger.info("Detected CapsWriter format, segmenting by lines")
            lines = [line.strip() for line in content.split('\n') if line.strip()]

            if len(lines) <= 1:
                logger.info("CapsWriter text lacks valid newlines, falling back to sentence segmentation")
                segments = self._segment_by_sentences(content)
                logger.info(f"Text segmentation completed: {len(segments)} segments")
                return segments

            current_segment = ""
            for line in lines:
                current_segment = self._append_fragment(line, segments, current_segment)
        else:
            segments = self._segment_by_sentences(content)
            logger.info(f"Text segmentation completed: {len(segments)} segments")
            return segments

        if current_segment.strip():
            segments.append(current_segment.strip())

        logger.info(f"Text segmentation completed: {len(segments)} segments")
        return segments

    def _segment_by_sentences(self, content: str) -> List[str]:
        """按标点符号分段"""
        segments = []
        sentences = re.split(r'[。！？!?\.…，,；;：:\n]', content)

        current_segment = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            fragment = sentence + "。"
            current_segment = self._append_fragment(fragment, segments, current_segment)

        if current_segment.strip():
            segments.append(current_segment.strip())

        return segments

    def _append_fragment(self, fragment: str, segments: List[str], current_segment: str) -> str:
        """确保单个片段不会超过 max_segment_size，并根据 segment_size 及时落盘"""
        fragment = fragment.strip()
        if not fragment:
            return current_segment

        while fragment:
            available = self.max_segment_size - len(current_segment)
            if available <= 0:
                if current_segment.strip():
                    segments.append(current_segment.strip())
                current_segment = ""
                available = self.max_segment_size

            take = min(len(fragment), available)
            current_segment += fragment[:take]
            fragment = fragment[take:]

            if len(current_segment) >= self.max_segment_size:
                segments.append(current_segment.strip())
                current_segment = ""

        if len(current_segment) >= self.segment_size:
            segments.append(current_segment.strip())
            current_segment = ""

        return current_segment
