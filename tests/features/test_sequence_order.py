#!/usr/bin/env python3
"""
测试消息序列号顺序的脚本
"""
import sys
import os
import time

# 添加src目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.webhook_rate_limiter import WebhookRateLimiter

def test_message_sequence_order():
    """测试消息序列号是否保证发送顺序"""
    print("=== 测试消息序列号顺序机制 ===")
    
    limiter = WebhookRateLimiter()
    test_webhook = "https://test.example.com/webhook"
    
    # 快速发送多条消息
    messages = [
        "消息1 - 校对文本第1段",
        "消息2 - 校对文本第2段",
        "消息3 - 校对文本第3段", 
        "消息4 - 总结文本",
        "消息5 - 任务完成通知",
    ]
    
    print(f"快速发送{len(messages)}条消息，观察序列号分配...")
    for i, msg in enumerate(messages, 1):
        success = limiter.send_message(test_webhook, msg)
        print(f"消息{i}加入队列: {'成功' if success else '失败'}")
        # 不加延迟，模拟快速连续发送
    
    print("\n等待队列处理...")
    print("检查日志中的序列号是否按顺序分配和发送")
    
    time.sleep(5)
    print("测试完成")

if __name__ == "__main__":
    test_message_sequence_order()