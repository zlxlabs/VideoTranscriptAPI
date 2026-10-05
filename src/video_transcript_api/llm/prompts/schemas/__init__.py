"""Schema 定义模块"""

from .key_info import KEY_INFO_SCHEMA
from .speaker_mapping import SPEAKER_MAPPING_SCHEMA
from .validation import VALIDATION_RESULT_SCHEMA
from .unified_validation import UNIFIED_VALIDATION_SCHEMA

__all__ = [
    "KEY_INFO_SCHEMA",
    "SPEAKER_MAPPING_SCHEMA",
    "VALIDATION_RESULT_SCHEMA",
    "UNIFIED_VALIDATION_SCHEMA",
]
