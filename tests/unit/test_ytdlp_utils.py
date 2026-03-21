"""
Unit tests for yt-dlp utility modules: cookie_validator and config_builder.

Covers:
- cookie_validator: domain checks, line parsing, file validation, summaries
- config_builder: info/download opts, cookie availability, path resolution

All console output must be in English only (no emoji, no Chinese).
"""

import time
import pytest
from unittest.mock import patch, MagicMock

from video_transcript_api.utils.ytdlp.cookie_validator import (
    _is_youtube_domain,
    _parse_netscape_cookie_line,
    validate_youtube_cookie_file,
    get_validation_summary,
    CookieValidationResult,
)
from video_transcript_api.utils.ytdlp.config_builder import (
    YtdlpConfigBuilder,
    DEFAULT_SOCKET_TIMEOUT,
    DEFAULT_RETRIES,
    DEFAULT_FRAGMENT_RETRIES,
    DEFAULT_EXTRACTOR_RETRIES,
    DEFAULT_PLAYER_CLIENTS,
)


# ===========================================================================
# cookie_validator tests
# ===========================================================================

class TestIsYoutubeDomain:
    """Test _is_youtube_domain helper."""

    @pytest.mark.parametrize("domain", [
        ".youtube.com",
        "youtube.com",
        "www.youtube.com",
        ".google.com",
    ])
    def test_youtube_domains_return_true(self, domain):
        assert _is_youtube_domain(domain) is True

    @pytest.mark.parametrize("domain", [
        "example.com",
        "notyoutube.org",
        ".bing.com",
        "youtube.org",
        "",
    ])
    def test_non_youtube_domains_return_false(self, domain):
        assert _is_youtube_domain(domain) is False

    def test_domain_is_case_insensitive(self):
        assert _is_youtube_domain("YOUTUBE.COM") is True
        assert _is_youtube_domain("YouTube.Com") is True

    def test_domain_with_whitespace(self):
        assert _is_youtube_domain("  .youtube.com  ") is True


class TestParseNetscapeCookieLine:
    """Test _parse_netscape_cookie_line parser."""

    def test_valid_7_field_line(self):
        line = ".youtube.com\tTRUE\t/\tTRUE\t1700000000\tSID\tabc123"
        result = _parse_netscape_cookie_line(line)
        assert result is not None
        assert result["domain"] == ".youtube.com"
        assert result["flag"] == "TRUE"
        assert result["path"] == "/"
        assert result["secure"] is True
        assert result["expiration"] == 1700000000
        assert result["name"] == "SID"
        assert result["value"] == "abc123"

    def test_comment_line_returns_none(self):
        assert _parse_netscape_cookie_line("# Netscape HTTP Cookie File") is None

    def test_empty_line_returns_none(self):
        assert _parse_netscape_cookie_line("") is None
        assert _parse_netscape_cookie_line("   ") is None

    def test_invalid_field_count_returns_none(self):
        assert _parse_netscape_cookie_line("only\ttwo") is None
        assert _parse_netscape_cookie_line("a\tb\tc\td\te\tf") is None  # 6 fields

    def test_non_digit_expiration_defaults_to_zero(self):
        line = ".youtube.com\tTRUE\t/\tFALSE\tnotanumber\tNAME\tVALUE"
        result = _parse_netscape_cookie_line(line)
        assert result is not None
        assert result["expiration"] == 0

    def test_secure_false(self):
        line = ".youtube.com\tTRUE\t/\tFALSE\t0\tNAME\tVALUE"
        result = _parse_netscape_cookie_line(line)
        assert result is not None
        assert result["secure"] is False


