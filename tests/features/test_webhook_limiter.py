#!/usr/bin/env python3
"""
测试webhook限流器功能
"""

import sys
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from video_transcript_api.utils.webhook_rate_limiter import (
    WebhookRateLimiter, 
    send_rate_limited_message,
    get_rate_limiter_stats,
    get_webhook_status
)
from video_transcript_api.utils.wechat import WechatNotifier
from video_transcript_api.utils.logger import setup_logger

logger = setup_logger("webhook_test")


def test_basic_functionality():
    """测试基本功能"""
    print("=== 测试基本功能 ===")
    
    # 模拟webhook地址
    webhook1 = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=e4d59de0-db79-41e7-a584-91147063047d"
    webhook2 = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=5df8967d-0180-435f-ac18-12ada9d40256"
    
    limiter = WebhookRateLimiter()
    
    # 发送几条消息
    for i in range(5):
        success1 = limiter.send_message(webhook1, f"测试消息1-{i+1}")
        success2 = limiter.send_message(webhook2, f"测试消息2-{i+1}")
        print(f"消息 {i+1}: webhook1={success1}, webhook2={success2}")
        time.sleep(0.1)
    
    # 等待一下让消息处理
    time.sleep(2)
    
    # 查看统计信息
    stats = get_rate_limiter_stats()
    print(f"统计信息: {stats}")
    
    # 查看具体webhook状态
    status1 = get_webhook_status(webhook1)
    status2 = get_webhook_status(webhook2)
    print(f"Webhook1状态: {status1}")
    print(f"Webhook2状态: {status2}")


def test_rate_limiting():
    """测试限流功能"""
    print("\n=== 测试限流功能 ===")
    
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=e4d59de0-db79-41e7-a584-91147063047d"
    
    # 快速发送30条消息（超过每分钟20条的限制）
    print("快速发送30条消息...")
    start_time = time.time()
    
    for i in range(30):
        success = send_rate_limited_message(webhook, f"限流测试消息 {i+1}")
        print(f"消息 {i+1}: {'✓' if success else '✗'}")
    
    end_time = time.time()
    print(f"发送完成，耗时: {end_time - start_time:.2f}秒")
    
    # 查看状态
    status = get_webhook_status(webhook)
    print(f"Webhook状态: {status}")
    
    # 等待队列处理完成
    print("等待消息处理完成...")
    time.sleep(10)
    
    final_stats = get_rate_limiter_stats()
    print(f"最终统计: {final_stats}")


def test_concurrent_sending():
    """测试并发发送"""
    print("\n=== 测试并发发送 ===")
    
    webhooks = [
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=concurrent1",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=concurrent2",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=concurrent3"
    ]
    
    def send_messages(webhook, count):
        """发送指定数量的消息"""
        for i in range(count):
            success = send_rate_limited_message(webhook, f"并发测试 {webhook[-10:]} 消息 {i+1}")
            if i % 5 == 0:
                print(f"线程 {webhook[-10:]} 发送第 {i+1} 条: {'✓' if success else '✗'}")
        return count
    
    # 使用线程池并发发送
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = []
        for webhook in webhooks:
            future = executor.submit(send_messages, webhook, 15)
            futures.append(future)
        
        # 等待完成
        for future in futures:
            result = future.result()
            print(f"线程完成，发送了 {result} 条消息")
    
    # 查看最终状态
    print("并发发送完成，查看状态...")
    for webhook in webhooks:
        status = get_webhook_status(webhook)
        print(f"Webhook {webhook[-10:]} 状态: 队列={status['queue_size']}, 最近发送={status['recent_sends_count']}")


def test_wechat_notifier_integration():
    """测试WechatNotifier集成"""
    print("\n=== 测试WechatNotifier集成 ===")
    
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=notifier_test"
    
    # 测试启用限流的通知器
    notifier_with_limit = WechatNotifier(webhook, use_rate_limit=True)
    print("使用限流通知器发送3条消息...")
    
    for i in range(3):
        success = notifier_with_limit.send_text(f"限流通知器测试消息 {i+1}")
        print(f"限流消息 {i+1}: {'✓' if success else '✗'}")
    
    # 测试禁用限流的通知器
    notifier_no_limit = WechatNotifier(webhook, use_rate_limit=False)
    print("使用非限流通知器发送3条消息...")
    
    for i in range(3):
        success = notifier_no_limit.send_text(f"非限流通知器测试消息 {i+1}")
        print(f"非限流消息 {i+1}: {'✓' if success else '✗'}")
    
    # 查看状态
    time.sleep(1)
    status = get_webhook_status(webhook)
    print(f"通知器测试后状态: {status}")


if __name__ == "__main__":
    print("开始测试webhook限流器...")
    
    try:
        # 基本功能测试
        test_basic_functionality()
        
        # 限流功能测试
        test_rate_limiting()
        
        # 并发测试
        test_concurrent_sending()
        
        # WechatNotifier集成测试
        test_wechat_notifier_integration()
        
        print("\n=== 所有测试完成 ===")
        
        # 最终统计
        final_stats = get_rate_limiter_stats()
        print(f"最终统计信息: {final_stats}")
        
    except Exception as e:
        logger.exception(f"测试过程中发生异常: {e}")
        print(f"测试失败: {e}")
    
    print("测试结束，等待后台线程处理完成...")
    time.sleep(5)