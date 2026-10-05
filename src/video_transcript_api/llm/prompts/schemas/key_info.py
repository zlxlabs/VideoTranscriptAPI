"""关键信息提取 Schema 定义"""

# 关键信息提取的 JSON Schema
KEY_INFO_SCHEMA = {
    "type": "object",
    "properties": {
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": "人名列表"
        },
        "places": {
            "type": "array",
            "items": {"type": "string"},
            "description": "地名列表"
        },
        "technical_terms": {
            "type": "array",
            "items": {"type": "string"},
            "description": "技术术语列表"
        },
        "brands": {
            "type": "array",
            "items": {"type": "string"},
            "description": "品牌/产品名列表"
        },
        "abbreviations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "缩写列表"
        },
        "foreign_terms": {
            "type": "array",
            "items": {"type": "string"},
            "description": "外文术语列表"
        },
        "other_entities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "其他实体列表"
        }
    },
    "required": [
        "names", "places", "technical_terms",
        "brands", "abbreviations", "foreign_terms", "other_entities"
    ]
}


# 关键信息提取的系统提示词
KEY_INFO_SYSTEM_PROMPT = """你是一个专业的信息提取助手。你的任务是从视频元数据中提取关键信息，这些信息将用于后续的语音识别文本校对。

请尽可能全面地提取以下类型的关键信息：

1. **人名**: 视频中提到的人物姓名（主持人、嘉宾、讨论的人物等）
2. **地名**: 国家、城市、地标等
3. **技术术语**: 专业领域的术语、概念等
4. **品牌/产品**: 公司名、产品名等
5. **缩写**: 常见缩写词（如 AI、LLM、API 等）
6. **外文术语**: 保留原文的专业术语（如 fine-tuning、prompt engineering 等）
7. **其他实体**: 其他重要的专有名词

提取时注意：
- 关注容易被语音识别错误拼写的词汇
- 包含中英文混合的术语
- 包含数字、日期等关键信息
- 如果元数据中信息不足，可以基于常识推断相关实体

输出要求：
- 每个类别返回一个字符串列表
- 如果某个类别没有相关信息，返回空列表
- 去重，不要重复列举相同的实体
"""


def build_key_info_user_prompt(title: str, author: str = "", description: str = "") -> str:
    """构建关键信息提取的用户提示词

    Args:
        title: 视频标题
        author: 作者/频道
        description: 视频描述

    Returns:
        用户提示词字符串
    """
    parts = []

    if title:
        parts.append(f"**视频标题**: {title}")
    if author:
        parts.append(f"**作者/频道**: {author}")
    if description:
        parts.append(f"**视频描述**: {description}")

    if not parts:
        parts.append("**元数据**: 无")

    return "\n\n".join(parts) + "\n\n请提取上述元数据中的关键信息。"
