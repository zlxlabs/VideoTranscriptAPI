"""MediaResolverDownloader：把抖音/小红书/视频号/X(Twitter)解析外包给 MediaResolverAPI。

接管平台：抖音 + 小红书 + 微信视频号 + X(Twitter)。本下载器：
- 用**归一化 url** 作缓存 key（绕开"先有 video_id 才能查"的鸡蛋悖论），
  一次 resolve 同时喂 `_fetch_metadata` 与 `_fetch_download_info`（FORK1-A）。
- resolver 返回 video_url 视频直链（视频号为流式代理端点），下载仍走基类 `download_file()`。
- 对 resolver 返回的直链下载前做 SSRF 校验（P0-2）。
- 下载 resolver 流式端点时携带 X-API-Key 头，第三方 CDN 直链不携带该头。
- 下载遇 403/失效 → force_refresh 重解析再下一次（P0-3 / FORK4-A），仍失败抛错。
- 无字幕：`get_subtitle` 返回 None。

详见 docs/designs/media-resolver-integration.md。
"""

import base64
import math
import os
import re
from typing import Dict, Optional
from urllib.parse import urlparse, urlunparse

import requests

from .base import BaseDownloader
from .models import VideoMetadata, DownloadInfo
from .media_resolver_client import MediaResolverClient
from ..errors import DownloadFailedError, ResolverResponseError
from ..utils.logging import setup_logger
from ..utils.url_validator import validate_url_safe, URLValidationError

logger = setup_logger("media_resolver_downloader")

# 接管的平台域名（抖音 + 小红书 + 微信视频号 + Twitter）
_SUPPORTED_DOMAINS = (
    "douyin.com",
    "v.douyin.com",
    "xiaohongshu.com",
    "xhslink.com",
    "weixin.qq.com",
    "twitter.com",
)

# 主机边界匹配 x.com，避免误判 notx.com / x.com.evil.com / ?q=x.com
_X_DOMAIN_RE = re.compile(
    r"(?:^|://|//|@|[\s])(?:[a-zA-Z0-9-]+\.)*x\.com(?::\d+)?(?:[/?#\s]|$)",
    re.IGNORECASE,
)

# 视频号流式端点 path（不含 /direct）；命中则走 CDN 直连拼接
_WECHAT_STREAM_PATH_RE = re.compile(
    r"^/api/stream/wechat_channels/([A-Za-z0-9]{1,64})$"
)
# /direct 换新链上限（不含首次拉取）
_WECHAT_DIRECT_MAX_REFRESHES = 5
_WECHAT_CDN_TIMEOUT = (10, 60)
_WECHAT_CDN_RETRY_STATUSES = frozenset({401, 403, 404, 410})
_MP4_FTYP_MAGIC = b"ftyp"


