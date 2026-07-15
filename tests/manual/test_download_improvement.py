#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
测试改进后的下载功能
"""

import os
import sys
import time

# 添加项目根目录到Python路径

from video_transcript_api.downloaders.generic import GenericDownloader

def test_download_improvement():
    """测试改进的下载功能"""
    
    print("测试改进的下载功能\n")
    
    # 创建通用下载器
    downloader = GenericDownloader()
    
    # 测试中等大小的音频文件
    test_url = "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
    filename = "test_soundhelix.mp3"
    
    print(f"测试URL: {test_url}")
    print(f"文件名: {filename}")
    print("-" * 60)
    
    start_time = time.time()
    
    try:
        # 测试下载
        result = downloader.download_file(test_url, filename)
        
        end_time = time.time()
        duration = end_time - start_time
        
        if result:
            file_size = os.path.getsize(result)
            print(f"\n[成功] 文件下载完成:")
            print(f"  - 本地路径: {result}")
            print(f"  - 文件大小: {file_size / (1024*1024):.2f} MB")
            print(f"  - 下载耗时: {duration:.2f} 秒")
            print(f"  - 平均速度: {(file_size / (1024*1024)) / duration:.2f} MB/s")
            
            # 清理测试文件
            downloader.clean_up(result)
            print(f"  - 测试文件已清理")
            
        else:
            print("[失败] 文件下载失败")
            return False
            
    except Exception as e:
        print(f"[异常] 测试异常: {str(e)}")
        return False
    
    return True


def test_resume_download():
    """测试断点续传功能"""
    
    print("\n" + "="*60)
    print("测试断点续传功能")
    print("="*60)
    
    # 创建通用下载器
    downloader = GenericDownloader()
    
    # 模拟一个较大的文件
    test_url = "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
    filename = "test_resume.mp3"
    
    print(f"测试URL: {test_url}")
    print(f"文件名: {filename}")
    print("-" * 60)
    
    try:
        # 第一次尝试 - 模拟中断（实际上会完成下载）
        print("第一次下载尝试...")
        result1 = downloader.download_file(test_url, filename)
        
        if result1:
            initial_size = os.path.getsize(result1)
            print(f"第一次下载完成，文件大小: {initial_size / (1024*1024):.2f} MB")
            
            # 人为截断文件模拟不完整下载
            truncated_size = initial_size // 2
            with open(result1, 'r+b') as f:
                f.truncate(truncated_size)
            
            print(f"模拟中断，截断到: {truncated_size / (1024*1024):.2f} MB")
            
            # 第二次尝试 - 测试断点续传
            print("第二次下载尝试（断点续传）...")
            result2 = downloader.download_file(test_url, filename)
            
            if result2:
                final_size = os.path.getsize(result2)
                print(f"\n[成功] 断点续传完成:")
                print(f"  - 初始大小: {initial_size / (1024*1024):.2f} MB")
                print(f"  - 截断大小: {truncated_size / (1024*1024):.2f} MB")
                print(f"  - 最终大小: {final_size / (1024*1024):.2f} MB")
                print(f"  - 续传正确: {'是' if final_size == initial_size else '否'}")
                
                # 清理测试文件
                downloader.clean_up(result2)
                print(f"  - 测试文件已清理")
                
                return final_size == initial_size
            else:
                print("[失败] 断点续传失败")
                return False
        else:
            print("[失败] 第一次下载失败")
            return False
            
    except Exception as e:
        print(f"[异常] 测试异常: {str(e)}")
        return False


if __name__ == "__main__":
    print("测试下载功能改进\n")
    
    # 测试基本下载功能
    success1 = test_download_improvement()
    
    # 测试断点续传功能
    success2 = test_resume_download()
    
    # 总结
    print("\n" + "="*60)
    print("测试总结:")
    print(f"  - 基本下载: {'通过' if success1 else '失败'}")
    print(f"  - 断点续传: {'通过' if success2 else '失败'}")
    
    if success1 and success2:
        print("\n[√] 所有测试通过，下载功能改进成功！")
        sys.exit(0)
    else:
        print("\n[×] 部分测试失败，需要进一步调试")
        sys.exit(1)