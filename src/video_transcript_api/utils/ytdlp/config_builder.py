"""yt-dlp configuration builder.

This module provides a builder class for constructing yt-dlp options
from application configuration, with support for cookie management.
"""

import os
from pathlib import Path

from ..logging import setup_logger
from .cookie_validator import (
    CookieValidationResult,
    validate_youtube_cookie_file,
    get_validation_summary,
)

logger = setup_logger("ytdlp_config_builder")

# Default configuration values
DEFAULT_SOCKET_TIMEOUT = 30
DEFAULT_RETRIES = 10
DEFAULT_FRAGMENT_RETRIES = 10
DEFAULT_EXTRACTOR_RETRIES = 5

# Optimized player client order for best success rate (2024)
DEFAULT_PLAYER_CLIENTS = [
    'android_vr',
    'android_music',
    'android',
    'ios',
    'tv_embedded',
]

# Standard HTTP headers
DEFAULT_HTTP_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate',
}


class YtdlpConfigBuilder:
    """Builder for yt-dlp configuration options.

    This class reads configuration from the application config and builds
    appropriate yt-dlp option dictionaries. It handles cookie validation
    and provides methods to check cookie availability.

    Attributes:
        config: The full application configuration dictionary.
        ytdlp_config: The 'ytdlp' section of the configuration.
    """

    def __init__(self, config: dict):
        """Initialize the configuration builder.

        Args:
            config: Full application configuration dictionary.
        """
        self.config = config
        self.ytdlp_config = config.get('ytdlp', {})
        self._cookie_validation_result: CookieValidationResult | None = None
        self._cookie_validated = False

    def _get_cookie_config(self) -> dict:
        """Get YouTube cookie configuration.

        Returns:
            Cookie configuration dictionary with defaults applied.
        """
        cookie_config = self.ytdlp_config.get('youtube_cookie', {})
        return {
            'enabled': cookie_config.get('enabled', False),
            'file_path': cookie_config.get('file_path', './config/youtube_cookies.txt'),
            'fallback_without_cookie': cookie_config.get('fallback_without_cookie', True),
        }

    def validate_cookie_on_startup(self) -> CookieValidationResult | None:
        """Validate cookie file on service startup.

        This method should be called during application startup to validate
        the cookie file and log appropriate messages.

        Returns:
            CookieValidationResult if cookie is enabled, None otherwise.
        """
        cookie_config = self._get_cookie_config()

        if not cookie_config['enabled']:
            logger.info("[ytdlp] YouTube cookie not enabled, using cookie-less mode")
            self._cookie_validated = True
            return None

        file_path = cookie_config['file_path']
        logger.info(f"[ytdlp] YouTube cookie enabled, validating: {file_path}")

        result = validate_youtube_cookie_file(file_path)
        self._cookie_validation_result = result
        self._cookie_validated = True

        # Log validation results
        if result.is_valid:
            logger.info(f"[ytdlp] Cookie file validation passed: {file_path}")
            logger.info(f"[ytdlp]   - YouTube cookies: {result.youtube_cookie_count}")

            if result.has_auth_cookies:
                auth_list = ', '.join(sorted(result.auth_cookies_found))
                logger.info(f"[ytdlp]   - Auth cookies detected: {auth_list}")
            else:
                logger.warning("[ytdlp]   - No auth cookies found")

            logger.info(f"[ytdlp]   - Expired cookies: {result.expired_count}")

            # Log warnings
            for warning in result.warnings:
                logger.warning(f"[ytdlp]   - {warning}")
        else:
            logger.error(f"[ytdlp] Cookie file validation failed: {file_path}")
            if result.error:
                logger.error(f"[ytdlp]   - Error: {result.error}")

            if cookie_config['fallback_without_cookie']:
                logger.info("[ytdlp] Will use cookie-less mode (fallback_without_cookie=true)")
            else:
                logger.warning("[ytdlp] Cookie-less fallback disabled, downloads may fail")

        return result

    def is_cookie_available(self) -> bool:
        """Check if cookie is available for use.

        Returns:
            True if cookie is enabled and validation passed.
        """
        cookie_config = self._get_cookie_config()

        if not cookie_config['enabled']:
            return False

        # Validate if not done yet
        if not self._cookie_validated:
            self.validate_cookie_on_startup()

        if self._cookie_validation_result is None:
            return False

        return self._cookie_validation_result.is_valid

    def should_fallback(self) -> bool:
        """Check if fallback to cookie-less mode is allowed.

        Returns:
            True if fallback is allowed when cookie fails.
        """
        cookie_config = self._get_cookie_config()
        return cookie_config['fallback_without_cookie']

    def get_cookie_file_path(self) -> str | None:
        """Get the cookie file path if available.

        Returns:
            Absolute path to cookie file if valid, None otherwise.
        """
        if not self.is_cookie_available():
            return None

        cookie_config = self._get_cookie_config()
        file_path = cookie_config['file_path']

        # Convert to absolute path
        path = Path(file_path)
        if not path.is_absolute():
            path = Path.cwd() / path

        return str(path)

    def _get_player_clients(self) -> list[str]:
        """Get player client list from config or use defaults.

        Returns:
            List of player client identifiers.
        """
        return self.ytdlp_config.get('player_client', DEFAULT_PLAYER_CLIENTS)

    def _get_base_opts(self) -> dict:
        """Build base yt-dlp options without cookie.

        Returns:
            Dictionary of base yt-dlp options.
        """
        return {
            'quiet': True,
            'no_warnings': True,
            'no_check_certificate': True,
            'socket_timeout': self.ytdlp_config.get('socket_timeout', DEFAULT_SOCKET_TIMEOUT),
            'retries': self.ytdlp_config.get('retries', DEFAULT_RETRIES),
            'fragment_retries': self.ytdlp_config.get('fragment_retries', DEFAULT_FRAGMENT_RETRIES),
            'extractor_retries': self.ytdlp_config.get('extractor_retries', DEFAULT_EXTRACTOR_RETRIES),
            'http_headers': DEFAULT_HTTP_HEADERS.copy(),
            'extractor_args': {
                'youtube': {
                    'player_client': self._get_player_clients(),
                    'player_skip': ['webpage'],
                }
            },
        }

    def build_info_opts(self, use_cookie: bool = True) -> dict:
        """Build yt-dlp options for extracting video info only.

        Args:
            use_cookie: Whether to include cookie if available.

        Returns:
            Dictionary of yt-dlp options for info extraction.
        """
        opts = self._get_base_opts()
        opts['extract_flat'] = False
        opts['skip_download'] = True

        # Add cookie if requested and available
        if use_cookie:
            cookie_path = self.get_cookie_file_path()
            if cookie_path:
                opts['cookiefile'] = cookie_path
                logger.debug(f"[ytdlp] Using cookie file for info extraction")

        return opts

    def build_download_opts(self,
                            output_template: str,
                            use_cookie: bool = True,
                            audio_only: bool = True) -> dict:
        """Build yt-dlp options for downloading.

        Args:
            output_template: Output file path template.
            use_cookie: Whether to include cookie if available.
            audio_only: Whether to download audio only.

        Returns:
            Dictionary of yt-dlp options for downloading.
        """
        opts = self._get_base_opts()
        opts['outtmpl'] = output_template

        if audio_only:
            # 格式优先级：纯音频 m4a > 纯音频 mp3 > 任意纯音频 > 最佳混合格式（用于直播回放等）
            opts['format'] = 'bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best'
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        else:
            opts['format'] = 'best'

        # Additional download options
        opts['skip_unavailable_fragments'] = False
        opts['hls_prefer_native'] = True

        # Add cookie if requested and available
        if use_cookie:
            cookie_path = self.get_cookie_file_path()
            if cookie_path:
                opts['cookiefile'] = cookie_path
                logger.debug(f"[ytdlp] Using cookie file for download")

        return opts

    def get_validation_result(self) -> CookieValidationResult | None:
        """Get the cookie validation result.

        Returns:
            CookieValidationResult if validated, None otherwise.
        """
        return self._cookie_validation_result

    def get_config_summary(self) -> str:
        """Get a summary of the current yt-dlp configuration.

        Returns:
            Multi-line string summary.
        """
        lines = [
            "[ytdlp] Configuration summary:",
            f"  - Socket timeout: {self.ytdlp_config.get('socket_timeout', DEFAULT_SOCKET_TIMEOUT)}s",
            f"  - Retries: {self.ytdlp_config.get('retries', DEFAULT_RETRIES)}",
            f"  - Player clients: {', '.join(self._get_player_clients())}",
        ]

        cookie_config = self._get_cookie_config()
        lines.append(f"  - Cookie enabled: {cookie_config['enabled']}")

        if cookie_config['enabled']:
            lines.append(f"  - Cookie file: {cookie_config['file_path']}")
            lines.append(f"  - Fallback mode: {cookie_config['fallback_without_cookie']}")

            if self._cookie_validation_result:
                lines.append(f"  - Cookie valid: {self._cookie_validation_result.is_valid}")

        return '\n'.join(lines)
