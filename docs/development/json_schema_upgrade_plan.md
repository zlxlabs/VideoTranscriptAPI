# LLM JSON Schema 结构化输出升级方案

## 1. 现状分析

### 1.1 当前实现方式

| 模块 | 期望输出 | 当前方式 | 问题 |
|------|----------|----------|------|
| `structured_calibrator.py` | 校对结果 JSON | Prompt 约定 + 正则提取 | 格式不保证 |
| `structured_calibrator.py` | 验证结果 JSON | Prompt 约定 + 正则提取 | 字段可能缺失 |
| `llm_enhanced.py` | 说话人映射 JSON | Prompt 约定 + 正则提取 | 需要手动 fallback |

### 1.2 核心问题

```python
# 当前方式 (llm.py)
data = {
    "model": model,
    "messages": [...],
    "stream": False
}
# ❌ 没有使用 response_format 参数
```

**后果**：
- 解析失败率高（需要正则提取 ```json...``` 代码块）
- 字段缺失时才发现问题
- 需要大量 fallback 代码

---

## 2. 升级目标

1. **兼容性**：支持 `json_schema`（严格模式）和 `json_object`（兼容模式）
2. **透明性**：业务代码统一使用 Schema 定义，底层自动选择模式
3. **可配置**：通过配置文件按模型名匹配输出模式
4. **可靠性**：`json_object` 模式下支持 Self-Correction 重试
5. **向后兼容**：扩展现有 `call_llm_api()` 函数，不破坏现有调用
6. **简化调用**：通过 `set_default_config()` 设置默认配置，调用时无需重复传入 config
7. **明确错误**：结构化输出返回 `StructuredResult`，包含 success/data/error 三个字段

---

## 3. 架构设计

### 3.1 统一入口设计

采用**单一函数 + 可选参数**的设计，通过 `response_schema` 参数区分输出模式：

```
┌────────────────────────────────────────────────────────────────────┐
│                         业务代码调用                                │
│   call_llm_api(..., response_schema=CALIBRATION_SCHEMA)           │
└───────────────────────────────┬────────────────────────────────────┘
                                │
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│                      call_llm_api() 统一入口                        │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  response_schema is None?                                    │  │
│  │  ├─ Yes → 纯文本输出路径 → return str                         │  │
│  │  └─ No  → 结构化输出路径 → return Dict | None                 │  │
│  └──────────────────────────────────────────────────────────────┘  │
└───────────────────────────────┬────────────────────────────────────┘
                                │
                                ▼ (结构化输出路径)
┌────────────────────────────────────────────────────────────────────┐
│                   _select_json_mode(model_name)                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  1. 读取配置 llm.json_output.mode_by_model                    │  │
│  │  2. 按模型名通配符匹配                                         │  │
│  │  3. 返回 "json_schema" 或 "json_object"                       │  │
│  └──────────────────────────────────────────────────────────────┘  │
└───────────────────────────────┬────────────────────────────────────┘
                                │
              ┌─────────────────┴─────────────────┐
              │                                   │
              ▼                                   ▼
┌─────────────────────────┐         ┌─────────────────────────────────┐
│    json_schema 模式     │         │       json_object 模式           │
│  response_format={...}  │         │  _call_with_json_object_mode    │
│                         │         ├─────────────────────────────────┤
│  - 100% 格式保证        │         │  1. Schema 转 Prompt 注入        │
│  - 无需额外处理         │         │  2. response_format=json_object  │
│                         │         │  3. 解析 + 验证必填字段          │
│                         │         │  4. 失败则 Self-Correction 重试  │
└─────────────────────────┘         └─────────────────────────────────┘
```

### 3.2 内部模块拆分

