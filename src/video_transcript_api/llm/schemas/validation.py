"""
验证结果 JSON Schema

用于 structured_calibrator.py 中的校对质量验证输出格式定义。
"""

VALIDATION_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_score": {
            "type": "number",
            "description": "整体质量分数 (0-10)"
        },
        "scores": {
            "type": "object",
            "description": "各维度分数",
            "properties": {
                "format_correctness": {
                    "type": "number",
                    "description": "格式正确性分数 (0-10)"
                },
                "content_fidelity": {
                    "type": "number",
                    "description": "内容保真度分数 (0-10)"
                },
                "text_quality": {
                    "type": "number",
                    "description": "文本质量分数 (0-10)"
                },
                "speaker_consistency": {
                    "type": "number",
                    "description": "说话人一致性分数 (0-10)"
                },
                "time_consistency": {
                    "type": "number",
                    "description": "时间一致性分数 (0-10)"
                }
            },
            "required": [
                "format_correctness",
                "content_fidelity",
                "text_quality",
                "speaker_consistency",
                "time_consistency"
            ],
            "additionalProperties": False
        },
        "pass": {
            "type": "boolean",
            "description": "是否通过验证"
        },
        "issues": {
            "type": "array",
            "description": "发现的问题列表",
            "items": {
                "type": "string"
            }
        },
        "recommendation": {
            "type": "string",
            "description": "改进建议"
        }
    },
    "required": ["overall_score", "scores", "pass", "issues", "recommendation"],
    "additionalProperties": False
}
