#!/usr/bin/env python3
"""
测试Markdown表格渲染功能
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.rendering import render_markdown_to_html

def test_table_rendering():
    """测试表格渲染"""
    print("=== 测试Markdown表格渲染 ===\n")

    # 测试用的表格内容
    test_markdown = """
# 测试表格

这是一个简单的表格：

| 情境                | 评分规则示例（满分10分） | 行为激励目标         |
|---------------------|--------------------------|----------------------|
| 高自信（≥80%）正确  | +10分                    | 奖励准确判断         |
| 高自信错误          | -5分                     | 惩罚盲目自信         |
| 低自信（≤40%）IDK   | +3分                     | 鼓励诚实回避不确定性 |
| 低自信正确          | +6分                     | 奖励谨慎正确         |

表格结束。
"""

    print("原始Markdown:")
    print(test_markdown)
    print("\n" + "="*50 + "\n")

    # 渲染为HTML
    html_result = render_markdown_to_html(test_markdown)

    print("渲染后的HTML:")
    print(html_result)
    print("\n" + "="*50 + "\n")

    # 检查是否包含表格标签
    if '<table>' in html_result:
        print("✅ 表格渲染成功！发现 <table> 标签")
    else:
        print("❌ 表格渲染失败！未发现 <table> 标签")

    # 检查其他表格元素
    table_elements = ['<table>', '<thead>', '<tbody>', '<tr>', '<th>', '<td>']
    for element in table_elements:
        if element in html_result:
            print(f"✅ 发现元素: {element}")
        else:
            print(f"❌ 缺少元素: {element}")

if __name__ == "__main__":
    test_table_rendering()
