"""
风控模块 - 敏感词检测和消敏处理

该模块负责：
1. 从云端URL列表加载敏感词库
2. 对企业微信发送内容进行敏感词检测
3. 对检测到的敏感词进行消敏处理（移除或替换为风控提示）

注意：只对即将发送到企业微信的文本进行消敏，不修改数据源
"""

from .sensitive_words_manager import SensitiveWordsManager
from .text_sanitizer import TextSanitizer

# 全局实例
_words_manager = None
_text_sanitizer = None


def init_risk_control(config):
    """
    初始化风控模块

    Args:
        config: 配置字典
    """
    global _words_manager, _text_sanitizer

    risk_config = config.get("risk_control", {})

    if not risk_config.get("enabled", False):
        return

    # 初始化敏感词库管理器
    _words_manager = SensitiveWordsManager(risk_config)
    sensitive_words = _words_manager.load_words()

    # 初始化文本消敏处理器
    _text_sanitizer = TextSanitizer(sensitive_words)


def is_enabled():
    """检查风控模块是否启用"""
    return _text_sanitizer is not None


def sanitize_text(text: str, text_type: str = "general") -> dict:
    """
    对文本进行敏感词检测和消敏处理

    Args:
        text: 待处理的文本
        text_type: 文本类型
            - "summary": 总结文本（如有敏感词则整体替换为风控提示）
            - "title": 标题（移除敏感词后取前6字符）
            - "author": 作者（移除敏感词后取前6字符）
            - "general": 普通文本（移除所有敏感词）

    Returns:
        {
            "has_sensitive": bool,      # 是否包含敏感词
            "sensitive_words": list,    # 检测到的敏感词列表
            "sanitized_text": str       # 消敏后的文本
        }
    """
    if not is_enabled():
        return {
            "has_sensitive": False,
            "sensitive_words": [],
            "sanitized_text": text
        }

    return _text_sanitizer.sanitize(text, text_type)


__all__ = [
    "init_risk_control",
    "is_enabled",
    "sanitize_text",
    "SensitiveWordsManager",
    "TextSanitizer"
]
