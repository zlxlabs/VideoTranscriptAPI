"""
YouTube API Server 错误码定义和映射

该模块定义了与 YouTube Download API Server 交互时可能遇到的错误类型。
"""

from enum import Enum
from typing import Tuple


class ErrorCode(str, Enum):
    """YouTube API Server 错误码枚举"""

    # 视频本身的问题（不可重试）
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
    VIDEO_PRIVATE = "VIDEO_PRIVATE"
    VIDEO_REGION_BLOCKED = "VIDEO_REGION_BLOCKED"
    VIDEO_AGE_RESTRICTED = "VIDEO_AGE_RESTRICTED"
    VIDEO_LIVE_STREAM = "VIDEO_LIVE_STREAM"

    # 临时性问题（可重试）
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_ERROR = "NETWORK_ERROR"
    POT_TOKEN_FAILED = "POT_TOKEN_FAILED"

    # 客户端错误
    TIMEOUT = "TIMEOUT"
    UNEXPECTED = "UNEXPECTED"


# 错误码映射表：错误码 -> (中文描述, 是否可重试)
ERROR_CODE_MAP: dict[str, Tuple[str, bool]] = {
    # 不可恢复错误 - 视频本身的问题
    ErrorCode.VIDEO_UNAVAILABLE: ("Video unavailable", False),
    ErrorCode.VIDEO_PRIVATE: ("Video is private", False),
    ErrorCode.VIDEO_REGION_BLOCKED: ("Video blocked in this region", False),
    ErrorCode.VIDEO_AGE_RESTRICTED: ("Video requires age verification", False),
    ErrorCode.VIDEO_LIVE_STREAM: ("Live streams not supported", False),

    # 可重试错误 - 临时性问题
    ErrorCode.DOWNLOAD_FAILED: ("Download failed", True),
    ErrorCode.RATE_LIMITED: ("Rate limited", True),
    ErrorCode.NETWORK_ERROR: ("Network error", True),
    ErrorCode.POT_TOKEN_FAILED: ("PO Token failed", True),

    # 客户端错误
    ErrorCode.TIMEOUT: ("Request timeout", True),
    ErrorCode.UNEXPECTED: ("Unexpected error", False),
}

# 不可重试的错误码集合
NON_RETRYABLE_ERRORS = {
    ErrorCode.VIDEO_UNAVAILABLE,
    ErrorCode.VIDEO_PRIVATE,
    ErrorCode.VIDEO_REGION_BLOCKED,
    ErrorCode.VIDEO_AGE_RESTRICTED,
    ErrorCode.VIDEO_LIVE_STREAM,
    ErrorCode.UNEXPECTED,
}


class YouTubeApiError(Exception):
    """
    YouTube API Server 错误基类

    Attributes:
        code: 错误码
        message: 错误消息
        retryable: 是否可重试
    """

    def __init__(self, code: str, message: str | None = None, retryable: bool | None = None):
        """
        初始化错误

        Args:
            code: 错误码字符串
            message: 错误消息，如果为 None 则使用默认消息
            retryable: 是否可重试，如果为 None 则根据错误码查表
        """
        self.code = code

        # 查找错误码映射
        try:
            error_enum = ErrorCode(code)
            default_message, default_retryable = ERROR_CODE_MAP.get(
                error_enum,
                ("Unknown error", False)
            )
        except ValueError:
            default_message = "Unknown error"
            default_retryable = False

        self.message = message if message is not None else default_message
        self.retryable = retryable if retryable is not None else default_retryable

        super().__init__(f"[{self.code}] {self.message}")

    @classmethod
    def from_api_response(cls, error_data: dict) -> "YouTubeApiError":
        """
        从 API 响应的 error 字段创建错误对象

        Args:
            error_data: API 响应中的 error 字典，格式如：
                {"code": "VIDEO_PRIVATE", "message": "This video is private"}

        Returns:
            YouTubeApiError 实例
        """
        code = error_data.get("code", "UNEXPECTED")
        message = error_data.get("message")
        return cls(code=code, message=message)

    def is_video_issue(self) -> bool:
        """
        判断是否是视频本身的问题（如私密、不可用等）

        Returns:
            bool: 如果是视频问题返回 True
        """
        try:
            error_enum = ErrorCode(self.code)
            return error_enum in NON_RETRYABLE_ERRORS
        except ValueError:
            return False


class YouTubeApiTimeoutError(YouTubeApiError):
    """任务超时错误"""

    def __init__(self, video_id: str, wait_time: float):
        super().__init__(
            code=ErrorCode.TIMEOUT,
            message=f"Task for video {video_id} did not complete within {wait_time}s",
            retryable=True
        )
        self.video_id = video_id
        self.wait_time = wait_time


class YouTubeApiNetworkError(YouTubeApiError):
    """网络错误"""

    def __init__(self, message: str, original_error: Exception | None = None):
        super().__init__(
            code=ErrorCode.NETWORK_ERROR,
            message=message,
            retryable=True
        )
        self.original_error = original_error
