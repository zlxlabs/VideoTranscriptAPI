#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
简化的分段校对功能测试
"""
import os
import sys
import json
from utils import load_config
from utils.text_segmentation import TextSegmentationProcessor
from utils.llm_enhanced import EnhancedLLMProcessor

def test_basic_functionality():
    """测试基本功能"""
    print("=== 测试基本功能 ===")
    
    # 加载配置
    config = load_config()
    print("[配置] 加载配置文件成功")
    
    # 创建处理器
    segmentation_processor = TextSegmentationProcessor(config)
    enhanced_processor = EnhancedLLMProcessor(config)
    
    print("[初始化] 处理器创建成功")
    
    # 检查配置参数
    segmentation_config = config.get('llm', {}).get('segmentation', {})
    if not segmentation_config:
        print("[错误] 配置文件中缺少 llm.segmentation 配置节")
        return
    
    required_keys = ['enable_threshold', 'segment_size', 'max_segment_size']
    for key in required_keys:
        if key not in segmentation_config:
            print(f"[错误] 配置文件中缺少 llm.segmentation.{key} 配置项")
            return
    
    enable_threshold = segmentation_config['enable_threshold']
    segment_size = segmentation_config['segment_size']
    max_segment_size = segmentation_config['max_segment_size']
    
    print(f"[配置] 分段阈值: {enable_threshold}")
    print(f"[配置] 分段大小: {segment_size}")
    print(f"[配置] 最大分段: {max_segment_size}")

def test_file_length():
    """测试文件长度统计"""
    print("\n=== 测试文件长度统计 ===")
    
    # 测试文件路径
    txt_file = r"cache_dir\youtube\2025\202508\njDochQ2zHs\transcript_capswriter.txt"
    json_file = r"cache_dir\xiaoyuzhou\2025\202508\68a3d1fe293471fed44ce974\transcript_funasr.json"
    
    config = load_config()
    segmentation_processor = TextSegmentationProcessor(config)
    
    # 测试TXT文件
    if os.path.exists(txt_file):
        txt_length = segmentation_processor.get_text_length(txt_file, 'txt')
        need_seg_txt = segmentation_processor.need_segmentation(txt_file, 'txt')
        print(f"[TXT] 文件: {os.path.basename(txt_file)}")
        print(f"[TXT] 长度: {txt_length} 字符")
        print(f"[TXT] 需要分段: {need_seg_txt}")
    else:
        print(f"[TXT] 文件不存在: {txt_file}")
    
    # 测试JSON文件
    if os.path.exists(json_file):
        json_length = segmentation_processor.get_text_length(json_file, 'json')
        need_seg_json = segmentation_processor.need_segmentation(json_file, 'json')
        print(f"[JSON] 文件: {os.path.basename(json_file)}")
        print(f"[JSON] 长度: {json_length} 字符")
        print(f"[JSON] 需要分段: {need_seg_json}")
    else:
        print(f"[JSON] 文件不存在: {json_file}")

def test_segmentation_logic():
    """测试分段逻辑"""
    print("\n=== 测试分段逻辑 ===")
    
    config = load_config()
    segmentation_processor = TextSegmentationProcessor(config)
    
    # 测试文本分段
    test_text = "这是第一个句子。这是第二个句子。这是第三个句子！这是第四个句子？" * 100
    
    print(f"[测试] 原始文本长度: {len(test_text)} 字符")
    
    segments = segmentation_processor.segment_txt_content(test_text)
    print(f"[结果] 分段数量: {len(segments)} 个")
    
    for i, segment in enumerate(segments[:3]):  # 只显示前3段
        print(f"[段落{i+1}] 长度: {len(segment)} 字符")
        print(f"[段落{i+1}] 内容: {segment[:100]}...")

def test_speaker_mapping():
    """测试说话人映射"""
    print("\n=== 测试说话人映射 ===")
    
    json_file = r"cache_dir\xiaoyuzhou\2025\202508\68a3d1fe293471fed44ce974\transcript_funasr.json"
    
    if not os.path.exists(json_file):
        print(f"[错误] JSON文件不存在: {json_file}")
        return
    
    config = load_config()
    segmentation_processor = TextSegmentationProcessor(config)
    
    try:
        # 测试说话人映射
        speaker_mapping = segmentation_processor.extract_speaker_mapping_from_json(
            json_file, "罗永浩的十字路口", "与理想汽车创始人李想的对话"
        )
        
        print(f"[映射] 说话人映射: {speaker_mapping}")
        
        # 测试JSON分段
        segments = segmentation_processor.segment_json_content(json_file, speaker_mapping)
        print(f"[分段] 共 {len(segments)} 个段落")
        
        if segments:
            first_segment = segments[0]
            print(f"[段落1] 包含 {len(first_segment.get('segments', []))} 个句子")
        
    except Exception as e:
        print(f"[错误] 说话人映射测试失败: {e}")

def main():
    """主测试函数"""
    print("开始分段校对功能测试")
    print("=" * 50)
    
    try:
        # 基本功能测试
        test_basic_functionality()
        
        # 文件长度测试
        test_file_length()
        
        # 分段逻辑测试
        test_segmentation_logic()
        
        # 说话人映射测试
        test_speaker_mapping()
        
        print("\n" + "=" * 50)
        print("所有测试完成")
        
    except Exception as e:
        print(f"\n[错误] 测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()