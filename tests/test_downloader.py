import os
import sys
import pytest
import unittest
from unittest.mock import MagicMock, patch

# 添加项目根目录到导入路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from downloaders import create_downloader
from downloaders.base import BaseDownloader
from downloaders.douyin import DouyinDownloader
from downloaders.bilibili import BilibiliDownloader
from downloaders.xiaohongshu import XiaohongshuDownloader
from downloaders.youtube import YoutubeDownloader


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


if __name__ == '__main__':
    unittest.main() 