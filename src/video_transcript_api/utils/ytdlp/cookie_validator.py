"""YouTube cookie file validator.

This module provides validation for Netscape format cookie files,
specifically checking for YouTube authentication cookies.
"""

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..logging import setup_logger

logger = setup_logger("ytdlp_cookie_validator")

# YouTube authentication cookie names that indicate logged-in state
YOUTUBE_AUTH_COOKIES = frozenset({
    'LOGIN_INFO',
    'SID',
    'HSID',
    'SSID',
    'APISID',
    'SAPISID',
    '__Secure-1PSID',
    '__Secure-3PSID',
})

# YouTube domain patterns
YOUTUBE_DOMAINS = frozenset({
    '.youtube.com',
    'youtube.com',
    'www.youtube.com',
    '.google.com',  # Some auth cookies are on google.com
})


@dataclass
class CookieValidationResult:
    """Result of cookie file validation.

    Attributes:
        is_valid: Whether the cookie file is usable for downloads.
        file_exists: Whether the cookie file exists at the specified path.
        format_valid: Whether the file follows Netscape cookie format.
        youtube_cookie_count: Number of YouTube-related cookies found.
        has_auth_cookies: Whether authentication cookies are present.
        auth_cookies_found: Set of auth cookie names that were found.
        expired_count: Number of expired cookies detected.
        warnings: List of warning messages.
        error: Error message if validation failed, None otherwise.
    """
    is_valid: bool = False
    file_exists: bool = False
    format_valid: bool = False
    youtube_cookie_count: int = 0
    has_auth_cookies: bool = False
    auth_cookies_found: set = field(default_factory=set)
    expired_count: int = 0
    warnings: list = field(default_factory=list)
    error: str | None = None


def _is_youtube_domain(domain: str) -> bool:
    """Check if a domain is YouTube-related.

    Args:
        domain: The domain string to check.

    Returns:
        True if the domain is YouTube-related.
    """
    domain = domain.lower().strip()
    for yt_domain in YOUTUBE_DOMAINS:
        if domain == yt_domain or domain.endswith(yt_domain):
            return True
    return False


def _parse_netscape_cookie_line(line: str) -> dict | None:
    """Parse a single line of Netscape cookie format.

    Netscape cookie format:
    domain<TAB>flag<TAB>path<TAB>secure<TAB>expiration<TAB>name<TAB>value

    Args:
        line: A single line from the cookie file.

    Returns:
        Dictionary with cookie data, or None if line is invalid/comment.
    """
    line = line.strip()

    # Skip empty lines and comments
    if not line or line.startswith('#'):
        return None

    # Split by tab
    parts = line.split('\t')

    # Netscape format requires exactly 7 fields
    if len(parts) != 7:
        return None

    try:
        return {
            'domain': parts[0],
            'flag': parts[1],
            'path': parts[2],
            'secure': parts[3].upper() == 'TRUE',
            'expiration': int(parts[4]) if parts[4].isdigit() else 0,
            'name': parts[5],
            'value': parts[6],
        }
    except (ValueError, IndexError):
        return None


