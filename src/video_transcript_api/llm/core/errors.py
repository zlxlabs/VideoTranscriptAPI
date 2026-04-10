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


class TimeoutError(RetryableError):
    """超时错误

    网络连接超时或读取超时，重试可能无效（同样的请求大概率同样超时）
    """
    pass


class TruncationError(RetryableError):
    """输出截断错误

    模型输出 token 耗尽导致 JSON 被截断，重试无意义（同样的输入产生同样的截断）
    """
    pass


class FatalError(LLMError):
    """不可重试错误

    包括：认证失败、权限拒绝、资源不存在、配置错误等
    """
    pass


def classify_error(error: Exception) -> type:
    """将异常分类为具体的错误类型

    分类优先级：Fatal > Timeout > Truncation > Retryable

    Args:
        error: 原始异常对象

    Returns:
        FatalError / TimeoutError / TruncationError / RetryableError 类型
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

    for pattern in fatal_patterns:
        if pattern in error_msg:
            return FatalError

    # 超时错误
    timeout_patterns = ['timed out', 'timeout', 'read timeout']
    for pattern in timeout_patterns:
        if pattern in error_msg:
            return TimeoutError

    # 输出截断错误（token 耗尽导致 JSON 不完整）
    truncation_patterns = ['unterminated string', 'unexpected end']
    for pattern in truncation_patterns:
        if pattern in error_msg:
            return TruncationError

    # 默认为可重试错误
    return RetryableError
