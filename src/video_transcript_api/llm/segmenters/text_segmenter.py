"""无说话人文本分段器

基于现有 text_segmentation.py 的分段逻辑重构
"""

import re
from typing import List

from ...utils.logging import setup_logger
from ..core.config import LLMConfig

logger = setup_logger(__name__)


# 断点字符集合与 design.md 3.1 的锁定决策一致：不认 ASCII `.`。
# 理由：小数（3.14）、版本号（v1.2.3）、缩写（e.g.）、URL 在 `.` 处断开会撕裂正文；
# 且本仓另外三处（capswriter_client / dialog_segmenter / paragraphize）都不认 `.`。
_SENTENCE_BREAK_RE = re.compile(r"([。！？!?…，,；;：:\n])")


class TextSegmenter:
    """无说话人文本分段器"""

    def __init__(self, config: LLMConfig):
        """初始化文本分段器

        Args:
            config: LLM 配置

        Raises:
            ValueError: ``max_segment_size`` 不是正数。必须响亮失败而不是 clamp——
                clamp 会把配置错误静默纠正成另一个宽度，正是「静默出错」那一档；
                而 ``_append_fragment`` 的 ``while fragment:`` 在上限 <= 0 时
                ``take = min(len(fragment), available)`` 恒为 0，循环变量不单调，
                进程会挂死且 ``segments`` 无界增长。
        """
        max_segment_size = config.max_segment_size
        if max_segment_size <= 0:
            raise ValueError(
                "TEXT_SEGMENT_INVALID_MAX_SIZE condition=max_len_not_positive "
                f"func=TextSegmenter.__init__ max_segment_size={max_segment_size}"
            )

        self.config = config
        self.segment_size = config.segment_size
        # 注意：``segment_size`` 故意不做取值校验。它只参与
        # ``if len(current_segment) >= self.segment_size`` 的落盘判断，
        # 不参与任何循环的终止条件，因此 <= 0 只会让落盘更频繁，不会挂死。
        self.max_segment_size = max_segment_size

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
        """按标点符号分段：只切不换，正文逐字守恒。

        split 用带捕获组的正则，断点字符原样跟随上一片段，**不丢弃也不注入**任何标点
        （此前会丢掉全部原标点并给每片补一个 `。`，把 ``3.14`` 改成 ``3。14``、
        ``https://a.b/c`` 改成 ``https。//a。b/c``）。长度上限由 ``_append_fragment``
        逐字符拼装保证，不依赖句号。
        """
        segments = []
        # re.split 带一个捕获组时，切片形态是 [文本, 断点, 文本, 断点, ..., 文本]。
        parts = _SENTENCE_BREAK_RE.split(content)

        # 先把「文本 + 其后的断点」拼成整片：连续断点（空文本 + 非空断点）挂回上一片，
        # 否则句末的 ``！`` / ``？`` 会被当成孤立断点丢掉。
        fragments = []
        for index in range(0, len(parts), 2):
            sentence = parts[index].strip()
            tail = parts[index + 1] if index + 1 < len(parts) else ""
            if sentence:
                fragments.append(sentence + tail)
            elif tail:
                if fragments:
                    fragments[-1] += tail
                else:
                    fragments.append(tail)

        current_segment = ""
        for fragment in fragments:
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
