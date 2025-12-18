# AI 模型 JSON 结构化输出方案设计文档

## 1. 背景与问题

### 1.1 问题描述

不同的 LLM 模型对 JSON 结构化输出的支持程度不同：

| 模型 | json_schema 支持 | json_object 支持 |
|------|-----------------|-----------------|
| GPT-4o / GPT-4-turbo | 完整支持 | 支持 |
| Claude 3.5 | 完整支持 | 支持 |
| Gemini 2.0 | 完整支持 | 支持 |
| DeepSeek | 不支持 | 支持 |
| Qwen (通义千问) | 部分支持 | 支持 |

**两种模式的区别：**

- **json_schema**: OpenAI 的结构化输出功能，100% 保证输出符合 Schema，模型在解码时受约束
- **json_object**: JSON Mode，仅保证输出是合法 JSON，但字段和类型不受约束，需要客户端校验

当项目需要支持多种模型时，硬编码使用 `json_schema` 会导致部分模型调用失败。

### 1.2 设计目标

1. **兼容性**: 支持所有主流 LLM 模型，无论其是否支持 `json_schema`
2. **透明性**: 对业务代码透明，调用方无需关心底层使用哪种模式
3. **可配置**: 通过配置文件灵活指定不同模型的输出模式
4. **可靠性**: 对 `json_object` 模式提供重试和自我纠正机制
5. **可观测**: 提供统计信息，便于监控和调试

---

## 2. 解决方案架构

```
┌────────────────────────────────────────────────────────────────────┐
│                         业务代码调用                                │
│   response_format={"type": "json_schema", "json_schema": {...}}   │
└───────────────────────────────┬────────────────────────────────────┘
                                │
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│                      AIClient.complete()                           │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  1. 检测 response_format 是否为 json_schema                   │  │
│  │  2. 根据 JSON_MODE_BY_MODEL 配置匹配当前模型                   │  │
│  │  3. 决定使用 json_schema 或 json_object 模式                  │  │
│  └──────────────────────────────────────────────────────────────┘  │
└───────────────────────────────┬────────────────────────────────────┘
                                │
              ┌─────────────────┴─────────────────┐
              │                                   │
              ▼                                   ▼
┌─────────────────────────┐         ┌─────────────────────────────────┐
│    json_schema 模式     │         │       json_object 模式           │
│  (直接调用 OpenAI API)  │         │  (_complete_with_json_object)   │
│                         │         ├─────────────────────────────────┤
│  - 100% 格式保证        │         │  1. Schema 转 Prompt 注入        │
│  - 无需额外处理         │         │  2. 调用 API                     │
│                         │         │  3. 解析 JSON                    │
│                         │         │  4. 验证必填字段                 │
│                         │         │  5. 失败则 Self-Correction 重试  │
└─────────────────────────┘         └─────────────────────────────────┘
```

---

## 3. 配置项说明

在 `.env` 文件中添加以下配置：

```bash
# ========== JSON 输出模式配置 ==========

# JSON 输出模式自动匹配规则（JSON格式）
# 按模型名前缀匹配，支持通配符 *
# 匹配顺序：从前到后，首次匹配生效
# 可选模式：
#   - json_schema: 使用 OpenAI 结构化输出（严格模式，100% 符合 Schema）
#   - json_object: 使用 JSON Mode + Prompt 注入（兼容模式，需客户端校验）
#
# 示例配置：
#   {"deepseek*": "json_object", "qwen*": "json_object", "*": "json_schema"}
#   - deepseek-chat -> json_object
#   - qwen-turbo -> json_object
#   - gpt-4o -> json_schema
#   - claude-3-5-sonnet -> json_schema
JSON_MODE_BY_MODEL={"deepseek*":"json_object","*":"json_schema"}

# json_object 模式下解析失败时的最大重试次数
# 重试时会使用 Self-Correction 提示，将错误信息反馈给模型
JSON_OBJECT_MAX_RETRIES=1

# 降级功能总开关
# 设为 false 时强制所有模型使用 json_schema（用于调试）
ENABLE_JSON_MODE_FALLBACK=true
```

