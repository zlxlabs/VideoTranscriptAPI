#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
测试TikHub API响应解析
"""

import os
import sys
import json
import argparse

# 添加项目根目录到导入路径
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, os.path.join(project_root, 'src'))

from video_transcript_api.utils.logging import setup_logger, load_config
from video_transcript_api.downloaders.base import BaseDownloader

# 创建日志记录器
logger = setup_logger("test_api")

class TestAPIDownloader(BaseDownloader):
    """测试API的下载器"""
    
    def can_handle(self, url):
        return True
    
    def get_video_info(self, url):
        pass
    
    def get_subtitle(self, url):
        return None
    
    def test_api(self, endpoint, params=None):
        """
        测试API请求
        
        参数:
            endpoint: API端点
            params: 请求参数
            
        返回:
            dict: API响应
        """
        logger.info(f"测试API请求: {endpoint}, 参数: {params}")
        return self.make_api_request(endpoint, params)

def test_douyin_api(aweme_id):
    """
    测试抖音API
    
    参数:
        aweme_id: 抖音视频ID
    """
    logger.info(f"测试抖音API, aweme_id: {aweme_id}")
    
    # 创建测试下载器
    downloader = TestAPIDownloader()
    
    # 测试API请求
    endpoint = "/api/v1/douyin/web/fetch_one_video"
    params = {"aweme_id": aweme_id}
    
    try:
        response = downloader.test_api(endpoint, params)
        
        # 打印响应基本信息
        logger.info(f"API响应码: {response.get('code')}")
        
        # 检查响应中的data字段
        if 'data' in response and isinstance(response['data'], dict):
            # 检查aweme_detail字段
            if 'aweme_detail' in response['data'] and isinstance(response['data']['aweme_detail'], dict):
                aweme_detail = response['data']['aweme_detail']
                
                # 打印视频信息
                desc = aweme_detail.get('desc', '无描述')
                author = aweme_detail.get('author', {}).get('nickname', '未知作者')
                
                logger.info(f"视频标题: {desc}")
                logger.info(f"视频作者: {author}")
                
                # 检查下载URL
                # 1. 尝试获取音频URL
                music_url = None
                if 'music' in aweme_detail and 'play_url' in aweme_detail['music']:
                    music_url = aweme_detail['music']['play_url'].get('uri')
                    if music_url:
                        logger.info(f"找到音频URL: music.play_url.uri")
                
                # 2. 尝试获取视频URL
                video_url = None
                if 'video' in aweme_detail and 'play_addr' in aweme_detail['video']:
                    url_list = aweme_detail['video']['play_addr'].get('url_list', [])
                    if url_list and len(url_list) > 0:
                        video_url = url_list[0]
                        logger.info(f"找到视频URL: video.play_addr.url_list[0]")
                
                # 打印找到的URL
                logger.info(f"音频URL: {music_url}")
                logger.info(f"视频URL: {video_url}")
                
                # 保存调试信息到JSON文件
                debug_file = f"debug_douyin_{aweme_id}.json"
                with open(debug_file, 'w', encoding='utf-8') as f:
                    json.dump(response, f, ensure_ascii=False, indent=2)
                logger.info(f"完整响应已保存到: {debug_file}")
                
                return True
            else:
                logger.error("响应中缺少aweme_detail字段")
        else:
            logger.error("响应中缺少data字段")
        
        # 保存完整响应以便调试
        debug_file = f"error_douyin_{aweme_id}.json"
        with open(debug_file, 'w', encoding='utf-8') as f:
            json.dump(response, f, ensure_ascii=False, indent=2)
        logger.info(f"出错响应已保存到: {debug_file}")
        
        return False
    except Exception as e:
        logger.exception(f"API测试失败: {str(e)}")
        return False

def main():
    """命令行入口函数"""
    parser = argparse.ArgumentParser(description="测试TikHub API")
    
    # 创建子命令解析器
    subparsers = parser.add_subparsers(dest="command", help="子命令")
    
    # 添加抖音API测试命令
    douyin_parser = subparsers.add_parser("douyin", help="测试抖音API")
    douyin_parser.add_argument("aweme_id", help="抖音视频ID")
    
    # 解析命令行参数
    args = parser.parse_args()
    
    if args.command == "douyin":
        success = test_douyin_api(args.aweme_id)
        return 0 if success else 1
    else:
        parser.print_help()
        return 1

if __name__ == "__main__":
    sys.exit(main()) 
