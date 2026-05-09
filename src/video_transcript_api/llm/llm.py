"""
LLM 调用模块

支持纯文本输出和 JSON Schema 结构化输出。
根据模型支持情况自动选择 json_schema（严格模式）或 json_object（兼容模式）。

底层 HTTP 调用、重试、provider 翻译、内容审查降级均由 llm-compat 处理。
"""
import json
import fnmatch
import re
import time
from typing import Any, Dict, List, Optional, Union, overload
from dataclasses import dataclass
from loguru import logger

from llm_compat import SyncLLMClient, set_custom_patterns


# ============================================================
# 全局默认配置 + SyncLLMClient 生命周期
# ============================================================

_default_config: Optional[Dict[str, Any]] = None
_sync_client: Optional[SyncLLMClient] = None

# 默认 API 请求超时（秒）
DEFAULT_LLM_TIMEOUT = 180


def _get_llm_timeout() -> int:
    """从默认配置中获取 LLM API 请求超时时间（秒）"""
    if _default_config is not None:
        return _default_config.get('llm', {}).get('timeout', DEFAULT_LLM_TIMEOUT)
    return DEFAULT_LLM_TIMEOUT


def set_default_config(config: Optional[Dict[str, Any]]) -> None:
    """设置模块级默认配置并初始化 SyncLLMClient"""
    global _default_config, _sync_client
    _default_config = config
    logger.debug("[LLM] Default config set")

    if config is None:
        _sync_client = None
        return

    llm_cfg = config.get("llm", {})
    api_key = llm_cfg.get("api_key", "")
    base_url = llm_cfg.get("base_url", "")

    if not api_key or not base_url:
        logger.warning("[LLM] api_key or base_url missing, SyncLLMClient not initialized")
        return

    # 注入自定义 provider patterns
    provider_patterns = llm_cfg.get("provider_patterns")
    if provider_patterns:
        patterns = {k: v for k, v in provider_patterns.items()}
        set_custom_patterns(patterns)
        logger.info(f"[LLM] Custom provider patterns set: {list(patterns.keys())}")

    _sync_client = SyncLLMClient(
        base_url=base_url,
        api_key=api_key,
        max_retries=llm_cfg.get("max_retries", 3),
        total_timeout=float(llm_cfg.get("total_timeout", llm_cfg.get("timeout", DEFAULT_LLM_TIMEOUT))),
        content_fallbacks=llm_cfg.get("content_fallbacks"),
        collector_url=llm_cfg.get("collector_url"),
        collector_project=llm_cfg.get("collector_project", ""),
        collector_api_key=llm_cfg.get("collector_api_key", ""),
        refusal_keywords_url=llm_cfg.get("refusal_keywords_url"),
    )
    logger.info("[LLM] SyncLLMClient initialized with llm-compat")


def get_default_config() -> Optional[Dict[str, Any]]:
    """获取当前默认配置"""
    return _default_config


def get_sync_client() -> SyncLLMClient:
    """获取全局 SyncLLMClient 实例"""
    if _sync_client is None:
        raise RuntimeError("SyncLLMClient not initialized. Call set_default_config() first.")
    return _sync_client


# ============================================================
# 异常类定义
# ============================================================

class LLMCallError(Exception):
    """
    LLM 调用失败异常

    当 LLM API 调用在所有重试后仍然失败时抛出此异常。
    上层调用者应捕获此异常并决定如何处理（如不写入结果文件）。

    Attributes:
        message: 错误描述
        last_error: 最后一次尝试的原始异常
    """

    def __init__(self, message: str, last_error: Optional[Exception] = None):
        super().__init__(message)
        self.message = message
        self.last_error = last_error

    def __str__(self) -> str:
        return self.message


# ============================================================
# 结构化输出结果类型（解决错误处理问题）
# ============================================================

@dataclass
class StructuredResult:
    """
    结构化输出结果，包含成功/失败信息

    Attributes:
        success: 是否成功
        data: 成功时的解析结果字典
        error: 失败时的错误描述
    """
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# ============================================================
# 统计模块
# ============================================================

