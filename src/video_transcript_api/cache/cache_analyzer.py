import os
import re
import json
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from ..utils.logging import setup_logger

logger = setup_logger("cache_analyzer")


@dataclass
class CacheCapabilities:
    """缓存能力信息"""

    cache_dir: str = ""  # 缓存目录路径
    has_speaker_data: bool = False  # 是否包含说话人数据
    primary_engine: Optional[str] = None  # 主要转录引擎 (funasr/capswriter)
    speakers_list: List[str] = None  # 说话人列表
    files_present: Dict[str, bool] = None  # 存在的文件列表

    def __post_init__(self):
        if self.speakers_list is None:
            self.speakers_list = []
        if self.files_present is None:
            self.files_present = {}


class CacheCapabilityAnalyzer:
    """
    缓存能力分析器
    负责分析缓存目录的引擎类型和说话人支持能力
    """

    def __init__(self):
        """初始化缓存能力分析器"""
        # 缓存文件映射
        self.cache_files = {
            "funasr_transcript": "transcript_funasr.json",
            "capswriter_transcript": "transcript_capswriter.txt",
            "structured_output": "llm_processed.json",
            "calibrated_text": "llm_calibrated.txt",
            "summary_text": "llm_summary.txt",
            "chapters": "llm_chapters.json",
        }

    def analyze_cache(self, cache_dir: str) -> CacheCapabilities:
        """
        分析缓存目录的完整能力信息

        Args:
            cache_dir: 缓存目录路径

        Returns:
            CacheCapabilities: 缓存能力信息
        """
        try:
            logger.info(f"开始分析缓存能力: {cache_dir}")
            capabilities = CacheCapabilities(cache_dir=cache_dir)

            # 检查文件存在性
            capabilities.files_present = self._check_files_existence(cache_dir)
            logger.info(f"  File existence status:")
            for file_key, exists in capabilities.files_present.items():
                if exists:
                    logger.info(
                        f"    [OK] {file_key}: {self.cache_files.get(file_key, 'unknown')}"
                    )

            # 分析转录引擎类型
            capabilities.primary_engine = self._detect_primary_engine(
                capabilities.files_present
            )
            logger.info(f"  主要引擎: {capabilities.primary_engine}")

            # 分析说话人数据
            if capabilities.primary_engine == "funasr":
                capabilities.has_speaker_data = True
                capabilities.speakers_list = self._extract_speakers_from_funasr(
                    cache_dir
                )
                logger.info(f"  有说话人数据: True (FunASR)")
                logger.info(f"  说话人列表: {capabilities.speakers_list}")
            else:
                capabilities.has_speaker_data = False
                capabilities.speakers_list = []
                logger.info(f"  有说话人数据: False (CapsWriter)")

            logger.info(f"缓存分析完成: {cache_dir}")
            return capabilities

        except Exception as e:
            logger.error(f"缓存分析失败 {cache_dir}: {e}", exc_info=True)
            return CacheCapabilities(
                cache_dir=cache_dir,
                has_speaker_data=False,
                primary_engine=None,
                speakers_list=[],
            )

    def _check_files_existence(self, cache_dir: str) -> Dict[str, bool]:
        """检查缓存文件存在性"""
        files_present = {}

        for file_key, filename in self.cache_files.items():
            file_path = os.path.join(cache_dir, filename)
            files_present[file_key] = (
                os.path.exists(file_path) and os.path.getsize(file_path) > 0
            )

        return files_present

    def _detect_primary_engine(self, files_present: Dict[str, bool]) -> Optional[str]:
        """检测主要转录引擎"""
        # 优先检查 FunASR
        if files_present.get("funasr_transcript", False):
            return "funasr"

        # 检查 CapsWriter
        if files_present.get("capswriter_transcript", False):
            return "capswriter"

        return None

    def _extract_speakers_from_funasr(self, cache_dir: str) -> List[str]:
        """从 FunASR 数据提取说话人列表"""
        try:
            funasr_file = os.path.join(cache_dir, self.cache_files["funasr_transcript"])
            if not os.path.exists(funasr_file):
                return []

            with open(funasr_file, "r", encoding="utf-8") as f:
                funasr_data = json.load(f)

            speakers = set()

            # 方法1：从 speakers 字段直接获取
            if isinstance(funasr_data, dict) and "speakers" in funasr_data:
                speakers_data = funasr_data["speakers"]
                if isinstance(speakers_data, list):
                    return speakers_data
                elif isinstance(speakers_data, dict):
                    return list(speakers_data.keys())

            # 方法2：从转录段落中提取
            segments = []
            if isinstance(funasr_data, list):
                segments = funasr_data
            elif isinstance(funasr_data, dict):
                for key in ["segments", "result", "data"]:
                    if key in funasr_data:
                        segments = funasr_data[key]
                        break

            for segment in segments:
                if isinstance(segment, dict):
                    for field in ["spk", "speaker", "speaker_id"]:
                        if field in segment and segment[field]:
                            speakers.add(str(segment[field]))
                            break

            return sorted(list(speakers))

        except Exception as e:
            logger.error(f"提取 FunASR 说话人失败 {cache_dir}: {e}")
            return []

    def get_cache_statistics(self, cache_base_dir: str) -> Dict:
        """获取缓存统计信息"""
        try:
            stats = {
                "total_caches": 0,
                "by_engine": {"funasr": 0, "capswriter": 0, "unknown": 0},
                "with_speaker_data": 0,
                "with_structured_output": 0,
            }

            for root, dirs, files in os.walk(cache_base_dir):
                if not any(
                    f in files
                    for f in ["transcript_funasr.json", "transcript_capswriter.txt"]
                ):
                    continue

                capabilities = self.analyze_cache(root)
                stats["total_caches"] += 1

                # 按引擎统计
                engine = capabilities.primary_engine or "unknown"
                stats["by_engine"][engine] += 1

                # 其他统计
                if capabilities.has_speaker_data:
                    stats["with_speaker_data"] += 1

                if capabilities.files_present.get("structured_output", False):
                    stats["with_structured_output"] += 1

            return stats

        except Exception as e:
            logger.error(f"获取缓存统计失败: {e}")
            return {}


def analyze_cache_capabilities(cache_dir: str) -> CacheCapabilities:
    """
    便捷函数：分析单个缓存目录的能力

    Args:
        cache_dir: 缓存目录路径

    Returns:
        CacheCapabilities: 缓存能力信息
    """
    analyzer = CacheCapabilityAnalyzer()
    return analyzer.analyze_cache(cache_dir)
