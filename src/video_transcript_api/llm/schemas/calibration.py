"""
校对结果 JSON Schema（ID 锚点版）

用于 speaker_aware_processor 的对话校对输出格式定义。

设计原则：时间戳/说话人/对话数量是 funasr ground truth，由确定性管线独占，
LLM 绝不回传也无法影响。LLM 仅按 id 锚点返回 {id, text} 修正项，合并时按 id 查表。
这样结构不匹配（条数/说话人对不上导致整块作废）这一故障类从根上消失。
"""

CALIBRATION_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "corrections": {
            "type": "array",
            "description": (
                "校对修正列表。每项对应输入中的一段对话（按 id 锚点）。"
                "应为每个输入段返回一项，即使该段无需修改也原样返回其 text。"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "对应输入对话的编号（与输入行 [id] 一致）",
                    },
                    "text": {
                        "type": "string",
                        "description": "校对后的文本内容（仅改文本，不含说话人/时间戳）",
                    },
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["corrections"],
    "additionalProperties": False,
}
