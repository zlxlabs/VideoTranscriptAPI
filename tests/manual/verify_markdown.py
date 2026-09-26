#!/usr/bin/env python3
"""
验证Markdown渲染结果
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

    # 检查原始内容是否包含表格标记
    print(f"原始内容包含 '|': {'|' in content}")
    print(f"原始内容包含 '---': {'---' in content}")

    # 查找表格标记的位置
    if '|' in content:
        pipe_index = content.find('|')
        print(f"第一个 '|' 位置: {pipe_index}")

        # 显示表格附近的原始内容
        start = max(0, pipe_index - 100)
        end = min(len(content), pipe_index + 300)
        context = content[start:end]
        print("\n表格附近的原始内容:")
        print(repr(context))

    # 测试Markdown渲染
    from video_transcript_api.utils.rendering import render_markdown_to_html

    try:
        html_result = render_markdown_to_html(content)
        print(f"\nMarkdown结果长度: {len(html_result)}")

        # 搜索各种标签
        tags_to_check = ['<table>', '<h1>', '<h2>', '<h3>', '<ul>', '<ol>', '<p>', '<div>']
        for tag in tags_to_check:
            count = html_result.count(tag)
            if count > 0:
                print(f"发现 {tag}: {count} 个")

        # 检查是否包含原始表格标记
        if '|' in html_result:
            print("HTML结果仍包含原始 '|' 字符")
            pipe_pos = html_result.find('|')
            start = max(0, pipe_pos - 50)
            end = min(len(html_result), pipe_pos + 150)
            context = html_result[start:end]
            print("HTML中表格标记附近:")
            print(repr(context))

        # 保存结果到文件供检查
        with open('debug_markdown_result.html', 'w', encoding='utf-8') as f:
            f.write(html_result)
        print("\n结果已保存到 debug_markdown_result.html")

    except Exception as e:
        print(f"出错: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
