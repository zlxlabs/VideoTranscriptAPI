#!/usr/bin/env python3
"""
测试核心功能（不依赖API服务器运行）
"""
import os
import sys

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(project_root, "src"))


def test_core_features():
    """测试核心功能（pytest 入口）"""
    assert _run_core_features_cases()


def _run_core_features_cases():
    """实际执行核心功能检查（__main__ 脚本入口需要 bool 来算 sys.exit 退出码）。

    每一步都用真断言表达判定；不再吞异常返回 False。
    """
    print("开始测试Web查看核心功能...")

    # 1. 测试数据库初始化和任务创建
    print("\n步骤1: 测试数据库初始化和任务创建...")

    from video_transcript_api.cache import CacheManager
    cache_manager = CacheManager("./test_cache")

    # 创建测试任务
    task_info = cache_manager.create_task("https://www.youtube.com/watch?v=test123", False)
    task_id = task_info["task_id"]
    view_token = task_info["view_token"]

    print("[OK] 任务创建成功")
    print(f"     task_id: {task_id}")
    print(f"     view_token: {view_token}")

    # 验证任务信息
    retrieved_task = cache_manager.get_task_by_id(task_id)
    assert retrieved_task, "任务信息检索异常：查不到任务"
    assert retrieved_task["view_token"] == view_token, "任务信息检索异常：view_token 不一致"
    print("[OK] 任务信息检索正常")

    # 2. 测试任务状态更新
    print("\n步骤2: 测试任务状态更新...")

    cache_manager.update_task_status(task_id, "processing")
    updated_task = cache_manager.get_task_by_id(task_id)

    assert updated_task, "任务状态更新异常：查不到任务"
    assert updated_task["status"] == "processing", f"任务状态更新异常：status={updated_task['status']}"
    print("[OK] 任务状态更新正常")

    # 3. 测试view_token查询
    print("\n步骤3: 测试view_token查询...")

    token_task = cache_manager.get_task_by_view_token(view_token)
    assert token_task, "view_token查询异常：查不到任务"
    assert token_task["task_id"] == task_id, "view_token查询异常：task_id 不一致"
    print("[OK] view_token查询正常")

    # 4. 测试Markdown渲染
    print("\n步骤4: 测试Markdown渲染...")

    from video_transcript_api.utils.rendering import render_markdown_to_html

    test_markdown = """
# 测试标题

这是一个**测试**文本，包含：

## 功能特性

- 支持基本语法
- 支持代码块
- 支持表格

| 功能 | 状态 |
|------|------|
| 渲染 | 正常 |

```python
def test():
    return "Hello World"
```

> 这是引用块测试
        """

    rendered_html = render_markdown_to_html(test_markdown)

    # 检查关键元素（修复检测逻辑）
    required_patterns = [
        ("<h1", "h1标题"),
        ("<h2", "h2标题"),
        ("<strong>", "粗体"),
        ("<code>", "代码"),
        ("<table>", "表格"),
        ("<blockquote>", "引用块")
    ]

    missing_elements = [desc for pattern, desc in required_patterns if pattern not in rendered_html]

    assert not missing_elements, f"Markdown渲染缺少元素: {missing_elements}"
    print("[OK] Markdown渲染功能正常")
    print(f"     渲染长度: {len(rendered_html)} 字符")

    # 5. 测试配置文件中的基础URL
    print("\n步骤5: 测试基础URL配置...")

    from video_transcript_api.utils.rendering import get_base_url
    base_url = get_base_url()

    assert base_url, "基础URL配置异常"
    print(f"[OK] 基础URL配置正常: {base_url}")

    # 6. 测试查看页面数据获取
    print("\n步骤6: 测试查看页面数据获取...")

    # 模拟处理中状态
    view_data = cache_manager.get_view_data_by_token(view_token)

    assert view_data, f"页面数据异常: {view_data}"
    assert view_data["status"] == "processing", f"页面数据异常: status={view_data['status']}"
    print("[OK] 处理中状态页面数据正常")

    # 7. 测试企业微信链接生成
    print("\n步骤7: 测试企业微信链接生成...")

    from video_transcript_api.utils.notifications import send_view_link_wechat

    # 不实际发送，只测试链接生成（未配置 webhook 时不会发出请求）
    send_view_link_wechat("测试视频", view_token, None)
    print("[OK] 企业微信链接生成功能正常")

    print("\n[SUCCESS] 核心功能测试完成！")
    print("测试总结:")
    print("  [OK] 数据库初始化")
    print("  [OK] 任务创建和UUID生成")
    print("  [OK] View token生成和查询")
    print("  [OK] 任务状态管理")
    print("  [OK] Markdown渲染")
    print("  [OK] 基础URL配置")
    print("  [OK] 查看页面数据获取")
    print("  [OK] 企业微信链接生成")

    print(f"\n生成的查看链接: {get_base_url()}/view/{view_token}")

    # 清理测试数据
    import shutil
    try:
        # 关闭数据库连接
        cache_manager.close()
        if os.path.exists("./test_cache"):
            shutil.rmtree("./test_cache")
            print("[OK] 测试数据清理完成")
    except Exception as e:
        print(f"[WARN] 测试数据清理失败: {e}")
        # 不影响测试结果

    return True


if __name__ == "__main__":
    # 确保在项目根目录
    project_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_root)
    sys.path.insert(0, project_root)

    try:
        success = _run_core_features_cases()
    except AssertionError as e:
        print(f"[ERROR] 断言失败: {e}")
        sys.exit(1)
    sys.exit(0 if success else 1)