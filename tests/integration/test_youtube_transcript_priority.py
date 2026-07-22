"""
测试 YouTube 字幕获取的优先级策略

验证场景：
1. API Server 启用且成功获取字幕
2. API Server 启用但返回空字幕（视频无字幕）- 不回退到 TikHub
3. API Server 启用但失败（异常）- 回退到 TikHub
4. API Server 未启用，本地方案成功
5. API Server 未启用，本地 IP 被封，回退到 TikHub
6. 验证优先级配置正确性
"""
import sys
import os
from unittest.mock import Mock, patch, MagicMock

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(project_root, 'src'))

from video_transcript_api.downloaders.youtube import YoutubeDownloader
from video_transcript_api.downloaders.subtitle_types import SubtitleResult
from video_transcript_api.utils.logging import setup_logger

logger = setup_logger("test_youtube_transcript_priority")


def test_api_server_enabled_success():
    """
    测试场景 1：启用 API Server 且成功获取字幕

    预期：
    - 调用 youtube_api_server.fetch_transcript_result()
    - 返回字幕文本
    - 不调用本地方案或 TikHub API
    """
    logger.info("=" * 60)
    logger.info("Test Case 1: API Server enabled and success")
    logger.info("=" * 60)

    # 创建下载器
    downloader = YoutubeDownloader()

    # 修改配置以启用 API Server
    downloader.config["youtube_api_server"] = {
        "enabled": True,
        "base_url": "http://test-server:8300",
        "api_key": "test-key",
        "timeout": 30,
        "poll_interval": 30,
        "max_wait_time": 3600
    }

    # Mock the authoritative result API used by get_subtitle_result().
    mock_transcript = "This is a test transcript from API server."
    downloader._youtube_api_client = Mock()
    downloader._youtube_api_client.fetch_transcript_result = Mock(
        return_value=SubtitleResult(text=mock_transcript)
    )

    # 测试 URL
    test_url = "https://www.youtube.com/watch?v=test_video_id"

    # 执行字幕获取
    result = downloader.get_subtitle(test_url)

    # 验证结果
    assert result == mock_transcript, f"Expected: {mock_transcript}, Got: {result}"
    assert downloader._youtube_api_client.fetch_transcript_result.called, "API Server should be called"

    logger.info(f"PASS: Got transcript from API Server: {result[:50]}...")
    logger.info("")


def test_api_server_enabled_no_transcript():
    """
    测试场景 2：启用 API Server 但返回空字幕（视频无字幕）

    预期：
    - 调用 youtube_api_server.fetch_transcript_result() 返回 None
    - 直接返回 None（不调用 TikHub API）
    - 理由：API Server 已确认视频无字幕，不需要重试
    """
    logger.info("=" * 60)
    logger.info("Test Case 2: API Server enabled but no transcript available")
    logger.info("=" * 60)

    # 创建下载器
    downloader = YoutubeDownloader()

    # 修改配置以启用 API Server
    downloader.config["youtube_api_server"] = {
        "enabled": True,
        "base_url": "http://test-server:8300",
        "api_key": "test-key",
        "timeout": 30
    }

    # Mock the authoritative result API returning no subtitles.
    downloader._youtube_api_client = Mock()
    downloader._youtube_api_client.fetch_transcript_result = Mock(return_value=None)

    # Mock TikHub API（不应该被调用）
    downloader._get_subtitle_result_with_tikhub_api = Mock()

    # 测试 URL
    test_url = "https://www.youtube.com/watch?v=test_video_id"

    # 执行字幕获取
    result = downloader.get_subtitle(test_url)

    # 验证结果
    assert result is None, f"Expected: None, Got: {result}"
    assert downloader._youtube_api_client.fetch_transcript_result.called, "API Server should be called"
    assert not downloader._get_subtitle_result_with_tikhub_api.called, "TikHub should NOT be called when no transcript"

    logger.info("PASS: API Server returned no transcript, correctly skipped TikHub")
    logger.info("")


def test_api_server_enabled_failure_fallback_to_tikhub():
    """
    测试场景 3：启用 API Server 但失败（异常），直接回退到 TikHub

    预期：
    - 调用 youtube_api_server.fetch_transcript_result() 抛出异常
    - 直接调用 TikHub API（跳过本地 youtube-transcript-api）
    - 返回 TikHub 的字幕文本
    """
    logger.info("=" * 60)
    logger.info("Test Case 3: API Server enabled but failed, fallback to TikHub")
    logger.info("=" * 60)

    # 创建下载器
    downloader = YoutubeDownloader()

    # 修改配置以启用 API Server
    downloader.config["youtube_api_server"] = {
        "enabled": True,
        "base_url": "http://test-server:8300",
        "api_key": "test-key",
        "timeout": 30
    }

    # Mock the authoritative result API raising an exception.
    downloader._youtube_api_client = Mock()
    downloader._youtube_api_client.fetch_transcript_result = Mock(
        side_effect=Exception("Network timeout")
    )

    # Mock TikHub API 返回成功
    tikhub_transcript = "This is a test transcript from TikHub API."
    downloader._get_subtitle_result_with_tikhub_api = Mock(
        return_value=SubtitleResult(text=tikhub_transcript)
    )

    # 测试 URL
    test_url = "https://www.youtube.com/watch?v=test_video_id"

    # 执行字幕获取
    result = downloader.get_subtitle(test_url)

    # 验证结果
    assert result == tikhub_transcript, f"Expected: {tikhub_transcript}, Got: {result}"
    assert downloader._youtube_api_client.fetch_transcript_result.called, "API Server should be called first"
    assert downloader._get_subtitle_result_with_tikhub_api.called, "TikHub API should be called after API Server failed"

    logger.info(f"PASS: API Server failed, got transcript from TikHub: {result[:50]}...")
    logger.info("")


