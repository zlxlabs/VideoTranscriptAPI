#!/usr/bin/env python3
"""
详细调试webhook消息顺序问题的测试脚本
"""
import sys
import os
import time

# 添加src目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.notifications import send_long_text_wechat, WechatNotifier

def test_detailed_webhook_order():
    """详细测试webhook消息顺序"""
    print("=== 详细调试webhook消息顺序 ===")
    print("(启用了DEBUG级别日志)")
    print()
    
    # 模拟视频信息
    title = "【测试视频】调试webhook顺序问题"
    url = "https://example.com/test-debug-video" 
    
    # 模拟校对文本（确保会分段，约2500字符）
    calibrated_text = "这是校对后的转录文本内容，用于测试消息顺序问题。" * 50  
    
    # 模拟总结文本
    summary_text = "这是内容总结，测试消息顺序。" * 5
    
    print(f"校对文本长度: {len(calibrated_text)} 字符")
    print(f"总结文本长度: {len(summary_text)} 字符")
    print()
    
    print("=== 开始模拟缓存模式发送流程 ===")
    
    # 1. 发送校对文本
    print("\n1. [缓存模式] 开始发送校对文本...")
    start_time = time.time()
    send_long_text_wechat(
        title=title,
        url=url,
        text=calibrated_text,
        is_summary=False,
        use_rate_limit=True
    )
    elapsed = time.time() - start_time
    print(f"   校对文本发送函数返回，耗时: {elapsed:.3f}s")
    
    # 2. 模拟缓存模式中的延迟
    print("\n2. [缓存模式] 校对文本发送完成，延迟100ms后发送总结文本")
    time.sleep(0.1)
    
    # 3. 发送总结文本
    print("\n3. [缓存模式] 开始发送总结文本...")
    start_time = time.time()
    send_long_text_wechat(
        title=title,
        url=url,
        text=summary_text,
        is_summary=True,
        use_rate_limit=True
    )
    elapsed = time.time() - start_time
    print(f"   总结文本发送函数返回，耗时: {elapsed:.3f}s")
    
    # 4. 模拟缓存模式中的延迟
    print("\n4. [缓存模式] 总结文本发送完成，延迟100ms后发送完成通知")
    time.sleep(0.1)
    
    # 5. 发送完成通知
    print("\n5. [缓存模式] 准备发送任务完成通知")
    completion_message = f"✅ 【任务完成】{title}\n\n转录和AI处理已全部完成！\n\n🔗 查看完整结果：http://example.com/view/test-token"
    
    notifier = WechatNotifier()  # 自动使用全局 WeComNotifier
    start_time = time.time()
    notifier.send_text(completion_message)
    elapsed = time.time() - start_time
    print(f"   完成通知发送函数返回，耗时: {elapsed:.3f}s")
    print("   [缓存模式] 任务完成通知已加入限流队列")
    
    print("\n=== 测试完成 ===")
    print("请查看详细日志，确认消息入队和发送顺序是否正确")
    print("预期顺序：校对文本分段1 → 校对文本分段2 → ... → 总结文本 → 完成通知")
    
    # 等待一段时间让队列处理完成
    print("\n等待3秒让队列处理完成...")
    time.sleep(3)
    print("队列处理完成")

if __name__ == "__main__":
    test_detailed_webhook_order()