@dataclass
class LLMStats:
    """LLM 调用统计"""
    text_calls: int = 0
    json_schema_calls: int = 0
    json_object_calls: int = 0
    json_object_parse_failures: int = 0
    json_object_retry_success: int = 0
    json_object_final_failures: int = 0


_stats = LLMStats()


def log_llm_stats() -> None:
    """输出 LLM 调用统计"""
    total = _stats.text_calls + _stats.json_schema_calls + _stats.json_object_calls
    if total == 0:
        return

    logger.info("=" * 60)
    logger.info("LLM Call Statistics")
    logger.info("-" * 60)
    logger.info(f"  Text output calls: {_stats.text_calls}")
    logger.info(f"  json_schema calls: {_stats.json_schema_calls}")
    logger.info(f"  json_object calls: {_stats.json_object_calls}")

    if _stats.json_object_calls > 0:
        success = _stats.json_object_calls - _stats.json_object_final_failures
        rate = success / _stats.json_object_calls * 100
        logger.info(f"  json_object parse failures: {_stats.json_object_parse_failures}")
        logger.info(f"  json_object retry successes: {_stats.json_object_retry_success}")
        logger.info(f"  json_object final failures: {_stats.json_object_final_failures}")
        logger.info(f"  json_object success rate: {rate:.1f}%")

    logger.info("=" * 60)


def get_llm_stats() -> LLMStats:
    """获取当前统计数据（用于测试或监控）"""
    return _stats


def reset_llm_stats() -> None:
    """重置统计（用于测试）"""
    global _stats
    _stats = LLMStats()


# ============================================================
# 内部工具函数
# ============================================================


def _is_truncation_error(error_msg: str) -> bool:
    """判断是否为输出截断错误（模型 token 耗尽导致 JSON 不完整）

    Args:
        error_msg: 错误消息（小写）

    Returns:
        True 表示是截断错误
    """
    truncation_patterns = ['unterminated string', 'unexpected end']
    return any(p in error_msg for p in truncation_patterns)


def _get_json_mode_for_model(model_name: str, config: Dict[str, Any]) -> str:
    """
    根据模型名称获取对应的 JSON 输出模式

    Args:
        model_name: 模型名称
        config: 配置字典

    Returns:
        str: "json_schema" 或 "json_object"
    """
    json_output_config = config.get('llm', {}).get('json_output', {})

    if not json_output_config.get('enable_fallback', True):
        return "json_schema"

    mode_mapping = json_output_config.get('mode_by_model', {'*': 'json_schema'})
    model_name_lower = model_name.lower()

    for pattern, mode in mode_mapping.items():
        if fnmatch.fnmatch(model_name_lower, pattern.lower()):
            return mode

    return "json_schema"


def _schema_to_prompt_instruction(schema: Dict[str, Any]) -> str:
    """
    将 JSON Schema 转换为 Prompt 中的格式说明

    Args:
        schema: JSON Schema 定义

    Returns:
        str: 格式说明文本
    """
    if not schema:
        return ""

    schema_json = json.dumps(schema, indent=2, ensure_ascii=False)

    return f"""
【输出格式要求】
请严格按照以下 JSON Schema 返回结果，确保输出是合法的 JSON 对象：

```json
{schema_json}
```

重要提示：
- 所有 required 字段必须存在
- enum 字段只能使用指定的值
- 数组字段即使为空也要返回 []
- 不要添加 Schema 中未定义的字段
- 直接返回 JSON，不要包裹在 markdown 代码块中
"""


def _validate_required_fields(
    parsed_json: Any,
    schema: Dict[str, Any]
) -> tuple[bool, str]:
    """
    验证 JSON 响应是否包含必填字段

    Args:
        parsed_json: 解析后的 JSON 对象
        schema: JSON Schema 定义

    Returns:
        tuple: (是否有效, 错误信息)
    """
    if not isinstance(parsed_json, dict):
        return False, f"Response is not a dict (type={type(parsed_json).__name__})"

    required_fields = schema.get("required", [])
    missing_fields = [f for f in required_fields if f not in parsed_json]

    if missing_fields:
        return False, f"Missing required fields: {', '.join(missing_fields)}"

    return True, ""


