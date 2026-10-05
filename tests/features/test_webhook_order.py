#!/usr/bin/env python3
"""
测试企业微信通知发送顺序的脚本

验证修复后的限流系统是否能保证消息的发送顺序
"""
import time
import threading
from unittest.mock import Mock, patch
import sys
import os

# 添加src目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.notifications import send_long_text_wechat, WechatNotifier


def test_webhook_message_order():
    """测试webhook消息发送顺序"""
    
    # 模拟的消息发送记录
    sent_messages = []
    
    def mock_send_request(webhook, data=None, **kwargs):
        """模拟发送请求，记录发送的消息"""
        import json
        
        # 解析data参数（可能是字符串或字典）
        if isinstance(data, str):
            try:
                data_dict = json.loads(data)
            except:
                data_dict = {"text": {"content": data}}
        else:
            data_dict = data or {}
            
        content = data_dict.get('text', {}).get('content', '')
        sent_messages.append(content)
        print(f"[模拟发送] {content[:50]}...")
        time.sleep(0.1)  # 模拟网络延迟
        
        # 模拟成功响应
        response_mock = Mock()
        response_mock.status_code = 200
        response_mock.json.return_value = {"errcode": 0}
        return response_mock
    
    print("开始测试企业微信通知发送顺序...")
    
    # 使用mock替换requests.post
    with patch('requests.post', side_effect=mock_send_request):
        # 模拟视频信息
        title = "测试视频标题"
        url = "https://example.com/test-video"
        
        # 模拟校对文本（较长）
        calibrated_text = "这是校对后的文本内容。" * 50  # 生成较长文本
        
        # 模拟总结文本（较短）
        summary_text = "这是总结内容。" * 10
        
        # 模拟完成通知
        completion_message = f"✅ 【任务完成】{title}\n\n转录和AI处理已全部完成！\n\n🔗 查看完整结果：http://example.com/view/test-token"
        
        print(f"\n1. 发送校对文本 ({len(calibrated_text)} 字符)")
        send_long_text_wechat(
            title=title,
            url=url,
            text=calibrated_text,
            is_summary=False
        )

        print(f"\n2. 发送总结文本 ({len(summary_text)} 字符)")
        send_long_text_wechat(
            title=title,
            url=url,
            text=summary_text,
            is_summary=True
        )

        print(f"\n3. 发送完成通知")
        notifier = WechatNotifier()  # 自动使用全局 WeComNotifier
        notifier.send_text(completion_message)
        
        # 等待所有消息发送完成
        print("\n等待所有消息发送完成...")
        time.sleep(5)
        
        # 分析发送顺序
        print(f"\n=== 消息发送顺序分析 ===")
        print(f"总共发送了 {len(sent_messages)} 条消息")
        
        for i, message in enumerate(sent_messages, 1):
            if "校对文本" in message:
                msg_type = "校对文本"
            elif "总结文本" in message:
                msg_type = "总结文本"
            elif "转录和AI处理已全部完成" in message:
                msg_type = "完成通知"
            else:
                msg_type = "其他"
            
            print(f"{i}. [{msg_type}] {message[:80]}...")
        
        # 验证顺序是否正确
        completion_found_at = None
        for i, message in enumerate(sent_messages):
            if "转录和AI处理已全部完成" in message:
                completion_found_at = i
                break
        
        if completion_found_at is not None:
            if completion_found_at == len(sent_messages) - 1:
                print(f"\n✅ 顺序正确：完成通知在最后一条消息位置 ({completion_found_at + 1})")
            else:
                print(f"\n❌ 顺序错误：完成通知出现在第 {completion_found_at + 1} 条消息，不是最后一条")
        else:
            print(f"\n⚠️  未找到完成通知消息")


if __name__ == "__main__":
    test_webhook_message_order()
