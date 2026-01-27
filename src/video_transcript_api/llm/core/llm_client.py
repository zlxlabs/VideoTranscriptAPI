"""LLM 客户端封装（含智能重试）"""

import time
from typing import Dict, Optional
from dataclasses import dataclass

from ...utils.logging import setup_logger
from ..llm import call_llm_api, LLMCallError, StructuredResult
from .errors import classify_error, RetryableError, FatalError

logger = setup_logger(__name__)


@dataclass
class LLMResponse:
    """LLM 响应数据类"""
    text: str  # 纯文本响应
    structured_output: Optional[Dict] = None  # 结构化输出（如果有）


class LLMClient:
    """LLM 客户端（含智能重试）"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        max_retries: int = 3,
        retry_delay: int = 5,
        config: Optional[Dict] = None,
    ):
        """初始化 LLM 客户端

        Args:
            api_key: API Key
            base_url: API Base URL
            max_retries: 最大重试次数
            retry_delay: 基础重试延迟（秒），实际延迟会指数增长
            config: 完整配置字典（用于读取 JSON 输出模式等设置）
        """
        self.api_key = api_key
        self.base_url = base_url
        self.max_retries = max_retries
        self.retry_delay = retry_delay
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
        """调用 LLM API（带智能重试）

        Args:
            model: 模型名称
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            response_schema: 响应 Schema（可选，用于结构化输出）
            reasoning_effort: reasoning effort 参数（可选）
            task_type: 任务类型，用于日志追踪（默认 "unknown"）

        Returns:
            LLMResponse 对象

        Raises:
            FatalError: 不可重试的错误
            RetryableError: 重试多次后仍失败
        """
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    logger.info(f"LLM API call retry: attempt {attempt}/{self.max_retries}")

                # 调用底层 API
                result = self._actual_call(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_schema=response_schema,
                    reasoning_effort=reasoning_effort,
                    task_type=task_type,
                )

                if attempt > 0:
                    logger.info("LLM API call retry successful")

                return result

            except Exception as e:
                last_error = e

                # 错误分类
                error_type = classify_error(e)

                # 致命错误，直接抛出
                if error_type == FatalError:
                    logger.error(f"LLM API call failed (fatal): {e}")
                    raise FatalError(f"Non-retryable error: {e}") from e

                # 可重试错误
                if attempt < self.max_retries:
                    # 计算延迟时间（指数退避）
                    delay = self._calculate_delay(attempt)
                    logger.warning(
                        f"LLM API call failed (retryable): {e}, "
                        f"waiting {delay:.1f}s before retry ({attempt + 1}/{self.max_retries})"
                    )
                    time.sleep(delay)
                else:
                    # 所有重试都失败
                    logger.error(f"LLM API call failed after {self.max_retries} retries: {e}")
                    raise RetryableError(
                        f"Failed after {self.max_retries} retries: {e}"
                    ) from e

    def _calculate_delay(self, attempt: int) -> float:
        """计算重试延迟时间（指数退避）

        Args:
            attempt: 当前重试次数（从 0 开始）

        Returns:
            延迟时间（秒）

        Examples:
            假设 retry_delay = 5
            - attempt 0: 5 * 2^0 = 5s
            - attempt 1: 5 * 2^1 = 10s
            - attempt 2: 5 * 2^2 = 20s
            - attempt 3: 5 * 2^3 = 40s
            - attempt 4: min(5 * 2^4, 60) = 60s（最多 60s）
        """
        delay = self.retry_delay * (2 ** attempt)
        # 限制最大延迟为 60 秒
        return min(delay, 60.0)

    def _actual_call(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: Optional[Dict],
        reasoning_effort: Optional[str],
        task_type: str,
    ) -> LLMResponse:
        """实际的 API 调用（不包含重试逻辑）"""
        try:
            result = call_llm_api(
                model=model,
                prompt=user_prompt,
                api_key=self.api_key,
                base_url=self.base_url,
                response_schema=response_schema,
                system_prompt=system_prompt,
                max_retries=0,  # 底层不重试，由 LLMClient 统一处理
                retry_delay=0,
                reasoning_effort=reasoning_effort,
                task_type=task_type,
                config=self.config,  # 传递配置，以便选择正确的 JSON 输出模式
            )

            # 判断返回类型
            if isinstance(result, StructuredResult):
                # StructuredResult 只有 success, data, error 三个属性
                if not result.success:
                    raise LLMCallError(f"Structured output failed: {result.error}")
                return LLMResponse(
                    text="",  # 结构化输出没有纯文本内容
                    structured_output=result.data or {},
                )
            else:
                return LLMResponse(text=result)

        except LLMCallError as e:
            logger.debug(f"LLM API call exception: {e}")
            raise
        except Exception as e:
            logger.error(f"Unknown error: {e}")
            raise LLMCallError(f"LLM call exception: {e}", e)
