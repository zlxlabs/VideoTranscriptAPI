#!/usr/bin/env python3
"""
简单的条件测试
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

def main():
    cache_dir = r"D:\prod\VideoTranscriptAPI\data\cache\bilibili\2025\202509\BV14AnVznEMp"

    calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')
    if not os.path.exists(calibrated_file):
        print("文件不存在")
        return

    with open(calibrated_file, 'r', encoding='utf-8') as f:
        content = f.read()

    print(f"原始内容长度: {len(content)}")

    # 测试Markdown渲染
    from video_transcript_api.utils.rendering import render_markdown_to_html

    try:
        html_result = render_markdown_to_html(content)
        print(f"Markdown结果长度: {len(html_result)}")

        # 检查关键条件
        has_table = '<table>' in html_result
        has_h1 = '<h1>' in html_result
        has_h2 = '<h2>' in html_result

        print(f"包含table: {has_table}")
        print(f"包含h1: {has_h1}")
        print(f"包含h2: {has_h2}")

        # 检查综合条件（模拟我修复的代码）
        conditions_met = (
            '<table>' in html_result or
            '<h1>' in html_result or
            '<h2>' in html_result or
            '<ul>' in html_result or
            '<ol>' in html_result or
            '<blockquote>' in html_result or
            '<pre>' in html_result
        )

        print(f"条件满足: {conditions_met}")

        if conditions_met:
            print("应该返回Markdown结果")
        else:
            print("应该降级")

        # 查找表格位置
        if has_table:
            table_pos = html_result.find('<table>')
            print(f"表格位置: {table_pos}")

            # 提取表格前后的内容
            start = max(0, table_pos - 50)
            end = min(len(html_result), table_pos + 200)
            context = html_result[start:end]
            print("表格附近内容:")
            print(repr(context))

    except Exception as e:
        print(f"出错: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
