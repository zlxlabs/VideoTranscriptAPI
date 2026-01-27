#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
View Token复用功能测试

测试相同URL多次请求时，view_token是否正确复用，
同时验证每次请求都正常创建新的task_id和处理流程。
"""

import sys
import os

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.join(project_root, "src"))

from video_transcript_api.cache import CacheManager

def test_view_token_reuse():
    """测试view_token复用功能"""
    
    # 使用测试数据库
    cache_manager = CacheManager(db_path=":memory:")
    
    test_url = "https://www.youtube.com/watch?v=test123"
    use_speaker_recognition = False
    
    print("=== View Token复用功能测试 ===\n")
    
    # 第一次请求：创建首个任务
    print("1. 第一次请求URL...")
    first_task = cache_manager.create_task(test_url, use_speaker_recognition)
    first_task_id = first_task["task_id"]
    first_view_token = first_task["view_token"]
    print(f"   第一个任务ID: {first_task_id}")
    print(f"   第一个view_token: {first_view_token}")
    
    # 模拟任务完成
    cache_manager.update_task_status(first_task_id, "success", 
                                   platform="youtube", 
                                   media_id="test123",
                                   title="测试视频",
                                   author="测试作者")
    
    # 第二次请求：应该创建新的task_id但复用view_token
    print("\n2. 第二次请求相同URL...")
    second_task = cache_manager.create_task(test_url, use_speaker_recognition)
    second_task_id = second_task["task_id"]
    second_view_token = second_task["view_token"]
    print(f"   第二个任务ID: {second_task_id}")
    print(f"   第二个view_token: {second_view_token}")
    
    # 验证结果
    print("\n3. 验证结果...")
    if first_task_id != second_task_id:
        print("   [OK] 正确创建了不同的task_id")
    else:
        print("   [ERROR] 错误：两次请求返回了相同的task_id")
        return False
    
    if first_view_token == second_view_token:
        print("   [OK] 正确复用了相同的view_token")
    else:
        print("   [ERROR] 错误：两次请求返回了不同的view_token")
        return False
    
    # 第三次请求（不同的说话人识别设置）：应该创建新的task_id和新的view_token
    print("\n4. 第三次请求相同URL但不同说话人识别设置...")
    third_task = cache_manager.create_task(test_url, True)  # use_speaker_recognition=True
    third_task_id = third_task["task_id"]
    third_view_token = third_task["view_token"]
    print(f"   第三个任务ID: {third_task_id}")
    print(f"   第三个view_token: {third_view_token}")
    
    if third_task_id != first_task_id and third_task_id != second_task_id:
        print("   [OK] 不同设置正确创建了新的task_id")
    else:
        print("   [ERROR] 不同设置错误复用了task_id")
        return False
    
    if third_view_token != first_view_token:
        print("   [OK] 不同设置正确创建了新的view_token")
    else:
        print("   [ERROR] 不同设置错误复用了view_token")
        return False
    
    # 模拟第三个任务也完成
    cache_manager.update_task_status(third_task_id, "success", 
                                   platform="youtube", 
                                   media_id="test123",
                                   title="测试视频（说话人识别）",
                                   author="测试作者")
    
    # 第四次请求（与第三次相同设置）：应该复用第三次的view_token
    print("\n5. 第四次请求相同URL和说话人识别设置...")
    fourth_task = cache_manager.create_task(test_url, True)
    fourth_task_id = fourth_task["task_id"]
    fourth_view_token = fourth_task["view_token"]
    print(f"   第四个任务ID: {fourth_task_id}")
    print(f"   第四个view_token: {fourth_view_token}")
    
    if fourth_task_id != third_task_id:
        print("   [OK] 正确创建了新的task_id")
    else:
        print("   [ERROR] 错误复用了task_id")
        return False
    
    if fourth_view_token == third_view_token:
        print("   [OK] 正确复用了说话人识别设置的view_token")
    else:
        print("   [ERROR] 错误：未复用说话人识别设置的view_token")
        return False
    
    print("\n=== 所有测试通过 ===")
    return True

if __name__ == "__main__":
    success = test_view_token_reuse()
    if not success:
        sys.exit(1)
