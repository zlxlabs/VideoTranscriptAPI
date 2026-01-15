# LLM API 工程实践指南

本文档旨在提供 LLM API 调用的最佳实践，便于在多个项目中复用。

---

## 目录

1. [基础架构](#1-基础架构)
2. [Prompt 工程与 Prefix Cache 优化](#2-prompt-工程与-prefix-cache-优化)
3. [结构化输出（JSON）](#3-结构化输出json)
4. [Reasoning Effort 配置](#4-reasoning-effort-配置)
5. [错误处理](#5-错误处理)
6. [可观测性](#6-可观测性)
7. [完整使用示例](#附录完整使用示例)

---

## 1. 基础架构

### 1.1 设计目标

- 统一封装 OpenAI 兼容 API，切换模型只需改配置
- 配置与代码分离，支持多环境

### 1.2 配置文件设计

```yaml
# config/llm.yaml
default_provider: "deepseek"

providers:
  openai:
    base_url: "https://api.openai.com/v1"
    api_key: "${OPENAI_API_KEY}"  # 环境变量注入
    default_model: "gpt-4o"

  deepseek:
    base_url: "https://api.deepseek.com/v1"
    api_key: "${DEEPSEEK_API_KEY}"
    default_model: "deepseek-chat"

  local:
    base_url: "http://localhost:8080/v1"
    api_key: "not-needed"
    default_model: "qwen2.5-7b"

defaults:
  temperature: 0
  max_tokens: 4096
  timeout: 60
```

### 1.3 客户端实现

```python
# llm/client.py
import os
from pathlib import Path
from dataclasses import dataclass
import yaml
import httpx

@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0
    max_tokens: int = 4096
    timeout: int = 60

def load_config(provider: str | None = None) -> LLMConfig:
    """加载 LLM 配置"""
    config_path = Path(__file__).parent.parent / "config" / "llm.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    provider = provider or config["default_provider"]
    provider_config = config["providers"][provider]
    defaults = config.get("defaults", {})

    # 环境变量注入
    api_key = provider_config["api_key"]
    if api_key.startswith("${") and api_key.endswith("}"):
        env_var = api_key[2:-1]
        api_key = os.environ.get(env_var, "")

    return LLMConfig(
        base_url=provider_config["base_url"],
        api_key=api_key,
        model=provider_config.get("default_model", defaults.get("model")),
        temperature=defaults.get("temperature", 0),
        max_tokens=defaults.get("max_tokens", 4096),
        timeout=defaults.get("timeout", 60),
    )

class LLMClient:
    """统一的 LLM 客户端"""

    def __init__(self, provider: str | None = None):
        self.config = load_config(provider)
        self.client = httpx.Client(
            base_url=self.config.base_url,
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            timeout=self.config.timeout,
        )

    def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        **kwargs
    ) -> str:
        """发送聊天请求，返回文本内容"""
        payload = {
            "model": model or self.config.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.config.temperature),
            "max_tokens": kwargs.get("max_tokens", self.config.max_tokens),
        }

        response = self.client.post("/chat/completions", json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
```

---

## 2. Prompt 工程与 Prefix Cache 优化

### 2.1 Prefix Cache 原理

OpenAI 兼容 API（如 DeepSeek、OpenAI）支持 **Prefix Cache**：当多个请求共享相同的 prompt 前缀时，服务端会缓存 KV 计算结果，后续请求可复用，从而：

- 降低首 token 延迟（TTFT）
- 减少计算成本（部分厂商对缓存命中有折扣，如 Claude 缓存命中 0.30$/M vs 未命中 3$/M，相差 10 倍）

**命中条件**：

- 前缀必须 **完全一致**（逐字符匹配）
- 通常要求前缀长度 ≥ 1024 tokens

### 2.2 Prompt 结构设计原则

```
┌─────────────────────────────────────┐
│  System Prompt (静态，长且稳定)       │  ← 缓存命中区
│  - 角色定义                          │
│  - 输出格式要求                       │
│  - Few-shot 示例                     │
├─────────────────────────────────────┤
│  User Prompt (动态，每次变化)         │  ← 不缓存
│  - 具体任务输入                       │
└─────────────────────────────────────┘
```

**最佳实践**：

| 规则 | 说明 |
|------|------|
| 静态内容前置 | 把不变的指令、格式要求、示例放在 system prompt |
| 动态内容后置 | 用户输入、变量放在 user prompt 末尾 |
| 避免动态时间戳 | `当前时间：2024-01-01` 会破坏缓存 |
| Few-shot 示例固定顺序 | 示例顺序变化会导致缓存失效 |
| 上下文只追加不修改 | 避免修改之前的 action/observation |
| JSON 序列化保持确定性 | 确保 key 顺序稳定 |

### 2.3 Prompt 模板管理（YAML）

```yaml
# config/prompts.yaml
extract_info:
  name: "信息提取"
  system: |
    你是一个信息提取助手。请从用户提供的文本中提取结构化信息。

    ## 输出格式
    请以 JSON 格式输出，包含以下字段：
    - title: 标题
    - date: 日期 (YYYY-MM-DD)
    - summary: 摘要 (不超过100字)

    ## 示例
    输入：2024年1月15日，公司发布了新产品X，这是一款革命性的...
    输出：{"title": "新产品X发布", "date": "2024-01-15", "summary": "公司发布革命性新产品X"}

  user: |
    请从以下文本中提取信息：

    {text}

translate:
  name: "翻译"
  system: |
    你是一个专业翻译。请将用户提供的文本翻译成{target_lang}。

    要求：
    - 保持原文风格
    - 专业术语翻译准确
    - 只输出翻译结果，不要解释

  user: "{text}"
```

### 2.4 模板加载与渲染

```python
# llm/prompt.py
from pathlib import Path
from dataclasses import dataclass
import yaml

@dataclass
class PromptTemplate:
    name: str
    system: str
    user: str

class PromptManager:
    """Prompt 模板管理器"""

    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path or Path(__file__).parent.parent / "config" / "prompts.yaml")
        self._templates: dict[str, PromptTemplate] = {}
        self._load_templates()

    def _load_templates(self) -> None:
        """加载所有模板"""
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        for key, value in data.items():
            self._templates[key] = PromptTemplate(
                name=value.get("name", key),
                system=value.get("system", ""),
                user=value.get("user", ""),
            )

    def render(self, template_name: str, **variables) -> list[dict]:
        """
        渲染模板为 messages 格式

        Args:
            template_name: 模板名称
            **variables: 模板变量

        Returns:
            OpenAI messages 格式的列表
        """
        template = self._templates.get(template_name)
        if not template:
            raise ValueError(f"Template not found: {template_name}")

        messages = []

        if template.system:
            system_content = template.system.format(**variables)
            messages.append({"role": "system", "content": system_content})

        if template.user:
            user_content = template.user.format(**variables)
            messages.append({"role": "user", "content": user_content})

        return messages

    def reload(self) -> None:
        """热重载模板（开发环境使用）"""
        self._templates.clear()
        self._load_templates()


# 使用示例
prompt_manager = PromptManager()
messages = prompt_manager.render(
    "extract_info",
    text="2024年3月，苹果公司发布了M3芯片..."
)
```

### 2.5 Prefix Cache 友好的设计检查清单

| 检查项 | 说明 |
|--------|------|
| ✅ System prompt 足够长 | 建议 ≥ 500 字符，增加缓存价值 |
| ✅ 静态内容在前 | Few-shot、格式说明放 system |
| ✅ 无动态时间戳 | 避免 `当前时间: xxx` 在前缀中 |
| ✅ 示例顺序固定 | Few-shot 示例不要随机打乱 |
| ✅ 变量只在末尾 | `{text}` 等变量放 user prompt |

---

## 3. 结构化输出（JSON）

### 3.1 JSON Mode 使用

OpenAI 兼容 API 支持 `response_format` 参数强制 JSON 输出：

```python
# 方式1: 简单 JSON Mode
response = client.post("/chat/completions", json={
    "model": "gpt-4o",
    "messages": messages,
    "response_format": {"type": "json_object"}
})

# 方式2: JSON Schema 约束（部分模型支持）
response = client.post("/chat/completions", json={
    "model": "gpt-4o",
    "messages": messages,
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "extraction_result",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "date": {"type": "string"},
                    "summary": {"type": "string"}
                },
                "required": ["title", "date", "summary"]
            }
        }
    }
})
```

### 3.2 Pydantic 模型校验

```python
# llm/schemas.py
from pydantic import BaseModel, Field, ValidationError
from typing import TypeVar, Type
import json
import logging

logger = logging.getLogger(__name__)

T = TypeVar('T', bound=BaseModel)

class ExtractionResult(BaseModel):
    """信息提取结果"""
    title: str = Field(description="标题")
    date: str = Field(description="日期，格式 YYYY-MM-DD")
    summary: str = Field(description="摘要", max_length=100)

class TranslationResult(BaseModel):
    """翻译结果"""
    text: str = Field(description="翻译后的文本")
    source_lang: str = Field(description="源语言")
    target_lang: str = Field(description="目标语言")


def parse_json_response(
    content: str,
    schema: Type[T],
    strict: bool = True
) -> T | None:
    """
    解析 LLM 返回的 JSON 并校验

    Args:
        content: LLM 返回的原始字符串
        schema: Pydantic 模型类
        strict: 严格模式，校验失败时抛异常

    Returns:
        解析后的模型实例，或 None（非严格模式下解析失败）
    """
    try:
        # 尝试提取 JSON（处理 markdown code block）
        text = content.strip()
        if text.startswith("```"):
            # 移除 markdown 代码块
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])

        data = json.loads(text)
        return schema.model_validate(data)

    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {e}, content: {content[:200]}")
        if strict:
            raise
        return None

    except ValidationError as e:
        logger.error(f"Schema 校验失败: {e}")
        if strict:
            raise
        return None
```

### 3.3 集成到 LLMClient

```python
# llm/client.py（扩展）
from typing import Type, TypeVar
from pydantic import BaseModel
from llm.schemas import parse_json_response

T = TypeVar('T', bound=BaseModel)

class LLMClient:
    # ... 之前的代码 ...

    def chat_json(
        self,
        messages: list[dict],
        schema: Type[T],
        model: str | None = None,
        **kwargs
    ) -> T:
        """
        发送请求并解析为结构化对象

        Args:
            messages: 消息列表
            schema: Pydantic 模型类
            model: 模型名称
            **kwargs: 其他参数

        Returns:
            解析后的模型实例
        """
        payload = {
            "model": model or self.config.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.config.temperature),
            "max_tokens": kwargs.get("max_tokens", self.config.max_tokens),
            "response_format": {"type": "json_object"},
        }

        response = self.client.post("/chat/completions", json=payload)
        response.raise_for_status()

        content = response.json()["choices"][0]["message"]["content"]
        return parse_json_response(content, schema, strict=True)


# 使用示例
from llm.schemas import ExtractionResult

client = LLMClient()
prompt_manager = PromptManager()

messages = prompt_manager.render("extract_info", text="...")
result = client.chat_json(messages, ExtractionResult)
print(result.title, result.date)
```

---

## 4. Reasoning Effort 配置

### 4.1 功能原理

新一代推理模型（如 Gemini 2.5、OpenAI o1/o3 系列）支持 **Thinking（思考）** 功能，模型会在输出前进行深度推理，从而提升复杂任务的准确性。

OpenAI 兼容 API 提供 `reasoning_effort` 参数来控制思考强度：

| 级别 | Token 预算 | 适用场景 |
|------|-----------|----------|
| `"low"` | ~1,024 tokens | 简单任务、低延迟场景 |
| `"medium"` | ~8,192 tokens | 中等复杂度任务 |
| `"high"` | ~24,576 tokens | 复杂推理、数学证明、代码生成 |
| `"none"` | 0 | 禁用思考（部分模型不支持关闭）|

**注意**：
- 思考 tokens 会计入用量，增加成本
- Gemini 2.5 Pro 等模型不支持完全关闭思考
- 不同厂商的实现可能略有差异

### 4.2 三态配置设计

为兼容不同模型的能力差异，配置使用三态值：

| 配置值 | 含义 | API 请求行为 |
|--------|------|-------------|
| `null` | 模型不支持此参数 | 请求中 **不携带** `reasoning_effort` 字段 |
| `"none"` | 模型支持但关闭思考 | 请求中携带 `"reasoning_effort": "none"` |
| `"low"` / `"medium"` / `"high"` | 启用对应级别思考 | 请求中携带对应值 |

**为什么需要 `null`？**

某些模型（如 GPT-4o、DeepSeek）不支持 `reasoning_effort` 参数。如果强行传入，可能导致：
- API 返回错误
- 参数被忽略但产生警告
- 行为不可预期

因此，配置中的 `null` 表示"此模型不支持，跳过该参数"。

### 4.3 配置文件示例

```yaml
# config/llm.yaml
llm:
  # 校对任务配置
  calibrate_model: "gpt-4.1-mini"
  calibrate_reasoning_effort: null      # GPT-4.1 不支持 reasoning

  # 总结任务配置
  summary_model: "gemini-2.5-flash"
  summary_reasoning_effort: "high"      # 总结需要深度思考

  # 风险内容处理（自动切换模型）
  risk_calibrate_model: "gpt-4o-mini"
  risk_calibrate_reasoning_effort: null
  risk_summary_model: "gemini-2.5-pro"
  risk_summary_reasoning_effort: "medium"

  # 质量验证器
  validator_model: "deepseek-chat"
  validator_reasoning_effort: null      # DeepSeek 不支持
```

**JSON 格式**：

```jsonc
{
  "llm": {
    "calibrate_model": "gpt-4.1-mini",
    "calibrate_reasoning_effort": null,     // null = 不携带参数

    "summary_model": "gemini-2.5-flash",
    "summary_reasoning_effort": "high",     // 启用高强度思考

    "validator_model": "deepseek-chat",
    "validator_reasoning_effort": null      // DeepSeek 不支持
  }
}
```

### 4.4 代码实现

```python
# llm/client.py
from typing import Optional, Literal

ReasoningEffort = Literal["none", "low", "medium", "high"]

class LLMClient:
    def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        reasoning_effort: Optional[ReasoningEffort] = None,  # None 表示不支持
        **kwargs
    ) -> str:
        """
        发送聊天请求

        Args:
            messages: 消息列表
            model: 模型名称
            reasoning_effort: 推理强度
                - None: 模型不支持此参数，请求中不携带
                - "none": 模型支持但禁用思考
                - "low"/"medium"/"high": 启用对应级别思考
        """
        payload = {
            "model": model or self.config.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.config.temperature),
            "max_tokens": kwargs.get("max_tokens", self.config.max_tokens),
        }

        # 关键：只有当 reasoning_effort 不为 None 时才添加参数
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort

        response = self.client.post("/chat/completions", json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
```

### 4.5 配置加载与使用

```python
# llm/config.py
from dataclasses import dataclass
from typing import Optional, Literal

ReasoningEffort = Optional[Literal["none", "low", "medium", "high"]]

@dataclass
class TaskConfig:
    """单个任务的 LLM 配置"""
    model: str
    reasoning_effort: ReasoningEffort = None  # None = 不支持

@dataclass
class LLMConfig:
    """完整 LLM 配置"""
    calibrate: TaskConfig
    summary: TaskConfig
    risk_calibrate: TaskConfig
    risk_summary: TaskConfig
    validator: TaskConfig


def load_llm_config(config: dict) -> LLMConfig:
    """从配置字典加载 LLM 配置"""
    llm = config.get("llm", {})

    return LLMConfig(
        calibrate=TaskConfig(
            model=llm.get("calibrate_model", "gpt-4o"),
            reasoning_effort=llm.get("calibrate_reasoning_effort"),  # 保留 None
        ),
        summary=TaskConfig(
            model=llm.get("summary_model", "gpt-4o"),
            reasoning_effort=llm.get("summary_reasoning_effort"),
        ),
        risk_calibrate=TaskConfig(
            model=llm.get("risk_calibrate_model", ""),
            reasoning_effort=llm.get("risk_calibrate_reasoning_effort"),
        ),
        risk_summary=TaskConfig(
            model=llm.get("risk_summary_model", ""),
            reasoning_effort=llm.get("risk_summary_reasoning_effort"),
        ),
        validator=TaskConfig(
            model=llm.get("validator_model", ""),
            reasoning_effort=llm.get("validator_reasoning_effort"),
        ),
    )


# 使用示例
config = load_llm_config(app_config)
client = LLMClient()

# 校对任务（不支持 reasoning）
result = client.chat(
    messages,
    model=config.calibrate.model,
    reasoning_effort=config.calibrate.reasoning_effort,  # None
)

# 总结任务（启用高强度思考）
result = client.chat(
    messages,
    model=config.summary.model,
    reasoning_effort=config.summary.reasoning_effort,  # "high"
)
```

### 4.6 模型兼容性速查表

| 模型系列 | 支持 reasoning_effort | 备注 |
|----------|----------------------|------|
| Gemini 2.5 Flash | ✅ | 支持 `"none"` 关闭 |
| Gemini 2.5 Pro | ✅ | 不支持 `"none"`，思考无法完全关闭 |
| OpenAI o1 / o3 | ✅ | 原生支持 |
| GPT-4o / GPT-4.1 | ❌ | 使用 `null` 跳过参数 |
| DeepSeek Chat | ❌ | 使用 `null` 跳过参数 |
| Claude 3.5 | ❌ | 使用 `null` 跳过参数 |
| Qwen 系列 | ❌ | 使用 `null` 跳过参数 |

**日志示例**：

```
2024-03-15 14:23:01 | INFO | [CALIBRATE] Model: gpt-4.1-mini | Reasoning: disabled
2024-03-15 14:23:03 | INFO | [SUMMARY] Model: gemini-2.5-flash | Reasoning: high
2024-03-15 14:23:05 | INFO | [VALIDATE] Model: deepseek-chat | Reasoning: disabled
```

---

## 5. 错误处理

### 5.1 重试策略

使用 **指数退避 + 抖动** 避免雪崩：

```python
# llm/retry.py
import time
import random
import logging
from functools import wraps
from typing import Callable, TypeVar
import httpx

logger = logging.getLogger(__name__)

T = TypeVar('T')

# 可重试的异常类型
RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.HTTPStatusError,  # 429, 500, 502, 503, 504
)

# 可重试的 HTTP 状态码
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
):
    """
    重试装饰器

    Args:
        max_retries: 最大重试次数
        base_delay: 基础延迟（秒）
        max_delay: 最大延迟（秒）
        exponential_base: 指数基数
        jitter: 是否添加随机抖动
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)

                except RETRYABLE_EXCEPTIONS as e:
                    last_exception = e

                    # 检查是否可重试
                    if isinstance(e, httpx.HTTPStatusError):
                        if e.response.status_code not in RETRYABLE_STATUS_CODES:
                            raise

                        # 处理 429 Rate Limit
                        if e.response.status_code == 429:
                            retry_after = e.response.headers.get("Retry-After")
                            if retry_after:
                                delay = float(retry_after)
                                logger.warning(f"Rate limited, waiting {delay}s (from Retry-After)")
                                time.sleep(delay)
                                continue

                    if attempt == max_retries:
                        logger.error(f"Max retries ({max_retries}) exceeded")
                        raise

                    # 计算延迟
                    delay = min(base_delay * (exponential_base ** attempt), max_delay)
                    if jitter:
                        delay = delay * (0.5 + random.random())  # 50%-150% 抖动

                    logger.warning(
                        f"Attempt {attempt + 1} failed: {e}. "
                        f"Retrying in {delay:.2f}s..."
                    )
                    time.sleep(delay)

            raise last_exception

        return wrapper
    return decorator
```

### 5.2 应用重试到 Client

```python
# llm/client.py（扩展）
from llm.retry import with_retry

class LLMClient:
    # ...

    @with_retry(max_retries=3, base_delay=1.0)
    def chat(self, messages: list[dict], **kwargs) -> str:
        """带重试的聊天请求"""
        payload = {
            "model": kwargs.get("model", self.config.model),
            "messages": messages,
            "temperature": kwargs.get("temperature", self.config.temperature),
            "max_tokens": kwargs.get("max_tokens", self.config.max_tokens),
        }

        response = self.client.post("/chat/completions", json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
```

### 5.3 超时控制

```python
# 使用 httpx 的 timeout 配置
client = httpx.Client(
    timeout=httpx.Timeout(
        connect=5.0,      # 连接超时
        read=60.0,        # 读取超时（LLM 响应可能较慢）
        write=10.0,       # 写入超时
        pool=5.0,         # 连接池超时
    )
)
```

---

## 6. 可观测性

### 6.1 日志规范

```python
# llm/logging.py
import logging
import time
import uuid
from functools import wraps
from dataclasses import dataclass, asdict

logger = logging.getLogger("llm")

@dataclass
class LLMLogEntry:
    """LLM 请求日志结构"""
    request_id: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    latency_ms: float
    status: str  # "success" | "error"
    error_message: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def mask_sensitive(text: str, visible_chars: int = 4) -> str:
    """脱敏处理：保留前后几位字符"""
    if len(text) <= visible_chars * 2:
        return "*" * len(text)
    return text[:visible_chars] + "***" + text[-visible_chars:]


def log_llm_call(func):
    """LLM 调用日志装饰器"""
    @wraps(func)
    def wrapper(self, messages: list[dict], **kwargs):
        request_id = str(uuid.uuid4())[:8]
        start_time = time.time()

        # 请求日志（脱敏）
        logger.info(f"[{request_id}] LLM request started", extra={
            "request_id": request_id,
            "model": kwargs.get("model", self.config.model),
            "message_count": len(messages),
        })

        try:
            result = func(self, messages, **kwargs)
            latency_ms = (time.time() - start_time) * 1000

            # 成功日志
            logger.info(f"[{request_id}] LLM request completed", extra={
                "request_id": request_id,
                "latency_ms": round(latency_ms, 2),
                "status": "success",
            })

            return result

        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000

            # 错误日志
            logger.error(f"[{request_id}] LLM request failed: {e}", extra={
                "request_id": request_id,
                "latency_ms": round(latency_ms, 2),
                "status": "error",
                "error_type": type(e).__name__,
            })
            raise

    return wrapper
```

### 6.2 Token 统计

```python
# llm/client.py（扩展）
from dataclasses import dataclass

@dataclass
class LLMResponse:
    """LLM 响应包装"""
    content: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str

    @property
    def cost_estimate(self) -> float:
        """估算成本（以 GPT-4o 为例）"""
        # 价格：$5/1M input, $15/1M output
        input_cost = self.prompt_tokens * 5 / 1_000_000
        output_cost = self.completion_tokens * 15 / 1_000_000
        return input_cost + output_cost


class LLMClient:
    # ...

    def chat_with_usage(self, messages: list[dict], **kwargs) -> LLMResponse:
        """返回包含 token 统计的完整响应"""
        payload = {
            "model": kwargs.get("model", self.config.model),
            "messages": messages,
            "temperature": kwargs.get("temperature", self.config.temperature),
            "max_tokens": kwargs.get("max_tokens", self.config.max_tokens),
        }

        response = self.client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()

        usage = data.get("usage", {})
        return LLMResponse(
            content=data["choices"][0]["message"]["content"],
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            model=data.get("model", payload["model"]),
        )
```

### 6.3 日志配置示例

```python
# config/logging.py
import logging
import sys

def setup_logging(level: str = "INFO"):
    """配置日志格式"""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            # 可选：文件输出
            # logging.FileHandler("logs/llm.log"),
        ]
    )

    # 降低第三方库日志级别
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
```

### 6.4 日志输出示例

```
2024-03-15 14:23:01 | INFO     | llm | [a1b2c3d4] LLM request started
2024-03-15 14:23:03 | INFO     | llm | [a1b2c3d4] LLM request completed | latency_ms=1823.45 | tokens=1234
2024-03-15 14:23:05 | ERROR    | llm | [e5f6g7h8] LLM request failed: 429 Too Many Requests
```

---

## 附录：完整使用示例

```python
# main.py
from llm.client import LLMClient
from llm.prompt import PromptManager
from llm.schemas import ExtractionResult
from config.logging import setup_logging

# 初始化
setup_logging("INFO")
client = LLMClient(provider="deepseek")
prompts = PromptManager()

# 构造请求
messages = prompts.render(
    "extract_info",
    text="2024年3月15日，OpenAI发布了GPT-4.5模型，性能大幅提升..."
)

# 发送请求（带重试、日志、JSON校验）
result = client.chat_json(messages, ExtractionResult)

print(f"标题: {result.title}")
print(f"日期: {result.date}")
print(f"摘要: {result.summary}")
```

---

## 相关文档

- [JSON 结构化输出方案设计](./json_output_mode_guide.md) - 多模型 JSON 输出兼容方案
- [Gemini OpenAI 兼容性](./gemini_openai_api.md) - Gemini API 的 OpenAI 兼容模式与 Thinking 配置
- [LLM 最佳实践](./manus%20-LLM最佳实践.md) - Manus 团队的上下文工程经验