class MediaResolverDownloader(BaseDownloader):
    """通过 MediaResolverAPI 解析抖音/小红书/微信视频号/X(Twitter)的下载器。"""

    def __init__(self):
        super().__init__()
        mr = self.config.get("media_resolver", {}) or {}
        base_url = mr.get("base_url", "")
        api_key = mr.get("api_key", "")
        self._resolver_netloc = (urlparse(base_url).netloc or "").lower()
        self._resolver_api_key = api_key
        self.client = MediaResolverClient(
            base_url=base_url,
            api_key=api_key,
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
        """接管抖音/小红书/微信视频号/X(Twitter)。"""
        if not url:
            return False
        if any(domain in url for domain in _SUPPORTED_DOMAINS):
            return True
        return bool(_X_DOMAIN_RE.search(url))

    def extract_video_id(self, url: str) -> str:
        """best-effort 提取 video_id（仅供日志；缓存 key 用归一化 url）。

        优先用已缓存 resolve 响应里的 video_id；否则从 url 粗提数字 id，
        取不到则回退归一化 url。不抛异常（解析交给 resolver）。
        """
        data = self._resolve_cache.get(self._normalize_url(url))
        if data and data.get("video_id"):
            return str(data["video_id"])
        import re

        m = re.search(
            r"(?:video|note|explore|item|sph|statuses?)/([A-Za-z0-9_-]+)", url
        ) or re.search(r"/(\d{6,})", url)
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
    def _prepare_download_headers(self, download_url: str) -> None:
        """根据下载 URL netloc 条件设置 download_headers。

        当下载 URL 的 netloc 与 resolver 的 base_url netloc 相等且 api_key 非空时，
        携带 X-API-Key 头（如视频号流式代理端点）；否则置空（避免向第三方 CDN 泄露密钥）。
        """
        try:
            target_netloc = (urlparse(download_url).netloc or "").lower()
            if self._resolver_netloc and target_netloc == self._resolver_netloc and self._resolver_api_key:
                self.download_headers = {"X-API-Key": self._resolver_api_key}
                return
        except Exception:
            pass
        self.download_headers = {}

    def download_file(self, url, filename, max_retries: int = 3):
        """覆写下载：常规下载失败后，对 resolver 直链做一次 force_refresh 重解析重下。

        基类 download_file 对 403/失效直链返回 None（不抛）。本方法在拿到 None 时，
        反查该 video_url 对应的页面 url，force_refresh 重解析得到新直链再下一次；
        仍失败则抛 DownloadFailedError（爆炸半径仅限本类）。
        """
        sph_code = self._wechat_stream_sph_code(url)
        if sph_code:
            return self._download_wechat_direct(sph_code, filename)

        self._prepare_download_headers(url)
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

        self._prepare_download_headers(fresh_url)
        local_file = super().download_file(fresh_url, filename, max_retries=max_retries)
        if local_file:
            return local_file
        raise DownloadFailedError(f"重解析后仍下载失败: {fresh_url[:100]}")

    # ------------------------------------------------------------------ #
    # 视频号：/direct 解密头 + CDN Range 直连拼接（不经 resolver 流式中转）
    # ------------------------------------------------------------------ #
    def _wechat_stream_sph_code(self, url: str) -> Optional[str]:
        """命中 resolver 视频号流式端点时返回 sph_code，否则 None。"""
        if not url or not self._resolver_netloc:
            return None
        try:
            parsed = urlparse(url)
        except Exception:
            return None
        if (parsed.netloc or "").lower() != self._resolver_netloc:
            return None
        match = _WECHAT_STREAM_PATH_RE.match(parsed.path or "")
        return match.group(1) if match else None

    @staticmethod
    def _decode_wechat_head(payload: dict, sph_code: str) -> bytes:
        """解码 head_b64 并校验 mp4 ftyp magic（bytes[4:8]）。"""
        head_b64 = payload.get("head_b64")
        if not isinstance(head_b64, str) or not head_b64:
            raise ResolverResponseError(f"wechat direct missing decrypted head sph_code={sph_code}")
        try:
            head = base64.b64decode(head_b64, validate=True)
        except ValueError as e:
            raise ResolverResponseError(f"wechat direct head decode failed sph_code={sph_code}") from e
        if len(head) != payload.get("encrypted_head_bytes"):
            raise ResolverResponseError(f"wechat direct head size mismatch sph_code={sph_code} decoded={len(head)}")
        if len(head) < 8 or head[4:8] != _MP4_FTYP_MAGIC:
            raise DownloadFailedError(f"wechat direct head lacks ftyp magic sph_code={sph_code} head_bytes={len(head)}")
        return head

    @staticmethod
    def _parse_content_range(header: Optional[str]):
        if not isinstance(header, str):
            return None
        m = re.fullmatch(r"(?i)bytes\s+(\d+)-(\d+)/(\d+)", header.strip())
        if not m:
            return None
        start, end, total = map(int, m.groups())
        return None if end < start or total <= 0 else (start, end, total)

    def _download_wechat_direct(self, sph_code: str, filename: str) -> str:
        """调 /direct 取解密头与 CDN 直链，Range 拼接成完整 mp4。"""
        logger.info(f"wechat direct download start sph_code={sph_code}")
        payload = self.client.fetch_wechat_direct(sph_code)
        content_length = payload.get("content_length")
        encrypted_head_bytes = payload.get("encrypted_head_bytes")
        if not isinstance(content_length, int) or isinstance(content_length, bool):
            raise ResolverResponseError(f"wechat direct invalid content_length sph_code={sph_code}")
        if self.max_download_bytes and content_length > self.max_download_bytes:
            raise DownloadFailedError(
                f"wechat direct file exceeds size limit sph_code={sph_code} "
                f"content_length={content_length} limit={self.max_download_bytes}"
            )
        head = self._decode_wechat_head(payload, sph_code)
        local_path = self.temp_manager.create_temp_file(suffix=".mp4")
        try:
            with open(local_path, "wb") as fh:
                fh.write(head)
                if encrypted_head_bytes >= content_length:
                    if len(head) != content_length:
                        raise DownloadFailedError(
                            f"wechat direct head-only size mismatch sph_code={sph_code} "
                            f"head_bytes={len(head)} content_length={content_length}"
                        )
                else:
                    self._append_wechat_cdn(fh, sph_code, payload, len(head), content_length)
            actual_size = os.path.getsize(local_path)
            if actual_size != content_length:
                raise DownloadFailedError(
                    f"wechat direct size mismatch sph_code={sph_code} "
                    f"actual={actual_size} content_length={content_length}"
                )
            logger.info(f"wechat direct download complete sph_code={sph_code} bytes={actual_size}")
            return str(local_path)
        except Exception:
            self._discard_temp(local_path)
            raise

    def _append_wechat_cdn(self, fh, sph_code, payload, offset, content_length):
        """CDN Range 追加；中断/4xx/Range 不符则 /direct 换链续传。"""
        origin = payload
        cdn_url = payload.get("cdn_url")
        if not isinstance(cdn_url, str) or not cdn_url.strip():
            raise ResolverResponseError(f"wechat direct missing CDN link sph_code={sph_code}")
        refreshes = 0
        zero_streak = 0
        while offset < content_length:
            try:
                validate_url_safe(cdn_url)
            except URLValidationError:
                raise DownloadFailedError(f"wechat direct CDN failed SSRF check sph_code={sph_code} offset={offset}")
            logger.info(
                f"wechat direct CDN GET sph_code={sph_code} offset={offset} "
                f"content_length={content_length} refreshes={refreshes}"
            )
            gained = self._stream_wechat_cdn_range(cdn_url, fh, sph_code, offset, content_length)
            offset += gained
            zero_streak = (zero_streak + 1) if gained == 0 else 0
            if offset == content_length:
                return
            if offset > content_length:
                raise DownloadFailedError(
                    f"wechat direct wrote past content_length sph_code={sph_code} "
                    f"offset={offset} content_length={content_length}"
                )
            if zero_streak >= 2:
                raise DownloadFailedError(f"wechat direct no progress sph_code={sph_code} offset={offset}")
            if refreshes >= _WECHAT_DIRECT_MAX_REFRESHES:
                raise DownloadFailedError(
                    f"wechat direct refresh limit sph_code={sph_code} offset={offset} refreshes={refreshes}"
                )
            logger.info(
                f"wechat direct refresh CDN link sph_code={sph_code} "
                f"offset={offset} refreshes={refreshes + 1}"
            )
            fresh = self.client.fetch_wechat_direct(sph_code)
            for field in ("content_length", "encrypted_head_bytes", "head_b64"):
                if origin.get(field) != fresh.get(field):
                    raise DownloadFailedError(
                        f"wechat direct object identity mismatch sph_code={sph_code} field={field}"
                    )
            cdn_url = fresh.get("cdn_url")
            if not isinstance(cdn_url, str) or not cdn_url.strip():
                raise ResolverResponseError(f"wechat direct missing CDN link after refresh sph_code={sph_code}")
            refreshes += 1
        raise DownloadFailedError(
            f"wechat direct incomplete sph_code={sph_code} offset={offset} content_length={content_length}"
        )

    def _stream_wechat_cdn_range(self, cdn_url, fh, sph_code, offset, content_length) -> int:
        """Range GET，返回本次写入字节。直链不进日志/异常；CDN 不带 X-API-Key。"""
        response = None
        gained = 0
        try:
            try:
                response = requests.get(
                    cdn_url, headers={"Range": f"bytes={offset}-"},
                    stream=True, timeout=_WECHAT_CDN_TIMEOUT, allow_redirects=False,
                )
            except requests.RequestException:
                logger.warning(f"wechat direct CDN connect failed sph_code={sph_code} offset={offset}")
                return 0
            parsed = self._parse_content_range(response.headers.get("Content-Range"))
            clen = response.headers.get("Content-Length")
            try:
                clen_ok = clen in (None, "") or int(clen) == parsed[1] - parsed[0] + 1
            except (TypeError, ValueError):
                clen_ok = False
            range_ok = parsed is not None and parsed[0] == offset and parsed[2] == content_length and clen_ok
            status = response.status_code
            if status in _WECHAT_CDN_RETRY_STATUSES or status != 206 or not range_ok:
                logger.warning(
                    f"wechat direct CDN unexpected response status={status} "
                    f"range={parsed} offset={offset} sph_code={sph_code}"
                )
                return 0
            try:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    if offset + gained + len(chunk) > content_length:
                        logger.error(
                            f"wechat direct CDN overflow sph_code={sph_code} offset={offset} "
                            f"gained={gained} chunk={len(chunk)} content_length={content_length}"
                        )
                        return gained
                    fh.write(chunk)
                    gained += len(chunk)
                    if offset + gained == content_length:
                        break
            except requests.RequestException:
                logger.warning(
                    f"wechat direct CDN stream interrupted sph_code={sph_code} offset={offset} gained={gained}"
                )
            return gained
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