def _extract_json_from_response(response: str) -> str:
    """
    从响应中提取 JSON 内容（处理可能的 markdown 包裹）

    Args:
        response: LLM 原始响应

    Returns:
        str: 提取的 JSON 字符串
    """
    # 尝试匹配 ```json ... ```
    json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
    if json_match:
        return json_match.group(1).strip()

    # 尝试匹配 ``` ... ```
    code_match = re.search(r'```\s*(.*?)\s*```', response, re.DOTALL)
    if code_match:
        return code_match.group(1).strip()

    return response.strip()


# ============================================================
# 内部调用函数
# ============================================================

def _call_with_text_output(
    model: str,
    prompt: str,
    system_prompt: str,
    reasoning_effort: Optional[str],
    task_type: str
) -> str:
    """纯文本输出调用（通过 llm-compat SyncLLMClient）"""
    global _stats
    _stats.text_calls += 1

    client = get_sync_client()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    start_time = time.time()
    logger.info(f"[{task_type.upper()}] Model: {model} | text mode")

    result = client.chat(model, messages, reasoning_effort=reasoning_effort, stream=False)
    content = str(result)
    duration = time.time() - start_time

    if result.fallback_from:
        logger.warning(
            f"[{task_type.upper()}] Content fallback: {result.fallback_from} -> {result.model}"
        )

    logger.info(f"[{task_type.upper()}] Succeeded | {len(content)} chars | {duration:.2f}s")
    return content


def _call_with_json_schema_mode(
    model: str,
    prompt: str,
    schema: Dict[str, Any],
    system_prompt: str,
    reasoning_effort: Optional[str],
    task_type: str
) -> StructuredResult:
    """使用 json_schema 模式调用（严格模式，通过 llm-compat）"""
    global _stats
    _stats.json_schema_calls += 1

    client = get_sync_client()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": f"{task_type}_response",
            "schema": schema,
            "strict": True,
        },
    }

    logger.info(f"[{task_type.upper()}] json_schema mode | Model: {model}")

    try:
        result = client.chat(
            model, messages,
            reasoning_effort=reasoning_effort,
            response_format=response_format,
            stream=False,
        )

        if result.fallback_from:
            logger.warning(
                f"[{task_type.upper()}] Content fallback: {result.fallback_from} -> {result.model}"
            )

        content = str(result).strip()
        parsed = json.loads(content)
        logger.info(f"[{task_type.upper()}] json_schema succeeded")
        return StructuredResult(success=True, data=parsed)

    except Exception as e:
        logger.error(f"[{task_type.upper()}] json_schema failed: {e}")
        return StructuredResult(success=False, error=f"json_schema call failed: {e}")


