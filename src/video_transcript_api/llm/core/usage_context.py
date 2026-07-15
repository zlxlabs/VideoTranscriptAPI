"""LLM 调用上下文（contextvars）

提供两类跨调用栈传递的上下文能力，服务于「LLM token 用量按任务/阶段审计」功能：

1. **调用上下文（task_id / stage）**
   记录当前 LLM 调用所属的任务 ID 与处理阶段（calibration / summary /
   speaker_inference / validation ...），供 `UsageRecorder` 落库时使用。
   由 `api/services/llm_ops.py` 的任务入口设置 task_id，由
   `llm/coordinator.py` 及各 processor 在进入具体阶段时用 `set_context`
   细化 stage。

   **关键**：processors 内部使用 `ThreadPoolExecutor` 并发处理 chunk/segment，
   Python 的 contextvars 默认不会自动传播到线程池 worker 线程。调用方必须在
   `executor.submit` 处显式用 `contextvars.copy_context()` 捕获当前上下文，
   再通过 `executor.submit(ctx.run, fn, ...)` 传播给 worker 线程，否则 worker
   线程内读到的会是该线程的默认值（'unknown'/'unknown'）。

2. **ChatResult usage 桥接（按次追加，非覆盖）**
   `llm/llm.py` 内部的 `_call_with_text_output` / `_call_with_json_schema_mode`
   / `_call_with_json_object_mode` 在拿到 llm-compat 返回的 `ChatResult` 后，
   会把其中的 `usage`/`model` 追加写入这里；`llm/core/llm_client.py::LLMClient.call()`
   在同一线程内紧接着调用完 `call_llm_api()` 后一次性读出全部快照并清空。

   之所以需要这层桥接：`call_llm_api()` 对外的公开返回类型是 `str`
   （纯文本模式）或 `StructuredResult`（结构化模式），两者均不携带
   `ChatResult.usage`，且这两个返回类型已被多处调用方直接使用，不能随意
   变更签名。桥接是在不破坏现有公开契约的前提下，把 usage 元数据带到
   审计记录点的最小侵入方案。桥接槽只在同一线程、同一次 `call_llm_api()`
   调用内"写入 -> 一次性读出并清空"，不存在跨线程/跨调用污染的风险。

   `call_llm_api()` 内部可能因 json_object 模式的 Self-Correction 重试
   触发多次真实 `client.chat()` 往返，每次都各自消耗真实 token——桥接槽
   按顺序累积全部快照（而非只保留最后一次），交由 `LLMClient._record_usage()`
   对全部快照求和落一行审计记录，避免失败重试消耗的 token 被静默丢弃。
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Tuple

# ============================================================
# 1. 调用上下文：task_id / stage
# ============================================================

_DEFAULT_CALL_CONTEXT: dict = {"task_id": "unknown", "stage": "unknown"}

_call_context: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "llm_call_context", default=_DEFAULT_CALL_CONTEXT
)


@contextmanager
def set_context(
    *, task_id: Optional[str] = None, stage: Optional[str] = None
) -> Iterator[None]:
    """在 with 块内设置/细化调用上下文，退出时自动恢复外层值。

    只覆盖显式传入的字段，未传入字段沿用当前上下文中的值 —— 这样
    coordinator 可以在只想切换 stage 时不必重复传一遍 task_id。

    Args:
        task_id: 当前 LLM 任务 ID（如提交任务队列时的 task_id）
        stage: 当前处理阶段（calibration / summary / speaker_inference / validation）

    Example:
        with set_context(task_id="abc123"):
            with set_context(stage="calibration"):
                ...  # 此时 get_context() == {"task_id": "abc123", "stage": "calibration"}
    """
    current = get_context()
    new_context = dict(current)
    if task_id is not None:
        new_context["task_id"] = task_id
    if stage is not None:
        new_context["stage"] = stage

    token = _call_context.set(new_context)
    try:
        yield
    finally:
        _call_context.reset(token)


def get_context() -> dict:
    """读取当前调用上下文。

    Returns:
        dict: 形如 {"task_id": ..., "stage": ...}，未设置过时返回
            {"task_id": "unknown", "stage": "unknown"}。
    """
    return _call_context.get()


def bind_task_id(task_id: Optional[str]) -> None:
    """在线程入口处一次性绑定 task_id，不使用 with，无需匹配的 reset。

    专用于 `ThreadPoolExecutor` worker 线程的任务入口（如
    `api/services/llm_ops.py::_handle_llm_task`）：这类线程池会复用线程处理
    后续不同任务，只要每次任务入口都重新调用本函数覆盖上一次的值，就不会有
    旧任务 task_id 泄漏到新任务的问题，因此不需要像 `set_context` 那样成对
    使用 token/reset。调用后 stage 会重置为 'unknown'，等待后续
    coordinator/processor 用 `set_context(stage=...)` 细化。

    Args:
        task_id: 任务 ID，为空时记为 'unknown'
    """
    _call_context.set({"task_id": task_id or "unknown", "stage": "unknown"})


# ============================================================
# 2. 最近一次 ChatResult usage 桥接
# ============================================================


@dataclass
class ChatUsageSnapshot:
    """一次真实 LLM API 调用（llm-compat client.chat()）的 usage 快照"""

    model: str
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    total_tokens: Optional[int]
    usage_missing: bool


# 累积同一次 call_llm_api() 调用内的全部真实 API 往返快照（而非只保留最后
# 一次）：json_object 模式的 Self-Correction 最多会触发 max_retries+1 次真实
# client.chat() 调用，早前失败重试消耗的 token 同样是真实计费成本，不能因为
# 只保留最后一次而从审计表里静默消失（ci-gate review）。default 用空 tuple
# （不可变）而非 [] ——record_chat_result_usage 每次都 .set() 一个新 tuple，
# 从不原地 mutate，天然避免"跨 context 共享同一个可变默认值"的经典坑。
_chat_usage_log: contextvars.ContextVar[Tuple[ChatUsageSnapshot, ...]] = contextvars.ContextVar(
    "llm_chat_usage_log", default=()
)


def record_chat_result_usage(*, model: str, usage: Any) -> None:
    """追加记录一次 llm-compat ChatResult 的 usage，供上层 llm_client.call() 读取。

    由 `llm/llm.py` 内部三个调用辅助函数在拿到 `client.chat()` 返回的
    `ChatResult` 后调用。同一次 `call_llm_api()` 调用内可能因 Self-Correction
    重试触发多次真实 API 调用，每次都会各自消耗 token 并产生真实费用——本函数
    按调用顺序追加快照（而不是覆盖），由 `pop_chat_result_usage()` 的调用方
    决定如何聚合（当前是 `LLMClient._record_usage()` 对全部快照求和落一行）。

    Args:
        model: 实际使用的模型名（优先用 ChatResult.model，因其能反映内容
            审查降级/fallback 后真正生效的模型）
        usage: llm-compat 的 `TokenUsage` 对象，部分 provider 不回报时为 None
    """
    if usage is None:
        snapshot = ChatUsageSnapshot(
            model=model,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            usage_missing=True,
        )
    else:
        snapshot = ChatUsageSnapshot(
            model=model,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
            usage_missing=False,
        )

    _chat_usage_log.set(_chat_usage_log.get() + (snapshot,))


def pop_chat_result_usage() -> Tuple[ChatUsageSnapshot, ...]:
    """读取并清空本次 call_llm_api() 调用内累积的全部 usage 快照。

    由 `LLMClient.call()` 在 `call_llm_api()` 返回后立即调用一次。按记录
    顺序返回一个 `ChatUsageSnapshot` 元组；返回空 tuple 表示本次调用从未
    走到任何真实 API 往返（如请求构建阶段就失败），此时调用方应按 usage
    完全缺失处理。多于一个元素表示触发过 Self-Correction 重试，调用方需
    自行决定聚合方式（求和/只取最后一次等）。
    """
    value = _chat_usage_log.get()
    _chat_usage_log.set(())
    return value


# ============================================================
# 3. 测试专用：重置本模块的 contextvar 状态
# ============================================================


def reset_context_for_testing() -> None:
    """仅供测试使用：把本模块的两个 ContextVar 重置回默认状态。

    生产代码不需要调用本函数——`bind_task_id`（线程池 worker 入口每次覆盖）
    和 `set_context`（with 块内 token/reset 严格配对）已经保证生产环境下
    不会有跨调用的状态泄漏，详见二者各自的 docstring。

    但部分集成测试（如 `tests/integration/test_llm_stage_terminal_state.py`、
    `test_layered_cache.py`、`test_llm_ops_status_backfill.py`）会直接同步
    调用生产入口函数 `api/services/llm_ops.py::_handle_llm_task()`，而不是
    真的经过 `ThreadPoolExecutor` worker 线程。该函数内部对 `bind_task_id()`
    的调用会把真实 task_id 一次性写入 `_call_context`，且按设计没有配对的
    reset。pytest 默认在同一进程/主线程里顺序执行所有用例，这个残留值会
    一直保留到后面执行到的、期望"默认干净上下文"的测试用例（例如
    `test_usage_context_propagation.py`），造成跨测试污染。

    调用方：`tests/conftest.py` 里的 autouse fixture，在每个测试前后调用，
    避免这种污染以任何测试收集顺序都能稳定复现。
    """
    _call_context.set(dict(_DEFAULT_CALL_CONTEXT))
    _chat_usage_log.set(())
