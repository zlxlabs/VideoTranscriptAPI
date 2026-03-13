"""
URL 解析器模块

提供统一的 URL 解析功能，用于提取平台信息和视频 ID，
支持多种平台和短链接自动解析。
"""

import re
import hashlib
import requests
from dataclasses import dataclass
from typing import Optional
from .logging import setup_logger

# 创建日志记录器
logger = setup_logger("url_parser")


@dataclass
class ParsedURL:
    """
    解析后的 URL 信息

    Attributes:
        platform: 平台名称 (youtube/bilibili/douyin/xiaohongshu/xiaoyuzhou/generic)
        video_id: 视频ID (唯一标识)
        normalized_url: 规范化的URL（长链接格式）
        is_short_url: 是否为短链接
        original_url: 原始URL
    """
    platform: str
    video_id: str
    normalized_url: str
    is_short_url: bool
    original_url: str


class URLParser:
    """
    统一的 URL 解析器

    功能：
    1. 识别视频平台（YouTube、Bilibili、抖音等）
    2. 提取视频 ID（唯一标识）
    3. 解析短链接（HTTP HEAD 请求获取长链接）
    4. 规范化 URL 格式

    使用示例：
        parser = URLParser()
        parsed = parser.parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        print(f"Platform: {parsed.platform}, Video ID: {parsed.video_id}")
    """

    # 平台 URL 模式（正则表达式）
    # 注意：使用 re.IGNORECASE 标志使匹配不区分大小写
    PATTERNS = {
        'youtube': [
            r'(?:youtube\.com/watch\?.*?v=|youtu\.be/)([a-zA-Z0-9_-]+)',  # 标准和短链接，支持查询参数
            r'youtube\.com/shorts/([a-zA-Z0-9_-]+)',  # Shorts
            r'youtube\.com/live/([a-zA-Z0-9_-]+)',  # Live
            r'youtube\.com/embed/([a-zA-Z0-9_-]+)',  # Embed
            r'[?&]v=([a-zA-Z0-9_-]+)',  # 查询参数
        ],
        'bilibili': [
            r'bilibili\.com/video/(BV[a-zA-Z0-9]+)',  # BV号
            r'bilibili\.com/video/(av\d+)',  # AV号
            r'b23\.tv/(\w+)',  # 短链接（需要解析）
        ],
        'douyin': [
            r'douyin\.com/(?:video|note)/(\d+)',  # 标准链接
            r'v\.douyin\.com/(\w+)',  # 短链接（需要解析）
        ],
        'xiaoyuzhou': [
            r'xiaoyuzhoufm\.com/episode/([a-z0-9]+)',  # 小宇宙播客
        ],
        'xiaohongshu': [
            r'xiaohongshu\.com/(?:explore|discovery/item|items)/(\w+)',  # 主域名
            r'xhslink\.com/(\w+)',  # 短链接（需要解析）
        ],
    }

    # 短链接域名映射
    SHORT_URL_DOMAINS = {
        'b23.tv': 'bilibili',
        'youtu.be': 'youtube',
        'v.douyin.com': 'douyin',
        'xhslink.com': 'xiaohongshu',
    }

    def parse(self, url: str, timeout: int = 10) -> ParsedURL:
        """
        解析 URL，提取平台和视频 ID

        策略：
        1. 检测是否为短链接域名
        2. 如果是短链接，先用 HTTP HEAD 解析成长链接
        3. 使用正则表达式提取 platform 和 video_id
        4. 返回 ParsedURL 对象

        Args:
            url: 要解析的 URL
            timeout: 短链接解析超时时间（秒），默认 10 秒

        Returns:
            ParsedURL: 解析结果

        Raises:
            ValueError: URL 格式无效
        """
        if not url or not isinstance(url, str):
            raise ValueError("URL must be a non-empty string")

        original_url = url.strip()
        logger.info(f"开始解析 URL: {original_url}")

        # 步骤1: 检测并解析短链接
        is_short_url = self._is_short_url(original_url)
        if is_short_url:
            logger.info(f"检测到短链接，尝试解析: {original_url}")
            normalized_url = self._resolve_short_url(original_url, timeout)
            if normalized_url != original_url:
                logger.info(f"短链接解析成功: {normalized_url}")
            else:
                logger.warning(f"短链接解析失败，使用原始 URL: {original_url}")
        else:
            normalized_url = original_url

        # 步骤2: 正则匹配提取 platform 和 video_id
        platform, video_id = self._extract_platform_and_id(normalized_url)

        # 步骤3: 如果是 generic 平台，生成哈希 ID
        if platform == 'generic':
            video_id = self._generate_hash_id(original_url)
            logger.info(f"无法识别平台，使用通用标识: platform={platform}, video_id={video_id}")
        else:
            logger.info(f"URL 解析成功: platform={platform}, video_id={video_id}")

        return ParsedURL(
            platform=platform,
            video_id=video_id,
            normalized_url=normalized_url,
            is_short_url=is_short_url,
            original_url=original_url
        )

    def _is_short_url(self, url: str) -> bool:
        """
        检测是否为已知的短链接域名

        Args:
            url: 要检测的 URL

        Returns:
            bool: 是否为短链接
        """
        for domain in self.SHORT_URL_DOMAINS:
            if domain in url:
                return True
        return False

    def _resolve_short_url(self, url: str, timeout: int = 10) -> str:
        """
        解析短链接（HTTP HEAD 请求）

        SSRF 防护：解析后会验证目标 URL 不指向私有地址。

        Args:
            url: 短链接 URL
            timeout: 请求超时时间（秒）

        Returns:
            str: 解析后的长链接，失败则返回原始 URL
        """
        try:
            logger.debug(f"发送 HTTP HEAD 请求解析短链接: {url}")
            response = requests.head(url, allow_redirects=True, timeout=timeout)
            resolved_url = response.url

            if resolved_url and resolved_url != url:
                # SSRF 防护：验证解析后的 URL 安全性
                try:
                    from .url_validator import validate_url_safe
                    validate_url_safe(resolved_url)
                except Exception as e:
                    logger.warning(f"Short URL resolved to unsafe target: {resolved_url}, reason: {e}")
                    return url

                logger.debug(f"短链接解析成功: {url} -> {resolved_url}")
                return resolved_url
            else:
                logger.warning(f"短链接解析未发生跳转: {url}")
                return url

        except requests.exceptions.Timeout:
            logger.warning(f"短链接解析超时 ({timeout}s): {url}")
            return url
        except requests.exceptions.RequestException as e:
            logger.warning(f"短链接解析失败: {url}, 错误: {e}")
            return url
        except Exception as e:
            logger.error(f"短链接解析发生未知错误: {url}, 错误: {e}")
            return url

    def _extract_platform_and_id(self, url: str) -> tuple[str, str]:
        """
        使用正则表达式提取 platform 和 video_id

        Args:
            url: 要提取的 URL（应该是长链接格式）

        Returns:
            tuple: (platform, video_id)，无法识别时返回 ('generic', '')
        """
        for platform, patterns in self.PATTERNS.items():
            for pattern in patterns:
                # 使用 IGNORECASE 标志使匹配不区分大小写
                match = re.search(pattern, url, re.IGNORECASE)
                if match:
                    video_id = match.group(1)
                    # 移除可能的片段标识符（#）
                    if '#' in video_id:
                        video_id = video_id.split('#')[0]
                    # 移除可能的查询参数（&）
                    if '&' in video_id:
                        video_id = video_id.split('&')[0]
                    logger.debug(f"正则匹配成功: platform={platform}, pattern={pattern}, video_id={video_id}")
                    return platform, video_id

        # 未匹配到任何平台
        logger.debug(f"未匹配到任何已知平台: {url}")
        return 'generic', ''

    def _generate_hash_id(self, url: str) -> str:
        """
        为无法识别的 URL 生成哈希 ID

        Args:
            url: 原始 URL

        Returns:
            str: 16 位 MD5 哈希
        """
        hash_id = hashlib.md5(url.encode()).hexdigest()[:16]
        logger.debug(f"生成哈希 ID: {hash_id}")
        return hash_id

    def extract_platform(self, url: str) -> str:
        """
        仅提取平台名称（轻量级操作，不解析短链接）

        Args:
            url: 要检测的 URL

        Returns:
            str: 平台名称，无法识别时返回 'generic'
        """
        for platform, patterns in self.PATTERNS.items():
            # 跳过短链接模式（避免误判）
            for pattern in patterns:
                if re.search(pattern, url, re.IGNORECASE):
                    return platform

        # 检查是否包含平台域名关键字（不依赖正则）
        url_lower = url.lower()
        if 'youtube.com' in url_lower or 'youtu.be' in url_lower:
            return 'youtube'
        elif 'bilibili.com' in url_lower or 'b23.tv' in url_lower:
            return 'bilibili'
        elif 'douyin.com' in url_lower:
            return 'douyin'
        elif 'xiaoyuzhoufm.com' in url_lower:
            return 'xiaoyuzhou'
        elif 'xiaohongshu.com' in url_lower or 'xhslink.com' in url_lower:
            return 'xiaohongshu'

        return 'generic'


# 便捷函数（模块级）

def parse_url(url: str, timeout: int = 10) -> ParsedURL:
    """
    解析 URL 的便捷函数（无需创建 URLParser 实例）

    Args:
        url: 要解析的 URL
        timeout: 短链接解析超时时间（秒）

    Returns:
        ParsedURL: 解析结果
    """
    parser = URLParser()
    return parser.parse(url, timeout)


def extract_platform(url: str) -> str:
    """
    提取平台名称的便捷函数

    Args:
        url: 要检测的 URL

    Returns:
        str: 平台名称
    """
    parser = URLParser()
    return parser.extract_platform(url)
