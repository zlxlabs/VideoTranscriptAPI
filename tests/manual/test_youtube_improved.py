"""
测试改进后的 YouTube 处理流程
"""
import asyncio
import sys
import os

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(project_root, 'src'))

from video_transcript_api.downloaders.youtube import YoutubeDownloader
from video_transcript_api.utils.logging import setup_logger

logger = setup_logger("test_youtube")

async def test_youtube_subtitle():
    """测试 YouTube 字幕获取"""
    downloader = YoutubeDownloader()
    
    # 测试视频URL（使用示例中的视频）
    test_url = "https://www.youtube.com/watch?v=crMrVozp_h8"
    
    logger.info(f"测试 YouTube 字幕获取: {test_url}")
    
    # 1. 测试视频ID提取
    video_id = downloader.extract_video_id(test_url)
    logger.info(f"提取的视频ID: {video_id}")
    
    # 2. 测试视频信息获取（应该优先使用 yt-dlp）
    logger.info("测试 get_video_info 方法...")
    video_info = downloader.get_video_info(test_url)
    logger.info(f"视频标题: {video_info.get('video_title')}")
    logger.info(f"作者: {video_info.get('author')}")
    logger.info(f"平台: {video_info.get('platform')}")
    
    # 3. 测试字幕获取（优先使用 youtube-transcript-api）
    subtitle = downloader.get_subtitle(test_url)
    
    if subtitle:
        logger.info(f"成功获取字幕，长度: {len(subtitle)} 字符")
        logger.info(f"字幕预览: {subtitle[:200]}...")
        
        # 测试 webhook 通知的 URL 清洗
        from video_transcript_api.utils.notifications import WechatNotifier
        notifier = WechatNotifier()
        clean_url = notifier._clean_url(test_url)
        logger.info(f"清洗后的URL: {clean_url}")
        
        return True
    else:
        logger.warning("未能获取字幕")
        
        # 测试音频下载备用方案
        logger.info("尝试下载音频用于转录...")
        audio_info = downloader.download_audio_for_transcription(test_url)
        
        if audio_info:
            logger.info(f"音频下载成功: {audio_info['audio_path']}")
            logger.info(f"视频标题: {audio_info['video_title']}")
            logger.info(f"作者: {audio_info['author']}")
            
            # 清理临时文件
            if os.path.exists(audio_info['audio_path']):
                os.unlink(audio_info['audio_path'])
                logger.info("临时音频文件已清理")
            
            return True
        else:
            logger.error("音频下载也失败了")
            return False

def test_url_cleaning():
    """测试 URL 清洗功能"""
    from video_transcript_api.utils.notifications import WechatNotifier
    notifier = WechatNotifier()
    
    test_cases = [
        ("https://www.youtube.com/watch?v=abc123&list=xyz&index=1", "https://www.youtube.com/watch?v=abc123"),
        ("https://youtu.be/abc123?t=60", "https://youtu.be/abc123"),
        ("https://www.xiaohongshu.com/note/123?xsec_token=xyz&other=param", "https://www.xiaohongshu.com/note/123?xsec_token=xyz"),
        ("https://example.com/video?id=123&tracking=456", "https://example.com/video")
    ]
    
    logger.info("测试 URL 清洗功能:")
    for original, expected in test_cases:
        cleaned = notifier._clean_url(original)
        status = "✓" if cleaned == expected else "✗"
        logger.info(f"{status} {original} -> {cleaned}")
        if cleaned != expected:
            logger.error(f"  期望: {expected}")

if __name__ == "__main__":
    # 测试 URL 清洗
    test_url_cleaning()
    
    # 测试 YouTube 字幕获取
    asyncio.run(test_youtube_subtitle())
