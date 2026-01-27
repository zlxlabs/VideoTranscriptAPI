"""
JSON Schema 定义模块

包含 LLM 结构化输出所需的所有 Schema 定义。
"""
from .calibration import CALIBRATION_RESULT_SCHEMA
from .validation import VALIDATION_RESULT_SCHEMA
from .speaker_mapping import SPEAKER_MAPPING_SCHEMA

__all__ = [
    "CALIBRATION_RESULT_SCHEMA",
    "VALIDATION_RESULT_SCHEMA",
    "SPEAKER_MAPPING_SCHEMA",
]
