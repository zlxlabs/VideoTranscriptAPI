#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
测试通用URL下载器功能
"""

import os
import sys
import requests
import json


from video_transcript_api.downloaders import create_downloader
from video_transcript_api.utils.logging import load_config, setup_logger

# 创建日志记录器
logger = setup_logger("test_generic_url")

def test_generic_downloader():
    """测试通用URL下载器"""
    
    # 测试URL列表
    test_urls = [
        # 直接的MP3文件链接
        "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
        # 非媒体文件链接（应该失败）
        "https://www.example.com/",
        # 常规平台链接（应该使用平台特定下载器）
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    ]
    
    for url in test_urls:
        print(f"\n{'='*60}")
        print(f"测试URL: {url}")
        print(f"{'='*60}")
        
        # 创建下载器
        downloader = create_downloader(url)
        print(f"使用下载器: {downloader.__class__.__name__}")
        
        # 测试是否是通用下载器
        if downloader.__class__.__name__ == "GenericDownloader":
            try:
                # 获取视频信息
                video_info = downloader.get_video_info(url)
                print(f"视频信息: {json.dumps(video_info, ensure_ascii=False, indent=2)}")
                
                # 检查is_generic标记
                assert video_info.get("is_generic") == True, "应该有is_generic标记"
                print("[√] is_generic标记正确")
                
                # 检查标题是否为空
                assert video_info.get("video_title") == "", "通用下载器标题应该为空"
                print("[√] 标题为空，等待LLM生成")
                
            except Exception as e:
                print(f"[×] 错误: {str(e)}")
        else:
            print(f"[√] 正确使用了平台特定下载器")


def test_api_with_generic_url():
    """测试API处理通用URL"""
    
    # 加载配置
    config = load_config()
    api_host = config.get("api", {}).get("host", "localhost")
    api_port = config.get("api", {}).get("port", 8000)
    auth_token = config.get("api", {}).get("auth_token", "")
    
    # API地址
    api_url = f"http://{api_host}:{api_port}/api/transcribe"
    
    # 测试直接的MP3链接
    test_url = "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
    
    print(f"\n{'='*60}")
    print(f"测试API处理通用URL")
    print(f"URL: {test_url}")
    print(f"{'='*60}")
    
    # 准备请求
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json"
    }
    
    data = {
        "url": test_url,
        "use_speaker_recognition": False
    }
    
    try:
        # 发送请求
        response = requests.post(api_url, headers=headers, json=data)
        result = response.json()
        
        print(f"API响应: {json.dumps(result, ensure_ascii=False, indent=2)}")
        
        if response.status_code == 202:
            print("[√] 任务已提交")
            task_id = result.get("data", {}).get("task_id")
            print(f"任务ID: {task_id}")
            
            # 提示用户检查任务状态
            print(f"\n可以通过以下命令检查任务状态:")
            print(f"curl -H \"Authorization: Bearer {auth_token}\" http://{api_host}:{api_port}/api/task/{task_id}")
        else:
            print(f"[×] API返回错误: {response.status_code}")
            
    except Exception as e:
        print(f"[×] API请求失败: {str(e)}")
        print("请确保API服务已启动")


if __name__ == "__main__":
    print("测试通用URL下载器功能")
    
    # 测试下载器
    test_generic_downloader()
    
    # 询问是否测试API
    print("\n" + "="*60)
    user_input = input("是否测试API功能？(需要先启动API服务) [y/N]: ")
    if user_input.lower() == 'y':
        test_api_with_generic_url()
    else:
        print("跳过API测试")
