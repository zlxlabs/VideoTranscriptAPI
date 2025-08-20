#!/usr/bin/env python3
"""
测试多个webhook地址的独立频控
"""
import sys
import os
import time
import threading

# 添加src目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.simple_rate_limiter import send_rate_limited_message

def test_multiple_webhook_independence():
    """测试多个webhook地址的独立频控"""
    print("=== 测试多个webhook地址独立频控 ===")
    
    webhook_a = "https://webhook-a.example.com/webhook"
    webhook_b = "https://webhook-b.example.com/webhook"
    
    def send_messages_to_webhook(webhook_url, webhook_name):
        """向特定webhook发送消息"""
        messages = [
            f"{webhook_name} - 消息1",
            f"{webhook_name} - 消息2", 
            f"{webhook_name} - 消息3",
        ]
        
        print(f"开始向{webhook_name}发送消息...")
        start_time = time.time()
        
        for i, msg in enumerate(messages, 1):
            success = send_rate_limited_message(webhook_url, msg)
            current_time = time.time()
            print(f"[{current_time - start_time:.2f}s] {webhook_name} 消息{i}: {'成功' if success else '失败'}")
        
        end_time = time.time()
        print(f"{webhook_name} 完成，总耗时: {end_time - start_time:.2f}秒")
    
    # 创建两个线程，同时向不同webhook发送消息
    print("创建两个线程，同时向不同webhook发送消息...")
    print("如果频控独立，两个webhook应该可以并行发送")
    print("如果频控不独立，会看到明显的串行等待\n")
    
    start_time = time.time()
    
    thread_a = threading.Thread(target=send_messages_to_webhook, args=(webhook_a, "WebhookA"))
    thread_b = threading.Thread(target=send_messages_to_webhook, args=(webhook_b, "WebhookB"))
    
    # 同时启动两个线程
    thread_a.start()
    thread_b.start()
    
    # 等待两个线程完成
    thread_a.join()
    thread_b.join()
    
    total_time = time.time() - start_time
    print(f"\n总体完成时间: {total_time:.2f}秒")
    
    # 分析结果
    if total_time < 5:  # 如果并行发送，应该在5秒内完成
        print("✅ 结果：不同webhook地址可以并行发送，频控独立！")
    else:
        print("❌ 结果：不同webhook地址被串行发送，频控不独立")

if __name__ == "__main__":
    test_multiple_webhook_independence()