def _call_with_json_object_mode(
    model: str,
    prompt: str,
    schema: Dict[str, Any],
    config: Dict[str, Any],
    system_prompt: str,
    reasoning_effort: Optional[str],
    task_type: str,
) -> StructuredResult:
    """使用 json_object 模式调用（兼容模式，带 Self-Correction，通过 llm-compat）"""
    global _stats
    _stats.json_object_calls += 1

    client = get_sync_client()

    json_output_config = config.get("llm", {}).get("json_output", {})
    json_object_retries = json_output_config.get("max_retries", 2)

    schema_instruction = _schema_to_prompt_instruction(schema)
    enhanced_prompt = f"{prompt}\n\n{schema_instruction}"

    last_response = None
    last_error = None

    for attempt in range(json_object_retries + 1):
        try:
            messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

            if attempt > 0 and last_response and last_error:
                messages.append({"role": "assistant", "content": last_response})
                messages.append({"role": "user", "content": f"Format error: {last_error}\nPlease output correct JSON directly."})
                logger.warning(f"[{task_type.upper()}] Self-Correction retry {attempt}/{json_object_retries}")
            else:
                messages.append({"role": "user", "content": enhanced_prompt})

            logger.info(
                f"[{task_type.upper()}] json_object mode | Model: {model} | "
                f"Attempt {attempt + 1}/{json_object_retries + 1}"
            )

            result = client.chat(
                model, messages,
                reasoning_effort=reasoning_effort,
                response_format={"type": "json_object"},
                stream=False,
            )

            if result.fallback_from:
                logger.warning(
                    f"[{task_type.upper()}] Content fallback: {result.fallback_from} -> {result.model}"
                )

            content = str(result).strip()
            last_response = content

            json_str = _extract_json_from_response(content)

            try:
                parsed = json.loads(json_str)
            except json.JSONDecodeError as e:
                error_msg = str(e).lower()
                if _is_truncation_error(error_msg):
                    raise LLMCallError(f"Output truncated: JSON parse failed: {e}", e)
                last_error = f"JSON parse failed: {str(e)}"
                _stats.json_object_parse_failures += 1
                continue

            is_valid, validation_error = _validate_required_fields(parsed, schema)
            if not is_valid:
                last_error = validation_error
                _stats.json_object_parse_failures += 1
                continue

            if attempt > 0:
                _stats.json_object_retry_success += 1
                logger.info(f"[{task_type.upper()}] Self-Correction succeeded")
            else:
                logger.info(f"[{task_type.upper()}] json_object succeeded")

            return StructuredResult(success=True, data=parsed)

        except LLMCallError:
            raise

        except Exception as e:
            last_error = f"API call error: {str(e)}"
            logger.warning(f"[{task_type.upper()}] json_object error: {e}")

        if attempt < json_object_retries:
            time.sleep(1)

    _stats.json_object_final_failures += 1
    logger.error(f"[{task_type.upper()}] json_object failed after {json_object_retries + 1} attempts")
    return StructuredResult(success=False, error=f"json_object call failed: {last_error}")


# ============================================================
# 公共 API
# ============================================================

@overload
def call_llm_api(
    model: str,
    prompt: str,
    reasoning_effort: Optional[str] = None,
    task_type: str = "unknown",
    *,
    response_schema: None = None,
    config: Optional[Dict[str, Any]] = None,
    system_prompt: str = "You are a helpful assistant.",
) -> str: ...


@overload
def call_llm_api(
    model: str,
    prompt: str,
    reasoning_effort: Optional[str] = None,
    task_type: str = "unknown",
    *,
    response_schema: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    system_prompt: str = "You are a helpful assistant.",
) -> StructuredResult: ...


def call_llm_api(
    model: str,
    prompt: str,
    reasoning_effort: Optional[str] = None,
    task_type: str = "unknown",
    *,
    response_schema: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    system_prompt: str = "You are a helpful assistant.",
    # Deprecated params kept for backward compat (ignored)
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    max_retries: Optional[int] = None,
    retry_delay: Optional[int] = None,
) -> Union[str, StructuredResult]:
    """调用 LLM API（通过 llm-compat SyncLLMClient）

    api_key/base_url/max_retries/retry_delay 参数已废弃，
    由 set_default_config() 初始化的 SyncLLMClient 统一管理。
    """
    if response_schema is None:
        return _call_with_text_output(
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            reasoning_effort=reasoning_effort,
            task_type=task_type,
        )

    effective_config = config if config is not None else _default_config
    if effective_config is None:
        effective_config = {"llm": {"json_output": {"mode_by_model": {"*": "json_schema"}}}}

    json_mode = _get_json_mode_for_model(model, effective_config)
    logger.info(f"[{task_type.upper()}] Model: {model} | JSON Mode: {json_mode}")

    if json_mode == "json_schema":
        return _call_with_json_schema_mode(
            model=model,
            prompt=prompt,
            schema=response_schema,
            system_prompt=system_prompt,
            reasoning_effort=reasoning_effort,
            task_type=task_type,
        )
    else:
        return _call_with_json_object_mode(
            model=model,
            prompt=prompt,
            schema=response_schema,
            config=effective_config,
            system_prompt=system_prompt,
            reasoning_effort=reasoning_effort,
            task_type=task_type,
        )
