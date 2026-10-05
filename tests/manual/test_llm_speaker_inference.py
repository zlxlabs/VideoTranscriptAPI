#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试LLM说话人推断功能（新架构）
"""
import os
import sys
import json
from datetime import datetime

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.append(os.path.join(project_root, 'src'))

from video_transcript_api.utils.logging import load_config
from video_transcript_api.llm import LLMCoordinator


def _load_funasr_segments(json_file: str):
    with open(json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get('segments', data.get('result', []))
    return []


def test_speaker_inference():
    """测试LLM说话人推断功能"""
    print("=== 测试LLM说话人推断功能（新架构） ===")

    json_file = r"cache_dir\bilibili\2025\202508\BV1Br8mzREBN\transcript_funasr.json"

    if not os.path.exists(json_file):
        print(f"[错误] 测试文件不存在: {json_file}")
        return

    config = load_config()

    output_dir = os.path.join(project_root, 'tests', 'llm', 'output')
    os.makedirs(output_dir, exist_ok=True)
    cache_dir = os.path.join(output_dir, 'cache')
    os.makedirs(cache_dir, exist_ok=True)

    coordinator = LLMCoordinator(config_dict=config, cache_dir=cache_dir)

    print(f"[文件] {os.path.basename(json_file)}")

    try:
        segments = _load_funasr_segments(json_file)
        if not segments:
            print("[错误] 未从FunASR文件中提取到segments")
            return

        print("[步骤1] 开始LLM说话人推断 + 结构化校对...")

        result = coordinator.process(
            content=segments,
            title="YouTube视频转录测试",
            author="",
            description="测试LLM说话人推断功能",
            platform="bilibili",
            media_id="BV1Br8mzREBN",
        )

        calibrated_text = result.get('calibrated_text', '')
        structured_data = result.get('structured_data', {})
        speaker_mapping = structured_data.get('speaker_mapping', {})
        dialogs = structured_data.get('dialogs', [])

        print("[成功] 处理完成")
        print(f"[结果] 说话人映射: {speaker_mapping}")
        print(f"[结果] 对话条数: {len(dialogs)}")
        print(f"[预览] {calibrated_text[:300]}...")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(output_dir, f"llm_speaker_inference_test_{timestamp}.txt")

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("# LLM说话人推断测试结果（新架构）\n")
            f.write(f"# 测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# 说话人映射: {speaker_mapping}\n")
            f.write(f"# 校对结果长度: {len(calibrated_text)} 字符\n")
            f.write("# ==========================================\n\n")
            f.write(calibrated_text)

        print(f"[保存] 结果已保存到: {output_file}")

        if speaker_mapping:
            if any(original_id in calibrated_text for original_id in speaker_mapping.keys()):
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
