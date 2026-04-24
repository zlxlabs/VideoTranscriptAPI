"""
LLM 调用模块

支持纯文本输出和 JSON Schema 结构化输出。
根据模型支持情况自动选择 json_schema（严格模式）或 json_object（兼容模式）。
"""
import json
import fnmatch
import re
import requests
import requests.exceptions
import time
from typing import Any, Dict, Optional, Union, overload
from dataclasses import dataclass
from loguru import logger

from . import providers


# ============================================================
# 全局默认配置（解决 config 参数强依赖问题）
# ============================================================

_default_config: Optional[Dict[str, Any]] = None

# 默认 API 请求超时（秒）
DEFAULT_LLM_TIMEOUT = 180


def _get_llm_timeout() -> int:
    """从默认配置中获取 LLM API 请求超时时间（秒）"""
    if _default_config is not None:
        return _default_config.get('llm', {}).get('timeout', DEFAULT_LLM_TIMEOUT)
    return DEFAULT_LLM_TIMEOUT


def set_default_config(config: Dict[str, Any]) -> None:
    """
    设置模块级默认配置（通常在应用启动时调用一次）

    Args:
        config: 完整配置字典，包含 llm.json_output 节点
    """
    global _default_config
    _default_config = config
    logger.debug("[LLM] Default config set")


def get_default_config() -> Optional[Dict[str, Any]]:
    """获取当前默认配置"""
    return _default_config


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
    api_key: str,
    base_url: str,
    system_prompt: str,
    max_retries: int,
    retry_delay: int,
    reasoning_effort: Optional[str],
    task_type: str
) -> str:
    """
    纯文本输出调用

    Args:
        model: 模型名称
        prompt: 用户提示词
        api_key: API 密钥
        base_url: API 基础 URL
        system_prompt: 系统提示词
        max_retries: 最大重试次数
        retry_delay: 重试间隔秒数
        reasoning_effort: 推理强度
        task_type: 任务类型

    Returns:
        str: 模型返回的文本内容
    """
    global _stats
    _stats.text_calls += 1

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    base_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "stream": False
    }

    # 由 providers 层按模型族翻译 thinking/reasoning 参数 + 深合并
    data = providers.build_request_payload(model, reasoning_effort, base_payload)

    last_error = None
    start_time = time.time()

    for attempt in range(max_retries + 1):
        try:
            desc = providers.describe_from_payload(data)
            logger.info(
                f"[{task_type.upper()}] Model: {desc['model']} ({desc['provider']}) | "
                f"Thinking: {desc['thinking_mode']} | "
                f"Attempt {attempt + 1}/{max_retries + 1}"
            )

            resp = requests.post(base_url, json=data, headers=headers, timeout=_get_llm_timeout())
            resp.raise_for_status()
            result = resp.json()

            content = result["choices"][0]["message"]["content"].strip()
            duration = time.time() - start_time

            if attempt > 0:
                logger.info(f"[{task_type.upper()}] Succeeded after {attempt + 1} attempts")
            else:
                logger.info(f"[{task_type.upper()}] Succeeded")

            logger.debug(f"[{task_type.upper()}] Response: {len(content)} chars | Duration: {duration:.2f}s")
            return content

        except Exception as e:
            last_error = e
            logger.warning(f"[{task_type.upper()}] Error: {e} | Attempt {attempt + 1}/{max_retries + 1}")

        if attempt < max_retries:
            time.sleep(retry_delay)

    error_msg = f"Failed after {max_retries + 1} attempts | Last error: {last_error}"
    logger.error(f"[{task_type.upper()}] {error_msg}")
    raise LLMCallError(error_msg, last_error)


