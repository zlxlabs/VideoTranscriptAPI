"""
敏感词库管理器

负责：
1. 从配置的URL列表下载敏感词库
2. 合并所有词库并去重
3. 保存到本地缓存
4. 下载失败时使用本地缓存
"""

import os
import requests
from typing import Set, List
from ..utils.logging import setup_logger

logger = setup_logger("sensitive_words_manager")


class SensitiveWordsManager:
    """敏感词库管理器"""

    def __init__(self, config: dict):
        """
        初始化敏感词库管理器

        Args:
            config: 风控配置字典
        """
        self.urls = config.get("sensitive_word_urls", [])
        self.cache_file = config.get("cache_file", "./data/risk_control/sensitive_words.txt")
        self.sensitive_words: Set[str] = set()

        # 确保缓存目录存在
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)

    def load_words(self) -> Set[str]:
        """
        加载敏感词库（服务启动时调用）

        Returns:
            敏感词集合
        """
        logger.info("Starting to load sensitive words from URLs...")

        # 尝试从URL下载
        words_from_urls = self._download_from_urls()

        if words_from_urls:
            # 下载成功，保存到缓存
            self.sensitive_words = words_from_urls
            self._save_to_cache(self.sensitive_words)
            logger.info(f"Successfully loaded {len(self.sensitive_words)} sensitive words from URLs")
        else:
            # 下载失败，尝试从缓存加载
            logger.warning("Failed to download from URLs, trying to load from cache...")
            cache_words = self._load_from_cache()

            if cache_words:
                self.sensitive_words = cache_words
                logger.info(f"Successfully loaded {len(self.sensitive_words)} sensitive words from cache")
            else:
                logger.error("No sensitive words loaded! Risk control will be disabled.")
                self.sensitive_words = set()

        return self.sensitive_words

    def get_words(self) -> Set[str]:
        """
        获取当前敏感词集合

        Returns:
            敏感词集合
        """
        return self.sensitive_words

    def _download_from_urls(self) -> Set[str]:
        """
        从URL列表下载敏感词库

        Returns:
            合并后的敏感词集合，失败返回空集合
        """
        all_words = set()
        success_count = 0

        for url in self.urls:
            try:
                logger.info(f"Downloading sensitive words from: {url}")
                response = requests.get(url, timeout=30)
                response.raise_for_status()

                # 解析文本内容，每行一个敏感词
                content = response.text
                words = self._parse_words(content)

                all_words.update(words)
                success_count += 1
                logger.info(f"Successfully downloaded {len(words)} words from {url}")

            except requests.exceptions.Timeout:
                logger.error(f"Timeout while downloading from {url}")
            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to download from {url}: {e}")
            except Exception as e:
                logger.exception(f"Unexpected error while downloading from {url}: {e}")

        if success_count == 0:
            logger.error("Failed to download from all URLs")
            return set()

        logger.info(f"Successfully downloaded from {success_count}/{len(self.urls)} URLs, total {len(all_words)} unique words")
        return all_words

    def _parse_words(self, content: str) -> Set[str]:
        """
        解析敏感词文本内容

        Args:
            content: 文本内容

        Returns:
            敏感词集合
        """
        words = set()

        for line in content.splitlines():
            word = line.strip()
            if word and not word.startswith("#"):  # 跳过空行和注释行
                # 转为小写存储（不区分大小写）
                words.add(word.lower())

        return words

    def _save_to_cache(self, words: Set[str]) -> bool:
        """
        保存敏感词库到本地缓存

        Args:
            words: 敏感词集合

        Returns:
            是否成功
        """
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                for word in sorted(words):  # 排序后保存，便于查看
                    f.write(word + '\n')

            logger.info(f"Successfully saved {len(words)} words to cache: {self.cache_file}")
            return True

        except Exception as e:
            logger.exception(f"Failed to save cache to {self.cache_file}: {e}")
            return False

    def _load_from_cache(self) -> Set[str]:
        """
        从本地缓存加载敏感词库

        Returns:
            敏感词集合，失败返回空集合
        """
        if not os.path.exists(self.cache_file):
            logger.warning(f"Cache file not found: {self.cache_file}")
            return set()

        try:
            words = set()
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                for line in f:
                    word = line.strip()
                    if word:
                        words.add(word.lower())

            logger.info(f"Successfully loaded {len(words)} words from cache: {self.cache_file}")
            return words

        except Exception as e:
            logger.exception(f"Failed to load cache from {self.cache_file}: {e}")
            return set()
