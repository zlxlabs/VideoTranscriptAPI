#!/usr/bin/env python3
"""
测试缓存模式下的企业微信通知发送顺序
"""
import sys
import os
import time

# 添加src目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.notifications import send_long_text_wechat

def test_cache_mode_notification_order():
    """测试缓存模式下通知顺序"""
    print("=== 测试缓存模式通知顺序 ===")
    
    # 模拟视频信息
    title = "测试视频标题"
    url = "https://example.com/test-video" 
    
    # 模拟校对文本（长一些，会分段）
    calibrated_text = "这是校对后的转录文本。" * 200  # 约5000字符，会分段
    
    # 模拟总结文本（短一些）
    summary_text = "这是内容总结。" * 10
    
    print(f"校对文本长度: {len(calibrated_text)} 字符")
    print(f"总结文本长度: {len(summary_text)} 字符")
    print()
    
    print("开始模拟缓存模式发送流程...")
    
    # 1. 发送校对文本
    print("1. 发送校对文本...")
    start_time = time.time()
    send_long_text_wechat(
        title=title,
        url=url,
        text=calibrated_text,
        is_summary=False,
        use_rate_limit=True
    )
    print(f"   校对文本发送完成，耗时: {time.time() - start_time:.3f}s")
    
    # 2. 延迟（模拟代码中的延迟）
    print("2. 等待延迟...")
    time.sleep(0.05)
    
    # 3. 发送总结文本
    print("3. 发送总结文本...")
    start_time = time.time()
    send_long_text_wechat(
        title=title,
        url=url,
        text=summary_text,
        is_summary=True,
        use_rate_limit=True
    )
    print(f"   总结文本发送完成，耗时: {time.time() - start_time:.3f}s")
    
    # 4. 延迟
    print("4. 等待延迟...")
    time.sleep(0.05)
    
    # 5. 发送完成通知
    print("5. 发送完成通知...")
    from video_transcript_api.utils.notifications import WechatNotifier
    completion_message = f"✅ 【任务完成】{title}\n\n转录和AI处理已全部完成！\n\n🔗 查看完整结果：http://example.com/view/test-token"
    
    notifier = WechatNotifier()  # 自动使用全局 WeComNotifier
    notifier.send_text(completion_message)
    print("   完成通知已加入队列")
    
    print("\n测试完成！现在所有消息应该按正确顺序加入限流队列")
    print("预期顺序：校对文本分段 → 总结文本 → 完成通知")

if __name__ == "__main__":
    test_cache_mode_notification_order()
