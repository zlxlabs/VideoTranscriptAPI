"""
测试缓存分析优化 - 验证不再重复分析 (已过时)

注意: 此测试已过时，因为缓存升级逻辑已被移除
保留此文件仅作为参考，不再需要运行

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
from video_transcript_api.cache import analyze_cache_capabilities
from video_transcript_api.utils.rendering import render_calibrated_content_smart

# 配置日志，监控 analyze_cache 调用
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
)


def test_cache_analysis_count():
    """测试缓存分析调用次数 (简化版 - 无升级逻辑)"""

    # 使用一个测试缓存目录
    test_cache_dir = r"data\cache\xiaoyuzhou\2025\202511\68ef3962bef7d51ca4042fb2"

    if not Path(test_cache_dir).exists():
        print(f"Test cache directory not found: {test_cache_dir}")
        print("Please provide a valid cache directory path")
        return

    print("=" * 80)
    print("Test: Simplified caching analysis (V2)")
    print("=" * 80)

    # V2方式：每次调用都分析（已简化，不再有升级逻辑）
    print("\n[V2] Calling analyze_cache_capabilities")
    cache_capabilities = analyze_cache_capabilities(test_cache_dir)

    print("\n[V2] Calling render_calibrated_content_smart")
    result = render_calibrated_content_smart(test_cache_dir)

    print(f"\nResult length: {len(result) if result else 0}")

    print("\n" + "=" * 80)
    print("Test completed!")
    print("=" * 80)
    print("\nNote: In V2, cache analysis is simple and doesn't have upgrade logic.")
    print("The analysis is only performed when needed.")


if __name__ == "__main__":
    test_cache_analysis_count()
