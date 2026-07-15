"""说话人映射 JSON Schema —— 向后兼容的 re-export

规范定义唯一在 ``llm.prompts.schemas.speaker_mapping``（实际被
SpeakerInferencer 消费的版本）。本模块只做 re-export，不重复定义，避免
两份 schema 漂移不同步；保留这条旧导入路径是为了不破坏可能存在的既有
调用方（ci-gate review）。
"""

from ..prompts.schemas.speaker_mapping import SPEAKER_MAPPING_SCHEMA

__all__ = ["SPEAKER_MAPPING_SCHEMA"]