### 3.1 配置项详解

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `JSON_MODE_BY_MODEL` | JSON字符串 | `{"*": "json_schema"}` | 模型名到输出模式的映射，支持通配符匹配 |
| `JSON_OBJECT_MAX_RETRIES` | 整数 | 1 | json_object 模式下 Self-Correction 最大重试次数 |
| `ENABLE_JSON_MODE_FALLBACK` | 布尔 | true | 总开关，false 时强制使用 json_schema |

### 3.2 匹配规则示例

```json
{
  "deepseek*": "json_object",    // deepseek-chat, deepseek-coder 等
  "qwen*": "json_object",        // qwen-turbo, qwen-plus 等
  "glm*": "json_object",         // glm-4, glm-3-turbo 等
  "*": "json_schema"             // 其他所有模型使用 json_schema
}
```

**匹配优先级**: 按配置顺序从前到后匹配，首次匹配生效。

---

## 4. 核心实现代码

### 4.1 配置解析 (settings.py)

```python
import json
import fnmatch
from pydantic_settings import BaseSettings
from pydantic import Field, AliasChoices

class Settings(BaseSettings):
    """应用配置类"""

    # JSON 输出模式配置
    json_mode_by_model: str = Field(
        '{"*": "json_schema"}',
        validation_alias=AliasChoices('JSON_MODE_BY_MODEL', 'json_mode_by_model'),
        description="按模型名匹配 JSON 输出模式，支持通配符"
    )
    json_object_max_retries: int = Field(
        1,
        validation_alias=AliasChoices('JSON_OBJECT_MAX_RETRIES', 'json_object_max_retries'),
        description="json_object 模式下解析失败时的最大重试次数"
    )
    enable_json_mode_fallback: bool = Field(
        True,
        validation_alias=AliasChoices('ENABLE_JSON_MODE_FALLBACK', 'enable_json_mode_fallback'),
        description="是否启用 JSON 模式降级功能"
    )

    def get_json_mode_for_model(self, model_name: str) -> str:
        """
        根据模型名称获取对应的 JSON 输出模式

        通过 JSON_MODE_BY_MODEL 配置按顺序匹配模型名，支持通配符。
        匹配规则：按配置顺序从前到后匹配，首次匹配生效。

        Args:
            model_name: 模型名称（如 "deepseek-chat", "gpt-4o"）

        Returns:
            str: JSON 输出模式，"json_schema" 或 "json_object"

        Example:
            配置: {"deepseek*": "json_object", "*": "json_schema"}
            - "deepseek-chat" -> "json_object"
            - "gpt-4o" -> "json_schema"
        """
        # 如果禁用了降级功能，始终返回 json_schema
        if not self.enable_json_mode_fallback:
            return "json_schema"

        try:
            mode_mapping = json.loads(self.json_mode_by_model)
        except json.JSONDecodeError:
            # 配置解析失败，使用默认值
            return "json_schema"

        model_name_lower = model_name.lower()

        # 按顺序匹配（Python 3.7+ dict 保持插入顺序）
        for pattern, mode in mode_mapping.items():
            if fnmatch.fnmatch(model_name_lower, pattern.lower()):
                return mode

        # 未匹配到任何规则，默认使用 json_schema
        return "json_schema"
```

### 4.2 AI 客户端核心逻辑 (ai_client.py)

#### 4.2.1 统计计数器

```python
class AIClient:
    """AI服务客户端"""

    # 类级别 JSON 模式统计（所有实例共享）
    _json_mode_stats: Dict[str, int] = {
        "json_schema_calls": 0,           # json_schema 调用次数
        "json_object_calls": 0,           # json_object 调用次数
        "json_object_parse_failures": 0,  # json_object 解析失败次数
        "json_object_retry_success": 0,   # Self-Correction 重试成功次数
        "json_object_fallback_to_none": 0 # 最终失败返回 None 次数
    }
```

#### 4.2.2 模式选择与降级逻辑