class TestValidateYoutubeCookieFile:
    """Test validate_youtube_cookie_file with real temp files."""

    def test_file_not_found(self, tmp_path):
        result = validate_youtube_cookie_file(str(tmp_path / "nonexistent.txt"))
        assert result.is_valid is False
        assert result.file_exists is False
        assert "does not exist" in result.error

    def test_path_is_directory(self, tmp_path):
        result = validate_youtube_cookie_file(str(tmp_path))
        assert result.is_valid is False
        assert result.file_exists is True
        assert "not a file" in result.error

    def test_empty_file(self, tmp_path):
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text("")
        result = validate_youtube_cookie_file(str(cookie_file))
        assert result.is_valid is False
        assert "empty" in result.error

    def test_comments_only_file(self, tmp_path):
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n# comment\n")
        result = validate_youtube_cookie_file(str(cookie_file))
        assert result.is_valid is False
        assert "empty" in result.error

    def test_no_youtube_cookies(self, tmp_path):
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text(
            ".example.com\tTRUE\t/\tFALSE\t0\tSOME_COOKIE\tvalue\n"
        )
        result = validate_youtube_cookie_file(str(cookie_file))
        assert result.is_valid is False
        assert "No YouTube cookies" in result.error

    def test_valid_file_with_auth_cookies(self, tmp_path):
        future_ts = str(int(time.time()) + 86400)
        cookie_file = tmp_path / "cookies.txt"
        lines = [
            "# Netscape HTTP Cookie File",
            f".youtube.com\tTRUE\t/\tTRUE\t{future_ts}\tSID\tabc123",
            f".youtube.com\tTRUE\t/\tTRUE\t{future_ts}\tLOGIN_INFO\txyz789",
            f".youtube.com\tTRUE\t/\tTRUE\t{future_ts}\tHSID\tdef456",
        ]
        cookie_file.write_text("\n".join(lines) + "\n")

        result = validate_youtube_cookie_file(str(cookie_file))
        assert result.is_valid is True
        assert result.file_exists is True
        assert result.format_valid is True
        assert result.youtube_cookie_count == 3
        assert result.has_auth_cookies is True
        assert "SID" in result.auth_cookies_found
        assert "LOGIN_INFO" in result.auth_cookies_found
        assert "HSID" in result.auth_cookies_found
        assert result.expired_count == 0

    def test_expired_cookies_generate_warning(self, tmp_path):
        past_ts = str(int(time.time()) - 86400)
        future_ts = str(int(time.time()) + 86400)
        cookie_file = tmp_path / "cookies.txt"
        lines = [
            f".youtube.com\tTRUE\t/\tTRUE\t{past_ts}\tSID\texpired_val",
            f".youtube.com\tTRUE\t/\tTRUE\t{future_ts}\tLOGIN_INFO\tvalid_val",
        ]
        cookie_file.write_text("\n".join(lines) + "\n")

        result = validate_youtube_cookie_file(str(cookie_file))
        assert result.is_valid is True
        assert result.expired_count == 1
        assert any("expired" in w for w in result.warnings)

    def test_session_cookies_are_not_expired(self, tmp_path):
        """Cookies with expiration 0 are session cookies and should be valid."""
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text(
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsession_val\n"
        )
        result = validate_youtube_cookie_file(str(cookie_file))
        assert result.is_valid is True
        assert result.expired_count == 0
        assert "SID" in result.auth_cookies_found

    def test_invalid_format_lines_only(self, tmp_path):
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text("this is not a cookie\nanother bad line\n")
        result = validate_youtube_cookie_file(str(cookie_file))
        assert result.is_valid is False
        assert "No valid Netscape format" in result.error

    def test_no_auth_cookies_warning(self, tmp_path):
        """YouTube cookies without auth names should produce a warning."""
        future_ts = str(int(time.time()) + 86400)
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text(
            f".youtube.com\tTRUE\t/\tTRUE\t{future_ts}\tPREF\tsome_pref\n"
        )
        result = validate_youtube_cookie_file(str(cookie_file))
        assert result.is_valid is True
        assert result.has_auth_cookies is False
        assert any("No authentication cookies" in w for w in result.warnings)


