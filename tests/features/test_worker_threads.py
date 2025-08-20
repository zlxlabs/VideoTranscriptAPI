#!/usr/bin/env python3
"""
测试worker线程创建的脚本
"""
import sys
import os
import time

# 添加src目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.webhook_rate_limiter import WebhookRateLimiter

def test_single_worker_thread():
    """测试确保只创建一个worker线程"""
    print("=== 测试worker线程创建 ===")
    
    limiter = WebhookRateLimiter()
    test_webhook = "https://test.example.com/webhook"
    
    # 快速连续发送多条消息，看是否会创建多个线程
    messages = [
        "测试消息1 - 这是第一条消息",
        "测试消息2 - 这是第二条消息", 
        "测试消息3 - 这是第三条消息",
        "测试消息4 - 这是第四条消息",
        "测试消息5 - 这是第五条消息",
    ]
    
    print(f"快速发送{len(messages)}条消息...")
    for i, msg in enumerate(messages, 1):
        success = limiter.send_message(test_webhook, msg)
        print(f"消息{i}加入队列: {'成功' if success else '失败'}")
        # 不加延迟，模拟高并发情况
    
    print("\n检查线程状态...")
    print(f"当前worker线程数量: {len(limiter.worker_threads)}")
    for webhook, thread in limiter.worker_threads.items():
        print(f"  Webhook: {webhook[:50]}...")
        print(f"  线程名: {thread.name}")
        print(f"  线程状态: {'活跃' if thread.is_alive() else '死亡'}")
    
    print("\n等待队列处理...")
    time.sleep(2)
    print("测试完成")

if __name__ == "__main__":
    test_single_worker_thread()