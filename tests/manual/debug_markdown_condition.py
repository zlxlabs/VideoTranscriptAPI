#!/usr/bin/env python3
"""
调试Markdown条件判断
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

def main():
    print("=== 调试Markdown条件判断 ===\n")

    cache_dir = r"D:\prod\VideoTranscriptAPI\data\cache\bilibili\2025\202509\BV14AnVznEMp"

    calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')
    if not os.path.exists(calibrated_file):
        print("校对文件不存在")
        return

    with open(calibrated_file, 'r', encoding='utf-8') as f:
        content = f.read()

    print(f"原始内容长度: {len(content)}")

    # 测试Markdown渲染
    from video_transcript_api.utils.rendering import render_markdown_to_html

    try:
        html_result = render_markdown_to_html(content)
        print(f"Markdown结果长度: {len(html_result)}")

        # 检查各种条件
        conditions = {
            '<table>': '<table>' in html_result,
            '<h1>': '<h1>' in html_result,
            '<h2>': '<h2>' in html_result,
            '<ul>': '<ul>' in html_result,
            '<ol>': '<ol>' in html_result,
            '<blockquote>': '<blockquote>' in html_result,
            '<pre>': '<pre>' in html_result
        }

        print("\n条件检查:")
        for condition, result in conditions.items():
            status = "✓" if result else "✗"
            print(f"  {status} {condition}: {result}")

        # 检查综合条件
        has_structured = any(conditions.values())
        print(f"\n综合条件 (any): {has_structured}")

        if has_structured:
            print("应该使用Markdown渲染结果")
        else:
            print("应该降级到对话渲染")

        # 显示前几行HTML
        print("\nHTML前5行:")
        lines = html_result.split('\n')[:5]
        for i, line in enumerate(lines):
            print(f"  {i+1}: {line}")

    except Exception as e:
        print(f"Markdown渲染失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
