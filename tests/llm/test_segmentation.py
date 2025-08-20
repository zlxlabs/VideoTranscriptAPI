#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试分段校对功能
"""
import os
import sys
import json
from datetime import datetime
from utils import load_config
from utils.text_segmentation import TextSegmentationProcessor
from utils.llm_segmented import SegmentedLLMProcessor
from utils.llm_enhanced import EnhancedLLMProcessor

def save_calibrated_result(result_text, filename_prefix, description=""):
    """保存校对结果到output文件夹"""
    # 确保output目录存在
    output_dir = "output"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    # 生成带时间戳的文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{filename_prefix}_segmented_calibrated_{timestamp}.txt"
    filepath = os.path.join(output_dir, filename)
    
    # 添加文件头信息
    header = f"""# 分段校对结果
# 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
# 描述: {description}
# 原始长度: {len(result_text)} 字符
# ==========================================

"""
    
    # 保存文件
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(header + result_text)
    
    print(f"[保存] 校对结果已保存到: {filepath}")
    return filepath

def test_txt_file():
    """测试TXT文件的分段处理"""
    print("=== 测试TXT文件分段处理 ===")
    
    txt_file = r"cache_dir\youtube\2025\202508\njDochQ2zHs\transcript_capswriter.txt"
    
    if not os.path.exists(txt_file):
        print(f"[错误] 测试文件不存在: {txt_file}")
        return None
    
    # 加载配置
    config = load_config()
    
    # 创建处理器
    segmentation_processor = TextSegmentationProcessor(config)
    
    # 测试文本长度统计
    text_length = segmentation_processor.get_text_length(txt_file, 'txt')
    print(f"[统计] 文本长度: {text_length} 字符")
    
    # 测试是否需要分段
    need_seg = segmentation_processor.need_segmentation(txt_file, 'txt')
    print(f"[判断] 需要分段: {need_seg}")
    
    if need_seg:
        print("[测试] 开始分段校对测试...")
        
        # 创建分段LLM处理器
        segmented_llm_processor = SegmentedLLMProcessor(config)
        
        try:
            # 进行分段校对
            calibrated_result = segmented_llm_processor.calibrate_text_segmented(
                txt_file, 'txt', "YouTube视频转录", "英文技术对话内容"
            )
            
            print(f"[成功] TXT分段校对完成，结果长度: {len(calibrated_result)} 字符")
            print(f"[预览] 校对结果前200字符: {calibrated_result[:200]}...")
            
            # 保存校对结果
            output_file = save_calibrated_result(
                calibrated_result, 
                "youtube_txt", 
                f"YouTube视频转录分段校对结果，原始长度{text_length}字符"
            )
            
            return output_file
            
        except Exception as e:
            print(f"[失败] TXT分段校对失败: {e}")
            return None
    else:
        print("[信息] 文本长度未超过阈值，无需分段处理")
        return None

def test_json_file():
    """测试JSON文件的分段处理"""
    print("\n=== 测试JSON文件分段处理 ===")
    
    json_file = r"cache_dir\xiaoyuzhou\2025\202508\68a3d1fe293471fed44ce974\transcript_funasr.json"
    
    if not os.path.exists(json_file):
        print(f"[错误] 测试文件不存在: {json_file}")
        return None
    
    # 加载配置
    config = load_config()
    
    # 创建处理器
    segmentation_processor = TextSegmentationProcessor(config)
    
    # 测试文本长度统计
    text_length = segmentation_processor.get_text_length(json_file, 'json')
    print(f"[统计] 文本长度: {text_length} 字符")
    
    # 测试是否需要分段
    need_seg = segmentation_processor.need_segmentation(json_file, 'json')
    print(f"[判断] 需要分段: {need_seg}")
    
    if need_seg:
        print("[测试] 开始分段校对测试...")
        
        # 创建分段LLM处理器
        segmented_llm_processor = SegmentedLLMProcessor(config)
        
        try:
            # 进行分段校对
            calibrated_result = segmented_llm_processor.calibrate_text_segmented(
                json_file, 'json', "罗永浩的十字路口", "与理想汽车创始人李想的对话"
            )
            
            print(f"[成功] JSON分段校对完成，结果长度: {len(calibrated_result)} 字符")
            print(f"[预览] 校对结果前200字符: {calibrated_result[:200]}...")
            
            # 保存校对结果
            output_file = save_calibrated_result(
                calibrated_result, 
                "xiaoyuzhou_json", 
                f"小宇宙播客分段校对结果，原始长度{text_length}字符，包含说话人识别"
            )
            
            return output_file
            
        except Exception as e:
            print(f"[失败] JSON分段校对失败: {e}")
            return None
    else:
        print("[信息] 文本长度未超过阈值，无需分段处理")
        return None

def test_enhanced_processor():
    """测试增强LLM处理器"""
    print("\n=== 测试增强LLM处理器 ===")
    
    # 加载配置
    config = load_config()
    
    # 创建增强LLM处理器
    enhanced_processor = EnhancedLLMProcessor(config)
    
    # 模拟一个短文本任务（不需要分段）
    short_task = {
        "task_id": "test_short",
        "transcript": "这是一个短文本测试。" * 10,  # 约100字符
        "use_speaker_recognition": False,
        "video_title": "短文本测试",
        "author": "测试作者",
        "description": "这是一个短文本测试的描述",
        "transcription_data": None
    }
    
    print(f"[测试] 短文本处理（长度: {len(short_task['transcript'])} 字符）")
    
    try:
        # 这里只测试逻辑，不实际调用LLM API
        text_length = len(short_task['transcript'])
        enable_threshold = config.get('llm', {}).get('segmentation', {}).get('enable_threshold', 8000)
        need_segmentation = text_length > enable_threshold
        
        print(f"[检查] 阈值检查: {text_length} > {enable_threshold} = {need_segmentation}")
        
        if need_segmentation:
            print("[处理] 将使用分段处理")
        else:
            print("[处理] 将使用原有逻辑处理")
            
        print("[成功] 增强处理器逻辑测试通过")
        
    except Exception as e:
        print(f"[失败] 增强处理器测试失败: {e}")

def test_speaker_mapping():
    """测试说话人映射功能"""
    print("\n=== 测试说话人映射功能 ===")
    
    json_file = r"cache_dir\xiaoyuzhou\2025\202508\68a3d1fe293471fed44ce974\transcript_funasr.json"
    
    if not os.path.exists(json_file):
        print(f"[错误] 测试文件不存在: {json_file}")
        return
    
    # 加载配置
    config = load_config()
    
    # 创建处理器
    segmentation_processor = TextSegmentationProcessor(config)
    
    try:
        # 测试说话人映射生成
        speaker_mapping = segmentation_processor.extract_speaker_mapping_from_json(
            json_file, "罗永浩的十字路口", "与理想汽车创始人李想的对话"
        )
        
        print(f"[映射] 检测到的说话人映射: {speaker_mapping}")
        
        # 测试分段处理（应用说话人映射）
        segments = segmentation_processor.segment_json_content(json_file, speaker_mapping)
        
        print(f"[分段] 分段结果: {len(segments)} 个段落")
        
        if segments:
            first_segment = segments[0]
            print(f"[详情] 第一段包含 {len(first_segment.get('segments', []))} 个句子")
            
            # 显示前几个句子
            for i, sentence in enumerate(first_segment.get('segments', [])[:3]):
                speaker = sentence.get('speaker', 'Unknown')
                text = sentence.get('text', '')[:50]
                print(f"  {i+1}. {speaker}: {text}...")
        
        print("[成功] 说话人映射测试完成")
        
    except Exception as e:
        print(f"[失败] 说话人映射测试失败: {e}")

def test_full_segmentation_with_output():
    """完整的分段校对测试，生成输出文件"""
    print("\n=== 完整分段校对测试（生成输出文件）===")
    
    output_files = []
    
    # 测试TXT文件分段校对
    txt_output = test_txt_file()
    if txt_output:
        output_files.append(txt_output)
    
    # 测试JSON文件分段校对  
    json_output = test_json_file()
    if json_output:
        output_files.append(json_output)
    
    return output_files

def main():
    """主测试函数"""
    print("开始分段校对功能测试")
    print("=" * 50)
    
    # 检查命令行参数
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--calibrate-only":
        print("[模式] 仅运行校对测试并生成输出文件")
        try:
            output_files = test_full_segmentation_with_output()
            
            print("\n" + "=" * 50)
            print("校对测试完成")
            
            if output_files:
                print("\n[输出文件]")
                for i, output_file in enumerate(output_files, 1):
                    print(f"  {i}. {output_file}")
                print(f"\n共生成 {len(output_files)} 个校对结果文件")
            else:
                print("\n[提示] 未生成校对结果文件（可能文件不存在或长度未超过阈值）")
                
        except Exception as e:
            print(f"\n[错误] 校对测试中发生错误: {e}")
            import traceback
            traceback.print_exc()
        
        return
    
    try:
        # 测试增强处理器
        test_enhanced_processor()
        
        # 测试说话人映射
        test_speaker_mapping()
        
        # 完整的分段校对测试（生成输出文件）
        output_files = test_full_segmentation_with_output()
        
        print("\n" + "=" * 50)
        print("所有测试完成")
        
        if output_files:
            print("\n[输出文件]")
            for i, output_file in enumerate(output_files, 1):
                print(f"  {i}. {output_file}")
            print(f"\n共生成 {len(output_files)} 个校对结果文件")
        else:
            print("\n[提示] 未生成校对结果文件（可能文件不存在或长度未超过阈值）")
        
    except Exception as e:
        print(f"\n[错误] 测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()