"""
文本消敏处理器

负责：
1. 提取文本中的所有URL并标记位置
2. 在非URL部分检测敏感词（不区分大小写）
3. 移除敏感词或替换为风控提示
"""

import re
from typing import Set, List, Tuple, Dict
from ..utils.logging import setup_logger

logger = setup_logger("text_sanitizer")

# URL匹配正则表达式
URL_PATTERN = re.compile(
    r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
)

# 风控提示文本
RISK_WARNING = "内容风险，请通过url查看"


class TextSanitizer:
    """文本消敏处理器"""

    def __init__(self, sensitive_words: Set[str]):
        """
        初始化文本消敏处理器

        Args:
            sensitive_words: 敏感词集合（已转为小写）
        """
        self.sensitive_words = sensitive_words
        logger.info(f"TextSanitizer initialized with {len(self.sensitive_words)} sensitive words")

    def sanitize(self, text: str, text_type: str = "general") -> Dict[str, any]:
        """
        对文本进行敏感词检测和消敏处理

        Args:
            text: 待处理的文本
            text_type: 文本类型
                - "summary": 总结文本（如有敏感词则整体替换为风控提示）
                - "title": 标题（移除敏感词后取前6字符）
                - "author": 作者（移除敏感词后取前6字符）
                - "general": 普通文本（移除所有敏感词）

        Returns:
            {
                "has_sensitive": bool,      # 是否包含敏感词
                "sensitive_words": list,    # 检测到的敏感词列表
                "sanitized_text": str       # 消敏后的文本
            }
        """
        if not text:
            return {
                "has_sensitive": False,
                "sensitive_words": [],
                "sanitized_text": text
            }

        # 1. 提取所有URL及其位置
        url_ranges = self._extract_url_ranges(text)

        # 2. 在非URL部分检测敏感词
        found_words = self._detect_sensitive_words(text, url_ranges)

        if not found_words:
            # 没有敏感词，直接返回原文
            return {
                "has_sensitive": False,
                "sensitive_words": [],
                "sanitized_text": text
            }

        # 3. 根据文本类型进行不同的消敏处理
        if text_type == "summary":
            sanitized_text = RISK_WARNING
        elif text_type in ["title", "author"]:
            # 移除敏感词后取前6字符
            text_without_sensitive = self._remove_sensitive_words(text, found_words, url_ranges)
            sanitized_text = text_without_sensitive[:6]
        else:  # general
            sanitized_text = self._remove_sensitive_words(text, found_words, url_ranges)

        logger.warning(f"Detected {len(found_words)} sensitive words in {text_type} text: {found_words[:5]}")

        return {
            "has_sensitive": True,
            "sensitive_words": found_words,
            "sanitized_text": sanitized_text
        }

    def _extract_url_ranges(self, text: str) -> List[Tuple[int, int]]:
        """
        提取文本中所有URL的位置范围

        Args:
            text: 文本内容

        Returns:
            [(start_pos, end_pos), ...] URL位置范围列表
        """
        ranges = []
        for match in URL_PATTERN.finditer(text):
            ranges.append((match.start(), match.end()))

        if ranges:
            logger.debug(f"Found {len(ranges)} URLs in text")

        return ranges

    def _is_in_url_range(self, start: int, end: int, url_ranges: List[Tuple[int, int]]) -> bool:
        """
        检查指定位置是否在URL范围内

        Args:
            start: 起始位置
            end: 结束位置
            url_ranges: URL范围列表

        Returns:
            是否在URL范围内
        """
        for url_start, url_end in url_ranges:
            # 检查是否有重叠
            if not (end <= url_start or start >= url_end):
                return True
        return False

    def _detect_sensitive_words(self, text: str, url_ranges: List[Tuple[int, int]]) -> List[str]:
        """
        在非URL部分检测敏感词

        Args:
            text: 文本内容
            url_ranges: URL范围列表

        Returns:
            检测到的敏感词列表（原始形式，去重）
        """
        found_words = set()
        text_lower = text.lower()

        for sensitive_word in self.sensitive_words:
            # 在文本中查找所有匹配位置
            start_pos = 0
            while True:
                pos = text_lower.find(sensitive_word, start_pos)
                if pos == -1:
                    break

                # 检查该位置是否在URL范围内
                word_end = pos + len(sensitive_word)
                if not self._is_in_url_range(pos, word_end, url_ranges):
                    # 找到一个不在URL中的敏感词
                    # 记录原始文本中的形式（保持大小写）
                    original_word = text[pos:word_end]
                    found_words.add(original_word)
                    break  # 同一个敏感词只记录一次

                start_pos = pos + 1

        return list(found_words)

    def _remove_sensitive_words(self, text: str, sensitive_words: List[str], url_ranges: List[Tuple[int, int]]) -> str:
        """
        从文本中移除所有敏感词（不在URL中的）

        Args:
            text: 原始文本
            sensitive_words: 需要移除的敏感词列表（原始形式）
            url_ranges: URL范围列表

        Returns:
            移除敏感词后的文本
        """
        # 收集所有需要移除的位置
        remove_ranges = []

        for sensitive_word in sensitive_words:
            # 在文本中查找所有该敏感词的位置（不区分大小写）
            start_pos = 0
            text_lower = text.lower()
            word_lower = sensitive_word.lower()

            while True:
                pos = text_lower.find(word_lower, start_pos)
                if pos == -1:
                    break

                word_end = pos + len(sensitive_word)

                # 检查是否在URL范围内
                if not self._is_in_url_range(pos, word_end, url_ranges):
                    remove_ranges.append((pos, word_end))

                start_pos = pos + 1

        # 按位置倒序排列，从后往前移除，避免位置偏移
        remove_ranges.sort(key=lambda x: x[0], reverse=True)

        # 执行移除
        result = text
        for start, end in remove_ranges:
            result = result[:start] + result[end:]

        return result