def validate_youtube_cookie_file(file_path: str) -> CookieValidationResult:
    """Validate a YouTube cookie file.

    Performs comprehensive validation including:
    1. File existence check
    2. Netscape format validation
    3. YouTube domain cookie presence
    4. Authentication cookie detection
    5. Expiration checking

    Args:
        file_path: Path to the cookie file.

    Returns:
        CookieValidationResult with detailed validation information.
    """
    result = CookieValidationResult()

    # Step 1: Check file existence
    path = Path(file_path)
    if not path.exists():
        result.error = f"Cookie file does not exist: {file_path}"
        logger.debug(f"Cookie validation failed: {result.error}")
        return result

    result.file_exists = True

    if not path.is_file():
        result.error = f"Path is not a file: {file_path}"
        logger.debug(f"Cookie validation failed: {result.error}")
        return result

    # Step 2: Read and parse file
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        # Try with latin-1 as fallback
        try:
            with open(file_path, 'r', encoding='latin-1') as f:
                lines = f.readlines()
        except Exception as e:
            result.error = f"Failed to read cookie file: {e}"
            logger.debug(f"Cookie validation failed: {result.error}")
            return result
    except Exception as e:
        result.error = f"Failed to read cookie file: {e}"
        logger.debug(f"Cookie validation failed: {result.error}")
        return result

    # Step 3: Parse cookies
    cookies = []
    valid_lines = 0
    invalid_lines = 0

    for line in lines:
        line = line.strip()
        # Skip empty lines and comments
        if not line or line.startswith('#'):
            continue

        cookie = _parse_netscape_cookie_line(line)
        if cookie:
            cookies.append(cookie)
            valid_lines += 1
        else:
            invalid_lines += 1

    # Check format validity
    if valid_lines == 0 and invalid_lines > 0:
        result.error = "No valid Netscape format cookies found in file"
        logger.debug(f"Cookie validation failed: {result.error}")
        return result

    if valid_lines == 0:
        result.error = "Cookie file is empty or contains only comments"
        logger.debug(f"Cookie validation failed: {result.error}")
        return result

    result.format_valid = True

    # Step 4: Filter YouTube cookies
    youtube_cookies = [c for c in cookies if _is_youtube_domain(c['domain'])]
    result.youtube_cookie_count = len(youtube_cookies)

    if result.youtube_cookie_count == 0:
        result.error = "No YouTube cookies found in file"
        logger.debug(f"Cookie validation failed: {result.error}")
        return result

    # Step 5: Check for authentication cookies
    current_time = int(time.time())
    expired_count = 0
    auth_cookies_found = set()

    for cookie in youtube_cookies:
        cookie_name = cookie['name']
        expiration = cookie['expiration']

        # Check expiration (0 means session cookie, which is valid)
        if expiration != 0 and expiration < current_time:
            expired_count += 1
            continue

        # Check if it's an auth cookie
        if cookie_name in YOUTUBE_AUTH_COOKIES:
            auth_cookies_found.add(cookie_name)

    result.expired_count = expired_count
    result.auth_cookies_found = auth_cookies_found
    result.has_auth_cookies = len(auth_cookies_found) > 0

    # Generate warnings
    if expired_count > 0:
        result.warnings.append(
            f"{expired_count} cookie(s) have expired, consider updating the cookie file"
        )

    if not result.has_auth_cookies:
        result.warnings.append(
            "No authentication cookies found (LOGIN_INFO, SID, etc.), "
            "some restricted videos may not be accessible"
        )

    # Final validation result
    result.is_valid = True

    logger.debug(
        f"Cookie validation completed: valid={result.is_valid}, "
        f"youtube_cookies={result.youtube_cookie_count}, "
        f"auth_cookies={list(result.auth_cookies_found)}, "
        f"expired={result.expired_count}"
    )

    return result


def get_validation_summary(result: CookieValidationResult) -> str:
    """Generate a human-readable summary of validation result.

    Args:
        result: The validation result to summarize.

    Returns:
        Multi-line string summary.
    """
    lines = []

    if result.is_valid:
        lines.append("Cookie file validation: PASSED")
        lines.append(f"  - YouTube cookies: {result.youtube_cookie_count}")
        if result.has_auth_cookies:
            auth_list = ', '.join(sorted(result.auth_cookies_found))
            lines.append(f"  - Auth cookies found: {auth_list}")
        else:
            lines.append("  - Auth cookies: NONE")
        lines.append(f"  - Expired cookies: {result.expired_count}")
    else:
        lines.append("Cookie file validation: FAILED")
        if result.error:
            lines.append(f"  - Error: {result.error}")

    for warning in result.warnings:
        lines.append(f"  - Warning: {warning}")

    return '\n'.join(lines)
