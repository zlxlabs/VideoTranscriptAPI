"""LLM 客户端薄封装

重试、provider 翻译、内容审查降级均由 llm-compat SyncLLMClient 内部处理。
本层只负责：参数组装 → call_llm_api 转发 → 结果包装 → 错误映射。
"""

from typing import Dict, Optional
from dataclasses import dataclass

from ...utils.logging import setup_logger
from ..llm import call_llm_api, LLMCallError, StructuredResult
from .errors import (
    map_llm_compat_error,
    FatalError,
    RetryableError,
    TimeoutError as LLMTimeoutError,
    TruncationError,
)

logger = setup_logger(__name__)


@dataclass
class LLMResponse:
    """LLM 响应数据类"""
    text: str
    structured_output: Optional[Dict] = None


class LLMClient:
    """LLM 客户端薄封装"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        max_retries: int = 3,
        retry_delay: int = 5,
        config: Optional[Dict] = None,
    ):
        self.config = config or {}

    def call(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: Optional[Dict] = None,
        reasoning_effort: Optional[str] = None,
        task_type: str = "unknown",
    ) -> LLMResponse:
        """调用 LLM API

        Raises:
            FatalError: 不可重试的错误（认证、权限等）
            TimeoutError: 超时
            TruncationError: 输出截断
            RetryableError: llm-compat 重试耗尽后的错误
        """
        try:
            result = call_llm_api(
                model=model,
                prompt=user_prompt,
                reasoning_effort=reasoning_effort,
                task_type=task_type,
                response_schema=response_schema,
                system_prompt=system_prompt,
                config=self.config,
            )

            if isinstance(result, StructuredResult):
                if not result.success:
                    raise LLMCallError(f"Structured output failed: {result.error}")
                return LLMResponse(
                    text="",
                    structured_output=result.data or {},
                )
            else:
                return LLMResponse(text=result)

        except LLMCallError:
            raise
        except (FatalError, LLMTimeoutError, TruncationError, RetryableError):
            raise
        except Exception as e:
            raise map_llm_compat_error(e) from e
