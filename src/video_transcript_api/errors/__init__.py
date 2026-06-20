"""统一错误分类体系

提供项目级别的错误基类和分类函数，覆盖网络、下载、转录等场景。
LLM 相关错误保留在 llm/core/errors.py 中，此处不重复定义。
"""

from .base import TranscriptAPIError
from .network import (
    NetworkError,
    DownloadTimeoutError,
    HTTPForbiddenError,
    ResolverAuthError,
    ResolverServerError,
)
from .transcription import ASRConnectionError, EmptyTranscriptError
from .download import (
    DownloadFailedError,
    InvalidMediaError,
    InvalidURLError,
    NonVideoContentError,
    ResolverResolveError,
    ResolverResponseError,
)

__all__ = [
    "TranscriptAPIError",
    "NetworkError",
    "DownloadTimeoutError",
    "HTTPForbiddenError",
    "ResolverAuthError",
    "ResolverServerError",
    "ASRConnectionError",
    "EmptyTranscriptError",
    "DownloadFailedError",
    "InvalidMediaError",
    "InvalidURLError",
    "NonVideoContentError",
    "ResolverResolveError",
    "ResolverResponseError",
]
