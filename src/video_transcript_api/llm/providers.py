"""
LLM Provider 抽象层：统一 OpenAI / Gemini / DeepSeek 的 thinking/reasoning API 差异。

核心设计
========
1. detect_provider(model) 按模型名前缀识别 provider family（fnmatch 通配符）
2. build_request_payload(model, effort, base) 深合并 base + thinking 翻译
3. describe_from_payload(payload) 从最终 payload 反推日志描述（确保不漂移）

2026 现状速查
=============
| 模型族       | 合法 effort                               | 关闭思考方式              | 默认        |
|-------------|-------------------------------------------|--------------------------|-------------|
| deepseek    | low/medium/high/max/xhigh                 | extra_body.thinking.type | enabled@high|
| gemini_25   | low/medium/high + "none"                  | reasoning_effort="none"  | enabled     |
| gemini_3    | minimal/low/medium/high                   | minimal (尽力；Pro 关不掉)| enabled@high|
| gemini      | low/medium/high                           | reasoning_effort="none"  | enabled     |
| openai_gpt5 | minimal/low/medium/high                   | minimal (关不掉)         | medium      |
| openai_gpt4 | (不支持)                                  | N/A (不思考)             | N/A         |
| openai_o    | low/medium/high                           | (关不掉)                 | enabled     |
| openai      | low/medium/high (fallback)                | ignore                    | -           |

如何添加新 provider
===================
1. 在 _DEFAULT_PROVIDER_PATTERNS 增加一条 pattern -> "myfamily"
2. 在 _FAMILY_CAPABILITIES 增加一项描述能力
3. (可选) 如需特殊翻译逻辑，扩展 _translate 的 disable_mode 分支
4. tests/unit/test_providers.py 增加一个 TestClass 覆盖新 family
"""
from __future__ import annotations

import fnmatch
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 顺序敏感：首个匹配命中即返回。更具体的 pattern 必须放在更通用 pattern 之前。
# OSS 贡献者加新 provider 时，插入到合适优先级位置，不要追加到末尾。
_DEFAULT_PROVIDER_PATTERNS: Tuple[Tuple[str, str], ...] = (
    # DeepSeek（含 legacy chat/reasoner，2026-07-24 弃用前兼容）
    ("deepseek-chat", "deepseek"),
    ("deepseek-reasoner", "deepseek"),
    ("deepseek-*", "deepseek"),
    # Gemini 2.5 vs 3.x（disable 语义不同，必须区分）
    ("gemini-2.5-*", "gemini_25"),
    ("gemini-3-*", "gemini_3"),
    ("gemini-3.*-*", "gemini_3"),
    ("gemini-*", "gemini"),
    # OpenAI GPT 系列（-5 与 -4 capability 不同）
    ("gpt-5", "openai_gpt5"),
    ("gpt-5-*", "openai_gpt5"),
    ("gpt-5.*", "openai_gpt5"),
    ("gpt-4*", "openai_gpt4"),
    ("gpt-*", "openai"),
    # OpenAI o-series
    ("o1*", "openai_o"),
    ("o3*", "openai_o"),
    ("o4*", "openai_o"),
    ("o5*", "openai_o"),
)

# 每个 family 的能力描述
# - disable_mode:
#   "native"            -> extra_body.thinking.type=disabled
#   "effort_none"       -> reasoning_effort="none"
#   "minimal_fallback"  -> reasoning_effort="minimal" (关不严，尽力)
#   "unsupported"       -> 完全关不掉，丢弃 disabled 意图 + warn
#   "na"                -> 模型本身就不思考（无需任何字段）
# - efforts: 模型接受的 effort 值集合
# - max_effort: 当用户传入不接受的值时 clamp 到此
# effort 强度排序（由低到高）用于 clamp
_EFFORT_RANK = {
    "minimal": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "max": 4,
    "xhigh": 5,
}

_FAMILY_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "disable_mode": "native",
        "efforts": frozenset({"low", "medium", "high", "max", "xhigh"}),
        "min_effort": "low",
        "max_effort": "xhigh",
    },
    "gemini_25": {
        "disable_mode": "effort_none",
        "efforts": frozenset({"low", "medium", "high"}),
        "min_effort": "low",
        "max_effort": "high",
    },
    "gemini_3": {
        "disable_mode": "minimal_fallback",
        "efforts": frozenset({"minimal", "low", "medium", "high"}),
        "min_effort": "minimal",
        "max_effort": "high",
    },
    "gemini": {
        "disable_mode": "effort_none",
        "efforts": frozenset({"low", "medium", "high"}),
        "min_effort": "low",
        "max_effort": "high",
    },
    "openai_gpt5": {
        "disable_mode": "minimal_fallback",
        "efforts": frozenset({"minimal", "low", "medium", "high"}),
        "min_effort": "minimal",
        "max_effort": "high",
    },
    "openai_gpt4": {
        "disable_mode": "na",
        "efforts": frozenset(),
        "min_effort": None,
        "max_effort": None,
    },
    "openai_o": {
        "disable_mode": "unsupported",
        "efforts": frozenset({"low", "medium", "high"}),
        "min_effort": "low",
        "max_effort": "high",
    },
    "openai": {
        "disable_mode": "na",
        "efforts": frozenset({"low", "medium", "high"}),
        "min_effort": "low",
        "max_effort": "high",
    },
}


