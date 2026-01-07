#!/usr/bin/env python3
"""
调试渲染策略选择
注意：此脚本已简化以匹配新的V2缓存系统
移除了 mapped/detected 策略，只保留 structured 和 capswriter_long_text
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from video_transcript_api.utils.rendering import DialogRenderer
from video_transcript_api.utils.cache import analyze_cache_capabilities


def main():
    print("=== 调试渲染策略选择 (V2简化版) ===\n")

    cache_dir = r"D:\MyFolders\Developments\0Python\250427_VideoTranscriptApi\data\cache\bilibili\2025\202509\BV14AnVznEMp"

    if not os.path.exists(cache_dir):
        print(f"缓存目录不存在: {cache_dir}")
        return

    # 分析缓存能力
    capabilities = analyze_cache_capabilities(cache_dir)

    print("缓存能力分析:")
    print(f"  has_speaker_data: {capabilities.has_speaker_data}")
    print(f"  primary_engine: {capabilities.primary_engine}")
    print(f"  files_present: {capabilities.files_present}")

    # 获取渲染策略
    renderer = DialogRenderer()
    strategy = renderer._get_optimal_rendering_strategy(cache_dir)

    print(f"\n选择的渲染策略: {strategy}")

    # 测试结构化渲染
    if strategy == "structured":
        print("\n测试结构化渲染:")
        try:
            result = renderer._render_from_structured_data(cache_dir)
            if result:
                print(f"结果长度: {len(result)}")
                if "<dialog-container>" in result:
                    print("包含对话容器标签 - 成功!")
                else:
                    print("不包含对话容器标签 - 失败!")
            else:
                print("返回空结果")
        except Exception as e:
            print(f"结构化渲染失败: {e}")
            import traceback

            traceback.print_exc()

    # 测试CapsWriter长文本渲染
    elif strategy == "capswriter_long_text":
        print("\n测试CapsWriter长文本渲染:")
        try:
            result = renderer._render_capswriter_long_text(cache_dir)
            if result:
                print(f"结果长度: {len(result)}")
                if "<p>" in result:
                    print("包含段落标签 - 成功!")
                else:
                    print("不包含段落标签 - 失败!")
            else:
                print("返回空结果")
        except Exception as e:
            print(f"CapsWriter长文本渲染失败: {e}")
            import traceback

            traceback.print_exc()

    # 测试普通文本渲染
    else:
        print("\n测试普通文本渲染 (normal fallback):")
        calibrated_file = os.path.join(cache_dir, "llm_calibrated.txt")
        if os.path.exists(calibrated_file):
            with open(calibrated_file, "r", encoding="utf-8") as f:
                content = f.read()

            try:
                result = renderer.render_dialog_html(content)
                if result:
                    print(f"结果长度: {len(result)}")
                    if "<p>" in result:
                        print("包含段落标签 - 成功!")
                    else:
                        print("不包含段落标签 - 失败!")
                else:
                    print("返回空结果")
            except Exception as e:
                print(f"普通文本渲染失败: {e}")
                import traceback

                traceback.print_exc()
        else:
            print("没有找到校对文本文件")


if __name__ == "__main__":
    main()
