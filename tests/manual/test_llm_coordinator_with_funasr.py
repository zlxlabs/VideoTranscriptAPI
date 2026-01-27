"""测试 LLM Coordinator 完整流程（使用真实 FunASR 转录数据）"""

import json
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root / "src"))

from video_transcript_api.llm.coordinator import LLMCoordinator
from video_transcript_api.utils.logging import setup_logger, load_config

logger = setup_logger(__name__)


def load_funasr_result(cache_file: Path) -> dict:
    """加载 FunASR 转录结果

    Args:
        cache_file: transcript_funasr.json 文件路径

    Returns:
        转录结果字典
    """
    logger.info(f"Loading FunASR result from: {cache_file}")

    with open(cache_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    segments = data.get("segments", [])
    speakers = data.get("speakers", [])

    logger.info(f"Loaded {len(segments)} segments, {len(speakers)} speakers")

    return data


def test_coordinator_with_funasr():
    """测试 Coordinator 完整流程"""

    # 1. 加载配置
    logger.info("Step 1: Loading configuration")
    config_dict = load_config()

    # 2. 加载 FunASR 转录结果
    logger.info("Step 2: Loading FunASR transcription result")
    cache_file = (
        project_root
        / "data"
        / "cache"
        / "xiaoyuzhou"
        / "2026"
        / "202601"
        / "68f7975f456ffec65ede5e47"
        / "transcript_funasr.json"
    )

    if not cache_file.exists():
        logger.error(f"FunASR result file not found: {cache_file}")
        return

    funasr_data = load_funasr_result(cache_file)
    segments = funasr_data.get("segments", [])

    # 3. 初始化 Coordinator
    logger.info("Step 3: Initializing LLM Coordinator")
    cache_dir = str(project_root / "data" / "cache")
    coordinator = LLMCoordinator(config_dict=config_dict, cache_dir=cache_dir)

    # 4. 准备元数据
    logger.info("Step 4: Preparing metadata")
    title = "99.身边的恋人让你烦了？这期节目听完你更爱TA了！"
    author = "三里人大俱乐部"
    description = "相亲约会故事分享"
    platform = "xiaoyuzhou"
    media_id = "68f7975f456ffec65ede5e47"

    logger.info(f"Title: {title}")
    logger.info(f"Segments count: {len(segments)}")
    logger.info(f"Platform: {platform}, Media ID: {media_id}")

    # 5. 执行完整流程
    logger.info("Step 5: Processing with LLM Coordinator")
    logger.info("=" * 80)

    try:
        result = coordinator.process(
            content=segments,  # 直接传递 segments 列表
            title=title,
            author=author,
            description=description,
            platform=platform,
            media_id=media_id,
            has_risk=False,
        )

        # 6. 输出结果
        logger.info("=" * 80)
        logger.info("Step 6: Processing completed successfully!")
        logger.info("=" * 80)

        # 统计信息
        stats = result.get("stats", {})
        logger.info("Statistics:")
        logger.info(f"  - Original length: {stats.get('original_length', 0)} chars")
        logger.info(f"  - Calibrated length: {stats.get('calibrated_length', 0)} chars")
        logger.info(f"  - Summary length: {stats.get('summary_length', 0)} chars")
        logger.info(f"  - Dialog count: {stats.get('dialog_count', 0)}")
        logger.info(f"  - Chunk count: {stats.get('chunk_count', 0)}")

        # 关键信息
        key_info = result.get("key_info", {})
        logger.info("\nKey Information:")
        logger.info(f"  - Names: {len(key_info.get('names', []))} items")
        logger.info(f"  - Technical terms: {len(key_info.get('technical_terms', []))} items")
        logger.info(f"  - Brands: {len(key_info.get('brands', []))} items")

        # 说话人映射
        structured_data = result.get("structured_data", {})
        if structured_data:
            speaker_mapping = structured_data.get("speaker_mapping", {})
            logger.info("\nSpeaker Mapping:")
            for speaker_id, name in speaker_mapping.items():
                logger.info(f"  - {speaker_id} → {name}")

        # 保存结果
        output_dir = project_root / "tests" / "manual" / "output"
        output_dir.mkdir(exist_ok=True)

        # 保存校对文本
        calibrated_text = result.get("calibrated_text", "")
        calibrated_file = output_dir / "calibrated_text.txt"
        with open(calibrated_file, "w", encoding="utf-8") as f:
            f.write(calibrated_text)
        logger.info(f"\nCalibrated text saved to: {calibrated_file}")

        # 保存总结文本
        summary_text = result.get("summary_text")
        if summary_text:
            summary_file = output_dir / "summary_text.txt"
            with open(summary_file, "w", encoding="utf-8") as f:
                f.write(summary_text)
            logger.info(f"Summary text saved to: {summary_file}")
        else:
            logger.info("No summary generated (text too short)")

        # 保存完整结果（JSON）
        result_file = output_dir / "full_result.json"
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"Full result saved to: {result_file}")

        logger.info("\n" + "=" * 80)
        logger.info("Test completed successfully!")
        logger.info("=" * 80)

    except Exception as e:
        logger.error(f"Processing failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    test_coordinator_with_funasr()