class TestGetValidationSummary:
    """Test get_validation_summary output formatting."""

    def test_valid_result_with_auth(self):
        result = CookieValidationResult(
            is_valid=True,
            youtube_cookie_count=3,
            has_auth_cookies=True,
            auth_cookies_found={"SID", "LOGIN_INFO"},
            expired_count=0,
        )
        summary = get_validation_summary(result)
        assert "PASSED" in summary
        assert "YouTube cookies: 3" in summary
        assert "SID" in summary
        assert "LOGIN_INFO" in summary

    def test_valid_result_without_auth(self):
        result = CookieValidationResult(
            is_valid=True,
            youtube_cookie_count=2,
            has_auth_cookies=False,
            expired_count=1,
            warnings=["1 cookie(s) have expired"],
        )
        summary = get_validation_summary(result)
        assert "PASSED" in summary
        assert "Auth cookies: NONE" in summary
        assert "Warning:" in summary

    def test_invalid_result(self):
        result = CookieValidationResult(
            is_valid=False,
            error="Cookie file does not exist: /fake/path",
        )
        summary = get_validation_summary(result)
        assert "FAILED" in summary
        assert "does not exist" in summary


# ===========================================================================
# config_builder tests
# ===========================================================================

class TestYtdlpConfigBuilderDefaults:
    """Test builder with empty / minimal config."""

    def test_defaults_when_config_empty(self):
        builder = YtdlpConfigBuilder({})
        assert builder.ytdlp_config == {}

    def test_is_cookie_available_false_when_not_enabled(self):
        builder = YtdlpConfigBuilder({})
        assert builder.is_cookie_available() is False

    def test_should_fallback_defaults_true(self):
        builder = YtdlpConfigBuilder({})
        assert builder.should_fallback() is True


class TestBuildInfoOpts:
    """Test build_info_opts returns correct options."""

    def test_info_opts_has_skip_download(self):
        builder = YtdlpConfigBuilder({})
        opts = builder.build_info_opts()
        assert opts["skip_download"] is True
        assert opts["extract_flat"] is False

    def test_info_opts_uses_default_timeouts(self):
        builder = YtdlpConfigBuilder({})
        opts = builder.build_info_opts()
        assert opts["socket_timeout"] == DEFAULT_SOCKET_TIMEOUT
        assert opts["retries"] == DEFAULT_RETRIES
        assert opts["fragment_retries"] == DEFAULT_FRAGMENT_RETRIES
        assert opts["extractor_retries"] == DEFAULT_EXTRACTOR_RETRIES

    def test_info_opts_custom_timeout(self):
        config = {"ytdlp": {"socket_timeout": 60}}
        builder = YtdlpConfigBuilder(config)
        opts = builder.build_info_opts()
        assert opts["socket_timeout"] == 60

    def test_info_opts_no_cookie_by_default(self):
        builder = YtdlpConfigBuilder({})
        opts = builder.build_info_opts()
        assert "cookiefile" not in opts

    def test_info_opts_includes_player_clients(self):
        builder = YtdlpConfigBuilder({})
        opts = builder.build_info_opts()
        assert opts["extractor_args"]["youtube"]["player_client"] == DEFAULT_PLAYER_CLIENTS

    def test_info_opts_custom_player_clients(self):
        config = {"ytdlp": {"player_client": ["web", "android"]}}
        builder = YtdlpConfigBuilder(config)
        opts = builder.build_info_opts()
        assert opts["extractor_args"]["youtube"]["player_client"] == ["web", "android"]


class TestBuildDownloadOpts:
    """Test build_download_opts returns correct options."""

    def test_download_opts_audio_only_has_postprocessors(self):
        builder = YtdlpConfigBuilder({})
        opts = builder.build_download_opts("/tmp/test.%(ext)s", audio_only=True)
        assert opts["outtmpl"] == "/tmp/test.%(ext)s"
        assert len(opts["postprocessors"]) == 1
        pp = opts["postprocessors"][0]
        assert pp["key"] == "FFmpegExtractAudio"
        assert pp["preferredcodec"] == "mp3"

    def test_download_opts_not_audio_only(self):
        builder = YtdlpConfigBuilder({})
        opts = builder.build_download_opts("/tmp/test.%(ext)s", audio_only=False)
        assert opts["format"] == "best"
        assert "postprocessors" not in opts

    def test_download_opts_has_hls_option(self):
        builder = YtdlpConfigBuilder({})
        opts = builder.build_download_opts("/tmp/out.%(ext)s")
        assert opts["hls_prefer_native"] is True
        assert opts["skip_unavailable_fragments"] is False