```python
async def complete(
    self,
    prompt: str,
    system_prompt: Optional[str] = None,
    temperature: float = 0.7,
    response_format: Optional[Dict[str, Any]] = None,
    max_retries: int = 3,
    reasoning_effort: Optional[str] = None,
    task_name: str = "未知任务",
    content_identifier: str = ""
) -> Optional[str]:
    """调用AI完成任务"""

    # ========== JSON 模式降级逻辑 ==========
    # 检查是否需要使用 json_object 模式降级
    if response_format and response_format.get("type") == "json_schema":
        json_mode = self.settings.get_json_mode_for_model(self.model)

        if json_mode == "json_object":
            # 提取 Schema 定义
            json_schema_config = response_format.get("json_schema", {})
            schema = json_schema_config.get("schema", {})

            # 使用 json_object 模式
            return await self._complete_with_json_object_mode(
                prompt=prompt,
                schema=schema,
                system_prompt=system_prompt,
                temperature=temperature,
                max_retries=max_retries,
                reasoning_effort=reasoning_effort,
                task_name=task_name,
                content_identifier=content_identifier
            )
        else:
            # 使用 json_schema 模式，记录统计
            AIClient._json_mode_stats["json_schema_calls"] += 1

    # ... 正常的 API 调用逻辑 ...
```

#### 4.2.3 Schema 转 Prompt 注入

```python
def _schema_to_prompt_instruction(self, schema: Dict[str, Any]) -> str:
    """
    将 JSON Schema 直接嵌入 Prompt 中作为格式说明

    用于 json_object 模式下，在 Prompt 中注入期望的输出格式。
    直接使用 JSON Schema 比转换成示例更准确，现代 LLM 对 Schema 理解良好。

    Args:
        schema: JSON Schema 定义

    Returns:
        str: 格式化的 Prompt 说明文本
    """
    if not schema:
        return ""

    schema_json = json.dumps(schema, indent=2, ensure_ascii=False)

    return f"""请严格按照以下 JSON Schema 返回结果，确保输出是合法的 JSON 对象：

```json
{schema_json}
```

重要提示：
- 所有 required 字段必须存在
- enum 字段只能使用指定的值
- 数组字段即使为空也要返回 []
- 不要添加 Schema 中未定义的字段"""
```

#### 4.2.4 字段验证

```python
def _validate_json_response_fields(
    self,
    parsed_json: Any,
    schema: Dict[str, Any]
) -> tuple[bool, str]:
    """
    验证 JSON 响应是否包含 Schema 中定义的必填字段

    Args:
        parsed_json: 已解析的 JSON 响应
        schema: 期望的 JSON Schema

    Returns:
        tuple[bool, str]: (是否有效, 错误信息)
    """
    # 首先验证是否为字典类型
    if not isinstance(parsed_json, dict):
        return False, f"响应不是字典类型 (type={type(parsed_json).__name__})"

    required_fields = schema.get("required", [])
    missing_fields = []

    for field in required_fields:
        if field not in parsed_json:
            missing_fields.append(field)

    if missing_fields:
        return False, f"缺少必填字段: {', '.join(missing_fields)}"

    return True, ""
```

#### 4.2.5 json_object 模式完整实现（含 Self-Correction）

