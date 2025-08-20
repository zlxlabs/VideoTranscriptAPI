#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试LLM说话人推断功能
"""
import os
import sys
import json
from datetime import datetime

# 添加项目根目录到路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import load_config
from utils.text_segmentation import TextSegmentationProcessor
from utils.llm_segmented import SegmentedLLMProcessor

def test_speaker_inference():
    """测试LLM说话人推断功能"""
    print("=== 测试LLM说话人推断功能 ===")
    
    # 测试文件路径
    json_file = r"cache_dir\bilibili\2025\202508\BV1Br8mzREBN\transcript_funasr.json"
    
    if not os.path.exists(json_file):
        print(f"[错误] 测试文件不存在: {json_file}")
        return
    
    # 加载配置
    config = load_config()
    
    # 创建分段处理器
    segmentation_processor = TextSegmentationProcessor(config)
    
    print(f"[文件] {os.path.basename(json_file)}")
    
    try:
        # 测试说话人推断
        print("[步骤1] 开始LLM说话人推断...")
        speaker_mapping = segmentation_processor.extract_speaker_mapping_from_json(
            json_file, 
            title="YouTube视频转录测试", 
            description="测试LLM说话人推断功能"
        )
        
        print(f"[成功] 说话人推断完成")
        print(f"[结果] 说话人映射: {speaker_mapping}")
        
        # 测试分段校对
        print("\n[步骤2] 开始分段校对...")
        segmented_llm_processor = SegmentedLLMProcessor(config)
        
        calibrated_result = segmented_llm_processor.calibrate_text_segmented(
            json_file, 'json', "YouTube视频转录测试", "测试LLM说话人推断功能"
        )
        
        print(f"[成功] 分段校对完成")
        print(f"[长度] {len(calibrated_result)} 字符")
        print(f"[预览] {calibrated_result[:300]}...")
        
        # 保存结果用于查看
        output_dir = "output"
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(output_dir, f"llm_speaker_inference_test_{timestamp}.txt")
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(f"# LLM说话人推断测试结果\n")
            f.write(f"# 测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# 说话人映射: {speaker_mapping}\n")
            f.write(f"# 校对结果长度: {len(calibrated_result)} 字符\n")
            f.write(f"# ==========================================\n\n")
            f.write(calibrated_result)
        
        print(f"[保存] 结果已保存到: {output_file}")
        
        # 检查说话人是否被正确替换
        if any(original_id in calibrated_result for original_id in speaker_mapping.keys()):
            print("[警告] 校对结果中仍包含原始speaker ID，可能存在替换问题")
        else:
            print("[验证] 说话人替换成功，原始speaker ID已被正确替换")
        
    except Exception as e:
        print(f"[失败] 测试失败: {e}")
        import traceback
        traceback.print_exc()

def main():
    """主测试函数"""
    print("开始LLM说话人推断功能测试")
    print("=" * 50)
    
    try:
        test_speaker_inference()
        
        print("\n" + "=" * 50)
        print("LLM说话人推断测试完成")
        
    except Exception as e:
        print(f"\n[错误] 测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()