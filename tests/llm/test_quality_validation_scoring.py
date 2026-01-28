"""质量验证打分测试脚本

此脚本用于测试分段校对+质量打分的完整流程，收集打分数据并分析。

测试目标：
1. 评估当前 prompt 是否能有效区分质量差异
2. 收集多个样本的打分数据，分析分数分布
3. 评估边界值设置（overall_score_threshold, minimum_single_score）
"""

import sys
from pathlib import Path
from typing import Dict, List, Any
import statistics

try:
    import commentjson as json
except ImportError:
    import json

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.video_transcript_api.llm.core.config import LLMConfig
from src.video_transcript_api.llm.core.llm_client import LLMClient
from src.video_transcript_api.llm.core.key_info_extractor import KeyInfoExtractor
from src.video_transcript_api.llm.core.speaker_inferencer import SpeakerInferencer
from src.video_transcript_api.llm.core.quality_validator import QualityValidator
from src.video_transcript_api.llm.processors.speaker_aware_processor import SpeakerAwareProcessor
from src.video_transcript_api.utils.logging import setup_logger

logger = setup_logger(__name__)


class ValidationScoreCollector:
    """质量验证打分收集器"""

    def __init__(self):
        self.scores = []

    def add_score(self, chunk_index: int, validation_result: Dict):
        """添加打分结果"""
        self.scores.append({
            "chunk_index": chunk_index,
            "overall_score": validation_result.get("overall_score", 0),
            "passed": validation_result.get("passed", False),
            "scores": validation_result.get("scores", {}),
            "issues": validation_result.get("issues", []),
            "recommendation": validation_result.get("recommendation", ""),
        })

    def get_statistics(self) -> Dict:
        """获取统计信息"""
        if not self.scores:
            return {"error": "No scores collected"}

        overall_scores = [s["overall_score"] for s in self.scores]
        passed_count = sum(1 for s in self.scores if s["passed"])

        # 收集各维度分数
        dimension_scores = {
            "format_correctness": [],
            "content_fidelity": [],
            "text_quality": [],
            "speaker_consistency": [],
            "time_consistency": [],
        }

        for score in self.scores:
            for dimension, value in score["scores"].items():
                if dimension in dimension_scores:
                    dimension_scores[dimension].append(value)

        # 计算各维度统计
        dimension_stats = {}
        for dimension, values in dimension_scores.items():
            if values:
                dimension_stats[dimension] = {
                    "mean": statistics.mean(values),
                    "median": statistics.median(values),
                    "min": min(values),
                    "max": max(values),
                    "stdev": statistics.stdev(values) if len(values) > 1 else 0,
                }

        return {
            "total_chunks": len(self.scores),
            "passed_count": passed_count,
            "failed_count": len(self.scores) - passed_count,
            "pass_rate": passed_count / len(self.scores) if self.scores else 0,
            "overall_score_stats": {
                "mean": statistics.mean(overall_scores),
                "median": statistics.median(overall_scores),
                "min": min(overall_scores),
                "max": max(overall_scores),
                "stdev": statistics.stdev(overall_scores) if len(overall_scores) > 1 else 0,
            },
            "dimension_stats": dimension_stats,
        }

    def print_detailed_report(self):
        """打印详细报告"""
        print("\n" + "=" * 80)
        print("Quality Validation Scoring Report")
        print("=" * 80)

        if not self.scores:
            print("No scores collected")
            return

        # 打印每个 chunk 的详细结果
        print("\n--- Individual Chunk Scores ---")
        for score in self.scores:
            print(f"\nChunk {score['chunk_index']}:")
            print(f"  Overall Score: {score['overall_score']:.2f}")
            print(f"  Passed: {score['passed']}")
            print(f"  Dimension Scores:")
            for dimension, value in score['scores'].items():
                print(f"    - {dimension}: {value:.2f}")
            if score['issues']:
                print(f"  Issues:")
                for issue in score['issues']:
                    print(f"    - {issue}")
            if score['recommendation']:
                print(f"  Recommendation: {score['recommendation']}")

        # 打印统计信息
        stats = self.get_statistics()
        print("\n" + "-" * 80)
        print("--- Statistical Summary ---")
        print(f"\nTotal Chunks: {stats['total_chunks']}")
        print(f"Passed: {stats['passed_count']} ({stats['pass_rate']:.1%})")
        print(f"Failed: {stats['failed_count']} ({1 - stats['pass_rate']:.1%})")

        print(f"\nOverall Score Statistics:")
        for key, value in stats['overall_score_stats'].items():
            print(f"  {key.capitalize()}: {value:.2f}")

        print(f"\nDimension Score Statistics:")
        for dimension, dim_stats in stats['dimension_stats'].items():
            print(f"\n  {dimension}:")
            for key, value in dim_stats.items():
                print(f"    {key.capitalize()}: {value:.2f}")

        # 边界值分析
        print("\n" + "-" * 80)
        print("--- Threshold Analysis ---")
        current_overall_threshold = 8.0
        current_single_threshold = 7.0
        print(f"\nCurrent thresholds:")
        print(f"  overall_score_threshold: {current_overall_threshold}")
        print(f"  minimum_single_score: {current_single_threshold}")

        # 模拟不同阈值的通过率
        print(f"\nPass rate with different overall_score thresholds:")
        for threshold in [6.0, 7.0, 7.5, 8.0, 8.5, 9.0]:
            simulated_passed = sum(
                1 for s in self.scores
                if s['overall_score'] >= threshold and
                   all(score >= current_single_threshold for score in s['scores'].values())
            )
            rate = simulated_passed / len(self.scores)
            marker = " (current)" if threshold == current_overall_threshold else ""
            print(f"  {threshold:.1f}: {simulated_passed}/{len(self.scores)} ({rate:.1%}){marker}")

        print(f"\nPass rate with different minimum_single_score thresholds:")
        for threshold in [5.0, 6.0, 6.5, 7.0, 7.5, 8.0]:
            simulated_passed = sum(
                1 for s in self.scores
                if s['overall_score'] >= current_overall_threshold and
                   all(score >= threshold for score in s['scores'].values())
            )
            rate = simulated_passed / len(self.scores)
            marker = " (current)" if threshold == current_single_threshold else ""
            print(f"  {threshold:.1f}: {simulated_passed}/{len(self.scores)} ({rate:.1%}){marker}")

        print("\n" + "=" * 80)