def _call_with_json_schema_mode(
    model: str,
    prompt: str,
    schema: Dict[str, Any],
    api_key: str,
    base_url: str,
    system_prompt: str,
    max_retries: int,
    retry_delay: int,
    reasoning_effort: Optional[str],
    task_type: str
) -> StructuredResult:
    """
    使用 json_schema 模式调用（严格模式）

    Args:
        model: 模型名称
        prompt: 用户提示词
        schema: JSON Schema 定义
        api_key: API 密钥
        base_url: API 基础 URL
        system_prompt: 系统提示词
        max_retries: 最大重试次数
        retry_delay: 重试间隔秒数
        reasoning_effort: 推理强度
        task_type: 任务类型

    Returns:
        StructuredResult: 包含 success/data/error 的结果
    """
    global _stats
    _stats.json_schema_calls += 1

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    base_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": f"{task_type}_response",
                "schema": schema,
                "strict": True
            }
        }
    }

    data = providers.build_request_payload(model, reasoning_effort, base_payload)

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            desc = providers.describe_from_payload(data)
            logger.info(
                f"[{task_type.upper()}] json_schema mode ({desc['provider']}, "
                f"thinking={desc['thinking_mode']}) | Attempt {attempt + 1}/{max_retries + 1}"
            )

            resp = requests.post(base_url, json=data, headers=headers, timeout=_get_llm_timeout())
            resp.raise_for_status()
            result = resp.json()

            content = result["choices"][0]["message"]["content"].strip()
            parsed = json.loads(content)

            logger.info(f"[{task_type.upper()}] json_schema succeeded")
            return StructuredResult(success=True, data=parsed)

        except Exception as e:
            last_error = str(e)
            logger.warning(f"[{task_type.upper()}] json_schema error: {e}")

        if attempt < max_retries:
            time.sleep(retry_delay)

    logger.error(f"[{task_type.upper()}] json_schema failed after {max_retries + 1} attempts")
    return StructuredResult(success=False, error=f"json_schema call failed: {last_error}")


