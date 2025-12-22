"""
LLM 处理工具模块

包含 LLM API 调用、结构化校对、分段处理、说话人映射等功能。
"""
from typing import Optional


def normalize_reasoning_effort(value: Optional[str]) -> Optional[str]:
    """
    规范化 reasoning_effort 配置值

    处理配置文件中可能出现的各种异常写法：
    - "null" 字符串 -> None
    - "none" 字符串 -> None（视为禁用）
    - "" 空字符串 -> None
    - 其他有效值保持不变（"low", "medium", "high"）

    Args:
        value: 原始配置值

    Returns:
        规范化后的值，无效值返回 None
    """
    if value is None:
        return None

    # 处理字符串形式的 null/none/空值
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("null", "none", ""):
            return None
        # 返回原始值（保持大小写）
        return value

    return None


from .llm import (
    call_llm_api,
    set_default_config,
    get_default_config,
    StructuredResult,
    LLMStats,
    get_llm_stats,
    reset_llm_stats,
    log_llm_stats,
)
from .llm_enhanced import EnhancedLLMProcessor
from .llm_segmented import SegmentedLLMProcessor
from .structured_calibrator import StructuredCalibrator
from .text_segmentation import TextSegmentationProcessor
from .speaker_mapping import SpeakerMappingInference, infer_speaker_mapping_from_cache
from .schemas import (
    CALIBRATION_RESULT_SCHEMA,
    VALIDATION_RESULT_SCHEMA,
    SPEAKER_MAPPING_SCHEMA,
)

__all__ = [
    # Utils
    "normalize_reasoning_effort",
    # LLM API
    "call_llm_api",
    "set_default_config",
    "get_default_config",
    "StructuredResult",
    "LLMStats",
    "get_llm_stats",
    "reset_llm_stats",
    "log_llm_stats",
    # Processors
    "EnhancedLLMProcessor",
    "SegmentedLLMProcessor",
    "StructuredCalibrator",
    "TextSegmentationProcessor",
    "SpeakerMappingInference",
    "infer_speaker_mapping_from_cache",
    # Schemas
    "CALIBRATION_RESULT_SCHEMA",
    "VALIDATION_RESULT_SCHEMA",
    "SPEAKER_MAPPING_SCHEMA",
]
