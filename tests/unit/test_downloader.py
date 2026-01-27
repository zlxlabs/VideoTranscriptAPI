import os
import sys
import pytest
import unittest
from unittest.mock import MagicMock, patch

# 添加项目根目录到导入路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from video_transcript_api.downloaders import create_downloader
from video_transcript_api.downloaders.base import BaseDownloader
from video_transcript_api.downloaders.douyin import DouyinDownloader
from video_transcript_api.downloaders.bilibili import BilibiliDownloader
from video_transcript_api.downloaders.xiaohongshu import XiaohongshuDownloader
from video_transcript_api.downloaders.youtube import YoutubeDownloader
from video_transcript_api.downloaders.xiaoyuzhou import XiaoyuzhouDownloader


class TestDownloaderFactory(unittest.TestCase):
    """测试下载器工厂类"""
    
    def test_create_downloader_douyin(self):
        """测试创建抖音下载器"""
        url = "https://v.douyin.com/sample"
        downloader = create_downloader(url)
        self.assertIsInstance(downloader, DouyinDownloader)
    
    def test_create_downloader_bilibili(self):
        """测试创建B站下载器"""
        url = "https://www.bilibili.com/video/BV1234"
        downloader = create_downloader(url)
        self.assertIsInstance(downloader, BilibiliDownloader)
    
    def test_create_downloader_xiaohongshu(self):
        """测试创建小红书下载器"""
        url = "https://www.xiaohongshu.com/explore/12345"
        downloader = create_downloader(url)
        self.assertIsInstance(downloader, XiaohongshuDownloader)
    
    def test_create_downloader_youtube(self):
        """测试创建YouTube下载器"""
        url = "https://www.youtube.com/watch?v=12345"
        downloader = create_downloader(url)
        self.assertIsInstance(downloader, YoutubeDownloader)
    
    def test_create_downloader_xiaoyuzhou(self):
        """测试创建小宇宙播客下载器"""
        url = "https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597"
        downloader = create_downloader(url)
        self.assertIsInstance(downloader, XiaoyuzhouDownloader)
    
    def test_create_downloader_unsupported(self):
        """测试创建不支持的下载器"""
        url = "https://www.unsupported.com/video/12345"
        downloader = create_downloader(url)
        self.assertIsNone(downloader)


class TestDouyinDownloader(unittest.TestCase):
    """测试抖音下载器"""
    
    @patch('downloaders.douyin.DouyinDownloader.make_api_request')
    def test_get_video_info(self, mock_api):
        """测试获取抖音视频信息"""
        # 模拟API响应
        mock_api.return_value = {
            "status": True,
            "data": {
                "aweme_detail": {
                    "item_title": "测试视频",
                    "author": {"nickname": "测试作者"},
                    "video": {
                        "bit_rate_audio": [{
                            "audio_meta": {
                                "url_list": {"main_url": "http://example.com/audio.mp3"}
                            }
                        }]
                    }
                }
            }
        }
        
        downloader = DouyinDownloader()
        
        # 模拟_extract_aweme_id方法
        downloader._extract_aweme_id = MagicMock(return_value="12345")
        
        # 调用被测试的方法
        info = downloader.get_video_info("https://v.douyin.com/sample")
        
        # 验证结果
        self.assertEqual(info["video_id"], "12345")
        self.assertEqual(info["video_title"], "测试视频")
        self.assertEqual(info["author"], "测试作者")
        self.assertEqual(info["platform"], "douyin")


class TestBilibiliDownloader(unittest.TestCase):
    """测试B站下载器"""
    
    @patch('downloaders.bilibili.BilibiliDownloader.make_api_request')
    def test_get_video_info(self, mock_api):
        """测试获取B站视频信息"""
        # 模拟第一次API响应（视频信息）
        mock_api.side_effect = [
            {
                "status": True,
                "data": {
                    "data": {
                        "title": "测试视频",
                        "owner": {"name": "测试作者"},
                        "cid": "67890"
                    }
                }
            },
            # 模拟第二次API响应（播放地址）
            {
                "status": True,
                "data": {
                    "data": {
                        "dash": {
                            "audio": [
                                {"baseUrl": "http://example.com/audio.m4s"}
                            ]
                        }
                    }
                }
            }
        ]
        
        downloader = BilibiliDownloader()
        
        # 模拟_extract_video_id方法
        downloader._extract_video_id = MagicMock(return_value="BV12345")
        
        # 调用被测试的方法
        info = downloader.get_video_info("https://www.bilibili.com/video/BV12345")
        
        # 验证结果
        self.assertEqual(info["video_id"], "BV12345")
        self.assertEqual(info["cid"], "67890")
        self.assertEqual(info["video_title"], "测试视频")
        self.assertEqual(info["author"], "测试作者")
        self.assertEqual(info["platform"], "bilibili")


