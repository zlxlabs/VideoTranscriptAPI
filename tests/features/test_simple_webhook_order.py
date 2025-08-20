#!/usr/bin/env python3
"""
简单的webhook顺序测试
直接测试限流队列的行为
"""
import time
import sys
import os
from datetime import datetime

# 添加src目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.webhook_rate_limiter import send_rate_limited_message

def test_queue_order():
    """测试限流队列的消息顺序"""
    
    print(f"=== webhook限流队列顺序测试 ===")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # 使用测试webhook URL（不会实际发送）
    test_webhook = "https://test.example.com/webhook"
    
    messages = [
        "1. 校对文本第一段 - 这是校对的内容...",
        "2. 校对文本第二段 - 继续校对的内容...", 
        "3. 总结文本 - 这是总结的内容...",
        "4. ✅ 【任务完成】测试视频\n\n转录和AI处理已全部完成！\n\n🔗 查看完整结果：http://example.com/view/test"
    ]
    
    print("正在将消息加入限流队列...")
    for i, message in enumerate(messages, 1):
        success = send_rate_limited_message(test_webhook, message)
        print(f"消息 {i} 加入队列: {'成功' if success else '失败'}")
        time.sleep(0.1)  # 短暂间隔
    
    print(f"\n所有消息已加入队列")
    print("注意：实际发送顺序应该与加入队列顺序一致")
    print("由于使用测试URL，消息不会真正发送，但会按顺序处理")
    
    # 等待一段时间让队列处理
    print("\n等待队列处理...")
    time.sleep(3)
    
    print("测试完成")

if __name__ == "__main__":
    test_queue_order()