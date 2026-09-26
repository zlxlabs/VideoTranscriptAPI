#!/usr/bin/env python3
"""
直接测试修复的函数
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.rendering import DialogRenderer

def main():
    print("=== 直接测试修复的函数 ===\n")

    cache_dir = r"D:\prod\VideoTranscriptAPI\data\cache\bilibili\2025\202509\BV14AnVznEMp"

    if not os.path.exists(cache_dir):
        print(f"缓存目录不存在: {cache_dir}")
        return

    renderer = DialogRenderer()

    print("测试修复后的 _render_calibrated_text_detection:")
    try:
        result = renderer._render_calibrated_text_detection(cache_dir)
        if result:
            print(f"结果长度: {len(result)}")
            if '<table>' in result:
                print("包含表格标签 - 修复成功!")

                # 找到并显示表格内容
                table_start = result.find('<table>')
                table_end = result.find('</table>', table_start) + 8
                if table_start != -1 and table_end != -1:
                    table_html = result[table_start:table_end]
                    print("\n表格HTML片段:")
                    print(table_html[:300] + "..." if len(table_html) > 300 else table_html)

            else:
                print("不包含表格标签 - 修复失败!")

                # 检查是否调用了Markdown渲染
                if '|' in result:
                    print("仍包含原始表格标记")

                    # 检查前几行来判断问题
                    lines = result.split('\n')[:10]
                    for i, line in enumerate(lines):
                        if '|' in line:
                            print(f"行 {i}: {line}")
        else:
            print("返回空结果")
    except Exception as e:
        print(f"函数执行失败: {e}")
        import traceback
        traceback.print_exc()

    # 同时测试直接调用Markdown渲染器
    print("\n" + "="*30)
    print("直接测试Markdown渲染器:")

    calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')
    if os.path.exists(calibrated_file):
        with open(calibrated_file, 'r', encoding='utf-8') as f:
            content = f.read()

        from video_transcript_api.utils.rendering import render_markdown_to_html

        try:
            markdown_result = render_markdown_to_html(content)
            if markdown_result:
                print(f"Markdown结果长度: {len(markdown_result)}")
                if '<table>' in markdown_result:
                    print("Markdown渲染包含表格 - Markdown功能正常!")

                    # 显示表格
                    table_start = markdown_result.find('<table>')
                    table_end = markdown_result.find('</table>', table_start) + 8
                    if table_start != -1 and table_end != -1:
                        table_html = markdown_result[table_start:table_end]
                        print("\nMarkdown表格HTML:")
                        print(table_html[:300] + "..." if len(table_html) > 300 else table_html)
                else:
                    print("Markdown渲染不包含表格 - Markdown有问题!")
            else:
                print("Markdown返回空结果")
        except Exception as e:
            print(f"Markdown渲染失败: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    main()
