#!/usr/bin/env python3
"""
简化的渲染流程测试
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.rendering import DialogRenderer, render_calibrated_content_smart

def main():
    print("=== 测试表格渲染问题 ===\n")

    # 使用真实的缓存目录
    cache_dir = r"D:\prod\VideoTranscriptAPI\data\cache\bilibili\2025\202509\BV14AnVznEMp"

    if not os.path.exists(cache_dir):
        print(f"缓存目录不存在: {cache_dir}")
        return

    print(f"测试缓存目录: {cache_dir}")

    # 检查校对文本文件
    calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')
    if not os.path.exists(calibrated_file):
        print("校对文本文件不存在")
        return

    print("发现校对文本文件")

    # 读取文件内容
    with open(calibrated_file, 'r', encoding='utf-8') as f:
        content = f.read()

    print(f"校对文本内容长度: {len(content)} 字符")

    # 检查是否包含表格
    if '|' in content and ('---' in content or '|-' in content):
        print("校对文本包含表格标记")

        # 提取表格部分
        lines = content.split('\n')
        table_lines = []

        for i, line in enumerate(lines):
            if '|' in line and ('情境' in line or '评分' in line or '行为' in line):
                # 找到表格开始
                for j in range(max(0, i-2), min(len(lines), i+10)):
                    if '|' in lines[j]:
                        table_lines.append(lines[j])
                    elif lines[j].strip() == '' and table_lines:
                        break
                break

        if table_lines:
            print("\n发现的表格内容:")
            for i, line in enumerate(table_lines):
                print(f"  {i+1}: {line}")
    else:
        print("校对文本不包含表格标记")

    print("\n" + "="*50)

    # 测试智能渲染函数
    print("测试智能渲染函数")

    try:
        html_result = render_calibrated_content_smart(cache_dir)

        if html_result:
            print("智能渲染函数成功返回结果")
            print(f"HTML长度: {len(html_result)} 字符")

            # 检查是否包含表格
            if '<table>' in html_result:
                print("HTML包含表格标签 - 成功!")

                # 找到表格内容
                table_start = html_result.find('<table>')
                table_end = html_result.find('</table>', table_start) + 8
                if table_start != -1 and table_end != -1:
                    table_html = html_result[table_start:table_end]
                    print("\n表格HTML:")
                    print(table_html)

            else:
                print("HTML不包含表格标签 - 失败!")

                # 检查是否有原始表格标记
                if '|' in html_result:
                    print("HTML包含原始表格标记，未转换为table标签")

                    # 查找问题
                    pipe_index = html_result.find('|')
                    if pipe_index != -1:
                        start = max(0, pipe_index - 50)
                        end = min(len(html_result), pipe_index + 200)
                        context = html_result[start:end]
                        print("\n表格标记附近的HTML:")
                        print(repr(context))

        else:
            print("智能渲染函数返回空结果")

    except Exception as e:
        print(f"智能渲染函数执行失败: {e}")
        import traceback
        traceback.print_exc()

    # 直接测试DialogRenderer
    print("\n" + "="*30)
    print("直接测试DialogRenderer")

    renderer = DialogRenderer()

    # 测试 _render_normal_text
    print("\n测试 _render_normal_text:")
    try:
        result = renderer._render_normal_text(content)
        if '<table>' in result:
            print("_render_normal_text 包含表格 - 成功!")
        else:
            print("_render_normal_text 不包含表格 - 问题在这里!")

            # 进一步调试
            if '|' in result:
                print("但包含原始表格标记")
                pipe_index = result.find('|')
                if pipe_index != -1:
                    start = max(0, pipe_index - 50)
                    end = min(len(result), pipe_index + 200)
                    context = result[start:end]
                    print("表格附近内容:")
                    print(repr(context))

    except Exception as e:
        print(f"_render_normal_text 失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
