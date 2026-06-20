"""网络相关错误"""

from .base import TranscriptAPIError


class NetworkError(TranscriptAPIError):
    """通用网络错误（可重试）

    包括连接超时、DNS 解析失败、连接被重置等。
    """

    def __init__(self, message: str = "Network error"):
        super().__init__(message, retryable=True)


class DownloadTimeoutError(NetworkError):
    """下载超时错误（可重试）"""

    def __init__(self, message: str = "Download timed out"):
        super().__init__(message)


class HTTPForbiddenError(TranscriptAPIError):
    """HTTP 403 禁止访问（不可重试）

    通常表示 API key 无效、IP 被封禁或资源需要付费。
    """

    def __init__(self, message: str = "HTTP 403 Forbidden"):
        super().__init__(message, retryable=False)


class ResolverAuthError(TranscriptAPIError):
    """MediaResolverAPI 鉴权失败（HTTP 401，不可重试）

    通常表示 media_resolver.api_key 配置错误或缺失，属于配置问题，
    重试无意义，应直接告警让运维修正配置。
    """

    def __init__(self, message: str = "Resolver authentication failed"):
        super().__init__(message, retryable=False)


class ResolverServerError(NetworkError):
    """MediaResolverAPI 服务端错误（HTTP 5xx，可重试）

    解析服务自身异常（非业务失败），属于暂时性故障，可退避重试。
    继承 NetworkError 以复用"可重试网络类"的统一处理。
    """

    def __init__(self, message: str = "Resolver server error"):
        super().__init__(message)
