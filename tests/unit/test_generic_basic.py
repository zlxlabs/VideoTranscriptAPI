#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
测试通用URL下载器基本功能
"""

import os
import sys
import json

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from video_transcript_api.downloaders import create_downloader

def test_generic_downloader():
    """测试通用URL下载器"""
    
    print("测试通用URL下载器功能\n")
    
    # 测试URL列表
    test_cases = [
        {
            "url": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
            "expected_downloader": "GenericDownloader",
            "should_succeed": True
        },
        {
            "url": "https://example.com/audio.mp3",
            "expected_downloader": "GenericDownloader", 
            "should_succeed": True
        },
        {
            "url": "https://example.com/video.mp4",
            "expected_downloader": "GenericDownloader",
            "should_succeed": True
        },
        {
            "url": "https://www.example.com/",
            "expected_downloader": "GenericDownloader",
            "should_succeed": False  # 不是媒体文件
        },
        {
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "expected_downloader": "YoutubeDownloader",
            "should_succeed": True
        },
    ]
    
    passed = 0
    failed = 0
    
    for i, test_case in enumerate(test_cases, 1):
        url = test_case["url"]
        expected_downloader = test_case["expected_downloader"]
        should_succeed = test_case["should_succeed"]
        
        print(f"测试 {i}: {url}")
        print("-" * 60)
        
        try:
            # 创建下载器
            downloader = create_downloader(url)
            actual_downloader = downloader.__class__.__name__
            
            # 检查下载器类型
            if actual_downloader != expected_downloader:
                print(f"[失败] 期望下载器: {expected_downloader}, 实际: {actual_downloader}")
                failed += 1
                continue
            
            print(f"[通过] 正确使用下载器: {actual_downloader}")
            
            # 如果是通用下载器，测试获取视频信息
            if actual_downloader == "GenericDownloader":
                try:
                    video_info = downloader.get_video_info(url)
                    
                    # 验证返回的信息
                    assert video_info.get("is_generic") == True
                    assert video_info.get("video_title") == ""
                    assert video_info.get("platform") == "generic"
                    
                    if should_succeed:
                        print(f"[通过] 成功获取视频信息")
                        print(f"  - 文件名: {video_info.get('filename')}")
                        print(f"  - 平台: {video_info.get('platform')}")
                        print(f"  - is_generic: {video_info.get('is_generic')}")
                        passed += 1
                    else:
                        print(f"[失败] 预期应该失败但成功了")
                        failed += 1
                        
                except Exception as e:
                    if not should_succeed:
                        print(f"[通过] 预期失败: {str(e)}")
                        passed += 1
                    else:
                        print(f"[失败] 获取视频信息失败: {str(e)}")
                        failed += 1
            else:
                passed += 1
                
        except Exception as e:
            print(f"[失败] 异常: {str(e)}")
            failed += 1
        
        print()
    
    # 总结
    print("=" * 60)
    print(f"测试完成: {passed} 通过, {failed} 失败")
    
    return failed == 0


if __name__ == "__main__":
    success = test_generic_downloader()
    sys.exit(0 if success else 1)