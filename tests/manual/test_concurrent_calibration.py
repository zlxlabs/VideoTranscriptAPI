#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试并发校对功能（新架构）
"""
import os
import sys
import time

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(project_root, 'src'))

from video_transcript_api.llm import LLMCoordinator
from video_transcript_api.utils.logging import load_config


def test_concurrent_calibration():
    """测试并发校对功能"""
    print("=== 测试并发校对功能 ===")

    txt_file = r"cache_dir\youtube\2025\202508\njDochQ2zHs\transcript_capswriter.txt"

    if not os.path.exists(txt_file):
        print(f"[错误] 测试文件不存在: {txt_file}")
        return

    config = load_config()

    output_dir = os.path.join(project_root, 'tests', 'llm', 'output')
    os.makedirs(output_dir, exist_ok=True)
    cache_dir = os.path.join(output_dir, 'cache')
    os.makedirs(cache_dir, exist_ok=True)

    coordinator = LLMCoordinator(config_dict=config, cache_dir=cache_dir)

    print(f"[文件] {os.path.basename(txt_file)}")

    with open(txt_file, 'r', encoding='utf-8') as f:
        transcript_text = f.read()

    start_time = time.time()

    try:
        result = coordinator.process(
            content=transcript_text,
            title="YouTube视频转录测试",
            author="",
            description="并发校对功能测试",
            platform="youtube",
            media_id="njDochQ2zHs",
        )

        end_time = time.time()
        duration = end_time - start_time

        calibrated_text = result.get('calibrated_text', '')

        print("[成功] 并发校对完成")
        print(f"[耗时] {duration:.2f} 秒")
        print(f"[结果] {len(calibrated_text)} 字符")
        print(f"[预览] {calibrated_text[:200]}...")

        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(output_dir, f"concurrent_calibration_test_{timestamp}.txt")

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(f"# 并发校对测试结果\n")
            f.write(f"# 测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# 处理耗时: {duration:.2f} 秒\n")
            f.write(f"# 结果长度: {len(calibrated_text)} 字符\n")
            f.write("# ==========================================\n\n")
            f.write(calibrated_text)

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
