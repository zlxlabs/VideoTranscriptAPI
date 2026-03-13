"""错误分类模块

提供 LLM 错误分类功能，区分可重试和不可重试的错误
"""


class LLMError(Exception):
    """LLM 错误基类"""
    pass


class RetryableError(LLMError):
    """可重试错误

    包括：超时、服务器错误、速率限制等
    """
    pass


class FatalError(LLMError):
    """不可重试错误

    包括：认证失败、权限拒绝、资源不存在、配置错误等
    """
    pass


def classify_error(error: Exception) -> type:
    """将异常分类为可重试或不可重试错误

    Args:
        error: 原始异常对象

    Returns:
        RetryableError 或 FatalError 类型
    """
    error_msg = str(error).lower()

    # 不可重试的错误模式
    fatal_patterns = [
        # 认证相关
        '401', 'unauthorized', 'auth', 'invalid api key',
        # 权限相关
        '403', 'forbidden', 'permission denied',
        # 资源不存在
        '404', 'not found',
        # 配置错误
        'invalid request', 'invalid parameter', 'invalid model',
        'bad request', '400',
    ]

    # 检查是否匹配不可重试模式
    for pattern in fatal_patterns:
        if pattern in error_msg:
            return FatalError

    # 默认为可重试错误
    return RetryableError
