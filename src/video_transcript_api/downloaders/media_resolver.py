"""MediaResolverDownloader：把抖音/小红书解析外包给 MediaResolverAPI。

v1 收窄范围：只接管抖音 + 小红书（不抽基类、不加平台）。本下载器：
- 用**归一化 url** 作缓存 key（绕开"先有 video_id 才能查"的鸡蛋悖论），
  一次 resolve 同时喂 `_fetch_metadata` 与 `_fetch_download_info`（FORK1-A）。
- resolver 只返回 video_url 视频直链，下载仍走基类 `download_file()`。
- 对 resolver 返回的直链下载前做 SSRF 校验（P0-2）。
- 下载遇 403/失效 → force_refresh 重解析再下一次（P0-3 / FORK4-A），仍失败抛错。
- 无字幕：`get_subtitle` 返回 None。

详见 docs/designs/media-resolver-integration.md。
"""

import math
import os
from typing import Dict, Optional
from urllib.parse import urlparse, urlunparse

from .base import BaseDownloader
from .models import VideoMetadata, DownloadInfo
from .media_resolver_client import MediaResolverClient
from ..errors import DownloadFailedError, ResolverResponseError
from ..utils.logging import setup_logger
from ..utils.url_validator import validate_url_safe, URLValidationError

logger = setup_logger("media_resolver_downloader")

# v1 接管的平台域名（抖音 + 小红书）
_SUPPORTED_DOMAINS = (
    "douyin.com",
    "v.douyin.com",
    "xiaohongshu.com",
    "xhslink.com",
)


