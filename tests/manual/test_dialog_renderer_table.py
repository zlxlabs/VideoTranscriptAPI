#!/usr/bin/env python3
"""
测试对话渲染器的表格支持
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.rendering import DialogRenderer

def test_table_in_dialog_renderer():
    """测试对话渲染器的表格支持"""
    print("=== 测试对话渲染器的表格渲染 ===\n")

    # 包含表格的测试文本（模拟LLM总结内容）
    test_text = """
# 评分机制设计

以下是新的评分机制：

| 情境                | 评分规则示例（满分10分） | 行为激励目标         |
|---------------------|--------------------------|----------------------|
| 高自信（≥80%）正确  | +10分                    | 奖励准确判断         |
| 高自信错误          | -5分                     | 惩罚盲目自信         |
| 低自信（≤40%）IDK   | +3分                     | 鼓励诚实回避不确定性 |
| 低自信正确          | +6分                     | 奖励谨慎正确         |

## 关键特点

- **负分机制**：高自信错误将被扣分
- **基础分支持**：IDK获得基础分而不是零分
- **动态阈值**：可根据任务调整自信度要求

这种设计能够有效降低AI幻觉问题。
"""

    renderer = DialogRenderer()

    print("原始文本内容:")
    print(test_text)
    print("\n" + "="*50 + "\n")

    # 测试普通文本渲染（应该会调用Markdown渲染器）
    html_result = renderer._render_normal_text(test_text)

    print("渲染后的HTML:")
    print(html_result)
    print("\n" + "="*50 + "\n")

    # 检查关键元素
    success_checks = []

    if '<table>' in html_result:
        success_checks.append("✅ 表格渲染成功")
    else:
        success_checks.append("❌ 表格未渲染")

    if '<h1>' in html_result:
        success_checks.append("✅ 标题渲染成功")
    else:
        success_checks.append("❌ 标题未渲染")

    if '<li>' in html_result or '<ul>' in html_result:
        success_checks.append("✅ 列表渲染成功")
    else:
        success_checks.append("❌ 列表未渲染")

    if 'high自信' in html_result or '高自信' in html_result:
        success_checks.append("✅ 表格内容保留")
    else:
        success_checks.append("❌ 表格内容丢失")

    print("检查结果:")
    for check in success_checks:
        print(check)

    # 检查CSS类兼容性
    if '评分' in html_result and '<table>' in html_result:
        print("\n✅ 表格内容和结构都正确渲染")
        return True
    else:
        print("\n❌ 表格渲染存在问题")
        return False

if __name__ == "__main__":
    success = test_table_in_dialog_renderer()
    if success:
        print("\n🎉 测试通过！表格渲染功能正常工作")
    else:
        print("\n❌ 测试失败，需要进一步调试")
