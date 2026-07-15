"""
LLM 处理工具模块

包含 LLM API 调用、结构化校对、分段处理、说话人映射等功能。
"""
import logging
from typing import Optional

_logger = logging.getLogger(__name__)

# 2026 reasoning_effort 合法值白名单
# - "disabled": 显式关闭思考（dispatcher 按 provider 翻译）
# - "minimal": GPT-5 / Gemini 3 的最低挡；DeepSeek 无此值
# - "low"/"medium"/"high": 三家通用
# - "max"/"xhigh": DeepSeek V4 特有
_VALID_EFFORTS = frozenset(
    {"disabled", "minimal", "low", "medium", "high", "max", "xhigh"}
)


def normalize_reasoning_effort(value: Optional[str]) -> Optional[str]:
    """
    规范化 reasoning_effort 配置值到 2026 白名单。

    返回语义三种可能：
    - None: 未设置，沿用 provider 默认（如 DeepSeek V4 默认 enabled@high）
    - "disabled": 显式关闭思考（dispatcher 按 provider 翻译为 thinking.type=disabled 或等价）
    - "minimal"/"low"/"medium"/"high"/"max"/"xhigh": 思考强度

    兼容迁移：
    - "null"/""/"   " → None（默认）
    - "none" → "disabled"（legacy 语义保持，warn deprecated）
    - 大小写不敏感，归一到小写
    - 非白名单值 → None + warn
    """
    if value is None:
        return None

    if not isinstance(value, str):
        return None

    normalized = value.strip().lower()

    if normalized in ("", "null"):
        return None

    # Legacy: 旧 "none" 的用户意图是"关闭思考"，不是"不设置"
    # 不映射到 "disabled" 会让 Gemini 2.5 用户从"关闭"静默变为"默认开启"
    if normalized == "none":
        _logger.warning(
            "reasoning_effort='none' is deprecated (legacy from 2024 Gemini 2.5); "
            "migrating to 'disabled'. Update your config to use 'disabled' explicitly."
        )
        return "disabled"

    if normalized not in _VALID_EFFORTS:
        _logger.warning(
            "Unknown reasoning_effort %r, treating as None (provider default). "
            "Valid values: %s",
            value,
            sorted(_VALID_EFFORTS),
        )
        return None

    return normalized


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
    UNIFIED_VALIDATION_SCHEMA,
)
# SPEAKER_MAPPING_SCHEMA 唯一定义在 prompts.schemas；llm.schemas 里的同名
# 导出是对它的 re-export（保留旧导入路径兼容），不是另一份定义
from .prompts.schemas import SPEAKER_MAPPING_SCHEMA
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