def load_config() -> Dict:
    """加载配置文件（支持 JSONC）"""
    config_path = project_root / "config" / "config.jsonc"
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_test_data(num_segments: int = 30) -> tuple:
    """加载测试数据

    Args:
        num_segments: 要加载的片段数量（默认30个，约5-10分钟）

    Returns:
        (dialogs, metadata)
    """
    data_path = project_root / "data" / "cache" / "xiaoyuzhou" / "2026" / "202601" / "69788224cbeabe94f34495af" / "transcript_funasr.json"

    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 截取前 num_segments 个片段
    segments = data["segments"][:num_segments]

    # 转换为对话格式
    dialogs = []
    for seg in segments:
        dialogs.append({
            "speaker": seg["speaker"],
            "text": seg["text"],
            "start_time": seg["start_time"],
            "end_time": seg["end_time"],
        })

    metadata = {
        "title": "测试播客 - 中兴随身WiFi产品访谈",
        "author": "三五环",
        "description": "中兴随身WiFi产品总监赵鑫访谈",
        "platform": "xiaoyuzhou",
        "media_id": "69788224cbeabe94f34495af",
    }

    logger.info(f"Loaded test data: {len(dialogs)} dialogs from {len(segments)} segments")

    return dialogs, metadata


class QualityValidatorWrapper:
    """质量验证器包装类，用于收集打分数据"""

    def __init__(self, validator: QualityValidator, collector: ValidationScoreCollector):
        self.validator = validator
        self.collector = collector
        self.current_chunk_index = 0

    def validate_by_score(self, original, calibrated, video_metadata=None, selected_models=None):
        """包装的验证方法，自动收集打分结果"""
        result = self.validator.validate_by_score(
            original, calibrated, video_metadata, selected_models
        )

        # 收集打分结果
        self.collector.add_score(self.current_chunk_index, result)
        self.current_chunk_index += 1

        return result

    def validate_by_length(self, original, calibrated, min_ratio=0.80):
        """代理方法"""
        return self.validator.validate_by_length(original, calibrated, min_ratio)

    def _check_threshold(self, overall_score, scores):
        """代理方法"""
        return self.validator._check_threshold(overall_score, scores)

    # 代理其他属性
    def __getattr__(self, name):
        return getattr(self.validator, name)


