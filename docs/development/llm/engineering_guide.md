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
7. [项目实践：说话人推断与诚实状态模型](#7-项目实践说话人推断与诚实状态模型)
8. [完整使用示例](#附录完整使用示例)

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
    default_model: "deepseek-v4-flash"

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

### 4.1 2026 API 现状

2026 年三家主流服务商的 thinking API **大幅收敛**：

| 服务商 | 合法 `reasoning_effort` | 关闭思考方式 | 默认 |
|--------|-------------------------|-------------|------|
| OpenAI GPT-5.x | `minimal`/`low`/`medium`/`high` | 只能 `minimal`（关不掉） | `medium` |
| OpenAI GPT-4.x | （不支持） | 不思考 | — |
| OpenAI o-series | `low`/`medium`/`high` | （关不掉） | enabled |
| Gemini 2.5 | `none`/`low`/`medium`/`high` | `reasoning_effort: "none"` | enabled |
| Gemini 3.x (OpenAI-compat) | `minimal`/`low`/`medium`/`high` | `minimal`（Pro 关不掉） | `high` |
| DeepSeek V4 | `low`/`medium`/`high`/`max`/`xhigh` | `extra_body.thinking.type=disabled` | `enabled@high` |

收敛点：三家在 2026 都支持 `minimal`（低延迟场景）。分化点：DeepSeek 有独家 `max/xhigh` + 独特的 thinking 开关字段。

### 4.2 多状态配置设计

项目用单字段 `reasoning_effort`，由 `llm-compat` 库按模型族自动翻译：

| 配置值 | 语义 | 请求行为 |
|--------|------|---------|
| `null` | 未设置，沿用 provider 默认 | payload 不加任何 thinking 字段 |
| `"disabled"` | **显式关闭思考** | DeepSeek → `extra_body.thinking.type="disabled"`；Gemini 2.5 → `reasoning_effort="none"`；GPT-5/Gemini-3 → 回退到 `minimal`；Gemini 3 Pro → warn 并丢弃 |
| `"minimal"` | GPT-5/Gemini-3 最低档 | 相应 provider 透传；DeepSeek 没这值，clamp 到 `low` |
| `"low"` / `"medium"` / `"high"` | 三家通用 | 原样透传（GPT-4.x 丢弃并 warn） |
| `"max"` / `"xhigh"` | DeepSeek 独有 | DeepSeek 透传；其他 clamp 到 `"high"` + warn |

**关键差异（与老架构相比）**：老单字段 `null`/`"none"`/强度的三态设计，把"默认"和"禁用"压成一个 `None` 状态。升级到 DeepSeek V4 会让 Gemini 2.5 旧用户的"关闭思考"**静默变为"默认开启"**。2026 起显式区分 `null`（默认）和 `"disabled"`（关闭），由 provider 翻译层消化差异。

**legacy 兼容**：
- `"none"` 自动归一到 `"disabled"`（保留用户意图），同时 emit deprecation warn
- `"null"` / `""` / 空白 → `None`（默认）
- 未知字符串 → `None` + warn

### 4.3 配置文件示例

```jsonc
{
  "llm": {
    "calibrate_model": "gpt-4.1-mini",
    "calibrate_reasoning_effort": null,       // GPT-4.x 不支持，任何值都会被 drop

    "summary_model": "deepseek-v4-flash",
    "summary_reasoning_effort": "high",       // v4-flash 真正触发思考

    "validator_model": "deepseek-v4-flash",
    "validator_reasoning_effort": "disabled", // 显式关闭思考，低成本验证

    // 可选：用自定义模型名覆盖默认识别
    "provider_patterns": {
      "dsproxy-*": "deepseek"
    }
  }
}
```

### 4.4 代码调用

**所有 LLM 调用通过 llm-compat SyncLLMClient**，provider 翻译在 llm-compat 内部自动完成：

```python
# llm/llm.py
from llm_compat import SyncLLMClient

# SyncLLMClient 在 set_default_config() 中初始化
client = get_sync_client()
result = client.chat(model, messages, reasoning_effort=reasoning_effort)
# llm-compat 内部自动：
# 1. detect_provider(model) 按 fnmatch 识别族
# 2. 翻译 reasoning_effort 为对应 provider 的 thinking 参数
# 3. 内容审查拒绝时自动 fallback 到 content_fallbacks 配置的模型
```

**日志由 llm-compat 自动输出**（带 request_id、latency、token 用量）。

### 4.5 启动日志与配置校验

项目启动时（`api/app.py:startup_event`）自动扫描 `llm.*_model` 字段，打印每个任务的摘要：

```
[LLM] calibrate: gpt-4.1-mini (openai_gpt4) | thinking=n/a(model_default)
[LLM] summary:   deepseek-v4-flash (deepseek) | thinking=high(reasoning_effort)
[LLM] validator: deepseek-v4-flash (deepseek) | thinking=disabled(extra_body.thinking)
[LLM] risk_summary: gemini-2.5-flash (gemini_25) | thinking=medium(reasoning_effort)
```

任何不兼容组合会在启动时立刻 warn（如 `gpt-4o + reasoning_effort=high` → 参数会被丢弃），不必等运行时才看到 400。

### 4.6 DeepSeek 模型迁移（2026-07-24 弃用节点）

旧模型 `deepseek-chat` / `deepseek-reasoner` 将于 2026-07-24 停服，统一迁移到 `deepseek-v4-flash`：

| 旧模型 | 新配置 |
|--------|--------|
| `deepseek-chat` | `deepseek-v4-flash` + `reasoning_effort: "disabled"` |
| `deepseek-reasoner` | `deepseek-v4-flash` + `reasoning_effort: "high"` |

**静默行为变化警告**：旧 `deepseek-chat` 不支持 `reasoning_effort`（被服务端忽略），而 `v4-flash` **真正响应**该参数。升级后：
- 如果原配置 `deepseek-chat + "high"` → 思考模式会实际触发，延迟和 token 成本明显上升
- 如果原配置 `deepseek-reasoner + null` → 改 v4-flash 后 `null` 会退化为默认（仍开思考），行为一致

### 4.7 如何添加新 provider

Provider 翻译逻辑已迁移到 [llm-compat](https://github.com/zj1123581321/llm-compat) 库。添加新 provider（如 Claude/Qwen3/豆包）请在 llm-compat 侧修改，本项目自动继承。

**项目侧唯一需要做的**：如果 New API 代理使用自定义模型名，在 `config.jsonc` 中配置 `provider_patterns`：

```jsonc
{
  "llm": {
    "provider_patterns": {
      "my-proxy-ds-*": "deepseek",
      "my-gpt-*": "openai_gpt4"
    }
  }
}
```

`set_default_config()` 启动时自动调用 `llm_compat.set_custom_patterns()` 注入。

**日志示例**：

```
2026-04-24 04:13:47 | INFO  | [LLM] calibrate: gpt-4.1-mini (openai_gpt4) | thinking=n/a(model_default)
2026-04-24 04:13:47 | INFO  | [LLM] summary: deepseek-v4-flash (deepseek) | thinking=high(reasoning_effort)
2026-04-24 04:13:47 | WARN  | Provider family 'openai_gpt4' does not support reasoning_effort; dropping value 'high'.
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

## 7. 项目实践：说话人推断与诚实状态模型

以下两点是本项目（VideoTranscriptAPI）在上述通用实践基础上落地的具体设计，记录于此供后续项目复用思路（区别于前面章节的通用示例代码，这里直接对应本仓库的真实实现路径）。

### 7.1 说话人推断：按人采样 + confidence 降级

早期实现对整段对话做"全局前 N 字符截断"采样，导致晚出场的说话人拿不到足够的发言样本，LLM 推断质量差且不可控。重构后的 `SpeakerInferencer`（`src/video_transcript_api/llm/core/speaker_inferencer.py`）改为**按说话人采样**：

- 每个说话人独立取前 `samples_per_speaker` 条发言（默认 3 条，单条截断 120 字符），总字符数不超过 `max_chars_per_speaker`（默认 400）——不管这个人第几次出场都能拿到样本
- 首次出场前额外采集 `context_dialogs` 条他人发言作为上下文（默认 2 条），捕捉"XX你好""欢迎 XX"之类的称呼线索
- LLM 返回推断结果时同时给出每个说话人的 confidence；`_apply_confidence_gate()` 按 `confidence_threshold`（默认 0.6）过滤：达标才采用推断姓名，未达标降级为 `说话人N` 占位符（N 优先取原始标签的数字序号，如 `Speaker3` → `3`；标签无数字时按其在列表中的出场顺序编号）

这样避免了"把低置信度的猜测当作确定结论展示给用户"——宁可展示占位符也不展示错误姓名。四个参数均在 `config.jsonc` 的 `llm.speaker_inference` 段配置：

```jsonc
"llm": {
    // ...
    "speaker_inference": {
        "samples_per_speaker": 3,      // 每个说话人采样的发言条数上限
        "max_chars_per_speaker": 400,  // 每个说话人采样文本的总字符上限
        "context_dialogs": 2,          // 首次出场前，采集他人发言作为上下文的条数
        "confidence_threshold": 0.6    // 低于此置信度时不采用推断姓名，降级为"说话人N"
    }
}
```

### 7.2 校对/总结的诚实状态模型（简述）

早期实现里，"总结跳过（文本过短）"与"总结失败"共用同一个 `None` 返回值，下游无法区分，最终表现为前端永久显示"总结处理中..."的 bug。修复方式是引入显式状态枚举（`src/video_transcript_api/utils/llm_status.py`），把"尝试了但失败"和"根本没尝试/没必要尝试"彻底分开：

- `CalibrationStatus`：`full`（全部成功）/ `partial`（部分降级）/ `none`（全部失败降级为原文）/ `disabled`（用户主动关闭校对）
- `SummaryStatus`：`generated`（成功）/ `skipped_short`（文本过短，正常跳过）/ `failed`（触发了但失败）/ `pending`（处理中）/ `disabled`（用户主动关闭总结）

状态沿 processor → coordinator → llm_ops → cache_manager → 前端全链路传递，落盘到缓存目录的 `llm_status.json`（读-改-写按字段合并，避免部分更新时误覆盖已有状态）与 `task_status` 表的对应列。这一模式的通用启示：**任何"跳过"和"失败"共享同一哨兵值（`None`/`""`/`0`）的设计，长期看几乎必然演化成一个不可调试的 bug**；引入显式状态枚举的成本远低于事后排查"为什么这个字段一直是空的"。

完整语义与前端/通知渲染细节见 [处理深度开关功能文档](../../features/processing_options.md) 与[系统架构文档](../../architecture.md#诚实状态模型)。

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
