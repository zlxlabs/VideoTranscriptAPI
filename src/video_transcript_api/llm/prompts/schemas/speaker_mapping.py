"""说话人映射 JSON Schema

用于说话人名称推断输出格式定义。
"""

SPEAKER_MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "speaker_mapping": {
            "type": "object",
            "description": "说话人标识到真实姓名的映射",
            "additionalProperties": {
                "type": "string"
            }
        },
        "confidence": {
            "type": "object",
            "description": "每个映射的置信度 (0-1)",
            "additionalProperties": {
                "type": "number"
            }
        },
        "reasoning": {
            "type": "string",
            "description": "推断依据说明"
        }
    },
    "required": ["speaker_mapping", "confidence", "reasoning"],
    "additionalProperties": False
}
