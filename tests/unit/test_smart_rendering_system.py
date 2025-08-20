# -*- coding: utf-8 -*-
"""
智能渲染系统集成测试
测试说话人映射推断、缓存分析和智能渲染的完整流程
"""
import os
import sys
import tempfile
import json
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.video_transcript_api.utils.speaker_mapping import SpeakerMappingInference, infer_speaker_mapping_from_cache
from src.video_transcript_api.utils.cache_analyzer import CacheCapabilityAnalyzer, analyze_cache_capabilities  
from src.video_transcript_api.utils.dialog_renderer import render_transcript_content_smart

def create_test_cache_directory():
    """创建测试用的缓存目录结构"""
    # 创建临时目录
    temp_dir = tempfile.mkdtemp()
    
    # 创建FunASR转录数据
    funasr_data = [
        {"spk": "speaker1", "text": "嗯，欢迎来到知行小酒馆，这是一档有知有行出品的播客节目。我是羽白。"},
        {"spk": "speaker2", "text": "承认自己是弱者，不是自我否定，反而能让人活得更有生命力。"},
        {"spk": "speaker1", "text": "这是中年危机的一种褒义的说法。终于我们把小酒馆听众最热爱的嘉宾之一少楠又请回来了。"},
        {"spk": "speaker2", "text": "是的，小酒馆是对不起的，对吧？"}
    ]
    
    funasr_file = os.path.join(temp_dir, 'transcript_funasr.json')
    with open(funasr_file, 'w', encoding='utf-8') as f:
        json.dump(funasr_data, f, ensure_ascii=False, indent=2)
    
    # 创建校对文本
    calibrated_text = """知白：嗯，欢迎来到知行小酒馆，这是一档有知有行出品的播客节目。我是羽白。

少楠：承认自己是弱者，不是自我否定，反而能让人活得更有生命力。

知白：这是中年危机的一种褒义的说法。终于我们把小酒馆听众最热爱的嘉宾之一少楠又请回来了。

少楠：是的，小酒馆是对不起的，对吧？"""
    
    calibrated_file = os.path.join(temp_dir, 'llm_calibrated.txt')
    with open(calibrated_file, 'w', encoding='utf-8') as f:
        f.write(calibrated_text)
    
    # 创建总结文本
    summary_text = """## 播客概览
这是知行小酒馆的一期节目，主持人羽白与嘉宾少楠的对话。

## 核心观点
少楠分享了关于"承认自己是弱者"的观点，认为这不是自我否定，反而能让人活得更有生命力。"""
    
    summary_file = os.path.join(temp_dir, 'llm_summary.txt')
    with open(summary_file, 'w', encoding='utf-8') as f:
        f.write(summary_text)
    
    return temp_dir

def test_speaker_mapping_inference():
    """测试说话人映射推断"""
    print("=== 测试说话人映射推断 ===")
    
    # 创建测试缓存
    cache_dir = create_test_cache_directory()
    
    try:
        # 测试说话人映射推断
        mapping = infer_speaker_mapping_from_cache(cache_dir)
        
        print(f"缓存目录: {cache_dir}")
        print(f"推断的映射关系: {mapping}")
        
        if mapping:
            print("✓ 说话人映射推断成功")
            # 验证映射关系
            expected_speakers = {'speaker1', 'speaker2'}
            actual_speakers = set(mapping.keys())
            if expected_speakers == actual_speakers:
                print("✓ 映射关系包含所有原始说话人")
            else:
                print(f"⚠ 映射关系异常: 期望 {expected_speakers}, 实际 {actual_speakers}")
        else:
            print("✗ 说话人映射推断失败")
        
        return mapping is not None
        
    finally:
        # 清理临时文件
        import shutil
        shutil.rmtree(cache_dir, ignore_errors=True)

def test_cache_analysis():
    """测试缓存能力分析"""
    print("\n=== 测试缓存能力分析 ===")
    
    cache_dir = create_test_cache_directory()
    
    try:
        # 测试缓存分析
        capabilities = analyze_cache_capabilities(cache_dir)
        
        print(f"缓存目录: {cache_dir}")
        print(f"格式版本: {capabilities.format_version}")
        print(f"主要引擎: {capabilities.primary_engine}")
        print(f"有说话人数据: {capabilities.has_speaker_data}")
        print(f"有结构化输出: {capabilities.has_structured_output}")
        print(f"说话人列表: {capabilities.speakers_list}")
        print(f"升级优先级: {capabilities.upgrade_priority}")
        
        # 验证分析结果
        success = True
        
        if capabilities.format_version != 'v1':
            print("✗ 格式版本检测错误")
            success = False
        
        if capabilities.primary_engine != 'funasr':
            print("✗ 主要引擎检测错误")
            success = False
        
        if not capabilities.has_speaker_data:
            print("✗ 说话人数据检测错误")
            success = False
        
        if capabilities.has_structured_output:
            print("✗ 结构化输出检测错误")
            success = False
        
        if success:
            print("✓ 缓存能力分析正确")
        
        return success
        
    finally:
        import shutil
        shutil.rmtree(cache_dir, ignore_errors=True)