```python
async def _complete_with_json_object_mode(
    self,
    prompt: str,
    schema: Dict[str, Any],
    system_prompt: Optional[str] = None,
    temperature: float = 0.7,
    max_retries: int = 3,
    reasoning_effort: Optional[str] = None,
    task_name: str = "未知任务",
    content_identifier: str = ""
) -> Optional[str]:
    """
    使用 json_object 模式调用 AI（带 Self-Correction 重试）

    当模型不支持 json_schema 时，降级使用此方法。

    Args:
        prompt: 原始 Prompt
        schema: 期望的 JSON Schema（用于生成格式说明和验证）
        system_prompt: 系统提示词
        temperature: 温度参数
        max_retries: 最大重试次数
        reasoning_effort: reasoning_effort 参数
        task_name: 任务名称（用于日志）
        content_identifier: 内容标识符（用于日志）

    Returns:
        Optional[str]: 符合格式的 JSON 字符串，或 None
    """
    log_context = f"[{task_name}]"
    if content_identifier:
        log_context += f" [{content_identifier}]"

    # 在 Prompt 中注入格式说明
    schema_instruction = self._schema_to_prompt_instruction(schema)
    enhanced_prompt = f"{prompt}\n\n{schema_instruction}"

    # 记录统计
    AIClient._json_mode_stats["json_object_calls"] += 1
    logger.info(f"{log_context} 使用 json_object 模式（模型不支持 json_schema）")

    # 获取配置的重试次数
    json_object_retries = self.settings.json_object_max_retries

    last_response = None
    last_error = None

    for attempt in range(json_object_retries + 1):  # +1 是因为第一次不算重试
        try:
            # 构建请求参数
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})

            # 如果是重试，添加 Self-Correction 提示
            if attempt > 0 and last_response and last_error:
                correction_prompt = (
                    f"你之前的响应无法被正确解析。\n"
                    f"错误信息：{last_error}\n\n"
                    f"你的响应（前500字符）：\n{last_response[:500]}\n\n"
                    f"请严格按照要求的 JSON 格式重新输出。\n\n"
                    f"原始任务：\n{enhanced_prompt}"
                )
                messages.append({"role": "user", "content": correction_prompt})
                logger.warning(f"{log_context} Self-Correction 重试 {attempt}/{json_object_retries}")
            else:
                messages.append({"role": "user", "content": enhanced_prompt})

            # 构建 API 调用参数
            kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "response_format": {"type": "json_object"}
            }

            # 添加 reasoning_effort 参数
            effective_reasoning_effort = reasoning_effort if reasoning_effort is not None else self.reasoning_effort
            if effective_reasoning_effort != "null":
                kwargs["reasoning_effort"] = effective_reasoning_effort

            # 调用 API
            response = await self.client.chat.completions.create(**kwargs)

            if not response.choices or not response.choices[0].message:
                last_error = "API 返回空响应"
                continue

            content = response.choices[0].message.content
            if not content or not content.strip():
                last_error = "API 返回空内容"
                continue

            last_response = content

            # 尝试解析 JSON
            try:
                parsed_json = json.loads(content)
            except json.JSONDecodeError as e:
                last_error = f"JSON 解析失败: {str(e)}"
                AIClient._json_mode_stats["json_object_parse_failures"] += 1
                continue

            # 验证必填字段
            is_valid, validation_error = self._validate_json_response_fields(parsed_json, schema)
            if not is_valid:
                last_error = validation_error
                AIClient._json_mode_stats["json_object_parse_failures"] += 1
                continue

            # 成功
            if attempt > 0:
                AIClient._json_mode_stats["json_object_retry_success"] += 1
                logger.info(f"{log_context} Self-Correction 重试成功")

            self.circuit_breaker.record_success()
            return content

        except Exception as e:
            last_error = f"API 调用异常: {str(e)}"
            self.circuit_breaker.record_failure()
            logger.warning(f"{log_context} json_object 模式调用失败: {e}")

    # 所有重试都失败
    AIClient._json_mode_stats["json_object_fallback_to_none"] += 1
    logger.warning(f"{log_context} json_object 模式最终失败，返回 None（调用方将回退原文）")
    return None
```

#### 4.2.6 统计输出

```python
@classmethod
def log_json_mode_stats(cls) -> None:
    """输出 JSON 模式统计信息"""
    stats = cls._json_mode_stats
    total_json_calls = stats["json_schema_calls"] + stats["json_object_calls"]

    if total_json_calls == 0:
        return

    logger.info("=" * 60)
    logger.info("JSON 模式统计")
    logger.info("-" * 60)
    logger.info(f"  json_schema 调用次数: {stats['json_schema_calls']}")
    logger.info(f"  json_object 调用次数: {stats['json_object_calls']}")

    if stats["json_object_calls"] > 0:
        success_count = stats["json_object_calls"] - stats["json_object_fallback_to_none"]
        success_rate = success_count / stats["json_object_calls"] * 100
        logger.info(f"  json_object 解析失败次数: {stats['json_object_parse_failures']}")
        logger.info(f"  json_object 重试成功次数: {stats['json_object_retry_success']}")
        logger.info(f"  json_object 最终失败次数: {stats['json_object_fallback_to_none']}")
        logger.info(f"  json_object 成功率: {success_rate:.1f}%")

    logger.info("=" * 60)

@classmethod
def reset_json_mode_stats(cls) -> None:
    """重置 JSON 模式统计（用于测试）"""
    cls._json_mode_stats = {
        "json_schema_calls": 0,
        "json_object_calls": 0,
        "json_object_parse_failures": 0,
        "json_object_retry_success": 0,
        "json_object_fallback_to_none": 0
    }
```

---

## 5. 使用示例

### 5.1 业务代码调用（无需关心底层模式）

