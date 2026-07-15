"""LLM 客户端薄封装

重试、provider 翻译、内容审查降级均由 llm-compat SyncLLMClient 内部处理。
本层只负责：参数组装 → call_llm_api 转发 → 结果包装 → 错误映射 → token 用量审计记录。
"""

import time
from typing import Dict, Optional
from dataclasses import dataclass

from ...utils.logging import setup_logger
from ...utils.logging.usage_recorder import get_usage_recorder
from ..llm import call_llm_api, LLMCallError, StructuredResult
from .errors import (
    map_llm_compat_error,
    FatalError,
    RetryableError,
    TimeoutError as LLMTimeoutError,
    TruncationError,
)
from .usage_context import get_context, pop_chat_result_usage

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
        # 发起调用前先清空桥接槽（读出即丢弃），防止 finally 里的 _record_usage
        # 读到本次调用之前遗留的陈旧快照。桥接槽设计上"写入 -> 立即读出并清空"
        # 只在同一次 call_llm_api() 调用内成立（见 usage_context.py 模块文档），
        # 但有调用方（如 llm_ops._generate_title_if_needed）绕过 LLMClient.call()
        # 直接调 call_llm_api()，写槽后没有对应的 pop。若本次调用在拿到真实
        # ChatResult 之前就失败（record_chat_result_usage 未被再次调用），
        # finally 会把那份陈旧快照错记到当前 task_id/stage 上。清空后，若本次
        # 确实产生了真实 API 往返，call_llm_api() 内部会同步写入新快照，不受影响。
        # 与 _record_usage 一样 fail-open：清槽本身出异常绝不能影响主调用流程。
        try:
            pop_chat_result_usage()
        except Exception as exc:
            logger.warning(f"LLM usage 桥接槽预清理异常（不影响 LLM 调用主流程）: {exc}")

        start = time.monotonic()
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
        finally:
            # 无论成功/失败都记一行用量审计（fail-open，不影响上面的返回值/异常）
            duration_ms = int((time.monotonic() - start) * 1000)
            self._record_usage(model=model, duration_ms=duration_ms)

    def _record_usage(self, *, model: str, duration_ms: int) -> None:
        """将 call_llm_api() 内部桥接的 ChatResult usage 快照聚合落审计库。

        usage 快照由 `llm/llm.py` 的三个调用辅助函数在拿到 llm-compat
        ChatResult 后按顺序追加写入 `usage_context` 的桥接槽（同线程、调用完
        一次性读出，详见 `usage_context.py` 模块文档）。task_id/stage 从当前
        调用上下文读取（由 `llm_ops._handle_llm_task` 与 `coordinator.py`/
        processors 通过 contextvars 设置，跨 ThreadPoolExecutor 需显式
        copy_context 传播）。

        json_object 模式的 Self-Correction 可能让一次 call() 内触发多次真实
        API 往返（多个快照），这里对全部快照的 token 求和落一行——而不是只取
        最后一次，否则失败重试同样消耗的真实 token 会从审计记录里静默消失
        （ci-gate review）。仍然只落一行（不按尝试拆多行），因为 duration_ms
        本就是整次 call() 的耗时，无法精确拆分到每次尝试；model 取最后一次
        快照（即最终生效结果对应的那次尝试）。

        本方法自身也包一层 try/except：即便 usage_context/UsageRecorder 出现
        非预期异常，也绝不能影响 call() 的返回值或已经在传播的异常
        （fail-open，见类文档）。

        Args:
            model: 请求时使用的模型名（桥接快照缺失或未回报 model 时的兜底值）
            duration_ms: 本次 call() 调用耗时（毫秒，含内部重试/Self-Correction）
        """
        try:
            snapshots = pop_chat_result_usage()
            context = get_context()

            if not snapshots:
                # call_llm_api() 从未走到任何真实 API 往返（如参数构建阶段就失败），
                # 仍记一行，flag usage_missing，避免静默丢弃这次调用尝试。
                get_usage_recorder().record(
                    task_id=context.get("task_id"),
                    stage=context.get("stage"),
                    model=model,
                    prompt_tokens=None,
                    completion_tokens=None,
                    total_tokens=None,
                    duration_ms=duration_ms,
                    usage_missing=True,
                )
                return

            known = [s for s in snapshots if not s.usage_missing]
            final_model = snapshots[-1].model or model

            if known:
                prompt_tokens = sum(s.prompt_tokens or 0 for s in known)
                completion_tokens = sum(s.completion_tokens or 0 for s in known)
                total_tokens = sum(s.total_tokens or 0 for s in known)
                usage_missing = False
            else:
                # 全部尝试的 provider 都没回报 usage（单次调用场景与此前行为
                # 一致；理论上也覆盖"重试多次但每次都不回报"的边界情况）。
                prompt_tokens = completion_tokens = total_tokens = None
                usage_missing = True

            get_usage_recorder().record(
                task_id=context.get("task_id"),
                stage=context.get("stage"),
                model=final_model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                duration_ms=duration_ms,
                usage_missing=usage_missing,
            )
        except Exception as exc:
            logger.warning(f"LLM usage 记录钩子异常（不影响 LLM 调用主流程）: {exc}")