def test_smart_rendering():
    """测试智能渲染"""
    print("\n=== 测试智能渲染 ===")
    
    cache_dir = create_test_cache_directory()
    
    try:
        # 测试智能渲染
        html_output = render_transcript_content_smart(cache_dir)
        
        print(f"缓存目录: {cache_dir}")
        print(f"渲染HTML长度: {len(html_output)}")
        
        # 检查关键元素
        checks = {
            'dialog-container': 'dialog-container' in html_output,
            'speaker-tag': 'speaker-tag' in html_output,
            'dialog-content': 'dialog-content' in html_output,
            '知白': '知白' in html_output,
            '少楠': '少楠' in html_output
        }
        
        print("渲染结果检查:")
        all_passed = True
        for check_name, passed in checks.items():
            status = "✓" if passed else "✗"
            print(f"  {status} {check_name}: {passed}")
            if not passed:
                all_passed = False
        
        if all_passed:
            print("✓ 智能渲染成功")
        else:
            print("✗ 智能渲染存在问题")
        
        # 显示渲染结果的一部分
        print(f"\n渲染结果预览:")
        preview = html_output[:300] + "..." if len(html_output) > 300 else html_output
        print(preview)
        
        return all_passed
        
    finally:
        import shutil
        shutil.rmtree(cache_dir, ignore_errors=True)

def test_structured_data_rendering():
    """测试结构化数据渲染"""
    print("\n=== 测试结构化数据渲染 ===")
    
    cache_dir = create_test_cache_directory()
    
    try:
        # 创建结构化数据文件
        structured_data = {
            "format_version": "v2",
            "video_metadata": {
                "video_title": "知行小酒馆测试",
                "author": "有知有行"
            },
            "speaker_mapping": {
                "speaker1": "知白",
                "speaker2": "少楠"
            },
            "dialogs": [
                {
                    "speaker": "知白",
                    "content": "嗯，欢迎来到知行小酒馆，这是一档有知有行出品的播客节目。我是羽白。"
                },
                {
                    "speaker": "少楠", 
                    "content": "承认自己是弱者，不是自我否定，反而能让人活得更有生命力。"
                },
                {
                    "speaker": "知白",
                    "content": "这是中年危机的一种褒义的说法。终于我们把小酒馆听众最热爱的嘉宾之一少楠又请回来了。"
                },
                {
                    "speaker": "少楠",
                    "content": "是的，小酒馆是对不起的，对吧？"
                }
            ]
        }
        
        # 保存结构化数据
        structured_file = os.path.join(cache_dir, 'llm_processed.json')
        with open(structured_file, 'w', encoding='utf-8') as f:
            json.dump(structured_data, f, ensure_ascii=False, indent=2)
        
        # 测试结构化渲染
        html_output = render_transcript_content_smart(cache_dir)
        
        print(f"结构化渲染HTML长度: {len(html_output)}")
        
        # 检查关键元素
        success = (
            'dialog-container' in html_output and
            'speaker-tag' in html_output and
            '知白' in html_output and
            '少楠' in html_output and
            '知行小酒馆' in html_output
        )
        
        if success:
            print("✓ 结构化数据渲染成功")
        else:
            print("✗ 结构化数据渲染失败")
        
        return success
        
    finally:
        import shutil
        shutil.rmtree(cache_dir, ignore_errors=True)

def main():
    """运行所有测试"""
    print("开始智能渲染系统集成测试...\n")
    
    tests = [
        test_speaker_mapping_inference,
        test_cache_analysis,
        test_smart_rendering,
        test_structured_data_rendering
    ]
    
    results = []
    for test_func in tests:
        try:
            result = test_func()
            results.append(result)
        except Exception as e:
            print(f"✗ 测试 {test_func.__name__} 发生异常: {e}")
            results.append(False)
    
    # 总结结果
    print(f"\n=== 测试结果总结 ===")
    passed = sum(results)
    total = len(results)
    
    print(f"通过: {passed}/{total}")
    
    if passed == total:
        print("🎉 所有测试通过！智能渲染系统工作正常")
    else:
        print(f"⚠ 有 {total - passed} 个测试失败，需要检查")
    
    return passed == total

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)