```
# 全局配置管理
set_default_config()                    # 设置默认配置（应用启动时调用）
get_default_config()                    # 获取默认配置

# 返回类型
StructuredResult                        # 结构化输出结果（success/data/error）

# 统一入口
call_llm_api()
├── _call_with_text_output()            # 分支：纯文本处理 → str
└── _call_with_structured_output()      # 分支：结构化处理 → StructuredResult
    ├── _get_json_mode_for_model()      # 选择输出模式
    ├── _call_json_schema_mode()        # 严格模式
    └── _call_json_object_mode()        # 兼容模式 + Self-Correction
        ├── _schema_to_prompt_instruction()  # Schema 转 Prompt
        └── _validate_required_fields()      # 字段验证
```

---

## 4. 配置设计

### 4.1 新增配置项

在 `config.jsonc` 的 `llm` 节点下新增：

```jsonc
"llm": {
    // ... 现有配置 ...

    // ------------------------------------------------------
    // JSON 结构化输出配置
    // ------------------------------------------------------
    "json_output": {
        // 按模型名匹配输出模式（支持通配符 *）
        // 匹配顺序：从前到后，首次匹配生效
        // 可选值：json_schema（严格模式）、json_object（兼容模式）
        "mode_by_model": {
            "deepseek*": "json_object",     // DeepSeek 系列不支持 json_schema
            "qwen*": "json_object",         // 通义千问部分支持
            "glm*": "json_object",          // 智谱 GLM 系列
            "*": "json_schema"              // 其他模型使用严格模式
        },
        // json_object 模式下解析失败时的最大重试次数
        "max_retries": 2,
        // 是否启用模式降级（false 时强制使用 json_schema，用于调试）
        "enable_fallback": true
    }
}
```

### 4.2 模型支持情况参考

| 模型 | json_schema 支持 | json_object 支持 | 推荐配置 |
|------|-----------------|-----------------|----------|
| GPT-4o / GPT-4-turbo | ✅ 完整支持 | ✅ 支持 | json_schema |
| Claude 3.5 | ✅ 完整支持 | ✅ 支持 | json_schema |
| Gemini 2.0 | ✅ 完整支持 | ✅ 支持 | json_schema |
| DeepSeek | ❌ 不支持 | ✅ 支持 | json_object |
| Qwen (通义千问) | ⚠️ 部分支持 | ✅ 支持 | json_object |
| GLM-4 (智谱) | ⚠️ 部分支持 | ✅ 支持 | json_object |

---

## 5. 核心代码实现

### 5.1 修改文件：`llm.py`

