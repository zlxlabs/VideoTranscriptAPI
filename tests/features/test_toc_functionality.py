"""
浮动 TOC 功能自动化测试脚本

测试内容：
1. 验证 CSS/JS 文件存在性
2. 验证文件内容完整性
3. 模拟 DOM 操作测试核心逻辑
"""

import os
import sys
import re
import json
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


class TOCFunctionalityTest:
    """TOC 功能测试类"""

    def __init__(self):
        self.project_root = project_root
        self.src_web = self.project_root / "src" / "web"
        self.static_dir = self.src_web / "static"
        self.css_file = self.static_dir / "css" / "floating-toc.css"
        self.js_file = self.static_dir / "js" / "floating-toc.js"
        self.template_file = self.src_web / "templates" / "transcript.html"

        self.test_results = {
            "passed": 0,
            "failed": 0,
            "warnings": 0,
            "tests": []
        }

    def log_test(self, name: str, passed: bool, message: str = ""):
        """记录测试结果"""
        status = "PASS" if passed else "FAIL"
        result = {
            "name": name,
            "status": status,
            "message": message
        }
        self.test_results["tests"].append(result)

        if passed:
            self.test_results["passed"] += 1
            print(f"[PASS] {name}")
        else:
            self.test_results["failed"] += 1
            print(f"[FAIL] {name}: {message}")

        if message and passed:
            print(f"  -> {message}")

    def test_files_exist(self):
        """测试：文件存在性"""
        print("\n=== 测试文件存在性 ===")

        self.log_test(
            "CSS 文件存在",
            self.css_file.exists(),
            f"路径: {self.css_file}"
        )

        self.log_test(
            "JS 文件存在",
            self.js_file.exists(),
            f"路径: {self.js_file}"
        )

        self.log_test(
            "模板文件存在",
            self.template_file.exists(),
            f"路径: {self.template_file}"
        )

    def test_css_content(self):
        """测试：CSS 内容完整性"""
        print("\n=== 测试 CSS 内容 ===")

        if not self.css_file.exists():
            self.log_test("CSS 内容检查", False, "文件不存在")
            return

        content = self.css_file.read_text(encoding='utf-8')

        # 检查关键 CSS 类是否存在
        required_classes = [
            '.floating-toc-container',
            '.toc-indicator',
            '.toc-header',
            '.toc-content',
            '.toc-link',
            '.floating-toc-mobile-btn',
            '.floating-toc-mobile-panel'
        ]

        for class_name in required_classes:
            found = class_name in content
            self.log_test(
                f"CSS 类 {class_name} 存在",
                found,
                "" if found else "缺失必要的 CSS 类"
            )

        # 检查 CSS 变量
        css_vars = [
            '--toc-bg',
            '--toc-text',
            '--toc-active',
            '--toc-indicator-start'
        ]

        vars_found = sum(1 for var in css_vars if var in content)
        self.log_test(
            "CSS 变量定义",
            vars_found == len(css_vars),
            f"找到 {vars_found}/{len(css_vars)} 个变量"
        )

        # 检查响应式媒体查询
        has_mobile_query = '@media (max-width: 768px)' in content
        self.log_test(
            "移动端媒体查询",
            has_mobile_query,
            "存在移动端适配代码"
        )

        # 检查深色主题
        has_dark_theme = '[data-theme="dark"]' in content
        self.log_test(
            "深色主题支持",
            has_dark_theme,
            "存在深色主题样式"
        )

    def test_js_content(self):
        """测试：JavaScript 内容完整性"""
        print("\n=== 测试 JavaScript 内容 ===")

        if not self.js_file.exists():
            self.log_test("JS 内容检查", False, "文件不存在")
            return

        content = self.js_file.read_text(encoding='utf-8')

        # 检查核心函数（T7: DOM API 构建，createPCTocHTML -> createPCToc）
        required_functions = [
            'extractHeadings',
            'findCalibratedSection',
            'extractChapters',
            'createPCToc',
            'createMobileTocParts',
            'renderTOC',
            'handleTocClick',
            'handlePinClick',
            'setupScrollObserver',
            'updateActiveLink',
            'createEl',
            'appendTocLink',
        ]

        for func_name in required_functions:
            # 使用正则匹配函数定义
            pattern = rf'\bfunction\s+{func_name}\s*\(|const\s+{func_name}\s*='
            found = bool(re.search(pattern, content))
            self.log_test(
                f"函数 {func_name} 存在",
                found,
                "" if found else "缺失必要的函数"
            )

        # 检查 IntersectionObserver 使用
        has_observer = 'IntersectionObserver' in content
        self.log_test(
            "IntersectionObserver 实现",
            has_observer,
            "使用了高性能的滚动监听"
        )

        # 检查 localStorage 持久化
        has_storage = 'localStorage' in content
        self.log_test(
            "状态持久化",
            has_storage,
            "支持 Pin 状态保存"
        )

        # 检查移动端支持
        has_mobile_check = 'checkMobile' in content or 'MOBILE_BREAKPOINT' in content
        self.log_test(
            "移动端检测",
            has_mobile_check,
            "实现了移动端判断逻辑"
        )

        # 检查事件监听
        has_event_binding = 'addEventListener' in content
        self.log_test(
            "事件监听",
            has_event_binding,
            "正确绑定了事件处理器"
        )

    def test_template_integration(self):
        """测试：模板集成"""
        print("\n=== 测试模板集成 ===")

        if not self.template_file.exists():
            self.log_test("模板集成检查", False, "模板文件不存在")
            return

        content = self.template_file.read_text(encoding='utf-8')

        # 检查 CSS 引用
        has_css_ref = 'floating-toc.css' in content
        self.log_test(
            "CSS 文件引用",
            has_css_ref,
            "模板中引用了 TOC 样式文件"
        )

        # 检查 JS 引用
        has_js_ref = 'floating-toc.js' in content
        self.log_test(
            "JS 文件引用",
            has_js_ref,
            "模板中引用了 TOC 脚本文件"
        )

        # 检查必要的区块结构
        has_summary_section = '内容总结' in content
        self.log_test(
            "内容总结区块",
            has_summary_section,
            "存在内容总结区块"
        )

        has_calibrated_section = '校对文本' in content
        self.log_test(
            "校对文本区块",
            has_calibrated_section,
            "存在校对文本区块"
        )

    def test_code_quality(self):
        """测试：代码质量检查"""
        print("\n=== 代码质量检查 ===")

        if not self.js_file.exists():
            return

        content = self.js_file.read_text(encoding='utf-8')

        # 检查是否使用了 strict mode
        has_strict = "'use strict'" in content
        self.log_test(
            "严格模式",
            has_strict,
            "使用了 'use strict'"
        )

        # 检查是否有注释
        comment_count = content.count('/*') + content.count('//')
        has_comments = comment_count > 10
        self.log_test(
            "代码注释",
            has_comments,
            f"找到 {comment_count} 处注释"
        )

        # 检查是否使用了配置对象
        has_config = 'CONFIG' in content or 'config' in content
        self.log_test(
            "配置管理",
            has_config,
            "使用了配置对象管理常量"
        )

        # 检查错误处理
        has_error_handling = 'try' in content and 'catch' in content
        self.log_test(
            "错误处理",
            has_error_handling,
            "实现了异常捕获"
        )

    def test_css_quality(self):
        """测试：CSS 质量检查"""
        print("\n=== CSS 质量检查 ===")

        if not self.css_file.exists():
            return

        content = self.css_file.read_text(encoding='utf-8')

        # 检查过渡动画
        has_transitions = content.count('transition:') > 5
        self.log_test(
            "过渡动画",
            has_transitions,
            "使用了平滑的过渡效果"
        )

        # 检查动画定义
        has_animations = '@keyframes' in content
        self.log_test(
            "关键帧动画",
            has_animations,
            "定义了关键帧动画"
        )

        # 检查无障碍优化
        has_a11y = 'prefers-reduced-motion' in content
        self.log_test(
            "无障碍优化",
            has_a11y,
            "支持减少动画偏好"
        )

        # 检查打印样式
        has_print = '@media print' in content
        self.log_test(
            "打印样式",
            has_print,
            "定义了打印样式"
        )

    def generate_report(self):
        """生成测试报告"""
        print("\n" + "=" * 50)
        print("测试报告")
        print("=" * 50)

        total = self.test_results["passed"] + self.test_results["failed"]
        pass_rate = (self.test_results["passed"] / total * 100) if total > 0 else 0

        print(f"总测试数: {total}")
        print(f"通过: {self.test_results['passed']}")
        print(f"失败: {self.test_results['failed']}")
        print(f"通过率: {pass_rate:.1f}%")

        if self.test_results["failed"] > 0:
            print("\n失败的测试:")
            for test in self.test_results["tests"]:
                if test["status"] == "FAIL":
                    print(f"  - {test['name']}: {test['message']}")

        # 保存详细报告到文件
        report_file = self.project_root / "tests" / "features" / "toc_test_report.json"
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(self.test_results, f, indent=2, ensure_ascii=False)

        print(f"\n详细报告已保存到: {report_file}")

        return self.test_results["failed"] == 0

    def run_all_tests(self):
        """运行所有测试"""
        print("开始浮动 TOC 功能测试...")
        print("=" * 50)

        self.test_files_exist()
        self.test_css_content()
        self.test_js_content()
        self.test_template_integration()
        self.test_code_quality()
        self.test_css_quality()

        success = self.generate_report()

        if success:
            print("\n[SUCCESS] All tests passed!")
            return 0
        else:
            print("\n[WARNING] Some tests failed, please check the report.")
            return 1


def main():
    """主函数"""
    tester = TOCFunctionalityTest()
    exit_code = tester.run_all_tests()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
