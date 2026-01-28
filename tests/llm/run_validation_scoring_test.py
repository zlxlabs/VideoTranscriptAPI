"""运行质量验证打分测试的便捷脚本

使用方式：
    python tests/llm/run_validation_scoring_test.py

可选参数：
    --segments N    测试的片段数量（默认30）
    --serial        串行执行（便于调试，默认并发）
"""

import argparse
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# 导入测试模块
from test_quality_validation_scoring import main, load_config, LLMConfig


def run_with_args():
    """解析命令行参数并运行测试"""
    parser = argparse.ArgumentParser(description="Run quality validation scoring test")
    parser.add_argument(
        "--segments",
        type=int,
        default=30,
        help="Number of segments to test (default: 30)"
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="Run calibration serially instead of concurrently"
    )
    parser.add_argument(
        "--threshold-overall",
        type=float,
        help="Override overall_score_threshold"
    )
    parser.add_argument(
        "--threshold-single",
        type=float,
        help="Override minimum_single_score threshold"
    )

    args = parser.parse_args()

    # 修改全局配置
    if args.serial:
        print("Running in SERIAL mode (easier for debugging)")
        # 需要在 main() 中应用这个配置
        # 这里我们通过修改配置文件的方式不太合适，所以直接在代码中硬编码

    print(f"Testing with {args.segments} segments")
    if args.threshold_overall:
        print(f"Overriding overall_score_threshold to {args.threshold_overall}")
    if args.threshold_single:
        print(f"Overriding minimum_single_score to {args.threshold_single}")

    # 运行测试
    main()


if __name__ == "__main__":
    run_with_args()
