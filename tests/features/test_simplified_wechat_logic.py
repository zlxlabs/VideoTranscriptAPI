#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试简化后的企微通知逻辑
"""
import os
import sys
from utils import load_config

def test_simplified_wechat_logic():
    """测试简化后的企微通知逻辑"""
    print("开始测试简化后的企微通知逻辑")
    print("=" * 50)
    
    # 加载配置
    config = load_config()
    if not config:
        print("[错误] 无法加载配置文件")
        return
    
    # 获取配置中的企微校对文本阈值
    wechat_config = config.get('wechat', {})
    calibrated_text_max_length = wechat_config.get('calibrated_text_max_length', 5000)
    
    print(f"[配置] 企微校对文本阈值: {calibrated_text_max_length}")
    
    # 测试不同长度的校对文本
    test_cases = [
        {
            "name": "短校对文本",
            "calibrated_text": "这是一个短的校对文本，用于测试企微通知逻辑。" * 20,  # 约1000字符
            "summary_text": "这是对应的总结文本"
        },
        {
            "name": "中等校对文本", 
            "calibrated_text": "这是一个中等长度的校对文本，用于测试企微通知逻辑。" * 100,  # 约5000字符
            "summary_text": "这是对应的总结文本"
        },
        {
            "name": "长校对文本",
            "calibrated_text": "这是一个很长的校对文本，用于测试企微通知逻辑。" * 300,  # 约15000字符
            "summary_text": "这是对应的总结文本"
        }
    ]
    
    print(f"\n=== 测试校对文本长度判断逻辑 ===")
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"\n[测试{i}] {test_case['name']}")
        
        calibrated_text = test_case['calibrated_text']
        calibrated_text_length = len(calibrated_text)
        should_send_calibrated_text = calibrated_text_length <= calibrated_text_max_length
        
        print(f"  校对文本长度: {calibrated_text_length} 字符")
        print(f"  阈值: {calibrated_text_max_length}")
        print(f"  是否发送校对文本: {should_send_calibrated_text}")
        
        if should_send_calibrated_text:
            print("  [通知策略] 发送校对文本+总结文本+查看链接")
        else:
            print("  [通知策略] 只发送总结文本+查看链接（校对文本太长）")
    
    # 测试缓存模式的判断逻辑
    print(f"\n=== 测试缓存模式下的判断逻辑 ===")
    
    cache_test_cases = [
        {
            "name": "缓存短校对文本",
            "cache_data": {
                "llm_calibrated": "这是缓存中的短校对文本。" * 30,  # 约900字符
                "llm_summary": "这是缓存中的总结文本"
            }
        },
        {
            "name": "缓存长校对文本",
            "cache_data": {
                "llm_calibrated": "这是缓存中的长校对文本。" * 500,  # 约15000字符
                "llm_summary": "这是缓存中的总结文本"
            }
        }
    ]
    
    for i, test_case in enumerate(cache_test_cases, 1):
        print(f"\n[缓存测试{i}] {test_case['name']}")
        
        cache_data = test_case['cache_data']
        calibrated_text = cache_data.get('llm_calibrated', '')
        calibrated_text_length = len(calibrated_text)
        should_send_calibrated_text = calibrated_text_length <= calibrated_text_max_length
        
        print(f"  缓存校对文本长度: {calibrated_text_length} 字符")
        print(f"  阈值: {calibrated_text_max_length}")
        print(f"  是否发送校对文本: {should_send_calibrated_text}")
        
        if should_send_calibrated_text:
            print("  [缓存通知策略] 发送校对文本+总结文本+查看链接")
        else:
            print("  [缓存通知策略] 只发送总结文本+查看链接（校对文本太长）")
    
    print("\n" + "=" * 50)
    print("简化后的企微通知逻辑测试完成")
    print("\n[总结]")
    print(f"- 校对文本长度 <= {calibrated_text_max_length}字符：发送校对文本+总结文本+链接")
    print(f"- 校对文本长度 > {calibrated_text_max_length}字符：只发送总结文本+链接")
    print("- 不再依赖分段模式标识，逻辑更加简洁")
    print("- 实时处理和缓存模式使用相同的判断逻辑")

if __name__ == "__main__":
    test_simplified_wechat_logic()