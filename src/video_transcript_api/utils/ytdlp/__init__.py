"""yt-dlp utility module.

This module provides configuration building and cookie validation
for yt-dlp based video downloading.
"""

from .cookie_validator import (
    CookieValidationResult,
    validate_youtube_cookie_file,
    get_validation_summary,
    YOUTUBE_AUTH_COOKIES,
)
from .config_builder import YtdlpConfigBuilder

__all__ = [
    "CookieValidationResult",
    "validate_youtube_cookie_file",
    "get_validation_summary",
    "YOUTUBE_AUTH_COOKIES",
    "YtdlpConfigBuilder",
]
