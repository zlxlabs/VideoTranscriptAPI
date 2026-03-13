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
    LLMCallError,
    StructuredResult,
    LLMStats,
    get_llm_stats,
    reset_llm_stats,
    log_llm_stats,
)
from .schemas import (
    CALIBRATION_RESULT_SCHEMA,
    VALIDATION_RESULT_SCHEMA,
    SPEAKER_MAPPING_SCHEMA,
    UNIFIED_VALIDATION_SCHEMA,
)
from .prompts import (
    # System prompts
    CALIBRATE_SYSTEM_PROMPT,
    CALIBRATE_SYSTEM_PROMPT_WITH_SPEAKER,
    SUMMARY_SYSTEM_PROMPT_SINGLE_SPEAKER,
    SUMMARY_SYSTEM_PROMPT_MULTI_SPEAKER,
    STRUCTURED_CALIBRATE_SYSTEM_PROMPT,
    VALIDATION_SYSTEM_PROMPT,
    UNIFIED_VALIDATION_SYSTEM_PROMPT,
    SPEAKER_INFERENCE_SYSTEM_PROMPT,
    # User prompt builders
    build_calibrate_user_prompt,
    build_summary_user_prompt,
    build_structured_calibrate_user_prompt,
    build_validation_user_prompt,
    build_unified_validation_user_prompt,
    build_speaker_inference_user_prompt,
)

# 新架构模块
from .coordinator import LLMCoordinator
from .core import (
    LLMConfig,
    LLMClient,
    LLMResponse,
    CacheManager,
    KeyInfo,
    KeyInfoExtractor,
    SpeakerInferencer,
    QualityValidator,
    LLMError,
    RetryableError,
    FatalError,
    classify_error,
)
from .validators import UnifiedQualityValidator
from .segmenters import TextSegmenter, DialogSegmenter
from .processors import PlainTextProcessor, SpeakerAwareProcessor

__all__ = [
    # Utils
    "normalize_reasoning_effort",
    # LLM API
    "call_llm_api",
    "set_default_config",
    "get_default_config",
    "LLMCallError",
    "StructuredResult",
    "LLMStats",
    "get_llm_stats",
    "reset_llm_stats",
    "log_llm_stats",
    # New Architecture
    "LLMCoordinator",
    "LLMConfig",
    "LLMClient",
    "LLMResponse",
    "CacheManager",
    "KeyInfo",
    "KeyInfoExtractor",
    "SpeakerInferencer",
    "QualityValidator",
    "UnifiedQualityValidator",
    "LLMError",
    "RetryableError",
    "FatalError",
    "classify_error",
    "TextSegmenter",
    "DialogSegmenter",
    "PlainTextProcessor",
    "SpeakerAwareProcessor",
    # Schemas
    "CALIBRATION_RESULT_SCHEMA",
    "VALIDATION_RESULT_SCHEMA",
    "UNIFIED_VALIDATION_SCHEMA",
    "SPEAKER_MAPPING_SCHEMA",
    # Prompts
    "CALIBRATE_SYSTEM_PROMPT",
    "CALIBRATE_SYSTEM_PROMPT_WITH_SPEAKER",
    "SUMMARY_SYSTEM_PROMPT_SINGLE_SPEAKER",
    "SUMMARY_SYSTEM_PROMPT_MULTI_SPEAKER",
    "STRUCTURED_CALIBRATE_SYSTEM_PROMPT",
    "VALIDATION_SYSTEM_PROMPT",
    "UNIFIED_VALIDATION_SYSTEM_PROMPT",
    "SPEAKER_INFERENCE_SYSTEM_PROMPT",
    "build_calibrate_user_prompt",
    "build_summary_user_prompt",
    "build_structured_calibrate_user_prompt",
    "build_validation_user_prompt",
    "build_unified_validation_user_prompt",
    "build_speaker_inference_user_prompt",
]