class MediaResolverDownloader(BaseDownloader):
    """通过 MediaResolverAPI 解析抖音/小红书的下载器。"""

    def __init__(self):
        super().__init__()
        mr = self.config.get("media_resolver", {}) or {}
        self.client = MediaResolverClient(
            base_url=mr.get("base_url", ""),
            api_key=mr.get("api_key", ""),
            timeout=mr.get("timeout", 30),
            max_retries=mr.get("max_retries", 2),
        )
        # 归一化 url -> resolve 响应 data（一次网络调用喂两个 _fetch_*）
        self._resolve_cache: Dict[str, dict] = {}
        # video_url -> 归一化页面 url（下载 403 时反查以 force_refresh 重解析）
        self._video_url_to_page: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # 路由
    # ------------------------------------------------------------------ #
    def can_handle(self, url: str) -> bool:
        """v1 仅接管抖音/小红书。"""
        if not url:
            return False
        return any(domain in url for domain in _SUPPORTED_DOMAINS)

    def extract_video_id(self, url: str) -> str:
        """best-effort 提取 video_id（仅供日志；缓存 key 用归一化 url）。

        优先用已缓存 resolve 响应里的 video_id；否则从 url 粗提数字 id，
        取不到则回退归一化 url。不抛异常（解析交给 resolver）。
        """
        data = self._resolve_cache.get(self._normalize_url(url))
        if data and data.get("video_id"):
            return str(data["video_id"])
        import re

        m = re.search(r"(?:video|note|explore|item)/([A-Za-z0-9]+)", url) or re.search(
            r"/(\d{6,})", url
        )
        return m.group(1) if m else self._normalize_url(url)

    # ------------------------------------------------------------------ #
    # 缓存 key：归一化 url（绕开 extract_video_id 鸡蛋悖论）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_url(url: str) -> str:
        """归一化 url 作缓存 key：去空白、scheme/host 小写、去 fragment 与末尾斜杠。"""
        u = (url or "").strip()
        try:
            p = urlparse(u)
            scheme = (p.scheme or "").lower()
            netloc = (p.netloc or "").lower()
            path = p.path.rstrip("/")
            return urlunparse((scheme, netloc, path, p.params, p.query, ""))
        except Exception:
            return u

    def _resolve(self, url: str, force_refresh: bool = False) -> dict:
        """解析 url 并缓存（同一归一化 url 只调用一次 resolver）。"""
        key = self._normalize_url(url)
        if not force_refresh and key in self._resolve_cache:
            logger.info(f"使用缓存 resolve 结果: {key}")
            return self._resolve_cache[key]

        data = self.client.resolve(url, translate=False, force_refresh=force_refresh)
        self._resolve_cache[key] = data
        video_url = data.get("video_url")
        if video_url:
            self._video_url_to_page[video_url] = key
        return data

    # ------------------------------------------------------------------ #
    # 覆写两个 public 方法：用归一化 url 作缓存 key
    # ------------------------------------------------------------------ #
    def get_metadata(self, url: str) -> VideoMetadata:
        key = self._normalize_url(url)
        if key in self._metadata_cache:
            logger.info(f"使用缓存元数据: {key}")
            return self._metadata_cache[key]
        metadata = self._fetch_metadata(url, "")
        self._metadata_cache[key] = metadata
        return metadata

    def get_download_info(self, url: str) -> DownloadInfo:
        key = self._normalize_url(url)
        if key in self._download_info_cache:
            logger.info(f"使用缓存下载信息: {key}")
            return self._download_info_cache[key]
        download_info = self._fetch_download_info(url, "")
        self._download_info_cache[key] = download_info
        return download_info

    # ------------------------------------------------------------------ #
    # 派生映射
    # ------------------------------------------------------------------ #
    def _fetch_metadata(self, url: str, video_id: str) -> VideoMetadata:
        data = self._resolve(url)
        duration = data.get("duration")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError, OverflowError):
            # OverflowError: resolver 响应经 response.json() 反序列化，JSON 整数无
            # 精度上限，恶意/畸形响应可能带一个如 10**400 的天文数字 int——
            # float() 转换会抛 OverflowError（而非静默变 inf），与既有的
            # TypeError/ValueError 同等对待，diagnostic 记 duration=None
            duration = None
        else:
            # float() 转换成功不代表数值合法：
            # - 字符串路径的溢出不抛异常——float("1e309") 静默变成 inf，绕开
            #   上面的 except；数值路径同理（JSON 里的 1e309 字面量本身在反
            #   序列化时就已经是 inf）。用 math.isfinite 兜底非有限值。
            # - 负数同样非法：时长不可能为负，视为脏数据与解析失败同等处理。
            if duration is not None and not (math.isfinite(duration) and duration >= 0):
                duration = None
        return VideoMetadata(
            video_id=str(data.get("video_id") or video_id or ""),
            platform=data.get("platform", ""),
            title=data.get("title", "") or "",
            author=data.get("author_name") or data.get("author") or "",
            description=data.get("description", "") or "",
            duration=duration,
            extra={
                "provider": data.get("provider"),
                "width": data.get("width"),
                "height": data.get("height"),
            },
        )

    def _fetch_download_info(self, url: str, video_id: str) -> DownloadInfo:
        data = self._resolve(url)
        video_url = data.get("video_url")
        if not video_url:
            raise ResolverResponseError(f"解析结果缺少 video_url: {url}")

        # P0-2：resolver 返回的直链下载前做 SSRF 校验
        try:
            validate_url_safe(video_url)
        except URLValidationError as e:
            logger.error(f"resolver 返回的 video_url 未通过 SSRF 校验，已阻止下载: {e}")
            raise ResolverResponseError(f"解析返回的直链不安全，已阻止下载: {e}")

        file_ext = self._infer_file_ext(video_url)
        platform = data.get("platform", "media")
        vid = str(data.get("video_id") or video_id or "media")
        filename = f"{platform}_{vid}.{file_ext}"
        return DownloadInfo(
            download_url=video_url,
            file_ext=file_ext,
            filename=filename,
            extra={"provider": data.get("provider")},
        )

    @staticmethod
    def _infer_file_ext(video_url: str) -> str:
        """contract #3：优先取 video_url 路径后缀；取不到默认 mp4。"""
        try:
            path = urlparse(video_url).path
            ext = os.path.splitext(path)[1].lstrip(".").lower()
            # 仅接受常见媒体后缀，避免把 .html/.php 等误当扩展名
            if ext in {"mp4", "m4a", "mp3", "webm", "mov", "flv", "mkv"}:
                return ext
        except Exception:
            pass
        return "mp4"

    def get_subtitle(self, url):
        """resolver 不提供字幕，返回 None。"""
        return None

    # ------------------------------------------------------------------ #
    # 下载：403/失效 → force_refresh 重解析再下一次（P0-3 / FORK4-A）
    # ------------------------------------------------------------------ #
    def download_file(self, url, filename, max_retries: int = 3):
        """覆写下载：常规下载失败后，对 resolver 直链做一次 force_refresh 重解析重下。

        基类 download_file 对 403/失效直链返回 None（不抛）。本方法在拿到 None 时，
        反查该 video_url 对应的页面 url，force_refresh 重解析得到新直链再下一次；
        仍失败则抛 DownloadFailedError（爆炸半径仅限本类）。
        """
        local_file = super().download_file(url, filename, max_retries=max_retries)
        if local_file:
            return local_file

        page_key = self._video_url_to_page.get(url)
        if not page_key:
            # 非 resolver 直链（无法重解析），保持基类语义返回 None
            logger.warning(f"download 失败且无法反查页面 url，放弃重解析: {url[:100]}")
            raise DownloadFailedError(f"resolver 直链下载失败: {url[:100]}")

        logger.warning(f"直链下载失败，force_refresh 重解析后重试: {page_key}")
        try:
            data = self._resolve(page_key, force_refresh=True)
        except Exception as e:
            logger.error(f"force_refresh 重解析失败: {e}")
            raise DownloadFailedError(f"重解析失败: {e}")

        fresh_url = data.get("video_url")
        if not fresh_url:
            raise DownloadFailedError("重解析未返回 video_url")

        # 刷新派生缓存，保证后续走新直链
        new_key = self._normalize_url(page_key)
        self._download_info_cache.pop(new_key, None)

        try:
            validate_url_safe(fresh_url)
        except URLValidationError as e:
            raise DownloadFailedError(f"重解析直链不安全: {e}")

        local_file = super().download_file(fresh_url, filename, max_retries=max_retries)
        if local_file:
            return local_file
        raise DownloadFailedError(f"重解析后仍下载失败: {fresh_url[:100]}")