```python
"""
LLM 调用模块
支持纯文本输出和 JSON Schema 结构化输出
"""
import json
import fnmatch
import requests
import time
from typing import Any, Dict, Optional, Union, overload
from dataclasses import dataclass, field
from loguru import logger


# ============================================================
# 全局默认配置（解决 config 参数强依赖问题）
# ============================================================

_default_config: Optional[Dict[str, Any]] = None


def set_default_config(config: Dict[str, Any]) -> None:
    """
    设置模块级默认配置（通常在应用启动时调用一次）

    Args:
        config: 完整配置字典，包含 llm.json_output 节点
    """
    global _default_config
    _default_config = config


def get_default_config() -> Optional[Dict[str, Any]]:
    """获取当前默认配置"""
    return _default_config


# ============================================================
# 结构化输出结果类型（解决错误处理问题）
# ============================================================

@dataclass
class StructuredResult:
    """结构化输出结果，包含成功/失败信息"""
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None  # 失败时的错误描述


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


def reset_llm_stats() -> None:
    """重置统计（用于测试）"""
    global _stats
    _stats = LLMStats()


# ============================================================
# 内部工具函数
# ============================================================

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
    """将 JSON Schema 转换为 Prompt 中的格式说明"""
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
    """验证 JSON 响应是否包含必填字段"""
    if not isinstance(parsed_json, dict):
        return False, f"响应不是字典类型 (type={type(parsed_json).__name__})"

    required_fields = schema.get("required", [])
    missing_fields = [f for f in required_fields if f not in parsed_json]

    if missing_fields:
        return False, f"缺少必填字段: {', '.join(missing_fields)}"

    return True, ""


def _extract_json_from_response(response: str) -> str:
    """从响应中提取 JSON 内容（处理可能的 markdown 包裹）"""
    import re

    json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
    if json_match:
        return json_match.group(1).strip()

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
    """纯文本输出调用"""
    global _stats
    _stats.text_calls += 1

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    data = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "stream": False
    }

    if reasoning_effort is not None:
        data["reasoning_effort"] = reasoning_effort

    last_error = None
    start_time = time.time()

    for attempt in range(max_retries + 1):
        try:
            reasoning_status = 'disabled' if reasoning_effort is None else reasoning_effort
            logger.info(
                f"[{task_type.upper()}] Model: {model} | Reasoning: {reasoning_status} | "
                f"Attempt {attempt + 1}/{max_retries + 1}"
            )

            resp = requests.post(base_url, json=data, headers=headers, timeout=360)
            resp.raise_for_status()
            result = resp.json()

            content = result["choices"][0]["message"]["content"].strip()
            duration = time.time() - start_time

            if attempt > 0:
                logger.info(f"[{task_type.upper()}] ✓ Succeeded after {attempt + 1} attempts")
            else:
                logger.info(f"[{task_type.upper()}] ✓ Succeeded")

            logger.debug(f"[{task_type.upper()}] Response: {len(content)} chars | Duration: {duration:.2f}s")
            return content

        except Exception as e:
            last_error = e
            logger.warning(f"[{task_type.upper()}] ✗ Error: {e} | Attempt {attempt + 1}/{max_retries + 1}")

        if attempt < max_retries:
            time.sleep(retry_delay)

    logger.error(f"[{task_type.upper()}] ✗ Failed after {max_retries + 1} attempts | Last error: {last_error}")
    return f"【LLM call failed】{last_error}"


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
    """使用 json_schema 模式调用（严格模式）"""
    global _stats
    _stats.json_schema_calls += 1

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    data = {
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

    if reasoning_effort is not None:
        data["reasoning_effort"] = reasoning_effort

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            logger.info(f"[{task_type.upper()}] json_schema mode | Attempt {attempt + 1}/{max_retries + 1}")

            resp = requests.post(base_url, json=data, headers=headers, timeout=360)
            resp.raise_for_status()
            result = resp.json()

            content = result["choices"][0]["message"]["content"].strip()
            parsed = json.loads(content)

            logger.info(f"[{task_type.upper()}] ✓ json_schema succeeded")
            return StructuredResult(success=True, data=parsed)

        except Exception as e:
            last_error = str(e)
            logger.warning(f"[{task_type.upper()}] ✗ json_schema error: {e}")

        if attempt < max_retries:
            time.sleep(retry_delay)

    logger.error(f"[{task_type.upper()}] ✗ json_schema failed after {max_retries + 1} attempts")
    return StructuredResult(success=False, error=f"json_schema 调用失败: {last_error}")


def _call_with_json_object_mode(
    model: str,
    prompt: str,
    schema: Dict[str, Any],
    api_key: str,
    base_url: str,
    config: Dict[str, Any],
    system_prompt: str,
    max_retries: int,
    retry_delay: int,
    reasoning_effort: Optional[str],
    task_type: str
) -> StructuredResult:
    """使用 json_object 模式调用（兼容模式，带 Self-Correction）"""
    global _stats
    _stats.json_object_calls += 1

    json_output_config = config.get('llm', {}).get('json_output', {})
    json_object_retries = json_output_config.get('max_retries', 2)

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
                messages.append({"role": "user", "content": f"格式错误：{last_error}\n请直接输出正确的 JSON。"})
                logger.warning(f"[{task_type.upper()}] Self-Correction retry {attempt}/{json_object_retries}")
            else:
                messages.append({"role": "user", "content": enhanced_prompt})

            data = {
                "model": model,
                "messages": messages,
                "stream": False,
                "response_format": {"type": "json_object"}
            }

            if reasoning_effort is not None:
                data["reasoning_effort"] = reasoning_effort

            logger.info(f"[{task_type.upper()}] json_object mode | Attempt {attempt + 1}/{json_object_retries + 1}")

            resp = requests.post(base_url, json=data, headers=headers, timeout=360)
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
                last_error = f"JSON 解析失败: {str(e)}"
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
                logger.info(f"[{task_type.upper()}] ✓ Self-Correction succeeded")
            else:
                logger.info(f"[{task_type.upper()}] ✓ json_object succeeded")

            return StructuredResult(success=True, data=parsed)

        except Exception as e:
            last_error = f"API 调用异常: {str(e)}"
            logger.warning(f"[{task_type.upper()}] ✗ json_object error: {e}")

        if attempt < json_object_retries:
            time.sleep(retry_delay)

    # 所有重试都失败
    _stats.json_object_final_failures += 1
    logger.error(f"[{task_type.upper()}] ✗ json_object failed after {json_object_retries + 1} attempts")
    return StructuredResult(success=False, error=f"json_object 调用失败: {last_error}")


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
        from video_transcript_api.utils.llm import set_default_config
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
            logger.error(f"失败: {result.error}")
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
```

