#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Test markdown rendering for nested lists"""

import sys
sys.path.insert(0, 'src')

from video_transcript_api.utils.rendering.markdown_renderer import render_markdown_to_html

# Test case 1: Bold text followed by list (no blank line)
test_md_1 = """**应用场景：**
- 适用于所有使用空调进行冬季制热的用户，尤其是在南方地区。
- 尤其适合那些对空调制热效果不满意、有空气干燥困扰，或因担心电费而犹豫是否开启空调的用户。"""

# Test case 2: Bold text followed by nested list
test_md_2 = """**第一步：认知升级——理解空调制热的科学原理**
- **核心：** 摆脱"空调制热=费电=干燥"的简单负面认知。
- **关键认知点：**
    - **热空气的物理特性：** 认识到热空气密度较低，会自然向上聚集。
    - **湿度与温度的关系：** 理解空气的含水量与温度的关系。"""

print("=" * 80)
print("Test Case 1: Bold text + list")
print("=" * 80)
html1 = render_markdown_to_html(test_md_1)
print(html1)
print("\n" + "=" * 80)
print("Test Case 2: Bold text + nested list")
print("=" * 80)
html2 = render_markdown_to_html(test_md_2)
print(html2)

# Check if lists are rendered
if '<ul>' in html1:
    print("\n✓ Test 1 PASSED: List tags found")
else:
    print("\n✗ Test 1 FAILED: No list tags found")

if '<ul>' in html2:
    print("✓ Test 2 PASSED: List tags found")
else:
    print("✗ Test 2 FAILED: No list tags found")
