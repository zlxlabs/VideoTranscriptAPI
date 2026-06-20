"""MediaResolverAPI HTTP 客户端。

把"短视频 URL → 无水印直链 + 元数据"的解析外包给专用 MediaResolverAPI 服务。
本客户端只负责一次 `POST /api/resolve` 调用与 HTTP/响应到项目异常的映射，
不做缓存、不做 SSRF 校验、不下载文件（这些由 MediaResolverDownloader 负责）。

契约（与 MediaResolverAPI 对齐，详见 docs/designs/media-resolver-integration.md）:
    POST {base_url}/api/resolve
    Header: X-API-Key: <api_key>
    Body:   {"url": str, "translate": bool, "force_refresh": bool}
    200 success=true:  {"success": true, "data": {platform, video_id, title,
                        author_name, video_url, width, height, duration, provider, ...}}
    200 success=false: {"success": false, "error": {"code": str, "message": str}}

异常映射见 Error & Rescue Registry（设计文档）。
"""

import time
from typing import Optional

import requests

from ..errors import (
    NetworkError,
    ResolverAuthError,
    ResolverServerError,
    InvalidURLError,
    NonVideoContentError,
    ResolverResolveError,
    ResolverResponseError,
)
from ..utils.logging import setup_logger

logger = setup_logger("media_resolver_client")

# success=false 时，error.code 到异常的判定契约。
# 终态（图文/删除/私密）→ NonVideoContentError；其余业务失败 → ResolverResolveError。
# ⚠️ 若 MediaResolverAPI 暂未提供 error.code，回退到文案关键词粗分（见 _classify_failure）。
_NON_VIDEO_CODES = {
    "NON_VIDEO_CONTENT",
    "IMAGE_TEXT",
    "IMAGE_POST",
    "NO_VIDEO",
    "DELETED",
    "NOT_FOUND",
    "PRIVATE",
    "UNAVAILABLE",
}
_RESOLVE_FAIL_CODES = {
    "ALL_SOURCES_FAILED",
    "RESOLVE_FAILED",
    "PROVIDER_ERROR",
}
# 文案兜底关键词（服务只回 message、无 code 时使用）
_NON_VIDEO_KEYWORDS = (
    "图文", "图片", "无视频", "已删除", "删除", "私密", "不存在", "下架",
    "image", "no video", "deleted", "private", "not found", "unavailable",
)


class MediaResolverClient:
    """MediaResolverAPI 解析客户端（requests 实现，超时/退避复用现有风格）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 30,
        max_retries: int = 2,
        retry_delay: int = 2,
    ):
        if not base_url:
            raise ValueError("media_resolver.base_url 未配置")
        if not api_key:
            raise ValueError("media_resolver.api_key 未配置")
        # 去掉末尾斜杠，避免拼出 //api/resolve
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.retry_delay = retry_delay

    @property
    def resolve_endpoint(self) -> str:
        return f"{self.base_url}/api/resolve"

    def resolve(
        self,
        url: str,
        translate: bool = False,
        force_refresh: bool = False,
    ) -> dict:
        """解析单条 URL，返回 data 字典（含 video_url 等）。

        Args:
            url: 待解析的短视频 URL
            translate: 是否翻译中文描述（v1 默认 False）
            force_refresh: 是否强制刷新解析（仅在下载 403/失效时为 True）

        Returns:
            dict: 解析成功的 data 字段（保证含非空 video_url）

        Raises:
            ResolverAuthError / InvalidURLError / NonVideoContentError /
            ResolverResolveError / ResolverResponseError / ResolverServerError /
            NetworkError: 见 Error & Rescue Registry
        """
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        payload = {
            "url": url,
            "translate": translate,
            "force_refresh": force_refresh,
        }

        last_network_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            logger.info(
                f"resolve request (attempt {attempt}/{self.max_retries}): "
                f"{self.resolve_endpoint} url={url[:80]} force_refresh={force_refresh}"
            )
            try:
                response = requests.post(
                    self.resolve_endpoint,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                # 服务不可达：超时/连接拒绝/DNS → 可重试网络错误
                last_network_error = e
                logger.warning(f"resolver unreachable (attempt {attempt}): {e}")
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                    continue
                raise NetworkError(f"解析服务暂不可用: {e}")
            except requests.RequestException as e:
                last_network_error = e
                logger.warning(f"resolver request exception (attempt {attempt}): {e}")
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                    continue
                raise NetworkError(f"解析服务请求异常: {e}")

            status = response.status_code
            logger.info(f"resolver response status: {status}")

            # —— HTTP 层判定（终态优先，5xx 可重试）——
            if status == 401:
                raise ResolverAuthError(
                    f"解析服务鉴权失败(401)，请检查 media_resolver.api_key: "
                    f"{self._safe_text(response)}"
                )
            if status == 400:
                raise InvalidURLError(
                    f"无法识别的链接(400): {self._safe_text(response)}"
                )
            if status >= 500:
                logger.warning(f"resolver server error {status} (attempt {attempt})")
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                    continue
                raise ResolverServerError(
                    f"解析服务异常({status}): {self._safe_text(response)}"
                )
            if status != 200:
                # 其余非 200（如 403/404）按响应畸形处理，记录全文
                raise ResolverResponseError(
                    f"解析服务返回意外状态码({status}): {self._safe_text(response)}"
                )

            # —— 200：解析 JSON ——
            try:
                body = response.json()
            except ValueError as e:
                raise ResolverResponseError(
                    f"解析响应非合法 JSON: {e}, 内容: {self._safe_text(response)}"
                )
            if not isinstance(body, dict):
                raise ResolverResponseError(
                    f"解析响应顶层非对象: {type(body).__name__}"
                )

            if body.get("success"):
                data = body.get("data")
                if not isinstance(data, dict) or not data.get("video_url"):
                    raise ResolverResponseError(
                        f"解析成功但缺少 video_url，响应: {self._truncate(body)}"
                    )
                logger.info(
                    f"resolve success: platform={data.get('platform')}, "
                    f"video_id={data.get('video_id')}, provider={data.get('provider')}"
                )
                return data

            # success=false：区分终态 vs 全源失败
            self._classify_failure(body)

        # 理论不可达：循环必然 return 或 raise；兜底
        raise NetworkError(
            f"解析服务多次失败: {last_network_error}"
        )

    def _classify_failure(self, body: dict) -> None:
        """根据 error.code / 文案把 success=false 映射为终态异常（必抛）。"""
        error = body.get("error") or {}
        if isinstance(error, str):
            error = {"message": error}
        code = (error.get("code") or "").strip().upper()
        message = error.get("message") or "解析失败"

        if code in _NON_VIDEO_CODES:
            raise NonVideoContentError(f"该内容无可转录视频: {message}")
        if code in _RESOLVE_FAIL_CODES:
            raise ResolverResolveError(f"解析失败，稍后再试: {message}")

        # 无 code 或未知 code：文案关键词兜底粗分
        lowered = message.lower()
        if any(kw in lowered for kw in _NON_VIDEO_KEYWORDS):
            raise NonVideoContentError(f"该内容无可转录视频: {message}")

        # 默认归类为全源失败（不可重试，提示稍后再试）
        raise ResolverResolveError(f"解析失败，稍后再试: {message}")

    @staticmethod
    def _safe_text(response, limit: int = 300) -> str:
        try:
            return response.text[:limit]
        except Exception:
            return "<no body>"

    @staticmethod
    def _truncate(obj, limit: int = 500) -> str:
        return str(obj)[:limit]