def detect_provider(
    model: Optional[str],
    custom_patterns: Optional[Tuple[Tuple[str, str], ...]] = None,
) -> str:
    """按模型名前缀识别 provider family。未知返回 "openai" 并 warn。"""
    if not model or not isinstance(model, str):
        logger.warning("detect_provider: invalid model name %r, defaulting to 'openai'", model)
        return "openai"
    patterns = custom_patterns if custom_patterns else _DEFAULT_PROVIDER_PATTERNS
    model_lower = model.lower()
    for pattern, family in patterns:
        if fnmatch.fnmatch(model_lower, pattern.lower()):
            return family
    logger.warning(
        "detect_provider: unknown model %r, defaulting to 'openai'. "
        "Add a pattern via config.jsonc llm.provider_patterns if needed.",
        model,
    )
    return "openai"


def _translate(family: str, effort: Optional[str]) -> Dict[str, Any]:
    """
    翻译 (family, effort) 为要合并进 payload 的字段 dict。

    返回 dict 可能的键：
    - "reasoning_effort": str
    - "extra_body": {"thinking": {"type": "disabled"}}
    或空 dict。
    """
    if effort is None:
        return {}

    caps = _FAMILY_CAPABILITIES.get(family, _FAMILY_CAPABILITIES["openai"])

    # "disabled" 意图：按 family 的 disable_mode 翻译
    if effort == "disabled":
        mode = caps["disable_mode"]
        if mode == "native":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        if mode == "effort_none":
            return {"reasoning_effort": "none"}
        if mode == "minimal_fallback":
            return {"reasoning_effort": "minimal"}
        if mode == "unsupported":
            logger.warning(
                "Provider family %r cannot disable thinking; dropping 'disabled' intent. "
                "Model will think at its default effort.",
                family,
            )
            return {}
        if mode == "na":
            # 模型本身不思考，无需任何字段
            return {}
        return {}

    # 具体 effort 值
    accepted = caps["efforts"]
    if not accepted:
        logger.warning(
            "Provider family %r does not support reasoning_effort; dropping value %r.",
            family,
            effort,
        )
        return {}

    if effort in accepted:
        return {"reasoning_effort": effort}

    # 不在接受集 -> 按强度排序 clamp 到最近边界
    requested_rank = _EFFORT_RANK.get(effort, _EFFORT_RANK["high"])
    min_effort = caps.get("min_effort")
    max_effort = caps.get("max_effort")
    if min_effort and requested_rank < _EFFORT_RANK[min_effort]:
        clamped = min_effort
    elif max_effort:
        clamped = max_effort
    else:
        clamped = next(iter(sorted(accepted))) if accepted else None
    if clamped is None:
        logger.warning(
            "Provider family %r has no accepted effort; dropping %r.",
            family,
            effort,
        )
        return {}
    logger.warning(
        "Provider family %r does not accept effort %r, clamping to %r.",
        family,
        effort,
        clamped,
    )
    return {"reasoning_effort": clamped}


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """深合并 overlay 到 base，返回新 dict，不修改入参。嵌套 dict 递归合并。"""
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def build_request_payload(
    model: str,
    reasoning_effort: Optional[str],
    base_payload: Dict[str, Any],
    custom_patterns: Optional[Tuple[Tuple[str, str], ...]] = None,
) -> Dict[str, Any]:
    """
    构建完整 chat/completions payload：深合并 base + thinking 翻译。

    Args:
        model: 模型名
        reasoning_effort: 归一后的 effort (None / "disabled" / minimal..xhigh)
        base_payload: 基础 payload（含 model、messages、stream、response_format 等）
        custom_patterns: 可选自定义 provider 匹配表（覆盖默认）

    Returns:
        完整 payload dict（不修改入参）
    """
    family = detect_provider(model, custom_patterns)
    translation = _translate(family, reasoning_effort)
    return _deep_merge(base_payload, translation)


def describe_from_payload(
    payload: Dict[str, Any],
    custom_patterns: Optional[Tuple[Tuple[str, str], ...]] = None,
) -> Dict[str, str]:
    """
    从最终 payload 反推人读日志描述。

    只输出白名单字段：{provider, model, thinking_mode, thinking_source}
    不输出 api_key / base_url / messages / headers 任何 secret-adjacent 字段。
    """
    model = payload.get("model", "unknown") if isinstance(payload, dict) else "unknown"
    family = detect_provider(model, custom_patterns)
    caps = _FAMILY_CAPABILITIES.get(family, _FAMILY_CAPABILITIES["openai"])

    effort = payload.get("reasoning_effort") if isinstance(payload, dict) else None
    extra_body = payload.get("extra_body") if isinstance(payload, dict) else None
    thinking_type = None
    if isinstance(extra_body, dict):
        thinking_cfg = extra_body.get("thinking")
        if isinstance(thinking_cfg, dict):
            thinking_type = thinking_cfg.get("type")

    if thinking_type == "disabled":
        mode = "disabled"
        source = "extra_body.thinking"
    elif effort == "none":
        mode = "disabled"
        source = "reasoning_effort=none"
    elif effort:
        mode = effort
        source = "reasoning_effort"
    else:
        # 未显式设置 -> 按模型默认
        if caps["disable_mode"] == "na":
            mode = "n/a"
        else:
            mode = f"default({family})"
        source = "model_default"

    return {
        "provider": family,
        "model": str(model),
        "thinking_mode": mode,
        "thinking_source": source,
    }