def _call_with_json_object_mode(
    model: str,
    prompt: str,
    schema: Dict[str, Any],
    api_key: str,
    base_url: str,
    config: Dict[str, Any],
    system_prompt: str,
    max_retries: Optional[int],
    retry_delay: int,
    reasoning_effort: Optional[str],
    task_type: str
) -> StructuredResult:
    """
    使用 json_object 模式调用（兼容模式，带 Self-Correction）

    Args:
        model: 模型名称
        prompt: 用户提示词
        schema: JSON Schema 定义
        api_key: API 密钥
        base_url: API 基础 URL
        config: 配置字典
        system_prompt: 系统提示词
        max_retries: 最大重试次数
        retry_delay: 重试间隔秒数
        reasoning_effort: 推理强度
        task_type: 任务类型

    Returns:
        StructuredResult: 包含 success/data/error 的结果
    """
    global _stats
    _stats.json_object_calls += 1

    json_output_config = config.get('llm', {}).get('json_output', {})
    # 参数优先，配置兜底（_actual_call 传 max_retries=0 以禁用底层重试）
    config_retries = json_output_config.get('max_retries', 2)
    json_object_retries = max_retries if max_retries is not None else config_retries

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    # 在 Prompt 中注入 Schema 说明
    schema_instruction = _schema_to_prompt_instruction(schema)
    enhanced_prompt = f"{prompt}\n\n{schema_instruction}"

    last_response = None
    last_error = None

    for attempt in range(json_object_retries + 1):
        try:
            messages = [{"role": "system", "content": system_prompt}]

            # Self-Correction：重试时将错误信息反馈给模型
            if attempt > 0 and last_response and last_error:
                messages.append({"role": "assistant", "content": last_response})
                messages.append({"role": "user", "content": f"Format error: {last_error}\nPlease output correct JSON directly."})
                logger.warning(f"[{task_type.upper()}] Self-Correction retry {attempt}/{json_object_retries}")
            else:
                messages.append({"role": "user", "content": enhanced_prompt})

            base_payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                "response_format": {"type": "json_object"}
            }

            data = providers.build_request_payload(model, reasoning_effort, base_payload)

            desc = providers.describe_from_payload(data)
            logger.info(
                f"[{task_type.upper()}] json_object mode ({desc['provider']}, "
                f"thinking={desc['thinking_mode']}) | Attempt {attempt + 1}/{json_object_retries + 1}"
            )

            resp = requests.post(base_url, json=data, headers=headers, timeout=_get_llm_timeout())
            resp.raise_for_status()
            result = resp.json()

            content = result["choices"][0]["message"]["content"].strip()
            last_response = content

            # 提取 JSON
            json_str = _extract_json_from_response(content)

            # 解析 JSON
            try:
                parsed = json.loads(json_str)
            except json.JSONDecodeError as e:
                error_msg = str(e).lower()
                # 截断错误（token 耗尽）：Self-Correction 无效，直接上抛
                if _is_truncation_error(error_msg):
                    raise LLMCallError(
                        f"Output truncated: JSON parse failed: {e}", e
                    )
                # 普通格式错误：允许 Self-Correction
                last_error = f"JSON parse failed: {str(e)}"
                _stats.json_object_parse_failures += 1
                continue

            # 验证必填字段
            is_valid, validation_error = _validate_required_fields(parsed, schema)
            if not is_valid:
                last_error = validation_error
                _stats.json_object_parse_failures += 1
                continue

            # 成功
            if attempt > 0:
                _stats.json_object_retry_success += 1
                logger.info(f"[{task_type.upper()}] Self-Correction succeeded")
            else:
                logger.info(f"[{task_type.upper()}] json_object succeeded")

            return StructuredResult(success=True, data=parsed)

        except requests.exceptions.Timeout as e:
            # 超时：没有响应可 Self-Correct，直接上抛
            raise LLMCallError(f"API timeout: {e}", e)

        except requests.exceptions.ConnectionError as e:
            # 连接失败：直接上抛
            raise LLMCallError(f"Connection error: {e}", e)

        except LLMCallError:
            # 已包装的错误（如截断），直接上抛
            raise

        except Exception as e:
            # 其他错误：允许 Self-Correction 继续
            last_error = f"API call error: {str(e)}"
            logger.warning(f"[{task_type.upper()}] json_object error: {e}")

        if attempt < json_object_retries:
            time.sleep(retry_delay)

    # 所有重试都失败
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
    api_key: str,
    base_url: str,
    max_retries: int = 2,
    retry_delay: int = 5,
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
    api_key: str,
    base_url: str,
    max_retries: int = 2,
    retry_delay: int = 5,
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
    api_key: str,
    base_url: str,
    max_retries: int = 2,
    retry_delay: int = 5,
    reasoning_effort: Optional[str] = None,
    task_type: str = "unknown",
    *,
    response_schema: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    system_prompt: str = "You are a helpful assistant.",
) -> Union[str, StructuredResult]:
    """
    调用大语言模型 API

    支持两种输出模式：
    1. 纯文本输出（默认）：不传 response_schema
    2. 结构化 JSON 输出：传入 response_schema

    Args:
        model: 模型名称
        prompt: 用户提示词
        api_key: API 密钥
        base_url: API 基础 URL
        max_retries: API 调用最大重试次数，默认 2
        retry_delay: 重试间隔秒数，默认 5
        reasoning_effort: 推理强度 ("none", "low", "medium", "high")
        task_type: 任务类型，用于日志追踪
        response_schema: JSON Schema 定义（传入则启用结构化输出）
        config: 配置字典（可选，不传则使用 set_default_config 设置的默认配置）
        system_prompt: 系统提示词

    Returns:
        - 无 response_schema: 返回 str（纯文本）
        - 有 response_schema: 返回 StructuredResult（包含 success/data/error）

    Examples:
        # 应用启动时设置默认配置（只需一次）
        from video_transcript_api.llm import set_default_config
        set_default_config(app_config)

        # 纯文本输出（兼容现有调用）
        text = call_llm_api(model, prompt, api_key, base_url, task_type="calibrate")

        # 结构化 JSON 输出（使用默认配置）
        result = call_llm_api(
            model, prompt, api_key, base_url,
            response_schema=CALIBRATION_SCHEMA,
            task_type="calibrate_chunk"
        )
        if result.success:
            dialogs = result.data["calibrated_dialogs"]
        else:
            logger.error(f"Failed: {result.error}")
    """
    # 纯文本输出路径
    if response_schema is None:
        return _call_with_text_output(
            model=model,
            prompt=prompt,
            api_key=api_key,
            base_url=base_url,
            system_prompt=system_prompt,
            max_retries=max_retries,
            retry_delay=retry_delay,
            reasoning_effort=reasoning_effort,
            task_type=task_type
        )

    # 结构化输出路径：优先使用传入的 config，否则使用默认配置
    effective_config = config if config is not None else _default_config
    if effective_config is None:
        # 无配置时使用默认行为：json_schema 模式
        effective_config = {"llm": {"json_output": {"mode_by_model": {"*": "json_schema"}}}}

    # 选择 JSON 输出模式
    json_mode = _get_json_mode_for_model(model, effective_config)
    logger.info(f"[{task_type.upper()}] Model: {model} | JSON Mode: {json_mode}")

    if json_mode == "json_schema":
        return _call_with_json_schema_mode(
            model=model,
            prompt=prompt,
            schema=response_schema,
            api_key=api_key,
            base_url=base_url,
            system_prompt=system_prompt,
            max_retries=max_retries,
            retry_delay=retry_delay,
            reasoning_effort=reasoning_effort,
            task_type=task_type
        )
    else:
        return _call_with_json_object_mode(
            model=model,
            prompt=prompt,
            schema=response_schema,
            api_key=api_key,
            base_url=base_url,
            config=effective_config,
            system_prompt=system_prompt,
            max_retries=max_retries,
            retry_delay=retry_delay,
            reasoning_effort=reasoning_effort,
            task_type=task_type
        )
