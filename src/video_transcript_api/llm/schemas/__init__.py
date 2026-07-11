"""
JSON Schema 定义模块

包含 LLM 结构化输出所需的所有 Schema 定义。

说话人映射 Schema（SPEAKER_MAPPING_SCHEMA）唯一定义在
``llm.prompts.schemas.speaker_mapping``（实际被 SpeakerInferencer 消费的版本），
此处不再重复定义，避免两份 schema 漂移不同步。需要时请从
``video_transcript_api.llm.prompts.schemas`` 导入。
"""
from .calibration import CALIBRATION_RESULT_SCHEMA
from .validation import VALIDATION_RESULT_SCHEMA
from .unified_validation import UNIFIED_VALIDATION_SCHEMA

__all__ = [
    "CALIBRATION_RESULT_SCHEMA",
    "VALIDATION_RESULT_SCHEMA",
    "UNIFIED_VALIDATION_SCHEMA",
]
