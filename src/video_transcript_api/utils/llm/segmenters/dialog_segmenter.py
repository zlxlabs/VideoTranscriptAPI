"""有说话人文本分段器

基于 structured_calibrator.py 的 _intelligent_chunking 逻辑重构
"""

import re
from typing import List, Dict, Any

from ...logging import setup_logger
from ..core.config import LLMConfig

logger = setup_logger(__name__)


class DialogSegmenter:
    """有说话人文本分段器"""

    def __init__(self, config: LLMConfig):
        """初始化对话分段器

        Args:
            config: LLM 配置
        """
        self.config = config
        self.min_chunk_length = config.min_chunk_length
        self.max_chunk_length = config.max_chunk_length
        self.preferred_chunk_length = config.preferred_chunk_length

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
            if chunks and len("".join(d.get("text", "") for d in current_chunk)) < self.min_chunk_length:
                chunks[-1].extend(current_chunk)
            else:
                chunks.append(current_chunk)

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

        logger.debug(f"Long dialog split: length {len(text)} -> {len(sub_dialogs)} fragments")
        return sub_dialogs

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