---

## 6. Schema 定义

### 6.1 新增目录结构

```
src/video_transcript_api/utils/llm/
├── __init__.py
├── llm.py                    # 修改：新增结构化输出支持
├── schemas/                  # 新增：Schema 定义
│   ├── __init__.py
│   ├── calibration.py        # 校对结果 Schema
│   ├── validation.py         # 验证结果 Schema
│   └── speaker_mapping.py    # 说话人映射 Schema
└── ...
```

### 6.2 校对结果 Schema

```python
# schemas/calibration.py

CALIBRATION_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "calibrated_dialogs": {
            "type": "array",
            "description": "校对后的对话列表",
            "items": {
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "string",
                        "description": "开始时间，格式 HH:MM:SS"
                    },
                    "speaker": {
                        "type": "string",
                        "description": "说话人名称"
                    },
                    "text": {
                        "type": "string",
                        "description": "校对后的文本内容"
                    }
                },
                "required": ["start_time", "speaker", "text"]
            }
        }
    },
    "required": ["calibrated_dialogs"],
    "additionalProperties": False
}
```

### 6.3 验证结果 Schema

```python
# schemas/validation.py

VALIDATION_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_score": {
            "type": "number",
            "description": "整体质量分数 (0-10)",
            "minimum": 0,
            "maximum": 10
        },
        "scores": {
            "type": "object",
            "description": "各维度分数",
            "properties": {
                "format_correctness": {"type": "number"},
                "content_fidelity": {"type": "number"},
                "text_quality": {"type": "number"},
                "speaker_consistency": {"type": "number"},
                "time_consistency": {"type": "number"}
            },
            "required": ["format_correctness", "content_fidelity", "text_quality",
                        "speaker_consistency", "time_consistency"]
        },
        "pass": {
            "type": "boolean",
            "description": "是否通过验证"
        },
        "issues": {
            "type": "array",
            "description": "发现的问题列表",
            "items": {"type": "string"}
        },
        "recommendation": {
            "type": "string",
            "description": "改进建议"
        }
    },
    "required": ["overall_score", "scores", "pass", "issues", "recommendation"],
    "additionalProperties": False
}
```

### 6.4 说话人映射 Schema

```python
# schemas/speaker_mapping.py

SPEAKER_MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "speaker_mapping": {
            "type": "object",
            "description": "说话人标识到真实姓名的映射",
            "additionalProperties": {"type": "string"}
        },
        "confidence": {
            "type": "object",
            "description": "每个映射的置信度 (0-1)",
            "additionalProperties": {"type": "number"}
        },
        "reasoning": {
            "type": "string",
            "description": "推断依据说明"
        }
    },
    "required": ["speaker_mapping", "confidence", "reasoning"],
    "additionalProperties": False
}
```

