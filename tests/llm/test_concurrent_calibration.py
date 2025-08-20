#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试并发校对功能
"""
import os
import time
from utils import load_config
from utils.llm_segmented import SegmentedLLMProcessor

def test_concurrent_calibration():
    """测试并发校对功能"""
    print("=== 测试并发校对功能 ===")
    
    # 测试文件路径
    txt_file = r"cache_dir\youtube\2025\202508\njDochQ2zHs\transcript_capswriter.txt"
    
    if not os.path.exists(txt_file):
        print(f"[错误] 测试文件不存在: {txt_file}")
        return
    
    # 加载配置
    config = load_config()
    
    # 创建处理器
    segmented_llm_processor = SegmentedLLMProcessor(config)
    
    print(f"[文件] {os.path.basename(txt_file)}")
    
    # 记录开始时间
    start_time = time.time()
    
    try:
        # 进行并发分段校对
        calibrated_result = segmented_llm_processor.calibrate_text_segmented(
            txt_file, 'txt', "YouTube视频转录测试", "并发校对功能测试"
        )
        
        # 记录结束时间
        end_time = time.time()
        duration = end_time - start_time
        
        print(f"[成功] 并发校对完成")
        print(f"[耗时] {duration:.2f} 秒")
        print(f"[长度] 原始文本 -> 校对后文本")
        print(f"[结果] {len(calibrated_result)} 字符")
        print(f"[预览] {calibrated_result[:200]}...")
        
        # 保存结果用于对比
        output_dir = "output"
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(output_dir, f"concurrent_calibration_test_{timestamp}.txt")
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(f"# 并发校对测试结果\n")
            f.write(f"# 测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# 处理耗时: {duration:.2f} 秒\n")
            f.write(f"# 结果长度: {len(calibrated_result)} 字符\n")
            f.write(f"# ==========================================\n\n")
            f.write(calibrated_result)
        
        print(f"[保存] 结果已保存到: {output_file}")
        
    except Exception as e:
        end_time = time.time()
        duration = end_time - start_time
        print(f"[失败] 并发校对失败: {e}")
        print(f"[耗时] {duration:.2f} 秒（失败）")

def main():
    """主测试函数"""
    print("开始并发校对功能测试")
    print("=" * 50)
    
    try:
        test_concurrent_calibration()
        
        print("\n" + "=" * 50)
        print("并发校对测试完成")
        
    except Exception as e:
        print(f"\n[错误] 测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()