class TestXiaoyuzhouDownloader(unittest.TestCase):
    """测试小宇宙播客下载器"""
    
    @patch('downloaders.xiaoyuzhou.requests.get')
    def test_get_video_info(self, mock_get):
        """测试获取小宇宙播客信息"""
        # 模拟HTML响应
        mock_response = MagicMock()
        mock_response.text = '''
        <html>
        <head>
            <title>E196 对话曹丰泽：只要不饿死，人生没有必修课 - 知行小酒馆 | 小宇宙 - 听播客，上小宇宙</title>
            <meta property="og:title" content="E196 对话曹丰泽：只要不饿死，人生没有必修课">
            <meta property="og:audio" content="https://media.xyzcdn.net/6013f9f58e2f7ee375cf4216/ls_H_O7Kt-7euS0WzYHUB9HTTt9r.m4a">
        </head>
        </html>
        '''
        mock_response.raise_for_status = MagicMock()
        mock_response.encoding = 'utf-8'
        mock_get.return_value = mock_response
        
        downloader = XiaoyuzhouDownloader()
        
        # 调用被测试的方法
        info = downloader.get_video_info("https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597")
        
        # 验证结果
        self.assertEqual(info["video_id"], "687893e0a12f9ff06a98a597")
        self.assertEqual(info["video_title"], "E196 对话曹丰泽：只要不饿死，人生没有必修课")
        self.assertEqual(info["author"], "知行小酒馆")
        self.assertEqual(info["download_url"], "https://media.xyzcdn.net/6013f9f58e2f7ee375cf4216/ls_H_O7Kt-7euS0WzYHUB9HTTt9r.m4a")
        self.assertEqual(info["platform"], "xiaoyuzhou")
        self.assertTrue(info["filename"].startswith("xiaoyuzhou_687893e0a12f9ff06a98a597_"))
        self.assertTrue(info["filename"].endswith(".m4a"))
    
    def test_can_handle(self):
        """测试URL处理能力判断"""
        downloader = XiaoyuzhouDownloader()
        
        # 应该能处理的URL
        self.assertTrue(downloader.can_handle("https://www.xiaoyuzhoufm.com/episode/12345"))
        
        # 不应该能处理的URL
        self.assertFalse(downloader.can_handle("https://www.youtube.com/watch?v=12345"))
        self.assertFalse(downloader.can_handle("https://www.bilibili.com/video/BV12345"))
    
    def test_extract_episode_id(self):
        """测试剧集ID提取"""
        downloader = XiaoyuzhouDownloader()
        
        # 正常URL
        episode_id = downloader._extract_episode_id("https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597")
        self.assertEqual(episode_id, "687893e0a12f9ff06a98a597")
        
        # 异常URL，应该抛出异常
        with self.assertRaises(ValueError):
            downloader._extract_episode_id("https://www.example.com/invalid")
    
    def test_get_subtitle(self):
        """测试获取字幕"""
        downloader = XiaoyuzhouDownloader()
        result = downloader.get_subtitle("https://www.xiaoyuzhoufm.com/episode/12345")
        self.assertIsNone(result)
    
    @patch('downloaders.xiaoyuzhou.requests.get')
    def test_get_video_info_fallback_parsing(self, mock_get):
        """测试标题解析失败时的降级处理"""
        # 模拟HTML响应 - 不标准的title格式
        mock_response = MagicMock()
        mock_response.text = '''
        <html>
        <head>
            <title>不标准的标题格式</title>
            <meta property="og:title" content="测试播客标题">
            <meta property="og:audio" content="https://media.xyzcdn.net/test.m4a">
        </head>
        </html>
        '''
        mock_response.raise_for_status = MagicMock()
        mock_response.encoding = 'utf-8'
        mock_get.return_value = mock_response
        
        downloader = XiaoyuzhouDownloader()
        
        # 调用被测试的方法
        info = downloader.get_video_info("https://www.xiaoyuzhoufm.com/episode/123abc456def")
        
        # 验证结果 - 应该使用og:title作为标题
        self.assertEqual(info["video_title"], "测试播客标题")
        self.assertEqual(info["author"], "未知作者")  # 无法解析时的默认值
    
    @patch('downloaders.xiaoyuzhou.requests.get')
    def test_get_video_info_with_pipe_in_author(self, mock_get):
        """测试作者名称中包含竖线符号的情况"""
        # 模拟HTML响应 - 作者名中包含 |
        mock_response = MagicMock()
        mock_response.text = '''
        <html>
        <head>
            <title>110. 逐段讲解Kimi K2报告并对照ChatGPT Agent、Qwen3-Coder等："系统工程的力量" - 张小珺Jùn｜商业访谈录 | 小宇宙 - 听播客，上小宇宙</title>
            <meta property="og:title" content="110. 逐段讲解Kimi K2报告并对照ChatGPT Agent、Qwen3-Coder等："系统工程的力量"">
            <meta property="og:audio" content="https://media.xyzcdn.net/test.m4a">
        </head>
        </html>
        '''
        mock_response.raise_for_status = MagicMock()
        mock_response.encoding = 'utf-8'
        mock_get.return_value = mock_response
        
        downloader = XiaoyuzhouDownloader()
        
        # 调用被测试的方法
        info = downloader.get_video_info("https://www.xiaoyuzhoufm.com/episode/abc123def456")
        
        # 验证结果
        self.assertEqual(info["video_title"], '110. 逐段讲解Kimi K2报告并对照ChatGPT Agent、Qwen3-Coder等："系统工程的力量"')
        self.assertEqual(info["author"], "张小珺Jùn｜商业访谈录")


if __name__ == '__main__':
    unittest.main() 