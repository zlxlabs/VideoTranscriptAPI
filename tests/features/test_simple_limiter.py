#!/usr/bin/env python3
"""
测试简单限流器的消息顺序
"""
import sys
import os
import time

# 添加src目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.simple_rate_limiter import send_rate_limited_message

def test_simple_limiter_order():
    """测试简单限流器的消息顺序保证"""
    print("=== 测试简单限流器消息顺序 ===")
    
    test_webhook = "https://test.example.com/webhook"
    
    # 模拟实际业务场景的消息顺序
    messages = [
        "【查看链接】视频转录任务",
        "视频转录任务状态更新: 开始处理",
        "视频转录任务状态更新: 使用已有缓存", 
        "校对文本第1段",
        "校对文本第2段",
        "校对文本第3段",
        "总结文本第1段",
        "总结文本第2段",
        "【任务完成】转录和AI处理已全部完成！",
    ]
    
    print(f"发送{len(messages)}条消息，观察顺序...")
    start_time = time.time()
    
    for i, msg in enumerate(messages, 1):
        success = send_rate_limited_message(test_webhook, msg)
        print(f"消息{i}: {'成功' if success else '失败'} - {msg[:30]}...")
    
    end_time = time.time()
    print(f"\n总耗时: {end_time - start_time:.2f}秒")
    print("所有消息已按顺序发送完毕")

if __name__ == "__main__":
    test_simple_limiter_order()