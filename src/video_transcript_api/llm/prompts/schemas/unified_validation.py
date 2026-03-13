"""统一质量验证结果 JSON Schema"""

UNIFIED_VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "properties": {
                "accuracy": {"type": "number", "minimum": 0, "maximum": 10},
                "completeness": {"type": "number", "minimum": 0, "maximum": 10},
                "fluency": {"type": "number", "minimum": 0, "maximum": 10},
                "format": {"type": "number", "minimum": 0, "maximum": 10},
            },
            "required": ["accuracy", "completeness", "fluency", "format"],
            "additionalProperties": False,
        },
        "issues": {
            "type": "array",
            "items": {"type": "string"},
        },
        "deleted_content_analysis": {"type": "string"},
        "recommendation": {"type": "string"},
    },
    "required": ["scores"],
    "additionalProperties": False,
}