---

## 7. 业务代码迁移示例

### 7.1 应用启动时初始化（推荐）

```python
# 在应用启动时设置默认配置（如 server.py 的 startup_event）
from video_transcript_api.utils.llm import set_default_config

def startup_event():
    # ... 其他初始化 ...
    set_default_config(app_config)  # 设置一次，后续调用无需传 config
```

### 7.2 纯文本输出（无变化）

```python
# 现有调用方式完全兼容
text = call_llm_api(
    model=self.calibrate_model,
    prompt=calibrate_prompt,
    api_key=self.api_key,
    base_url=self.base_url,
    max_retries=self.max_retries,
    retry_delay=self.retry_delay,
    reasoning_effort=self.calibrate_reasoning_effort,
    task_type="calibrate"
)
```

### 7.3 结构化校对结果（structured_calibrator.py）

**改造前：**
```python
response = call_llm_api(
    model=self.calibrate_model,
    prompt=prompt,
    api_key=self.api_key,
    base_url=self.base_url,
    max_retries=self.max_retries,
    retry_delay=self.retry_delay,
    reasoning_effort=self.calibrate_reasoning_effort,
    task_type="calibrate_chunk"
)
# 手动解析 JSON（正则提取 + json.loads）
calibrated_data = self._parse_calibration_response(response)
```

**改造后：**
```python
from .schemas.calibration import CALIBRATION_RESULT_SCHEMA

result = call_llm_api(
    model=self.calibrate_model,
    prompt=prompt,
    api_key=self.api_key,
    base_url=self.base_url,
    max_retries=self.max_retries,
    retry_delay=self.retry_delay,
    reasoning_effort=self.calibrate_reasoning_effort,
    task_type="calibrate_chunk",
    response_schema=CALIBRATION_RESULT_SCHEMA,  # 新增（config 已通过 set_default_config 设置）
)

if not result.success:
    raise Exception(f"校对结果解析失败: {result.error}")

# 直接使用，无需手动解析
calibrated_dialogs = result.data["calibrated_dialogs"]
```

### 7.4 说话人映射（llm_enhanced.py）

**改造后：**
```python
from .schemas.speaker_mapping import SPEAKER_MAPPING_SCHEMA

result = call_llm_api(
    model=self.summary_model,
    prompt=speaker_inference_prompt,
    api_key=self.api_key,
    base_url=self.base_url,
    max_retries=self.max_retries,
    retry_delay=self.retry_delay,
    reasoning_effort=self.summary_reasoning_effort,
    task_type="speaker_inference",
    response_schema=SPEAKER_MAPPING_SCHEMA,
)

if not result.success:
    # 降级：使用原始标识，并记录错误原因
    logger.warning(f"说话人推断失败: {result.error}")
    speaker_mapping = {speaker: speaker for speaker in original_speakers}
else:
    speaker_mapping = result.data["speaker_mapping"]
```

---

## 8. 文件变更清单

### 8.1 新增文件

| 文件路径 | 说明 |
|----------|------|
| `src/video_transcript_api/utils/llm/schemas/__init__.py` | Schema 定义包 |
| `src/video_transcript_api/utils/llm/schemas/calibration.py` | 校对结果 Schema |
| `src/video_transcript_api/utils/llm/schemas/validation.py` | 验证结果 Schema |
| `src/video_transcript_api/utils/llm/schemas/speaker_mapping.py` | 说话人映射 Schema |

### 8.2 修改文件

