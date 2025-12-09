"""
测试缓存分析优化 - 验证不再重复分析

运行方式:
    python tests/manual/test_cache_analysis_optimization.py

说明:
    此脚本通过监控日志输出，验证在渲染流程中缓存分析只执行一次
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root / "src"))

import logging
from video_transcript_api.utils.cache import analyze_cache_capabilities
from video_transcript_api.utils.rendering import render_calibrated_content_smart
from video_transcript_api.api.routes.views import _trigger_cache_upgrade_if_needed

# 配置日志，监控 analyze_cache 调用
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s'
)

def test_cache_analysis_count():
    """测试缓存分析调用次数"""

    # 使用一个测试缓存目录
    test_cache_dir = r"data\cache\xiaoyuzhou\2025\202511\68ef3962bef7d51ca4042fb2"

    if not Path(test_cache_dir).exists():
        print(f"Test cache directory not found: {test_cache_dir}")
        print("Please provide a valid cache directory path")
        return

    print("=" * 80)
    print("Test 1: Old approach (without optimization) - 模拟旧的调用方式")
    print("=" * 80)

    # 旧的方式：每个函数都会分析一次
    print("\n[Old] Calling render_calibrated_content_smart (will analyze)")
    render_calibrated_content_smart(test_cache_dir, capabilities=None)

    print("\n[Old] Calling _trigger_cache_upgrade_if_needed (will analyze again)")
    view_data = {"title": "测试视频", "author": "测试作者"}
    _trigger_cache_upgrade_if_needed(test_cache_dir, view_data, capabilities=None)

    print("\n" + "=" * 80)
    print("Test 2: New approach (with optimization) - 使用优化后的调用方式")
    print("=" * 80)

    # 新的方式：只分析一次，然后复用
    print("\n[New] Analyzing cache once")
    cache_capabilities = analyze_cache_capabilities(test_cache_dir)

    print("\n[New] Calling render_calibrated_content_smart (reusing analysis)")
    render_calibrated_content_smart(test_cache_dir, capabilities=cache_capabilities)

    print("\n[New] Calling _trigger_cache_upgrade_if_needed (reusing analysis)")
    _trigger_cache_upgrade_if_needed(test_cache_dir, view_data, capabilities=cache_capabilities)

    print("\n" + "=" * 80)
    print("Test completed!")
    print("=" * 80)
    print("\nExpected results:")
    print("  - Test 1: analyze_cache should be called 2 times")
    print("  - Test 2: analyze_cache should be called 1 time only")
    print("\nPlease check the log output above to verify.")

if __name__ == "__main__":
    test_cache_analysis_count()
