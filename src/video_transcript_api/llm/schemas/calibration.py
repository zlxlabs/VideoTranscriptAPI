"""
校对结果 JSON Schema

用于 structured_calibrator.py 中的对话校对输出格式定义。
"""

CALIBRATION_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "calibrated_dialogs": {
            "type": "array",
            "description": "校对后的对话列表",
            "items": {
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "string",
                        "description": "开始时间，格式 HH:MM:SS"
                    },
                    "speaker": {
                        "type": "string",
                        "description": "说话人名称"
                    },
                    "text": {
                        "type": "string",
                        "description": "校对后的文本内容"
                    }
                },
                "required": ["start_time", "speaker", "text"],
                "additionalProperties": False
            }
        }
    },
    "required": ["calibrated_dialogs"],
    "additionalProperties": False
}