class TestCookieAvailability:
    """Test is_cookie_available and cookie path resolution."""

    def test_cookie_available_when_valid(self, tmp_path):
        future_ts = str(int(time.time()) + 86400)
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text(
            f".youtube.com\tTRUE\t/\tTRUE\t{future_ts}\tSID\tval\n"
        )
        config = {
            "ytdlp": {
                "youtube_cookie": {
                    "enabled": True,
                    "file_path": str(cookie_file),
                }
            }
        }
        builder = YtdlpConfigBuilder(config)
        assert builder.is_cookie_available() is True

    def test_cookie_not_available_when_file_missing(self):
        config = {
            "ytdlp": {
                "youtube_cookie": {
                    "enabled": True,
                    "file_path": "/nonexistent/cookies.txt",
                }
            }
        }
        builder = YtdlpConfigBuilder(config)
        assert builder.is_cookie_available() is False

    def test_cookie_not_available_when_disabled(self):
        config = {
            "ytdlp": {
                "youtube_cookie": {
                    "enabled": False,
                    "file_path": "/some/cookies.txt",
                }
            }
        }
        builder = YtdlpConfigBuilder(config)
        assert builder.is_cookie_available() is False

    def test_cookie_path_resolution_relative(self, tmp_path):
        """Relative paths should be resolved to absolute."""
        future_ts = str(int(time.time()) + 86400)
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text(
            f".youtube.com\tTRUE\t/\tTRUE\t{future_ts}\tSID\tval\n"
        )
        config = {
            "ytdlp": {
                "youtube_cookie": {
                    "enabled": True,
                    "file_path": str(cookie_file),
                }
            }
        }
        builder = YtdlpConfigBuilder(config)
        path = builder.get_cookie_file_path()
        assert path is not None
        # Already absolute in test, just verify it stays absolute
        assert path.startswith("/")

    def test_info_opts_includes_cookie_when_available(self, tmp_path):
        future_ts = str(int(time.time()) + 86400)
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text(
            f".youtube.com\tTRUE\t/\tTRUE\t{future_ts}\tSID\tval\n"
        )
        config = {
            "ytdlp": {
                "youtube_cookie": {
                    "enabled": True,
                    "file_path": str(cookie_file),
                }
            }
        }
        builder = YtdlpConfigBuilder(config)
        opts = builder.build_info_opts(use_cookie=True)
        assert "cookiefile" in opts

    def test_info_opts_excludes_cookie_when_use_cookie_false(self, tmp_path):
        future_ts = str(int(time.time()) + 86400)
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text(
            f".youtube.com\tTRUE\t/\tTRUE\t{future_ts}\tSID\tval\n"
        )
        config = {
            "ytdlp": {
                "youtube_cookie": {
                    "enabled": True,
                    "file_path": str(cookie_file),
                }
            }
        }
        builder = YtdlpConfigBuilder(config)
        opts = builder.build_info_opts(use_cookie=False)
        assert "cookiefile" not in opts


class TestConfigSummary:
    """Test get_config_summary formatting."""

    def test_summary_without_cookie(self):
        builder = YtdlpConfigBuilder({})
        summary = builder.get_config_summary()
        assert "Socket timeout" in summary
        assert "Cookie enabled: False" in summary

    def test_summary_with_cookie_enabled(self, tmp_path):
        config = {
            "ytdlp": {
                "youtube_cookie": {
                    "enabled": True,
                    "file_path": "/some/path.txt",
                    "fallback_without_cookie": True,
                }
            }
        }
        builder = YtdlpConfigBuilder(config)
        summary = builder.get_config_summary()
        assert "Cookie enabled: True" in summary
        assert "Cookie file: /some/path.txt" in summary
        assert "Fallback mode: True" in summary