```python
# 业务代码始终使用 json_schema 格式，底层自动处理模式选择
result = await ai_client.complete(
    prompt="分析以下内容...",
    temperature=0.3,
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "content_analysis_response",
            "schema": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["科技", "财经", "娱乐", "其他"]
                    },
                    "score": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 10
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"}
                    }
                },
                "required": ["category", "score", "tags"],
                "additionalProperties": False
            },
            "strict": True
        }
    },
    task_name="内容分析",
    content_identifier="article_123"
)
```

### 5.2 不同模型的自动行为

当配置为 `JSON_MODE_BY_MODEL={"deepseek*":"json_object","*":"json_schema"}` 时：

```python
# 使用 GPT-4o (json_schema 模式)
# -> 直接传递 response_format={"type": "json_schema", ...}
# -> API 保证 100% 符合 Schema

# 使用 DeepSeek (json_object 模式)
# -> 自动转换为 response_format={"type": "json_object"}
# -> Prompt 中注入 Schema 说明
# -> 客户端验证必填字段
# -> 失败时 Self-Correction 重试
```

### 5.3 在程序结束时输出统计

```python
# main.py
async def main():
    try:
        # ... 业务逻辑 ...
        pass
    finally:
        # 输出 JSON 模式统计
        AIClient.log_json_mode_stats()
```

输出示例：
```
============================================================
JSON 模式统计
------------------------------------------------------------
  json_schema 调用次数: 150
  json_object 调用次数: 50
  json_object 解析失败次数: 3
  json_object 重试成功次数: 2
  json_object 最终失败次数: 1
  json_object 成功率: 98.0%
============================================================
```

---

## 6. 迁移指南

### 6.1 在其他项目中使用此方案

1. **复制配置项定义** 到 `settings.py`
2. **复制 `get_json_mode_for_model` 方法** 到配置类
3. **复制 AI 客户端的相关方法**:
   - `_json_mode_stats` 统计字典
   - `_schema_to_prompt_instruction` 方法
   - `_validate_json_response_fields` 方法
   - `_complete_with_json_object_mode` 方法
   - `complete` 方法中的降级逻辑
   - `log_json_mode_stats` 和 `reset_json_mode_stats` 方法
4. **添加环境变量配置**

### 6.2 依赖说明

```txt
# requirements.txt
pydantic>=2.0
pydantic-settings>=2.0
openai>=1.0
loguru>=0.7
```

---

## 7. 最佳实践

### 7.1 Schema 设计建议

```python
# 好的 Schema 设计
schema = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": ["A", "B", "C"],  # 使用 enum 限制取值
            "description": "内容分类"  # 添加描述帮助模型理解
        },
        "items": {
            "type": "array",
            "items": {"type": "string"},
            "description": "提取的项目列表"
        }
    },
    "required": ["category", "items"],  # 明确必填字段
    "additionalProperties": False  # 禁止额外字段
}
```

### 7.2 错误处理

```python
result = await ai_client.complete(...)
if result is None:
    # json_object 模式最终失败，使用默认值或原文
    return default_value

parsed = json.loads(result)
# json_schema 模式保证 100% 符合 Schema
# json_object 模式已经过字段验证
```

### 7.3 调试技巧

```bash
# 临时禁用降级功能，强制使用 json_schema 调试
ENABLE_JSON_MODE_FALLBACK=false

# 增加重试次数
JSON_OBJECT_MAX_RETRIES=3
```

---

## 8. 已知限制

1. **json_object 模式的字段验证是浅层的**: 只检查顶层必填字段是否存在，不递归检查嵌套结构
2. **enum 约束不保证**: json_object 模式下，enum 字段可能返回非预期值，需要业务代码处理
3. **复杂 Schema 支持有限**: 对于包含 `anyOf`、`oneOf` 等高级 JSON Schema 特性，建议使用支持 json_schema 的模型

---

## 9. 版本历史

| 版本 | 日期 | 说明 |
|------|------|------|
| v1.0 | 2024-12 | 初始版本，支持 json_schema 和 json_object 两种模式 |
| v2.0 | 2025-01 | 添加 Self-Correction 重试机制，增加统计功能 |
| v2.1 | 2025-06 | 添加字段类型验证，修复 json_object 解析非字典类型的问题 |