def test_api_server_disabled_local_success():
    """
    测试场景 4：未启用 API Server，使用本地方案成功

    预期：
    - 不调用 youtube_api_server
    - 调用本地 youtube-transcript-api
    - 返回本地获取的字幕文本
    """
    logger.info("=" * 60)
    logger.info("Test Case 4: API Server disabled, local method success")
    logger.info("=" * 60)

    # 创建下载器
    downloader = YoutubeDownloader()

    # 确保 API Server 未启用（默认配置中就是 enabled=true，需要覆盖）
    downloader.config["youtube_api_server"] = {"enabled": False}
    downloader._youtube_api_client = None

    # Mock the authoritative local result API.
    local_transcript = "This is a test transcript from local youtube-transcript-api."
    downloader._fetch_youtube_transcript_result = Mock(
        return_value=SubtitleResult(text=local_transcript)
    )

    # 测试 URL
    test_url = "https://www.youtube.com/watch?v=test_video_id"

    # 执行字幕获取
    result = downloader.get_subtitle(test_url)

    # 验证结果
    assert result == local_transcript, f"Expected: {local_transcript}, Got: {result}"
    assert downloader._fetch_youtube_transcript_result.called, "Local method should be called"
    assert downloader._youtube_api_client is None, "API Server should not be initialized"

    logger.info(f"PASS: Got transcript from local method: {result[:50]}...")
    logger.info("")


def test_api_server_disabled_local_ip_blocked_fallback_to_tikhub():
    """
    测试场景 5：未启用 API Server，本地方案 IP 被封，回退到 TikHub

    预期：
    - 调用本地 youtube-transcript-api 返回 IP_BLOCKED
    - 回退到 TikHub API
    - 返回 TikHub 的字幕文本
    """
    logger.info("=" * 60)
    logger.info("Test Case 5: API Server disabled, local IP blocked, fallback to TikHub")
    logger.info("=" * 60)

    # 创建下载器
    downloader = YoutubeDownloader()

    # 确保 API Server 未启用
    downloader.config["youtube_api_server"] = {"enabled": False}
    downloader._youtube_api_client = None

    # Mock the authoritative local result API returning the IP block sentinel.
    downloader._fetch_youtube_transcript_result = Mock(return_value="IP_BLOCKED")

    # Mock TikHub API 返回成功
    tikhub_transcript = "This is a test transcript from TikHub API after IP blocked."
    downloader._get_subtitle_result_with_tikhub_api = Mock(
        return_value=SubtitleResult(text=tikhub_transcript)
    )

    # 测试 URL
    test_url = "https://www.youtube.com/watch?v=test_video_id"

    # 执行字幕获取
    result = downloader.get_subtitle(test_url)

    # 验证结果
    assert result == tikhub_transcript, f"Expected: {tikhub_transcript}, Got: {result}"
    assert downloader._fetch_youtube_transcript_result.called, "Local method should be called first"
    assert downloader._get_subtitle_result_with_tikhub_api.called, "TikHub API should be called after IP blocked"

    logger.info(f"PASS: Local IP blocked, got transcript from TikHub: {result[:50]}...")
    logger.info("")


def test_subtitle_priority_verification():
    """
    测试场景 6：验证优先级顺序

    验证在不同配置下的调用顺序是否正确
    """
    logger.info("=" * 60)
    logger.info("Test Case 6: Verify priority order")
    logger.info("=" * 60)

    # 场景 5.1：API Server 启用
    downloader_enabled = YoutubeDownloader()
    downloader_enabled.config["youtube_api_server"] = {
        "enabled": True,
        "base_url": "http://test-server:8300",
        "api_key": "test-key"
    }
    downloader_enabled._youtube_api_client = Mock()

    assert downloader_enabled.use_api_server is True, "API Server should be enabled"
    logger.info("PASS: API Server correctly enabled")

    # 场景 5.2：API Server 未启用
    downloader_disabled = YoutubeDownloader()
    downloader_disabled.config["youtube_api_server"] = {"enabled": False}
    downloader_disabled._youtube_api_client = None

    assert downloader_disabled.use_api_server is False, "API Server should be disabled"
    logger.info("PASS: API Server correctly disabled")

    logger.info("")


def run_all_tests():
    """运行所有测试"""
    logger.info("")
    logger.info("=" * 60)
    logger.info("Starting YouTube Transcript Priority Tests")
    logger.info("=" * 60)
    logger.info("")

    try:
        test_api_server_enabled_success()
        test_api_server_enabled_no_transcript()
        test_api_server_enabled_failure_fallback_to_tikhub()
        test_api_server_disabled_local_success()
        test_api_server_disabled_local_ip_blocked_fallback_to_tikhub()
        test_subtitle_priority_verification()

        logger.info("=" * 60)
        logger.info("ALL TESTS PASSED")
        logger.info("=" * 60)
        return True

    except AssertionError as e:
        logger.error(f"TEST FAILED: {e}")
        logger.error("=" * 60)
        return False
    except Exception as e:
        logger.exception(f"TEST ERROR: {e}")
        logger.error("=" * 60)
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
