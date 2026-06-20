"""下载相关错误"""

from .base import TranscriptAPIError


class DownloadFailedError(TranscriptAPIError):
    """文件下载失败（可重试）

    下载过程中发生错误，如文件大小为 0、下载中断等。
    """

    def __init__(self, message: str = "File download failed"):
        super().__init__(message, retryable=True)


class InvalidMediaError(TranscriptAPIError):
    """无效的媒体文件（不可重试）

    下载的文件无法被 ffprobe 识别为有效的音视频文件。
    """

    def __init__(self, message: str = "Invalid media file"):
        super().__init__(message, retryable=False)


class InvalidURLError(TranscriptAPIError):
    """MediaResolverAPI 无法识别的链接（HTTP 400，不可重试）

    解析服务返回 400，表示传入的 URL 不被任何平台解析器识别，
    重试无意义，应提示用户"无法识别的链接"。
    """

    def __init__(self, message: str = "Invalid or unrecognized URL"):
        super().__init__(message, retryable=False)


class NonVideoContentError(TranscriptAPIError):
    """内容无可转录视频（终态，不可重试）

    解析服务返回 success=false 且判定为图文/已删除/私密等终态场景，
    没有可下载的视频直链，重试无意义，应提示用户"该内容无可转录视频"。
    """

    def __init__(self, message: str = "Content has no transcribable video"):
        super().__init__(message, retryable=False)


class ResolverResolveError(TranscriptAPIError):
    """解析失败：全部解析源均失败（不可重试）

    解析服务返回 success=false 且为 TikHub + Cobalt 等全源失败，
    本次解析已穷尽手段，重试无即时收益，应提示用户"解析失败，稍后再试"。
    """

    def __init__(self, message: str = "All resolver sources failed"):
        super().__init__(message, retryable=False)


class ResolverResponseError(TranscriptAPIError):
    """解析响应畸形（不可重试）

    解析服务返回 200 但缺少 video_url 或 JSON 结构异常，
    属于契约破坏，应记录全文并失败。
    """

    def __init__(self, message: str = "Malformed resolver response"):
        super().__init__(message, retryable=False)