def main():
    """主测试函数"""
    print("Starting quality validation scoring test...")

    # 1. 加载配置
    print("\n[1/5] Loading config...")
    config_dict = load_config()

    # 2. 创建 LLMConfig（强制启用验证）
    print("[2/5] Creating LLM config...")
    llm_config = LLMConfig.from_dict(config_dict)

    # 强制启用分段质量验证
    llm_config.enable_validation = True

    # 使用串行执行以确保打分结果的顺序与chunk索引一致
    llm_config.calibration_concurrent_limit = 1  # 串行执行，便于调试和收集数据

    # 可以调整其他参数以控制测试行为
    # llm_config.max_chunk_length = 2000  # 增大 chunk 大小
    # llm_config.min_chunk_length = 1000

    print(f"  Validation enabled: {llm_config.enable_validation}")
    print(f"  Validator model: {llm_config.validator_model}")
    print(f"  Overall score threshold: {llm_config.overall_score_threshold}")
    print(f"  Minimum single score: {llm_config.minimum_single_score}")

    # 3. 初始化组件
    print("\n[3/5] Initializing components...")
    llm_client = LLMClient(
        api_key=llm_config.api_key,
        base_url=llm_config.base_url,
        max_retries=llm_config.max_retries,
        retry_delay=llm_config.retry_delay,
        config=config_dict,
    )

    key_info_extractor = KeyInfoExtractor(
        llm_client=llm_client,
        model=llm_config.key_info_model or llm_config.calibrate_model,
        reasoning_effort=llm_config.key_info_reasoning_effort,
    )

    speaker_inferencer = SpeakerInferencer(
        llm_client=llm_client,
        model=llm_config.speaker_model or llm_config.calibrate_model,
        reasoning_effort=llm_config.speaker_reasoning_effort,
    )

    quality_validator = QualityValidator(
        llm_client=llm_client,
        model=llm_config.validator_model,
        reasoning_effort=llm_config.validator_reasoning_effort,
        overall_score_threshold=llm_config.overall_score_threshold,
        minimum_single_score=llm_config.minimum_single_score,
    )

    # 4. 加载测试数据
    print("\n[4/5] Loading test data...")
    dialogs, metadata = load_test_data(num_segments=30)  # 使用前30个片段

    # 5. 运行处理流程并收集打分数据
    print("\n[5/5] Processing with quality validation...")
    collector = ValidationScoreCollector()

    # 使用包装器包装 quality_validator 以收集打分数据
    wrapped_validator = QualityValidatorWrapper(quality_validator, collector)

    processor = SpeakerAwareProcessor(
        config=llm_config,
        llm_client=llm_client,
        key_info_extractor=key_info_extractor,
        speaker_inferencer=speaker_inferencer,
        quality_validator=wrapped_validator,
    )

    try:
        result = processor.process(
            dialogs=dialogs,
            title=metadata["title"],
            author=metadata["author"],
            description=metadata["description"],
            platform=metadata["platform"],
            media_id=metadata["media_id"],
            selected_models=llm_config.select_models_for_task(has_risk=False),
        )

        print(f"\nProcessing completed:")
        print(f"  Original length: {result['stats']['original_length']}")
        print(f"  Calibrated length: {result['stats']['calibrated_length']}")
        print(f"  Dialog count: {result['stats']['dialog_count']}")
        print(f"  Chunk count: {result['stats']['chunk_count']}")

    except Exception as e:
        logger.error(f"Processing failed: {e}", exc_info=True)
        print(f"\nERROR: Processing failed - {e}")
        return

    # 6. 打印详细报告
    collector.print_detailed_report()

    # 7. 保存结果到文件
    output_file = project_root / "tests" / "llm" / "validation_scoring_results.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump({
            "test_metadata": {
                "num_segments": 30,
                "platform": metadata["platform"],
                "media_id": metadata["media_id"],
                "validator_model": llm_config.validator_model,
                "overall_score_threshold": llm_config.overall_score_threshold,
                "minimum_single_score": llm_config.minimum_single_score,
            },
            "scores": collector.scores,
            "statistics": collector.get_statistics(),
        }, f, ensure_ascii=False, indent=2)

    print(f"\nResults saved to: {output_file}")


if __name__ == "__main__":
    main()
