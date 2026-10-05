#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试企微通知优化功能
"""
import os
import sys
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(project_root, "src"))

from video_transcript_api.utils.logging import load_config
def test_notification_logic():
    """测试通知逻辑"""
    print("开始测试企微通知优化功能")
    print("=" * 50)
    
    # 加载配置
    config = load_config()
    if not config:
        print("[错误] 无法加载配置文件")
        return
    
    segmentation_config = config.get('llm', {}).get('segmentation', {})
    if not segmentation_config or 'enable_threshold' not in segmentation_config:
        print("[错误] 配置文件中缺少 llm.segmentation.enable_threshold 配置项")
        return
    enable_threshold = segmentation_config['enable_threshold']
    
    print(f"[配置] 分段阈值: {enable_threshold}")
    
    # 测试短文本（不需要分段）
    print("\n=== 测试短文本 ===")
    short_text = "这是一个短文本测试" * 100  # 约1000字符
    test_short_task = {
        "task_id": "test_short",
        "transcript": short_text,
        "use_speaker_recognition": False,
        "video_title": "短文本测试",
        "author": "测试用户",
        "description": "短文本描述"
    }
    
    print(f"[测试] 短文本长度: {len(short_text)} 字符")
    
    # 模拟处理（不实际调用LLM）
    text_length = len(short_text)
    need_segmentation = text_length > enable_threshold
    print(f"[判断] 需要分段: {need_segmentation}")
    
    if need_segmentation:
        print("[通知策略] 分段模式 - 只发送总结文本+查看链接")
    else:
        print("[通知策略] 普通模式 - 发送校对文本+总结文本+查看链接")
    
    # 测试长文本（需要分段）
    print("\n=== 测试长文本 ===")
    long_text = "这是一个长文本测试，用来模拟超长音频转录。" * 1000  # 约30000字符
    test_long_task = {
        "task_id": "test_long",
        "transcript": long_text,
        "use_speaker_recognition": True,
        "video_title": "长文本测试",
        "author": "测试用户",
        "description": "长文本描述"
    }
    
    print(f"[测试] 长文本长度: {len(long_text)} 字符")
    
    # 模拟处理（不实际调用LLM）
    text_length = len(long_text)
    need_segmentation = text_length > enable_threshold
    print(f"[判断] 需要分段: {need_segmentation}")
    
    if need_segmentation:
        print("[通知策略] 分段模式 - 只发送总结文本+查看链接")
    else:
        print("[通知策略] 普通模式 - 发送校对文本+总结文本+查看链接")
    
    # 测试缓存模式判断
    print("\n=== 测试缓存模式判断 ===")
    
    # 模拟缓存数据
    cache_data_short = {
        'transcript_data': short_text,
        'transcript_type': 'capswriter',
        'llm_calibrated': '校对后的短文本',
        'llm_summary': '短文本总结'
    }
    
    cache_data_long = {
        'transcript_data': long_text,
        'transcript_type': 'capswriter',
        'llm_calibrated': '校对后的长文本',
        'llm_summary': '长文本总结'
    }
    
    # 短文本缓存判断（使用修复后的逻辑）
    def get_transcript_from_cache(cache_data):
        transcript_data = cache_data.get('transcript_data', '')
        if cache_data.get('transcript_type') == 'funasr' and isinstance(transcript_data, dict):
            segments = transcript_data.get('segments', [])
            if segments:
                return '\n'.join([seg.get('text', '') for seg in segments])
            else:
                return ''
        elif isinstance(transcript_data, str):
            return transcript_data
        else:
            return ''
    
    original_transcript_short = get_transcript_from_cache(cache_data_short)
    is_segmented_mode_short = len(original_transcript_short) > enable_threshold
    print(f"[缓存判断] 短文本长度: {len(original_transcript_short)}, 推断分段模式: {is_segmented_mode_short}")
    
    if is_segmented_mode_short:
        print("[缓存通知策略] 分段模式 - 只发送总结文本+查看链接")
    else:
        print("[缓存通知策略] 普通模式 - 发送校对文本+总结文本+查看链接")
    
    # 长文本缓存判断
    original_transcript_long = get_transcript_from_cache(cache_data_long)
    is_segmented_mode_long = len(original_transcript_long) > enable_threshold
    print(f"[缓存判断] 长文本长度: {len(original_transcript_long)}, 推断分段模式: {is_segmented_mode_long}")
    
    if is_segmented_mode_long:
        print("[缓存通知策略] 分段模式 - 只发送总结文本+查看链接")
    else:
        print("[缓存通知策略] 普通模式 - 发送校对文本+总结文本+查看链接")
    
    print("\n=" * 50)
    print("企微通知优化功能测试完成")
    print("\n[总结]")
    print("- 短文本（<20000字符）：发送校对文本+总结文本+链接")
    print("- 长文本（>=20000字符）：只发送总结文本+链接（校对文本太长）")
    print("- 缓存模式：根据原始转录文本长度自动判断通知策略")

if __name__ == "__main__":
    test_notification_logic()