| 文件路径 | 修改内容 |
|----------|----------|
| `config/config.example.jsonc` | 新增 `llm.json_output` 配置节 |
| `src/video_transcript_api/utils/llm/llm.py` | 新增 `set_default_config()`、`StructuredResult`，扩展 `call_llm_api()` |
| `src/video_transcript_api/utils/llm/structured_calibrator.py` | 使用新的结构化输出参数，处理 `StructuredResult` |
| `src/video_transcript_api/utils/llm/llm_enhanced.py` | 使用新的结构化输出参数，处理 `StructuredResult` |
| `src/video_transcript_api/utils/llm/__init__.py` | 导出 `set_default_config`、`StructuredResult` 和 Schema |
| `src/video_transcript_api/api/app.py` | 启动时调用 `set_default_config()` |

### 8.3 可删除代码

| 位置 | 可删除内容 |
|------|-----------|
| `structured_calibrator.py` | `_parse_calibration_response()` 方法 |
| `structured_calibrator.py` | `_parse_validation_response()` 方法 |
| `llm_enhanced.py` | `_parse_speaker_mapping_result()` 方法 |

---

## 9. 测试计划

### 9.1 单元测试

```python
# tests/unit/test_llm_json_output.py

def test_get_json_mode_for_model():
    """测试模型匹配逻辑"""
    config = {
        "llm": {
            "json_output": {
                "mode_by_model": {
                    "deepseek*": "json_object",
                    "*": "json_schema"
                },
                "enable_fallback": True
            }
        }
    }

    assert _get_json_mode_for_model("deepseek-chat", config) == "json_object"
    assert _get_json_mode_for_model("gpt-4o", config) == "json_schema"
    assert _get_json_mode_for_model("DEEPSEEK-CODER", config) == "json_object"


def test_validate_required_fields():
    """测试字段验证"""
    schema = {"required": ["name", "age"]}

    valid, _ = _validate_required_fields({"name": "test", "age": 18}, schema)
    assert valid

    valid, error = _validate_required_fields({"name": "test"}, schema)
    assert not valid
    assert "age" in error


def test_call_llm_api_text_output():
    """测试纯文本输出（向后兼容）"""
    # Mock API 调用
    pass


def test_call_llm_api_structured_output():
    """测试结构化输出"""
    # Mock API 调用
    pass
```

### 9.2 集成测试

```python
# tests/integration/test_structured_calibration.py

def test_calibration_with_json_schema():
    """测试使用 json_schema 模式的校对"""
    pass


def test_calibration_with_json_object():
    """测试使用 json_object 模式的校对（如 DeepSeek）"""
    pass


def test_self_correction():
    """测试 Self-Correction 重试机制"""
    pass
```

---

## 10. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 部分模型 json_schema 支持不稳定 | 调用失败 | 配置为 json_object 模式 |
| json_object 模式字段验证不完整 | 运行时错误 | 业务代码增加 null 检查 |
| 现有调用签名变化 | 类型检查警告 | 使用 `@overload` 保证类型安全 |
| Schema 设计不合理 | LLM 输出不符合预期 | 充分测试，迭代优化 |

---

## 11. 实施步骤

### Phase 1: 基础设施
1. 修改 `llm.py`，新增 `set_default_config()`、`StructuredResult` 和结构化输出支持
2. 新增 `llm.json_output` 配置项
3. 编写单元测试

### Phase 2: Schema 定义
1. 创建 `schemas/` 目录
2. 定义三个核心 Schema
3. 验证 Schema 与现有 Prompt 的兼容性

### Phase 3: 业务迁移
1. 在 `app.py` 启动时调用 `set_default_config()`
2. 迁移 `structured_calibrator.py`，使用 `StructuredResult`
3. 迁移 `llm_enhanced.py` 说话人映射
4. 删除冗余的解析函数
5. 集成测试

### Phase 4: 监控与优化
1. 添加统计日志输出（程序退出时调用 `log_llm_stats()`）
2. 根据实际运行数据调整配置
3. 优化 Schema 设计

---

## 12. 参考资料

- [OpenAI Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs)
- [JSON Schema Specification](https://json-schema.org/specification)
- 项目内参考：`docs/development/json_output_mode_guide.md`
