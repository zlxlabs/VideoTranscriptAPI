#!/usr/bin/env python3
"""
测试完整的渲染流程，模拟从缓存文件到最终HTML的整个过程
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.rendering import DialogRenderer, render_calibrated_content_smart

def test_real_cache_rendering():
    """测试真实缓存目录的渲染"""
    print("=== 测试真实缓存目录的渲染流程 ===\n")

    # 使用真实的缓存目录
    cache_dir = r"D:\prod\VideoTranscriptAPI\data\cache\bilibili\2025\202509\BV14AnVznEMp"

    if not os.path.exists(cache_dir):
        print(f"❌ 缓存目录不存在: {cache_dir}")
        return False

    print(f"📁 测试缓存目录: {cache_dir}")

    # 列出目录中的文件
    files = os.listdir(cache_dir)
    print(f"📄 目录中的文件: {files}")

    # 检查校对文本文件
    calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')
    if os.path.exists(calibrated_file):
        print("✅ 发现校对文本文件")

        # 读取文件内容
        with open(calibrated_file, 'r', encoding='utf-8') as f:
            content = f.read()

        print(f"📝 校对文本内容长度: {len(content)} 字符")

        # 检查是否包含表格
        if '|' in content and '---' in content:
            print("✅ 校对文本包含表格标记")

            # 提取表格部分进行分析
            lines = content.split('\n')
            table_lines = []
            in_table = False

            for line in lines:
                if '|' in line:
                    in_table = True
                    table_lines.append(line)
                elif in_table and line.strip() == '':
                    break
                elif in_table:
                    table_lines.append(line)

            if table_lines:
                print("\n📊 发现的表格内容:")
                for i, line in enumerate(table_lines[:10]):  # 只显示前10行
                    print(f"  {i+1}: {line}")
                if len(table_lines) > 10:
                    print(f"  ... 共 {len(table_lines)} 行")
        else:
            print("❌ 校对文本不包含表格标记")
    else:
        print("❌ 未发现校对文本文件")
        return False

    print("\n" + "="*50)
    print("测试智能渲染函数")

    # 测试智能渲染函数
    try:
        html_result = render_calibrated_content_smart(cache_dir)

        if html_result:
            print("✅ 智能渲染函数成功返回结果")
            print(f"📏 HTML长度: {len(html_result)} 字符")

            # 检查是否包含表格
            if '<table>' in html_result:
                print("✅ HTML包含表格标签")

                # 计算表格数量
                table_count = html_result.count('<table>')
                print(f"📊 发现 {table_count} 个表格")

                # 提取第一个表格内容
                table_start = html_result.find('<table>')
                table_end = html_result.find('</table>', table_start) + 8
                if table_start != -1 and table_end != -1:
                    table_html = html_result[table_start:table_end]
                    print("\n🔍 第一个表格的HTML:")
                    print(table_html[:500] + "..." if len(table_html) > 500 else table_html)

            else:
                print("❌ HTML不包含表格标签")

                # 检查原始文本内容
                if '|' in html_result:
                    print("⚠️  HTML包含原始表格标记，但未转换为table标签")

                    # 查找表格标记附近的内容
                    pipe_index = html_result.find('|')
                    if pipe_index != -1:
                        start = max(0, pipe_index - 100)
                        end = min(len(html_result), pipe_index + 300)
                        context = html_result[start:end]
                        print("\n🔍 表格标记附近的HTML内容:")
                        print(context)

            print("\n" + "="*30)
            print("HTML结构分析:")

            elements = ['<table>', '<h1>', '<h2>', '<h3>', '<p>', '<ul>', '<li>', '<div>']
            for element in elements:
                count = html_result.count(element)
                if count > 0:
                    print(f"  {element}: {count}")

        else:
            print("❌ 智能渲染函数返回空结果")

    except Exception as e:
        print(f"❌ 智能渲染函数执行失败: {e}")
        import traceback
        traceback.print_exc()
        return False

    return True

def test_dialog_renderer_directly():
    """直接测试DialogRenderer"""
    print("\n" + "="*50)
    print("直接测试DialogRenderer")

    cache_dir = r"D:\prod\VideoTranscriptAPI\data\cache\bilibili\2025\202509\BV14AnVznEMp"
    calibrated_file = os.path.join(cache_dir, 'llm_calibrated.txt')

    if not os.path.exists(calibrated_file):
        print("❌ 校对文件不存在")
        return False

    with open(calibrated_file, 'r', encoding='utf-8') as f:
        content = f.read()

    renderer = DialogRenderer()

    # 测试不同的渲染方法
    print("\n1. 测试 _render_normal_text 方法:")
    try:
        result1 = renderer._render_normal_text(content)
        if '<table>' in result1:
            print("✅ _render_normal_text 包含表格")
        else:
            print("❌ _render_normal_text 不包含表格")

        print(f"📏 结果长度: {len(result1)}")
    except Exception as e:
        print(f"❌ _render_normal_text 失败: {e}")

    print("\n2. 测试 render_dialog_html 方法:")
    try:
        result2 = renderer.render_dialog_html(content)
        if '<table>' in result2:
            print("✅ render_dialog_html 包含表格")
        else:
            print("❌ render_dialog_html 不包含表格")

        print(f"📏 结果长度: {len(result2)}")
    except Exception as e:
        print(f"❌ render_dialog_html 失败: {e}")

if __name__ == "__main__":
    print("开始完整渲染流程测试...\n")

    success = test_real_cache_rendering()
    test_dialog_renderer_directly()

    if success:
        print("\n✅ 测试完成")
    else:
        print("\n❌ 测试发现问题")
