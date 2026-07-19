"""Application-owned runtime resources.

Safe import -> lifespan validates config -> RuntimeContext owns resources ->
shutdown releases them. Compatibility accessors below never construct resources
at module import; routes and services use lazy proxies until a lifespan is active.
"""

import asyncio
import concurrent.futures
import contextvars
import datetime
import os
import queue
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, TypeVar

from fastapi.templating import Jinja2Templates

from ..utils.logging import load_config, setup_logger, shutdown_logger
from ..utils.task_status import TaskStatus


class ConfigError(ValueError):
    """Raised when production configuration is missing or invalid."""


def _require_dict(config: dict, key: str) -> dict:
    value = config.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be an object")
    return value


def _require_non_empty(section: dict, field: str, path: str) -> str:
    value = section.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path} must be a non-empty string")
    return value


_WHITESPACE_PATTERN = re.compile(r"\s")


def _require_no_whitespace(value: str, path: str) -> None:
    """真实鉴权 transcription.py::verify_token 用
    `authorization.split()`（按任意空白切分，无参数形式）要求
    `Bearer <token>` 恰好切成两段。token 本身含任意空白字符（含前导/尾随/
    内部空格、tab、换行）会让这个请求永远无法凑出恰好两段——不是运行时
    偶发失败，而是这个 token 从写入配置那一刻起就永久无法通过鉴权。
    legacy 单 token 模式下整个 API 会被锁死，多用户模式下则是对应用户被
    锁死；但 `_require_non_empty` 只查 strip 后非空，对此完全没有察觉，
    --check-config 会绿灯放行一个实际上鉴权不了的配置。这里在非空校验
    之外单独拦截，错误信息只指名字段路径，不回显 token 值本身，避免把
    敏感凭证写进日志/终端。"""
    if _WHITESPACE_PATTERN.search(value):
        raise ConfigError(f"{path} must not contain whitespace characters")


def _reject_unknown(section: dict, allowed: set[str], path: str) -> None:
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ConfigError(f"{path}.{unknown[0]} is not a supported field")


def validate_config(config: Any) -> dict:
    """Validate lifecycle-critical fields without connecting to any backend."""
    if not isinstance(config, dict):
        raise ConfigError("configuration root must be an object")

    api = _require_dict(config, "api")
    _reject_unknown(api, {"host", "port", "auth_token"}, "api")
    _require_non_empty(api, "host", "api.host")
    auth_token = _require_non_empty(api, "auth_token", "api.auth_token")
    _require_no_whitespace(auth_token, "api.auth_token")
    port = api.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError("api.port must be an integer between 1 and 65535")

    concurrent = _require_dict(config, "concurrent")
    _reject_unknown(
        concurrent, {"max_workers", "queue_size", "llm_max_workers"}, "concurrent"
    )
    for field in ("max_workers", "queue_size", "llm_max_workers"):
        value = concurrent.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"concurrent.{field} must be a positive integer")

    storage = _require_dict(config, "storage")
    _reject_unknown(
        storage,
        {
            "cache_dir",
            "workspace_dir",
            "temp_dir",
            "audit_db",
            "temp_retention_hours",
            "cache_retention_days",
            "task_status_retention_days",
            "audit_log_retention_days",
            "max_download_size_mb",
        },
        "storage",
    )
    for field in ("cache_dir", "workspace_dir", "temp_dir"):
        _require_non_empty(storage, field, f"storage.{field}")
    for field in (
        "cache_retention_days",
        "task_status_retention_days",
        "audit_log_retention_days",
    ):
        if field in storage:
            value = storage[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError(f"storage.{field} must be a non-negative integer")
    if "temp_retention_hours" in storage:
        value = storage["temp_retention_hours"]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < 0
        ):
            raise ConfigError(
                "storage.temp_retention_hours must be a non-negative number"
            )
    if "audit_db" in storage:
        _require_non_empty(storage, "audit_db", "storage.audit_db")
    if "max_download_size_mb" in storage:
        value = storage["max_download_size_mb"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(
                "storage.max_download_size_mb must be a non-negative integer"
            )

    # llm is required, not optional-like the backend_sections below: unlike
    # ASR backends (which stay dormant when disabled/absent), RuntimeContext
    # .start() unconditionally constructs LLMCoordinator -> LLMConfig
    # .from_dict(), which reads api_key/base_url/calibrate_model/
    # summary_model via hard `llm_config["..."]` subscripts with no `.get`
    # default (see llm/core/config.py). A config missing the llm section, or
    # missing any one of these four keys, previously passed
    # `--check-config` clean and only crashed with a bare KeyError the first
    # time a real lifespan called RuntimeContext.start() -- a boot-fatal gap
    # behind a green preflight. This system has no supported "no LLM"
    # deployment mode; the per-task calibrate/summarize switches are
    # request-level only and do not change this.
    llm_section = _require_dict(config, "llm")
    for field in ("api_key", "base_url", "calibrate_model", "summary_model"):
        _require_non_empty(llm_section, field, f"llm.{field}")

    # Same "green preflight, boot-fatal real start" gap as the four hard keys
    # above, one level down: LLMConfig.from_dict (llm/core/config.py) also
    # does `float(llm_config.get("total_timeout", 300.0))` -- a non-numeric
    # value raises ValueError -- and immediately treats
    # segmentation/structured_calibration/speaker_inference/quality_validation
    # as dicts (`.get(...)` is called on each a few lines later in from_dict),
    # so a string/list/int there raises AttributeError. Both only surface the
    # first time a real lifespan constructs LLMConfig; absent keys are fine
    # (from_dict's own `.get(key, {})` / `.get(key, 300.0)` defaults apply),
    # only present-but-wrong-typed values are rejected here.
    if "total_timeout" in llm_section:
        total_timeout = llm_section["total_timeout"]
        if not isinstance(total_timeout, (int, float)) or isinstance(
            total_timeout, bool
        ):
            raise ConfigError("llm.total_timeout must be a number")

    for nested_key in (
        "segmentation",
        "structured_calibration",
        "speaker_inference",
        "quality_validation",
    ):
        nested_section = llm_section.get(nested_key)
        if nested_section is not None and not isinstance(nested_section, dict):
            raise ConfigError(f"llm.{nested_key} must be an object")

    # Credentials are strict only when the corresponding backend is explicitly
    # enabled. Disabled or absent optional backends do not block startup.
    backend_sections = {
        "tikhub": (
            "api_key",
            {"enabled", "api_key", "alternate_api_key", "max_retries", "retry_delay", "timeout"},
        ),
        "capswriter": (
            "server_url",
            {
                "enabled", "server_url", "max_retries", "retry_delay",
                "connection_timeout", "file_seg_duration", "file_seg_overlap",
                "enable_hot_words",
            },
        ),
        "funasr": ("server_url", {"enabled", "server_url"}),
        "funasr_spk_server": (
            "server_url",
            {
                "enabled", "server_url", "max_retries", "retry_delay",
                "connection_timeout", "poll_interval", "poll_recv_timeout",
                "total_timeout", "first_delay_fallback",
            },
        ),
    }
    for section_name, (credential, allowed) in backend_sections.items():
        section = config.get(section_name)
        if section is not None and not isinstance(section, dict):
            raise ConfigError(f"{section_name} must be an object")
        if section:
            _reject_unknown(section, allowed, section_name)
        if section and "enabled" in section and not isinstance(section["enabled"], bool):
            raise ConfigError(f"{section_name}.enabled must be a boolean")
        if section and section.get("enabled") is True:
            _require_non_empty(section, credential, f"{section_name}.{credential}")

        if not section:
            continue
        for string_field in (credential, "alternate_api_key"):
            if string_field in section and not isinstance(section[string_field], str):
                raise ConfigError(f"{section_name}.{string_field} must be a string")
        for field in ("max_retries",):
            if field in section:
                value = section[field]
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ConfigError(
                        f"{section_name}.{field} must be a non-negative integer"
                    )
        for field in (
            "retry_delay",
            "timeout",
            "connection_timeout",
            "file_seg_duration",
            "file_seg_overlap",
            "poll_interval",
            "poll_recv_timeout",
            "total_timeout",
            "first_delay_fallback",
        ):
            if field in section:
                value = section[field]
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or value < 0
                ):
                    raise ConfigError(
                        f"{section_name}.{field} must be a non-negative number"
                    )
        if "enable_hot_words" in section and not isinstance(
            section["enable_hot_words"], bool
        ):
            raise ConfigError(f"{section_name}.enable_hot_words must be a boolean")

    risk_control = config.get("risk_control")
    if risk_control is not None and not isinstance(risk_control, dict):
        raise ConfigError("risk_control must be an object")
    if risk_control and "enabled" in risk_control and not isinstance(
        risk_control["enabled"], bool
    ):
        raise ConfigError("risk_control.enabled must be a boolean")
    return config


def _validate_users_json(config: dict) -> None:
    """Validate the users.json a real boot will load, without creating a
    database, thread, or external connection.

    UserManager has no config-driven override for where users.json lives:
    every real construction site (RuntimeContext.start() below, and the
    legacy get_user_manager() singleton in utils/accounts/user_manager.py)
    calls UserManager(...) without a users_config_path, so it always
    resolves to the same hardcoded default (<project_root>/config/
    users.json). --check-config must therefore use that identical
    resolution rather than inventing a separate path lookup that could
    drift from it.

    UserManager.__init__ itself has no side effects beyond reading that one
    file and holding the parsed result in memory (a threading.Lock is
    created but never acquired/started as a real thread here, and no
    database or network connection is touched), so constructing it directly
    is safe to run during --check-config. A missing file is the one
    legacy-fallback case (single-token mode) and does not raise; an
    existing-but-invalid file (empty, malformed JSON, duplicate user_id,
    disallowed permission, ...) raises UserConfigError from
    UserManager._validate_users, which is re-raised here as a ConfigError so
    --check-config fails with a non-zero exit and a readable message instead
    of silently passing a config that will only blow up once a real
    lifespan calls RuntimeContext.start().
    """
    from ..utils.accounts.user_manager import UserConfigError, UserManager

    try:
        UserManager(fallback_config=config)
    except UserConfigError as exc:
        raise ConfigError(f"users configuration invalid: {exc}") from exc


def _rehearse_llm_config(config: dict) -> None:
    """演练真实启动会对 llm 段做的解析，堵住 validate_config 显式校验漏掉的
    深层字段（如 llm.quality_validation.quality_threshold 类型、
    llm.segmentation.quality_validation 类型等）。

    根治动机：validate_config 此前只逐个白名单式校验它认识的 llm 深层字段
    （见上方 total_timeout / 四个嵌套段的显式检查），每次 LLMConfig.from_dict
    新增一个隐式依赖 dict 形状的读取，就得同步在这里手工补一条校验，永远
    滞后一步。这里改为直接调用真实启动会调用的解析器本身
    （LLMConfig.from_dict + set_default_config 同款的 provider_patterns 校验），
    让"预检通过 ⇒ 真实解析不炸"从调用关系上成立，而不是靠人工逐键对齐维持。

    两者均为纯解析（无 IO/网络/线程/文件写，逐行读过 llm/core/config.py 与
    _normalize_provider_patterns 确认）：
    - LLMConfig.from_dict 只做 dict 取值与 dataclass 构造。
    - _normalize_provider_patterns 只做类型校验与 dict 浅拷贝，是
      set_default_config 中会产生副作用（构造全局 SyncLLMClient、写入
      llm-compat 全局 custom patterns 注册表）那部分之外的纯校验切片。

    现有 validate_config 的显式校验予以保留（面向常见错误给出更聚焦的
    字段名提示）；这里是兜底层，用于捕获显式校验暂未覆盖到的形状错误，
    并在 from_dict 未来新增硬键时自动跟上，无需再手工加白名单条目。
    """
    from ..llm.core.config import LLMConfig
    from ..llm.llm import _normalize_provider_patterns

    try:
        LLMConfig.from_dict(config)
        _normalize_provider_patterns(config.get("llm") or {})
    except Exception as exc:
        raise ConfigError(f"llm configuration cannot be parsed at startup: {exc}") from exc


def _rehearse_set_default_config_types(config: dict) -> None:
    """演练 set_default_config（llm/llm.py）在真实启动时对 llm 段剩余字段做的
    数值转换与构造参数校验，堵住 _rehearse_llm_config 覆盖不到的那一处。

    真实启动路径：api/app.py::startup_event() 无条件调用
    `set_default_config(config)`。该函数在 api_key/base_url 都非空时
    （validate_config 已保证这两者非空，故这个分支在通过校验的配置上总会
    触达）会执行：
        total_timeout = float(llm_cfg.get(
            "total_timeout", llm_cfg.get("timeout", DEFAULT_LLM_TIMEOUT)
        ))
    这条转换和 llm/core/config.py::LLMConfig.from_dict 里的 total_timeout
    转换是两套独立逻辑——from_dict 只读 "total_timeout" 键，这里在其基础上
    还多一层向后兼容的 "timeout" 键 fallback。validate_config 现有的
    llm.total_timeout 类型检查、以及 _rehearse_llm_config 对
    LLMConfig.from_dict 的演练，都覆盖不到"只填了 timeout、没填
    total_timeout"这种配置——这类配置此前能穿过 --check-config，只在真实
    启动调用 set_default_config 时才因 float() 转换失败而崩溃。

    同一函数接着会把 refusal_keywords_url/collector_url 等原始配置值直接
    传给 `SyncLLMClient(...)`（llm-compat 的 `BaseClient.__init__`，见
    .venv 下 `llm_compat/_base.py`），后者在**构造期**（不是等到真正发起
    一次 chat() 调用时）就立即消费这两个参数：
        if refusal_keywords_url:
            if isinstance(refusal_keywords_url, str):
                ...
            else:
                self._refusal_keywords_urls = list(refusal_keywords_url)
            for url in self._refusal_keywords_urls:
                get_cached_keywords(url)   # 真实网络请求
        if collector_url:
            self._collector = CollectorClient(url=collector_url, ...)
            # CollectorClient.__init__ 内部立即执行 url.rstrip("/")
    一个 truthy 但非字符串/不可迭代的 refusal_keywords_url（如误填的整数、
    布尔值）会让 `list(refusal_keywords_url)` 立即抛 TypeError；一个
    truthy 但非字符串的 collector_url 会让 CollectorClient 内的
    `url.rstrip("/")` 立即抛 AttributeError。两者都不会等到真实发起 LLM
    请求，而是在 set_default_config() 本身、也就是 app 启动的那一刻就炸——
    这类配置此前能穿过 --check-config（因为这里从未构造过 SyncLLMClient），
    只在真实启动时才崩溃，与 total_timeout 那处是同一类"预检绿灯、启动
    崩溃"缺口。

    max_retries/base_delay/max_delay/content_fallbacks 等其余构造参数经
    逐行核对 `BaseClient.__init__`：均只是原样赋值给实例属性，不在构造期
    做任何类型转换或方法调用——只有在真正发起一次 chat() 请求时才可能因
    类型不对而出错，那已经是运行时路径，不是"预检绿灯、启动即崩溃"的
    这类缺口，故不纳入这里。

    不调用 set_default_config 本身或构造 SyncLLMClient：前者会真的初始化
    全局 SyncLLMClient 单例并写入 llm-compat 的 custom patterns 注册表；
    后者的 __init__（llm_compat/sync.py）在配置了 refusal_keywords_url 时
    会通过 get_cached_keywords(url) 发起真实网络请求拉取远端关键词列表，
    是本函数明确不允许触发的副作用。这里只单独复现上面那一行数值转换 +
    对 refusal_keywords_url/collector_url 的结构校验，纯计算，无 IO。
    """
    from ..llm.llm import DEFAULT_LLM_TIMEOUT

    llm_cfg = config.get("llm") or {}
    api_key = llm_cfg.get("api_key", "")
    base_url = llm_cfg.get("base_url", "")
    if not api_key or not base_url:
        # set_default_config 本身在这种情况下提前 return，不会走到下面的数值
        # 转换。validate_config 已将 llm.api_key/base_url 列为必需字段，通过
        # 校验的配置不会落入这个分支；保留只是为了和真实函数的分支结构一致。
        return
    try:
        float(llm_cfg.get("total_timeout", llm_cfg.get("timeout", DEFAULT_LLM_TIMEOUT)))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"llm configuration cannot be parsed at startup: {exc}") from exc

    refusal_keywords_url = llm_cfg.get("refusal_keywords_url")
    if refusal_keywords_url is not None:
        if isinstance(refusal_keywords_url, str):
            pass
        elif isinstance(refusal_keywords_url, list):
            if not all(isinstance(item, str) for item in refusal_keywords_url):
                raise ConfigError(
                    "llm.refusal_keywords_url list items must all be strings"
                )
        else:
            raise ConfigError(
                "llm.refusal_keywords_url must be a string, a list of strings, or null"
            )

    collector_url = llm_cfg.get("collector_url")
    if collector_url is not None and not isinstance(collector_url, str):
        raise ConfigError("llm.collector_url must be a string or null")


def _rehearse_log_config(config: dict) -> None:
    """演练真实启动会对 log 段做的解析（RuntimeContext.__init__ ->
    utils/logging/logger.py::setup_logger(bootstrap=False)）。

    setup_logger 在生产模式下会真的创建 loguru sink 并写文件（副作用），
    --check-config 不能直接调用它本体。这里改为调用从 setup_logger 里抽出的
    纯解析函数 _parse_log_settings（无 IO），复现 log.level 是否为字符串
    且为合法级别名、log.file 是否为非空字符串、max_size/backup_count 是否
    为 loguru rotation/retention 能接受的 int/str 这几处类型约束——垃圾类型
    （如 log.level 传数字、log.file 传对象）此前只会在真实启动创建 sink 时
    才炸，--check-config 看不到。
    """
    from ..utils.logging.logger import _parse_log_settings

    try:
        _parse_log_settings(config)
    except Exception as exc:
        raise ConfigError(f"log configuration cannot be parsed at startup: {exc}") from exc


def _rehearse_notification_config(config: dict) -> None:
    """演练真实启动会对通知相关配置段做的解析（api/app.py::startup_event()
    无条件调用的 init_all_notifiers()，进而触达
    utils/notifications/channel.py::init_global_feishu_notifier() 与
    utils/notifications/router.py::NotificationRouter._init_channels_from_config()）。

    这两处都会执行 `config.get("wechat", {}).get(...)` /
    `config.get("feishu", {}).get(...)`——若 wechat/feishu 配置成非对象
    （字符串/数字等），`.get()` 会在真实启动时抛出未捕获的 AttributeError；
    init_all_notifiers() 在 app.py 里没有包 try/except，会直接中断 lifespan
    启动。--check-config 目前完全不读这两个配置段，看不到这类缺口。

    不直接调用这三个真实入口：它们各自都会触发真实的、有副作用的构造
    （WeComNotifier/FeishuNotifier 全局单例、NotificationRouter 内部按配置
    尝试真的构造 WeComChannel/FeishuChannel），且都通过裸调用
    `utils.logging.load_config()`（走模块级配置缓存/环境变量路径解析）读取
    配置，而不是复用这里传入的、已校验过的 config 字典——直接调用会绕开
    --check-config 指定的 --config 路径，读到另一份配置。这里只做与它们
    完全相同的"取值 + 当 dict 用 .get()"这一步，纯读取，无副作用、无网络。
    """
    for section_name in ("wechat", "feishu"):
        section = config.get(section_name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"{section_name} must be an object")


def _rehearse_ytdlp_config(config: dict) -> None:
    """演练真实启动会对 ytdlp 段做的解析。

    validate_config 完全不读 ytdlp 段，但 app.py 启动时无条件构造
    `YtdlpConfigBuilder(config)` 并调用 `validate_cookie_on_startup()`
    （见 api/app.py 中 "Initializing yt-dlp configuration" 附近）——
    `ytdlp` 为非对象、或 `ytdlp.youtube_cookie` 为非对象时，`--check-config`
    此前会放行，真实启动才在 `.get(...)` 上炸出 AttributeError。这里直接
    调用与启动同一套构造 + 校验，堵住这类"预检绿灯、启动崩溃"的缺口。

    YtdlpConfigBuilder 的构造函数只保存引用，无副作用；
    validate_cookie_on_startup() 仅在 ytdlp.youtube_cookie.enabled 为真时
    才读取本地 cookie 文件（与真实启动完全一致的行为，不是本次校验新引入
    的副作用），不发起任何网络请求。
    """
    from ..utils.ytdlp import YtdlpConfigBuilder

    try:
        YtdlpConfigBuilder(config).validate_cookie_on_startup()
    except Exception as exc:
        raise ConfigError(f"ytdlp configuration cannot be parsed at startup: {exc}") from exc


def load_and_validate_config(config_path: str | os.PathLike | None = None) -> dict:
    """加载配置并在不产生副作用的前提下演练所有启动期解析器。

    演练覆盖（有界合同——新增一类启动期解析器时，先在这里补一条演练，再把
    它加进下面这份清单，供后续 review 直接对照，不必逐个 commit 翻找）：
        1. llm：LLMConfig.from_dict + set_default_config 的剩余数值转换
           （_rehearse_llm_config / _rehearse_set_default_config_types）
        2. ytdlp：YtdlpConfigBuilder 构造 + validate_cookie_on_startup
           （_rehearse_ytdlp_config）
        3. users.json：UserManager(fallback_config=...) 的严格校验
           （_validate_users_json）
        4. log：setup_logger 消费的 log 段类型校验（_rehearse_log_config）
        5. notification：init_all_notifiers() 消费的 wechat/feishu 段结构
           （_rehearse_notification_config）
    """
    try:
        config = load_config(config_path, use_cache=False)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {exc.filename}") from exc
    except Exception as exc:
        raise ConfigError(f"failed to parse configuration: {exc}") from exc
    validated = validate_config(config)
    _validate_users_json(validated)
    _rehearse_llm_config(validated)
    _rehearse_set_default_config_types(validated)
    _rehearse_ytdlp_config(validated)
    _rehearse_log_config(validated)
    _rehearse_notification_config(validated)
    return validated


# 关闭流程的总预算上限(秒)——不是每个阶段各自独立的预算（本地 codex
# review 第 11 轮 N1 修复前的实际语义）。_stop_workers 的 producers/
# maintenance/llm 三段 wait_for、_shutdown_llm_owner 的线程 join、以及
# 关闭清算（_drain_non_terminal_tasks_on_shutdown -> CacheManager.
# drain_non_terminal_tasks_on_shutdown）总共最多五处有界等待，此前各自
# 独立拿满这同一个字面量 5——最坏情况下（每一段都真的等到超时）整个
# aclose()/close() 的总耗时可以累加到 ~25s，违反"关闭在
# WORKER_STOP_TIMEOUT_SECONDS 内有界返回"这条对外承诺的不变式（本地
# codex review 把常量 monkeypatch 成 0.05s、构造连续两段超时的场景实测
# 复现：两段耗时会累加，而不是共享同一份预算）。
#
# 修复：aclose()/close() 入口各自只调用一次 _new_shutdown_deadline() 算出
# 绝对 deadline（time.monotonic() + WORKER_STOP_TIMEOUT_SECONDS），随后
# 全部阶段都传入同一个 deadline，各自只消费
# _remaining_shutdown_budget(deadline) 算出的剩余预算——预算跨阶段累计
# 消耗、不重新充满，总耗时因此有界于单一份 WORKER_STOP_TIMEOUT_SECONDS，
# 不再是"阶段数 x 预算"。剩余预算耗尽为 0 时，各阶段的
# wait_for(..., timeout=0) / join(timeout=0) 会立即检查一次真实状态后
# 快速返回、不做无谓阻塞，返回值仍然反映真实状态（如
# _maintenance_confirmed_stopped 不会被硬编码为"未确认"，而是如实记录
# 这一次即时检查的结果）；清算阶段剩余预算为 0 时则整体跳过（连查询
# 非终态任务的 SELECT 都不发起），未清算的任务留给下一次启动的孤儿恢复
# 兜底（该兜底语义已存在，非本次改动新增）。
#
# 内部方法 (_stop_workers/_shutdown_llm_owner/_finish_close/
# _drain_non_terminal_tasks_on_shutdown) 的 deadline 参数默认 None：仅
# 服务于绕开 aclose()/close() 直接调用这些方法的单测（构造单一阶段场景），
# 退化为"这一次调用现算一个新 deadline"，等价于把整份预算单独给这一次
# 调用——真正的跨阶段累加只发生在 aclose()/close() 显式传入同一个
# deadline、贯穿多阶段调用的生产路径上。
WORKER_STOP_TIMEOUT_SECONDS = 5.0


# LLM 队列容量上限（受理位，不是执行位——见 _InflightTaskRegistry 类文档
# 里"消费者立即 submit 给无界执行器"的背压绕过问题）。此前在
# RuntimeContext.start() 与 get_llm_queue() 的 legacy 单例分支各自硬编码
# 字面量 100，提成单一常量避免两处独立维护同一个容量数字将来悄悄漂移；
# 也是 _InflightTaskRegistry "llm" kind 的容量上限来源（本地 codex review
# 第 12 轮 P1）。
LLM_QUEUE_MAXSIZE = 100


class _InflightTaskRegistry:
    """进程内在途任务登记表：按 kind 分桶记录 task_id -> 受理时间戳
    (time.monotonic())，是背压闭环的核心（本地 codex review 第 12 轮 P1，
    统一修复发现 a/b/c）。

    背景：transcription/llm 两条流水线此前只在"排队位"设容量上限
    （asyncio.Queue(maxsize=queue_size) / queue.Queue(maxsize=
    LLM_QUEUE_MAXSIZE)）——消费者（process_task_queue / process_llm_queue）
    把任务从队列取出后立即 submit 给各自的 ThreadPoolExecutor（内部是无界
    的 SimpleQueue）并归还队列名额，此时任务的实际工作往往还没开始跑。
    持续的请求流量下，"排队中+执行中"的任务总量可以无限增长，503 几乎不可
    达；消费者取出即归还名额，队列自身的 maxsize 起不到真正的背压作用。

    修复思路：把容量上限从"排队位"挪到"受理位"——调用方（HTTP 路由）在
    真正接受一个任务时先 try_register 占一个名额，占满即拒绝（503）；
    任务的 worker future 完成时才 release 归还名额（见 RuntimeContext.
    track_future 的 task_id 参数）。register 到 release 之间横跨排队、
    执行两个阶段，是"受理中+执行中总量"的真实上限，不会被消费者的及时
    出队绕过。"transcription" 流水线的消费者本身（出队→submit）不需要
    改：它唯一的准入来源就是 try_register，已经严格钳制在 capacities
    里，出队→submit 不引入新的准入点，执行器积压自然 ≤ 容量上限。"llm"
    流水线不满足这个前提——见下面 "llm" 分支末尾与 register_internal
    文档"数学上界"一节（第 14 轮订正）。

    两条流水线容量语义不同、互不影响，按 kind 分桶各自独立计数：
    - "transcription"：容量取 concurrent.queue_size（与 asyncio.Queue 的
      既有容量语义保持一致，只是把它从"排队位"重新解释为"受理位"）。
      /api/transcribe admits 到这个桶，track_future(kind="transcription")
      的完成回调 release。
    - "llm"：容量取 LLM_QUEUE_MAXSIZE（与 queue.Queue(maxsize=...) 的既有
      容量语义同理延伸）。/api/recalibrate 经 try_register 准入到这个桶。
      transcription worker 内部把已完成转录的任务交给 llm_task_queue 时
      用的是阻塞 put()（transcription.py 五处 llm_task_queue.put 调用），
      经 register_internal（而非 try_register）无条件登记进同一个桶——
      此前（本地 codex review 第 12 轮 P1 时）的取舍认为这类内部交接
      "天然被 transcription 桶的容量钳制住、不需要单独登记"，第 13 轮
      发现这个假设不成立：transcription worker 的 future 在 put() 成功
      后很快就完成（"transcription" 桶名额随之释放），但 llm_task_queue.
      put() 到 llm future 真正完成之间的窗口（排队+执行）远长于此——
      release 前一直不注册进任何桶的话，运行期对账
      （CacheManager.reconcile_runtime_orphaned_tasks）会把这类任务误判
      为孤儿、CAS 成 failed，而任务其实仍在合法排队/执行（calibrating）
      中，之后真正完成的 success 写入会被终态 CAS 拒绝——见
      register_internal 的方法文档。

      register_internal 登记本身不检查容量（见其方法文档"数学上界"
      一节），两个准入点因此管不到"进桶之后消费者敢不敢立刻转交给
      执行器"——第 14 轮发现：process_llm_queue（消费侧）出队后立即
      submit 给无界的 llm_executor，"已提交未完成"的这部分工作完全
      绕开两个准入点的容量检查，会随 "transcription" 桶的高频周转
      持续累积、没有上限（llm_task_queue 自身的 maxsize 起不到背压
      作用——消费者出队即提交，队列几乎永远填不满，也就永远不会真正
      阻塞上游 put()）。

      修复没有落在这张登记表上：直觉的做法是在 process_llm_queue
      出队后、submit 前比较 size("llm") 与这个桶的配置容量，超限就
      等待——第 14 轮实测证明这条路径有真实的死锁风险：size("llm")
      同时统计"已经在排队、还没被消费者出队"和"已经 submit、还没
      完成"两类条目，二者语义不同却混在一个数字里。一旦持续到达的
      register_internal 交接在消费者提交出第一个 future 之前就把
      size 推过容量（多个 transcription worker 几乎同时完成、集中
      交接是完全正常的场景），消费者会在从未成功 submit 过任何一项
      的状态下卡在这道比较上——没有任何 future 存在，谁都无法完成、
      无法释放名额，闸门永久打不开，整条 LLM 流水线彻底停摆，只能
      重启进程。真正的修复必须只统计"已经 submit、尚未完成"这一类，
      与"还没被消费者碰过"的排队条目彻底分开计数，才能保证从零
      开始时第一次 submit 总能立刻成功、不依赖任何已有的 future 先
      完成——这正是 RuntimeContext.llm_submit_semaphore（下方新增）
      要解决的问题：它只在 process_llm_queue 真正调用 llm_executor.
      submit() 前后 acquire/release，从空载状态开始，不看 registry
      当前积压了多少。详见 llm_ops.process_llm_queue 的 docstring 与
      register_internal 文档"数学上界"一节的订正。

    释放挂点（"终态即注销"，见各调用方的详细注释）：
    - 正常路径：RuntimeContext.track_future 的 future 完成回调（覆盖
      成功/异常/取消三种完成方式）——future 完成即代表这个任务已经离开
      "受理中+执行中"窗口，不依赖 task_status 表的某次终态写入是否
      恰好成功，比挂在 update_task_status 上更贴近事实、覆盖面更广。
      "llm" 桶的这个挂点对 try_register（/api/recalibrate 准入）和
      register_internal（transcription worker 内部交接）两种登记来源
      一视同仁——释放只认 kind+task_id，不区分登记时走的是哪个方法。
    - 队列拒绝路径：HTTP 路由自己在 QueueFull/queue.Full 分支里显式
      release（此时任务从未被提交给任何 worker，future 永远不会存在）。
    - 提交失败路径：消费者的 executor.submit() 自身抛异常时，消费者显式
      release（future 同样从未真正创建）——llm_ops.process_llm_queue 的
      这条路径此前对内部交接来源的 task_id 是空操作（未登记过，release
      幂等地静默忽略），register_internal 上线后自然生效为真正的释放。
    - 建库失败路径：HTTP 路由在 try_register 成功之后、任务行落库
      （create_task / recalibrate 的 INSERT）本身失败时显式 release。

    幂等：try_register/register_internal 对已登记的 task_id 直接返回
    （不重复占位、不报错）；release 对未登记的 task_id 或未知 kind 静默
    忽略，不抛错——上面这几条路径可能重复触达同一个 task_id，调用方不
    需要先自行判断"是否真的登记过"。
    """

    def __init__(self, capacities: Dict[str, int]):
        self._capacities = dict(capacities)
        self._entries: Dict[str, Dict[str, float]] = {
            kind: {} for kind in self._capacities
        }
        self._guard = threading.Lock()

    def try_register(self, kind: str, task_id: str) -> bool:
        """尝试为 task_id 在 kind 桶里占一个名额。

        容量已满返回 False（调用方应当以此判定 503，不落库/不入队）；
        成功占位或 task_id 已经登记过（幂等）返回 True。
        """
        with self._guard:
            bucket = self._entries[kind]
            if task_id in bucket:
                return True
            if len(bucket) >= self._capacities[kind]:
                return False
            bucket[task_id] = time.monotonic()
            return True

    def register_internal(self, kind: str, task_id: str) -> None:
        """无条件登记 task_id 到 kind 桶，不检查容量、不会失败（本地 codex
        review 第 13 轮唯一发现）：供进程内部"任务从一条流水线交接到另一
        条"场景使用，当前唯一调用方是 transcription.py 五处
        llm_task_queue.put() 前的 "transcription"->"llm" 交接。

        与 try_register 的语义分工：
        - try_register 是 HTTP 准入点专用的"是否接受新工作"决策——容量
          已满必须能够拒绝（返回 False，调用方据此判定 503，任务行不落
          库/不入队）。
        - register_internal 用于内部交接：调用时这份工作已经被上游接受、
          正在进行（transcription worker 已经完成下载+转录，即将把结果
          转交给 LLM 阶段），不存在"可以拒绝"这个选项——拒绝也无法撤销
          已经做完的转录工作，只会让任务在两个桶之间产生一段"哪个桶都
          不认领"的真空窗口（第 13 轮发现：这段真空期内若任务恰好超过
          运行期对账的宽限期，会被误判为孤儿写成 failed）。因此这里
          无条件登记、不检查 capacities，且幂等（重复调用同一 task_id
          直接返回，不重复计数、不报错）——调用方不需要、也不允许因为
          这一步失败而中断交接。

        瞬时上界依然成立（不会因为这里跳过容量检查而失控——真正的容量
        钳制留在两个 HTTP 准入点）（第 14 轮把这里原先的"数学上界"
        订正为"瞬时上界"，理由见下方新增段落）："llm" 桶里"仍在尝试
        交接"的登记数
        ≤ recalibrate 经 try_register 的准入量（硬上限 LLM_QUEUE_MAXSIZE）
        + transcription worker 经 register_internal 的内部交接量。后者
        的上界是 "transcription" 桶自身的容量（concurrent.queue_size）：
        调用 register_internal("llm", task_id) 的唯一时机是该 task_id
        仍在 "transcription" 桶里占着名额（调用方——转录 worker 线程
        本身——运行在 process_transcription 内部，其 future 只有在
        put() 完成、函数返回之后才会完成，"transcription" 名额届时才
        释放；见 transcription.py 调用处的注释），也就是说同一时刻能够
        执行这次内部交接的 task_id 数量，天然被 "transcription" 桶当前
        的占用数上限钳制住，不需要 register_internal 自己重复设限。

        队列满时内部 put() 的阻塞行为是这个瞬时上界成立的关键一环：
        llm_task_queue.put() 在队列已满（LLM_QUEUE_MAXSIZE）时会阻塞
        （而不是像 try_nowait 那样立即失败），阻塞期间该 worker 线程
        没有返回、"transcription" 名额没有释放——背压因此原样传导回
        "transcription" 桶（进而传导到 /api/transcribe 的 try_register
        准入拒绝），不需要 register_internal 参与阻塞或拒绝。

        但这个上界只在"该 task_id 仍在尝试交接"这一瞬间成立，不构成
        "持续"（sustained）意义上的上界（第 14 轮发现，订正上面这段
        此前的过度引申）：register_internal 登记、put() 都成功返回之后，
        "transcription" 名额立刻释放，但这个 task_id 在 "llm" 桶里的
        登记条目并不会同步释放——它要一直存活到对应的 LLM future 真正
        完成（release 挂在 track_future 的完成回调上，见类文档"释放
        挂点"一节）。转录耗时通常远短于 LLM 校对/总结耗时，持续过载
        下 "transcription" 桶会以远高于 LLM 完成速度的频率周转出新的
        distinct task_id，每次周转都在 "llm" 桶里留下一个存活期更长
        的条目——只看 register_internal 本身，"llm" 桶的持续总量并没
        有上界，且这张登记表本身也不适合用来堵这个口子：size("llm")
        同时统计"还在排队、消费者还没碰过"和"已经 submit、还没完成"
        两类条目，拿它与容量比较来决定要不要暂停 submit，在持续到达
        的交接把 size 推过容量、而消费者一个 future 都还没提交过时会
        死锁（第 14 轮实测复现：谁都无法完成、无法释放名额，闸门永久
        打不开）。真正堵住这个口子的是消费侧 process_llm_queue 新增的
        RuntimeContext.llm_submit_semaphore——只统计"已经 submit、
        尚未完成"这一类，与登记表的排队总量彻底分开计数，从空载状态
        起步、不依赖任何已有条目先完成，因此不会重现上面这个死锁。
        组合起来，"llm" 桶的持续总量才有一个真正意义上的上界，详见
        process_llm_queue 的 docstring 与本类文档"llm"分支末尾。

        Args:
            kind: 桶名（当前只有内部交接用到 "llm"）。
            task_id: 待登记的任务 ID，幂等——已登记过直接返回。
        """
        with self._guard:
            bucket = self._entries[kind]
            bucket.setdefault(task_id, time.monotonic())

    def release(self, kind: str, task_id: str) -> None:
        """注销 task_id 在 kind 桶里的登记，归还一个名额。

        幂等：kind 未知或 task_id 未登记时静默忽略，不抛错。
        """
        with self._guard:
            bucket = self._entries.get(kind)
            if bucket is not None:
                bucket.pop(task_id, None)

    def all_task_ids(self) -> set:
        """返回当前所有 kind 登记的 task_id 并集快照，供运行期对账
        （CacheManager.reconcile_runtime_orphaned_tasks）用作排除名单——
        只要任务仍在这张表里，不论运行多久都不能被对账判定为孤儿。"""
        with self._guard:
            return {
                task_id
                for bucket in self._entries.values()
                for task_id in bucket
            }

    def size(self, kind: str) -> int:
        """返回某个 kind 当前登记的任务数，供测试/可观测性使用。"""
        with self._guard:
            return len(self._entries.get(kind, {}))


class RuntimeContext:
    """Resources owned by one FastAPI lifespan."""

    def __init__(self, config: dict):
        self.config = validate_config(config)
        self.logger = setup_logger("api_server", config=config, bootstrap=False)
        self.started = False
        self.closed = False
        self.resources_safe: bool | None = None
        # _stop_workers 在每次调用时都会重新赋值；默认 True 只覆盖
        # "_stop_workers 从未被调用过就直接调 _finish_close" 这类边缘路径
        # （目前没有真实调用方这样做，纯防御，见 K2 修复）。
        self._maintenance_confirmed_stopped: bool = True
        self.background_tasks: list[asyncio.Task] = []
        self.llm_thread: threading.Thread | None = None
        self.llm_stop_event = threading.Event()
        self.worker_futures: set[tuple[str, concurrent.futures.Future]] = set()
        self._worker_futures_condition = threading.Condition()
        # 进程内在途任务登记表（本地 codex review 第 12 轮 P1）：容量取自
        # 本次已校验通过的 config，与 worker_futures/track_future 同属
        # "受理中+执行中"追踪机制的一部分，紧邻声明。详见
        # _InflightTaskRegistry 类文档。
        self.inflight_registry = _InflightTaskRegistry(
            {
                "transcription": self.config["concurrent"]["queue_size"],
                "llm": LLM_QUEUE_MAXSIZE,
            }
        )
        # LLM 消费泵容量闸门（本地 codex review 第 14 轮，补第 12 轮验收
        # 标准的遗漏项——见 llm_ops.process_llm_queue 的 docstring 与
        # register_internal 文档"数学上界"一节的订正）：process_llm_queue
        # 出队后、submit 给 llm_executor 前必须先 acquire 这个信号量，
        # future 完成时（track_future 的完成回调）release 归还——只统计
        # "已经 submit、尚未完成"这一类工作，容量取 LLM_QUEUE_MAXSIZE
        # （与 inflight_registry 的 "llm" 桶同一个数字，呼应审查原文
        # "计入同一个明确容量限制"的措辞，不引入第二个需要手动保持同步的
        # 容量数字）。
        #
        # 刻意不用 inflight_registry.size("llm") 与配置容量比较来做这件
        # 事：那张登记表统计的是"受理中+执行中"总量，同时包含"还在排队、
        # 消费者还没出队处理过"和"已经 submit、还没完成"两类条目，语义
        # 上比这里需要的"已提交未完成"更宽。第 14 轮实测证明拿它做闸门
        # 会死锁——持续到达的 register_internal 交接可以在消费者提交出
        # 第一个 future 之前就把登记总量推过容量，此时没有任何 future
        # 存在，谁都无法完成、无法释放名额，闸门永久打不开。信号量从
        # "空载"状态起步，只在真正调用 submit() 前后 acquire/release，
        # 不看登记表当前积压了多少，因此第一次 submit 永远能立刻成功，
        # 不存在这个死锁窗口。
        self.llm_submit_semaphore = threading.BoundedSemaphore(LLM_QUEUE_MAXSIZE)
        self.asr_monitor = None
        self.maintenance_executor: concurrent.futures.ThreadPoolExecutor | None = None
        # 启动恢复失败重试标志（本地 codex review 第 6 轮 G3）：app.py::
        # startup_event() 里 recover_orphaned_tasks() 若异常，只记日志继续
        # 启动，不再重试——遗留的非终态任务会在服务正常运行期永久悬挂。这里
        # 置位后，_periodic_maintenance 每轮维护会检查该标志，仅当置位时才
        # 重试一次恢复，成功后清除；不能把 recover_orphaned_tasks 直接无条件
        # 加进周期维护——那样会把本进程当前正在处理的 queued/processing/
        # calibrating 任务也标记 failed，误杀活任务（见 recover_orphaned_
        # tasks 的 cutoff 参数说明）。
        self.recovery_pending: bool = False
        # 进程启动时刻（UTC）——仅作为通用的进程元信息保留（如未来的运行
        # 时长上报），本身不再驱动任何恢复判定。在 start() 里赋值（而不是
        # __init__），与"进程真正接入请求处理"的时刻对齐。
        self.started_at: datetime.datetime | None = None
        # 启动恢复重试的判定边界（本地 codex review 第 7 轮 H4 曾改为 rowid
        # 水位线语义；CI review 第 5 轮 P1 发现该方案在非 AUTOINCREMENT 的
        # task_status 表上会被 rowid 复用打破——删除当前持有最大 rowid 的
        # 行后，下一次插入会复用那个 rowid，可能让启动之后才创建的新任务
        # 落回水位线以下、被恢复重试误杀写成 failed）：改为进程启动时刻
        # 拍下的 task_id 快照（当时仍处于 queued/processing/calibrating 的
        # 全部任务），供 recovery_pending 置位后的周期维护重试传给
        # recover_orphaned_tasks(restrict_to_task_ids=...)。这份集合只在
        # 这一刻拍一次、此后永远不会再增长，本进程后来受理的新任务无论
        # rowid 如何变化都不可能出现在里面，天然免疫 rowid 复用（见
        # CacheManager.get_non_terminal_task_ids 的详细说明）。在 start()
        # 里赋值——拍摄快照需要 cache_manager 已经建好、DB 可查询。
        self.startup_recovery_task_ids: frozenset[str] | None = None
        # 终态写入待补偿登记（K1 桶 b，CI review 第 3 轮 major）：
        # llm_ops.process_llm_queue 的提交失败分支写 FAILED 终态时，如果
        # 这次写入本身也抛异常（如 DB 瞬时不可用），此前只记 ERROR 日志，
        # 任务永久卡在非终态、无人再碰——运行期对账
        # （reconcile_runtime_orphaned_tasks）虽然最终能兜底，但那是按
        # created_at 宽限期猜测出来的，不是针对这次已知失败的显式补偿。
        # 这里改为显式登记 task_id，_periodic_maintenance 每轮维护
        # （app.py，见 context._retry_terminal_write_pending）drain 这个
        # 集合，对每个 id 重试写 FAILED，成功则移除、失败保留到下一轮——
        # 有界、可观察，不依赖宽限期猜测。这是本集合唯一的主动补偿通道；
        # 关闭清算路径（_drain_non_terminal_tasks_on_shutdown）不再单独
        # drain 它——能进入这个集合的 task_id，DB 行必然仍是非终态（写入
        # 本身失败才会走到登记这一步），已经被同一次关闭清算前段的
        # drain_non_terminal_tasks_on_shutdown 覆盖处理，重复 drain 纯属
        # 冗余且会绕开该阶段的有界预算（PR3 review hardening，详见
        # _drain_non_terminal_tasks_on_shutdown 内联注释）。
        #
        # 集合本身很小（预期只在"提交失败 + 终态写入也失败"这种双重
        # 故障下才会有条目），register/drain 两个方法足够，不需要更复杂
        # 的数据结构或独立模块。
        self.terminal_write_pending: set[str] = set()
        self._terminal_write_pending_guard = threading.Lock()

    def start(self) -> None:
        if self.started:
            return
        self.started_at = datetime.datetime.now(datetime.timezone.utc)
        # Imports stay here so importing routes never creates databases, queues,
        # executors, or LLM clients.
        from ..cache import CacheManager
        from ..llm import LLMCoordinator
        from ..utils.accounts.user_manager import UserManager
        from ..utils.logging.audit_logger import AuditLogger
        from ..utils.logging.usage_recorder import UsageRecorder
        from ..utils.tempfile_manager import get_shared_temp_manager

        storage = self.config["storage"]
        concurrency_config = self.config["concurrent"]
        self.user_manager = UserManager(fallback_config=self.config)
        self.cache_manager = CacheManager(storage["cache_dir"])
        self.startup_recovery_task_ids = (
            self.cache_manager.get_non_terminal_task_ids()
        )
        audit_path = storage.get("audit_db")
        self.audit_logger = AuditLogger(audit_path) if audit_path else AuditLogger()
        self.cache_manager.audit_logger = self.audit_logger
        self.usage_recorder = UsageRecorder(self.audit_logger)
        self.temp_manager = get_shared_temp_manager()
        self.workspace_dir = storage["workspace_dir"]
        self.llm_coordinator = LLMCoordinator(
            config_dict=self.config,
            cache_dir=storage["cache_dir"],
            media_cache_manager=self.cache_manager,
        )
        self.task_queue = asyncio.Queue(concurrency_config["queue_size"])
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency_config["max_workers"], thread_name_prefix="transcription"
        )
        self.llm_queue = queue.Queue(maxsize=LLM_QUEUE_MAXSIZE)
        self.llm_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency_config["llm_max_workers"], thread_name_prefix="llm"
        )
        # 单线程即可：_periodic_maintenance 的每轮维护调用都是顺序 await，同一
        # 时刻只会有一个阻塞调用在跑。见 run_maintenance 的说明。
        self.maintenance_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="maintenance"
        )
        self.started = True

    def track_future(
        self, future: concurrent.futures.Future, *, kind: str = "transcription",
        task_id: str | None = None,
    ) -> None:
        """Track a worker so shutdown does not close resources underneath it.

        task_id（可选，本地 codex review 第 12 轮 P1）：提供时，future 完成
        （成功/异常/取消，add_done_callback 的三种触发条件都算）会额外从
        inflight_registry 里注销这个 task_id——"终态即注销"的主挂点：
        future 完成即代表这个任务已经离开"受理中+执行中"窗口，不论它
        最终把 task_status 行写成 success 还是 failed，配额都应立即归还
        给下一个请求，不需要等终态写入本身成功。task_id 为 None（如
        kind="maintenance" 的调用方）时不触发 release 调用——
        inflight_registry.release 本身也是幂等的，这里提前判断只是避免
        无意义调用。

        kind="llm" 时，future 完成还会额外 release 一次
        llm_submit_semaphore（本地 codex review 第 14 轮）：这是
        process_llm_queue 在 submit() 之前 acquire 的同一个信号量，见
        __init__ 里 llm_submit_semaphore 的注释与 process_llm_queue 的
        docstring。当前整个代码库里唯一以 kind="llm" 调用 track_future
        的地方就是 process_llm_queue 自己的 submit 调用（每次成功 submit
        都对应恰好一次 acquire），所以这里按 kind 分支释放不会误伤其他
        调用方——不像 inflight_registry.release 是无条件幂等 no-op，
        BoundedSemaphore.release() 超过 acquire 次数会抛 ValueError，
        因此没有采用"顺手对所有 kind 都 release"这种防御性写法。

        H1（增量复核）：完成回调此前只做"移除 worker_futures + 释放配额/
        信号量"，从不消费 done_future.exception()——worker 内部某个终态写库
        失败重新抛出（例如 llm_ops._handle_llm_task 的 FAILED 收口，见其
        docstring）后，异常确实到达了这个 future，但从这里往上没有任何
        调用方在 await/result() 它（worker 提交给线程池后即"发射后不管"），
        异常因此被静默滞留，可观察性没有真正闭环。现在显式调用
        exception() 并在非 None 时记一条 ERROR 日志（含 kind/task_id/异常
        repr），把它变成日志系统里可检索的信号，不再依赖运行期对账兜底
        发现。cancelled 的 future 需要先用 cancelled() 排除掉——
        Future.exception() 在 CANCELLED 状态下会抛 CancelledError 而不是
        返回 None，属于取消语义的正常表现，不是"未消费的真实异常"，不应
        当成错误记录。这里只记日志、不重新抛出：done callback 在
        Future 的内部回调线程里同步执行，从这里抛出的异常不会传播到任何
        调用方，只会被 concurrent.futures 自己 log 一条"exception calling
        callback"噪音，对可观察性没有增益。

        J3（本地增量复核第 3 轮）：H1 加上的异常消费+记日志，落地顺序却
        排在"移除 worker_futures + notify_all"之后——aclose() 的关闭清算
        （_stop_workers）正是靠 wait_for(...) 在 _worker_futures_condition
        上等这个 notify_all 才继续往下走到 shutdown_logger()（关闭日志
        sink）。如果这条 worker future 恰好带着异常在关闭窗口完成，旧顺序
        会先唤醒关闭流程、日志系统随时可能已经被关掉，H1 刚加的 ERROR 才
        姗姗来迟地尝试写入——日志有丢失窗口，可观察性又出现新缺口。现在
        重排为 try/finally：try 块里先消费异常并记 ERROR 日志（cancelled
        判断不变）；finally 块里再释放 inflight 配额/llm_submit_semaphore、
        从 worker_futures 移除、notify_all——保证任何等待关闭完成的调用方
        在被唤醒之前，这条 future 的异常已经确实记完日志；用 finally 而不
        是顺序 try 之后再写，是为了防止 logger.error 本身抛出异常时反而
        漏掉释放配额/漏掉唤醒等待者，造成新的资源泄漏或关闭流程卡死。
        """
        with self._worker_futures_condition:
            self.worker_futures.add((kind, future))

        def discard(done_future):
            try:
                if not done_future.cancelled():
                    exc = done_future.exception()
                    if exc is not None:
                        self.logger.error(
                            f"worker future 异常终止且未被上层消费: kind={kind} "
                            f"task_id={task_id} exc={exc!r}"
                        )
            finally:
                if task_id is not None:
                    self.inflight_registry.release(kind, task_id)
                if kind == "llm":
                    self.llm_submit_semaphore.release()
                with self._worker_futures_condition:
                    self.worker_futures.discard((kind, done_future))
                    self._worker_futures_condition.notify_all()

        future.add_done_callback(discard)

    def register_terminal_write_pending(self, task_id: str) -> None:
        """登记一个终态写入待补偿的 task_id（线程安全，K1 桶 b）。

        唯一调用方是 llm_ops.process_llm_queue 的提交失败分支：出队后
        submit() 失败、随即尝试写 FAILED 终态本身也失败时，在
        llm_task_queue.task_done() 之后调用（见该函数 docstring）——
        task_done() 影响的是队列自身的记账（unfinished_tasks），与这里
        的终态补偿登记是两件独立的事，不应该互相牵连。幂等：重复登记
        同一个 task_id 只是 set 的 no-op。
        """
        with self._terminal_write_pending_guard:
            self.terminal_write_pending.add(task_id)

    def drain_terminal_write_pending(self) -> set[str]:
        """取出并清空当前登记的全部 task_id（线程安全），供
        app.py::_periodic_maintenance 每轮维护重试——这是本集合唯一的
        主动消费方（关闭清算路径不再调用，PR3 review hardening：见
        RuntimeContext._drain_non_terminal_tasks_on_shutdown 内联注释的
        冗余性论证）。

        Returns:
            set[str]: 本次取出的 task_id 快照；调用方对其中重试仍失败的
            id，应重新调用 register_terminal_write_pending 登记回去，
            留给下一次触达此方法的时机继续重试。
        """
        with self._terminal_write_pending_guard:
            drained = set(self.terminal_write_pending)
            self.terminal_write_pending.clear()
        return drained

    async def run_maintenance(self, func, *args, **kwargs):
        """在专属单线程池提交一次阻塞维护调用（如 repair_task_snapshots /
        cleanup_old_cache），并像 transcription/llm worker 一样纳入
        worker_futures 追踪，供 aclose() 的关闭清算在真正安全时才继续。

        根治动机（本地 Codex review，核实为真的竞态）：_periodic_maintenance
        此前直接用裸 `asyncio.to_thread(...)` 跑这类阻塞调用。裸
        to_thread/run_in_executor 返回的是包了一层的 asyncio.Future：取消
        这层包装 Future 会立刻把它标记为 CANCELLED——asyncio.Future.cancel()
        对处于 PENDING 状态的 Future 总是成功，并不检查"底层真实工作是否
        还在跑"，这与 concurrent.futures.Future.cancel()"运行中返回 False"
        的语义完全不同。于是 aclose() 取消 background_tasks 后
        `await asyncio.gather(...)` 几乎立刻返回，即使
        repair_task_snapshots 之类的阻塞 DB 调用仍在其执行器线程里真实运行
        ——随后 `_stop_workers`/`_finish_close` 的"关闭清算"
        （_drain_non_terminal_tasks_on_shutdown）会与它并发，可能踩中同一批
        任务行（用一段可复现实验证实：见
        test_aclose_waits_for_in_flight_maintenance_work_before_draining）。

        修复：不再依赖"取消 async 包装层"来判断阻塞调用是否结束，而是复用
        transcription/llm 两个 worker 已经在用的同一套机制——把真正执行阻塞
        调用的 concurrent.futures.Future 交给 track_future 追踪。
        _stop_workers 通过 wait_for 等待 worker_futures 里对应 kind 的条目被
        它自己的 add_done_callback 摘除，这个回调只在底层线程真正跑完时才
        触发，不受 asyncio 取消语义影响，因此能在关闭清算之前提供真实的
        "已经跑完"保证。

        Args:
            func: 阻塞可调用对象（如 CacheManager.cleanup_old_cache）。
            *args/**kwargs: 透传给 func。

        Returns:
            func 的返回值。
        """
        executor = self.maintenance_executor
        if executor is None:
            # start() 未运行的场景兜底（如直接构造 RuntimeContext 的单测）：
            # 没有专属线程池可提交，直接同步跑——这类场景本就不构成真实的
            # 并发关闭窗口。
            return func(*args, **kwargs)
        future = executor.submit(func, *args, **kwargs)
        self.track_future(future, kind="maintenance")
        return await asyncio.wrap_future(future)

    def _new_shutdown_deadline(self) -> float:
        """本次关闭调用的绝对预算截止点（monotonic 时钟，本地 codex
        review 第 11 轮 N1）。aclose()/close() 各自在入口只调用一次，作为
        整条关闭链路（_stop_workers 的三段 wait_for、_shutdown_llm_owner
        的 join、_drain_non_terminal_tasks_on_shutdown 的清算循环）共用的
        同一份预算基准——预算因此在这些阶段之间累计消耗，不会每进入一个
        阶段就重新充满。详见 WORKER_STOP_TIMEOUT_SECONDS 上方的说明。
        """
        return time.monotonic() + WORKER_STOP_TIMEOUT_SECONDS

    def _remaining_shutdown_budget(self, deadline: float) -> float:
        """deadline 相对当前 monotonic 时间的剩余可用秒数，钳制到 >= 0。

        各阶段直接把这个值当 timeout 传给 wait_for/join：预算耗尽时传 0，
        Condition.wait_for/Thread.join 在 timeout=0 时会立即检查一次真实
        状态并返回、不做无谓阻塞——不需要在每个调用点重复写
        `if remaining <= 0: skip` 分支就能实现"预算耗尽后续阶段快速
        跳过"，且返回值仍然反映真实状态（不是硬编码为失败/未确认）。
        """
        return max(0.0, deadline - time.monotonic())

    def new_shutdown_deadline(self) -> float:
        """_new_shutdown_deadline 的公开入口（本地 codex review 第 16 轮
        Q4）。

        lifespan 的关闭起点（app.py::_close_runtime_in_order）需要在调用
        aclose() 之前，先用同一份 deadline 给 shutdown_event() 里原本
        无界的临时目录清扫计时——预算必须从 lifespan 的关闭入口统一起算，
        而不是等到 aclose() 内部才第一次创建，否则清扫这一段仍然不受
        WORKER_STOP_TIMEOUT_SECONDS 约束。内部逻辑与 _new_shutdown_deadline
        完全一致，只是作为公开方法暴露给 context.py 之外的调用方；按照
        该方法与 WORKER_STOP_TIMEOUT_SECONDS 上方注释描述的"预算跨阶段
        累计、不重新充满"原则，调用方应在每一次真实关闭序列的最开头只
        调用一次，并把结果一路透传下去（包括传给 aclose(deadline=...)），
        不要重复调用。
        """
        return self._new_shutdown_deadline()

    async def aclose(self, deadline: float | None = None) -> bool:
        """Cancel and await async owners, then perform bounded blocking cleanup.

        Args:
            deadline: 外部传入的统一关闭预算截止点（本地 codex review 第
                16 轮 Q4）。lifespan 的关闭入口
                （app.py::_close_runtime_in_order）会在调用这里之前，先用
                同一个 deadline（由 new_shutdown_deadline() 算出）给
                shutdown_event() 里的临时目录清扫计时——预算必须贯穿到
                aclose()，不能让 aclose() 自己另起一份全新预算，否则总
                关闭耗时会变成"清扫预算 + aclose 自己的
                WORKER_STOP_TIMEOUT_SECONDS"两段相加，重新踩中下方
                WORKER_STOP_TIMEOUT_SECONDS 说明里"各阶段独立预算累加"
                那类问题，只是换了个发生位置。None（默认）时退化为旧
                行为——自己现算一份新 deadline，等价于把整份预算单独给
                这次调用，供不经 _close_runtime_in_order 直接调用
                aclose() 的场景（如既有单测）使用，不改变它们的既有
                行为。

        对 background_tasks 的 cancel+gather 只保证 asyncio 层面的任务对象
        进入完成状态，不保证它们通过 run_maintenance 提交给
        maintenance_executor 的阻塞调用已经真正跑完（两者是有意分开的两层
        保证）——真实的"已跑完"由 `_stop_workers` 对 worker_futures 的
        wait_for 提供，严格发生在 `_finish_close` 的关闭清算之前。见
        run_maintenance 与 _stop_workers 的说明。

        deadline 只在这里计算一次（本地 codex review 第 11 轮 N1），随后
        贯穿传给 _stop_workers 与 _finish_close，让二者共用同一份关闭
        预算——见 WORKER_STOP_TIMEOUT_SECONDS 上方的详细说明。

        deadline 的创建时机移到取消动作之前、且改用有界的 asyncio.wait
        （而非 gather 包 wait_for，本地 codex review 第 12 轮 P2 发现
        d）：此前 deadline 在 cancel+gather 完全跑完之后才计算——gather
        本身对 background_tasks 的完成没有任何超时保护，一个迟迟不响应
        取消的后台任务会让这一步无界悬挂，"aclose 在单份预算内有界返回"
        这条对外承诺在这一步就已经失效，deadline 甚至还没来得及存在。

        用 asyncio.wait(tasks, timeout=...) 而不是
        asyncio.wait_for(gather(...), timeout=...)：本地实测验证过
        （构造一个"取消后需要比预算更久才能真正停下"的任务）两者行为并不
        等价——wait_for 包 gather 在超时触发后，其内部清理逻辑会继续
        await 被取消的 gather() 直到它真正解决，等待时长等于任务实际响应
        取消所需的时间，而不是传入的 timeout；等价于沿用了 gather 本身
        "等到所有子任务真正完成"的语义，只是把 TimeoutError 的抛出时机
        错误地推迟到了那一刻，并没有真正提供有界保证。asyncio.wait 则会
        在 timeout 到达时如实返回 (done, pending) 两个集合，不等待
        pending 里的任务真正完成——这才是这里需要的"最多等这么久，之后
        无论如何都继续走后续步骤"语义。

        超时（即返回时 pending 非空）不重新抛出，而是继续走后续的
        _stop_workers/_finish_close（用此刻已经耗尽大半的剩余预算，两者
        各自的 wait_for(timeout=0) 语义已经能安全处理"预算耗尽"这一
        情况，见 _remaining_shutdown_budget 的说明），并把这次超时如实
        计入 resources_safe——一个未确认完成取消的后台任务（可能仍在
        运行、仍在访问即将被关闭的资源）本身就构成不安全条件，
        _stop_workers 的 worker_futures 检查覆盖不到这类 asyncio 层面
        （而非线程池 future 层面）的残留。
        """
        if self.closed:
            return self.resources_safe is not False
        if deadline is None:
            deadline = self._new_shutdown_deadline()
        for task in self.background_tasks:
            task.cancel()
        background_tasks_settled = True
        if self.background_tasks:
            _done, pending = await asyncio.wait(
                self.background_tasks,
                timeout=self._remaining_shutdown_budget(deadline),
            )
            if pending:
                background_tasks_settled = False
                self.logger.error(
                    "%d 个后台任务未能在关闭预算内响应取消，继续走后续关闭"
                    "步骤，本次关闭结果如实标记为不安全",
                    len(pending),
                )
        resources_safe = await self._stop_workers_off_shared_pool(deadline)
        if not background_tasks_settled:
            resources_safe = False
        self._finish_close(resources_safe, deadline)
        return resources_safe

    async def _stop_workers_off_shared_pool(self, deadline: float) -> bool:
        """在专用、即用即弃的单线程执行器里运行 _stop_workers，不借道进程
        级共享默认线程池（本地 codex review 第 16 轮 Q5）。

        此前用 `asyncio.to_thread(self._stop_workers, deadline)` 提交——
        `asyncio.to_thread`/`loop.run_in_executor(None, ...)` 都提交给
        同一个进程级共享的默认线程池。一旦业务侧其他阻塞调用（例如
        views.py::_prepare_success_view 的同步文件读取 + Markdown 渲染）
        恰好把这个共享池占满，_stop_workers 的提交会在池的内部队列里排队
        等待空闲线程——deadline 只在 _stop_workers 真正开始执行之后才
        生效，排队等待期完全不受这份预算约束，形同虚设。

        改为每次调用现建一个 max_workers=1 的专用 ThreadPoolExecutor：
        提交是立即的（这个专用池只服务这一个任务，不可能被业务侧其它
        提交占满），再用同一份 deadline 的剩余预算有界等待结果——排队与
        执行都由同一预算约束。超时（asyncio.wait 返回 pending）不重新
        抛出、也不强行中断底层线程（_stop_workers 是同步阻塞函数，无法
        从外部安全地中途取消），只是不再等待它、如实按既有语义把结果
        标记为不安全——与上面 aclose() 对 background_tasks 超时"不重新
        抛出、只标记不安全"的既有处理方式保持一致。用后即弃：
        shutdown(wait=False) 不阻塞当前协程，专用池对象在函数返回后即被
        丢弃，不留存、不复用、不影响任何其它调用；被放弃等待的
        _stop_workers 仍会在这个专用线程里自然运行完（它自身的三段
        wait_for 同样受 deadline 约束，很快就会跟着耗尽预算返回），只是
        没有协程再等它。
        """
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(self._stop_workers, deadline)
            wrapped = asyncio.wrap_future(future)
            _done, pending = await asyncio.wait(
                {wrapped}, timeout=self._remaining_shutdown_budget(deadline)
            )
            if pending:
                self.logger.error(
                    "_stop_workers 未能在关闭预算内确认完成（专用线程可能"
                    "仍在后台运行，不再等待），本次关闭结果如实标记为不安全"
                )
                # L4 修复（CI review 第 5 轮 P1）：显式把 _maintenance_
                # confirmed_stopped 置 False，不能依赖后台线程里仍在跑的
                # _stop_workers 自己稍后去设置这个标志——它会（_stop_workers
                # 内部无条件设置），但时机不确定，很可能晚于下面
                # _finish_close 的调用。不显式置 False 的话，_finish_close
                # 会读到 __init__ 里预置的默认值 True（getattr(self,
                # "_maintenance_confirmed_stopped", True)），误判维护线程
                # 已确认停止，对着仍在后台线程里真实执行的维护调用（如
                # repair_task_snapshots）并发跑关闭清算
                # （_drain_non_terminal_tasks_on_shutdown），与其共享同一份
                # DB 连接——K2/G2 两轮修复要堵的正是这个竞态，这条"外层放弃
                # 等待"的路径此前恰好被漏掉。置为 False 后，_finish_close
                # 会整体跳过清算，未清算的任务留给下一次启动的孤儿恢复兜底
                # （语义已存在，见 CacheManager.recover_orphaned_tasks）。
                self._maintenance_confirmed_stopped = False
                return False
            return wrapped.result()
        finally:
            executor.shutdown(wait=False)

    def close(self) -> bool:
        """close() 是 aclose() 的同步对应版本（无 background_tasks 的
        cancel+gather 前置步骤），deadline 计算与传递方式相同（本地 codex
        review 第 11 轮 N1）。"""
        if self.closed:
            return self.resources_safe is not False
        for task in self.background_tasks:
            task.cancel()
        deadline = self._new_shutdown_deadline()
        resources_safe = self._stop_workers(deadline)
        self._finish_close(resources_safe, deadline)
        return resources_safe

    def _stop_workers(self, deadline: float | None = None) -> bool:
        """Stop thread owners within a bounded wait; return resource safety.

        Args:
            deadline: 本次关闭调用的统一预算截止点（本地 codex review
                第 11 轮 N1，见 WORKER_STOP_TIMEOUT_SECONDS 的说明）。
                aclose()/close() 总是显式传入，三段 wait_for 各自只消费
                _remaining_shutdown_budget(deadline) 算出的剩余预算，跨
                阶段累计消耗、不重新充满。None（默认）只服务于绕开
                aclose()/close() 直接调用本方法的单测，退化为"这一次调用
                现算一个新 deadline"，等价于把整份预算单独给这一次调用。
        """
        if deadline is None:
            deadline = self._new_shutdown_deadline()
        transcription_executor = getattr(self, "executor", None)
        if transcription_executor is not None:
            transcription_executor.shutdown(wait=False, cancel_futures=True)
        with self._worker_futures_condition:
            producers_finished = self._worker_futures_condition.wait_for(
                lambda: not any(
                    kind == "transcription" for kind, _ in self.worker_futures
                ),
                timeout=self._remaining_shutdown_budget(deadline),
            )

        # K2 修复（本地 codex review 第 8 轮）：maintenance executor 的
        # shutdown + 有界等待此前只在 producers_finished=True 时才执行——
        # producer 超时的分支会在这里直接 return False，跳过下面这一步。
        # 但 _finish_close 的关闭清算（_drain_non_terminal_tasks_on_shutdown）
        # 是无条件调用的（G2 修复），于是"清算必须等维护调用真正跑完再动手"
        # 这条不变式在 producer 超时路径上完全失效——清算可能与仍在线程里
        # 真实执行的维护调用（如 repair_task_snapshots）并发，重新踩中
        # run_maintenance 文档描述的那个竞态，只是触发条件换成了"producer
        # 也同时超时"。
        #
        # 新增的维护提交只会来自 _periodic_maintenance 这个 background
        # task，而 aclose()/close() 在调用 _stop_workers 之前已经
        # cancel + gather 过全部 background_tasks，此刻不会再有新的维护
        # 提交——无论 producer 是否超时，下面这段 shutdown + 有界等待都可以
        # 安全执行，不需要以 producers_finished 为前提。cancel_futures=True
        # 只丢弃尚未开始跑的排队任务；若此刻正好有一次维护调用已经在线程里
        # 真实执行，下面的 wait_for 会等它借由 track_future 的
        # add_done_callback 真正跑完再继续——这是堵住"关闭清算与仍在写 DB
        # 的维护调用并发"竞态的关键一步，不能省略（见 run_maintenance 的
        # 说明）。
        maintenance_executor = getattr(self, "maintenance_executor", None)
        if maintenance_executor is not None:
            maintenance_executor.shutdown(wait=False, cancel_futures=True)
        with self._worker_futures_condition:
            maintenance_finished = self._worker_futures_condition.wait_for(
                lambda: not any(
                    kind == "maintenance" for kind, _ in self.worker_futures
                ),
                timeout=self._remaining_shutdown_budget(deadline),
            )
        # _finish_close 用这个信号判断关闭清算是否能安全执行——维护调用
        # 未确认停止时，清算必须跳过，留给下次启动的孤儿恢复兜底（语义见
        # _drain_non_terminal_tasks_on_shutdown），不能只看整体
        # resources_safe：producer 超时时 resources_safe 也会是 False，但
        # maintenance 阶段现在无论 producer 是否超时都会跑完，这个信号比
        # resources_safe 更精确。上面的 wait_for 在剩余预算已耗尽
        # （timeout=0）时会立即检查一次真实状态后返回，这个信号如实反映
        # 那一次即时检查的结果，不会被硬编码为"未确认"（本地 codex review
        # 第 11 轮 N1，见 WORKER_STOP_TIMEOUT_SECONDS 的说明）。
        self._maintenance_confirmed_stopped = maintenance_finished

        if not producers_finished:
            # A timed-out producer may still enqueue its final LLM work. Keep
            # the daemon consumer and notification clients available until
            # process exit instead of creating an unconsumed blocking queue.
            return False

        if not maintenance_finished:
            self._shutdown_llm_owner(deadline)
            return False

        with self._worker_futures_condition:
            llm_drained = self._worker_futures_condition.wait_for(
                lambda: not any(kind == "llm" for kind, _ in self.worker_futures)
                and getattr(getattr(self, "llm_queue", None), "unfinished_tasks", 0) == 0,
                timeout=self._remaining_shutdown_budget(deadline),
            )
        if not llm_drained:
            self._shutdown_llm_owner(deadline)
            return False

        return self._shutdown_llm_owner(deadline)

    def _shutdown_llm_owner(self, deadline: float | None = None) -> bool:
        """Bounded final stop for the LLM consumer and its executor.

        Args:
            deadline: 见 _stop_workers 同名参数说明（本地 codex review
                第 11 轮 N1）。None 时现算一个新的，供不经 _stop_workers
                直接调用本方法的场景使用。
        """
        if deadline is None:
            deadline = self._new_shutdown_deadline()
        llm_thread = self.llm_thread
        llm_thread_was_alive = bool(llm_thread and llm_thread.is_alive())
        self.llm_stop_event.set()
        if hasattr(self, "llm_queue"):
            try:
                self.llm_queue.put_nowait(None)
            except queue.Full:
                pass
        if llm_thread_was_alive:
            llm_thread.join(timeout=self._remaining_shutdown_budget(deadline))
        llm_executor = getattr(self, "llm_executor", None)
        if llm_executor is not None:
            llm_executor.shutdown(wait=False, cancel_futures=True)
        return not (llm_thread and llm_thread.is_alive())

    def _finish_close(self, resources_safe: bool, deadline: float | None = None) -> None:
        """Drain unconditionally, then close owner-thread resources only
        once workers are confirmed stopped.

        Args:
            deadline: 见 _stop_workers 同名参数说明（本地 codex review
                第 11 轮 N1）。透传给 _drain_non_terminal_tasks_on_shutdown，
                让清算阶段的预算并入 aclose()/close() 入口计算的同一个
                deadline，而不是重新给满一份独立预算——取代此前"清算预算
                与其余阶段同一量级、但各自独立计满"的语义。None 时现算
                一个新的，供不经 aclose()/close() 直接调用本方法的场景
                使用。

        本地 codex review 第 6 轮 G2：此前"关闭清算"
        （_drain_non_terminal_tasks_on_shutdown）只在 resources_safe=True
        时才跑——但 resources_safe=False 恰恰意味着 _stop_workers 里某个
        有界等待（producers/maintenance/llm）超时了，这条路径上极可能
        存在一个刚被 llm_executor.shutdown(cancel_futures=True) 取消掉的
        排队中 LLM future：它从未真正执行 llm_ops._handle_llm_task，因此
        既不会走到它自己的 `finally: llm_task_queue.task_done()`，也不会
        调用 cache_manager.update_task_status 把任务写成 failed——任务
        永久停在 queued/processing/calibrating，而恰恰是这条最需要清算的
        超时路径把清算跳过了（见 _drain_non_terminal_tasks_on_shutdown
        的详细说明与其取舍）。

        修复：清算不再以 resources_safe=True 为前提，只要 cache_manager
        存在就执行（该方法内部已有 None 判断）；DB 连接的关闭仍然只在
        resources_safe=True 时执行——worker 未确认停止时连接必须留着，
        避免仍在跑的线程操作已经关闭的连接。

        K2 修复（本地 codex review 第 8 轮）：上面这条"清算不再以
        resources_safe=True 为前提"仍然不够精确——resources_safe=False
        可能是因为 producer 超时（与维护调用是否已经真正跑完无关），此时
        maintenance executor 可能仍在线程里执行一次阻塞 DB 调用，清算若照样
        无条件跑，会重新与它并发，回到 G2 修复之前要堵住的那个竞态。改为
        用 _stop_workers 设置的 _maintenance_confirmed_stopped 作为清算的
        唯一前提——它比 resources_safe 更精确地回答"维护调用是否已确认
        停止"这一个问题；未确认停止时清算整体跳过，留给下次启动的孤儿恢复
        兜底（该兜底语义已存在，见 CacheManager.recover_orphaned_tasks）。
        """
        if deadline is None:
            deadline = self._new_shutdown_deadline()
        self.resources_safe = resources_safe
        maintenance_confirmed_stopped = getattr(
            self, "_maintenance_confirmed_stopped", True
        )
        if maintenance_confirmed_stopped:
            self._drain_non_terminal_tasks_on_shutdown(deadline)
        else:
            self.logger.error(
                "Maintenance executor shutdown timed out; skipping shutdown "
                "drain, leaving non-terminal tasks for next startup's orphan "
                "recovery"
            )
        if resources_safe:
            for name in ("cache_manager", "audit_logger"):
                resource = getattr(self, name, None)
                close = getattr(resource, "close", None)
                if close:
                    close()
        else:
            self.logger.error(
                "Worker shutdown timed out; shared resources remain open until process exit"
            )
        self.closed = True
        if resources_safe:
            shutdown_logger()

    def _drain_non_terminal_tasks_on_shutdown(self, deadline: float | None = None) -> None:
        """关闭清算：把仍处于 queued/processing/calibrating 的任务经 CAS
        终态路径写成 failed。

        Args:
            deadline: 见 _stop_workers 同名参数说明（本地 codex review
                第 11 轮 N1）。剩余预算为 0 时整体跳过——连
                CacheManager.drain_non_terminal_tasks_on_shutdown 内部
                第一步的 SELECT 查询都不发起：这一步此前总是无条件拿满
                一份独立的 WORKER_STOP_TIMEOUT_SECONDS 预算，现在并入
                aclose()/close() 入口计算的同一个 deadline，预算耗尽时
                不再重新充满。未清算的任务留给下一次启动的孤儿恢复兜底
                （该兜底语义已存在，非本次改动新增）。None 时现算一个
                新的，供不经 aclose()/close() 直接调用本方法的场景使用。

        此前 aclose()/close() 取消队列消费者、停掉线程池后，已受理但没跑完
        的任务会静默停在非终态，只能等下一次启动的孤儿恢复
        （CacheManager.recover_orphaned_tasks）才被发现，期间客户端一直
        轮询到超时（本地 codex review 追加发现）。这里复用同一套逐任务 CAS
        终态写入循环（CacheManager._fail_non_terminal_tasks，reason 换成
        "shutdown_drain"），在关闭路径上也做一次同样的清算。

        由 _finish_close 无条件调用（只要 cache_manager 存在），不再要求
        resources_safe=True（本地 codex review 第 6 轮 G2 修复）：
        resources_safe=False 的超时路径——尤其是 LLM 排队积压导致
        llm_drained 等待超时、进而 llm_executor.shutdown(cancel_futures=
        True) 直接取消掉尚未开始跑的 LLM future——恰恰是最需要清算的场景。
        被取消的 future 从未真正执行 llm_ops._handle_llm_task，任务会永久
        停留在非终态，只能靠下一次启动的孤儿恢复才被发现，期间客户端一直
        轮询到超时。

        与仍在真实运行的 worker 竞争的取舍：update_task_status 的 CAS
        （终态一旦写入不可覆盖）保证原子性——若某个仍在跑的 worker 稍后
        才真正完成并尝试写 success，会被这里已经写入的 failed 挡回去，
        等同于把一次本该成功的任务误判为失败；但进程本就在退出，任务的
        产物（若已生成）仍留在缓存里，下次同样的请求会直接命中缓存，不会
        真的丢失工作成果——这比此前"任务永久卡在非终态、客户端轮询到
        超时"的默认结果更好，因此接受这个取舍。

        清算自身的异常不得阻断关闭：这里只记录并继续，不重新抛出——关闭
        路径必须保证进程能退出，这与 CacheManager 方法本身对普通调用方
        "失败即抛错"的语义并不冲突（那是给启动恢复等场景的默认行为，是否
        兜底由各自调用方决定）。
        """
        cache_manager = getattr(self, "cache_manager", None)
        if cache_manager is None:
            return
        if deadline is None:
            deadline = self._new_shutdown_deadline()
        remaining_budget = self._remaining_shutdown_budget(deadline)
        if remaining_budget <= 0:
            # 本地 codex review 第 11 轮 N1：预算已被前面几个阶段（producers/
            # maintenance/llm 的有界等待）耗尽，清算并入的是同一份 deadline，
            # 不重新充满——整体跳过，不发起任何查询，留给下一次启动的孤儿
            # 恢复兜底。
            self.logger.error(
                "关闭预算已耗尽，跳过关闭清算，留给下次启动的孤儿恢复兜底"
            )
            return
        try:
            # 清算预算与 _stop_workers 的三段有界等待并入同一个 deadline
            # （本地 codex review 第 7 轮 H3 引入独立预算，第 11 轮 N1 改为
            # 与其余阶段共用同一份总预算）：任务量大、或单任务写入被拖慢时，
            # 清算必须能在剩余预算内提前停止返回，不能无界阻塞
            # aclose()/close()。
            cache_manager.drain_non_terminal_tasks_on_shutdown(
                deadline_seconds=remaining_budget,
            )
        except Exception:
            self.logger.exception("关闭清算失败，已忽略并继续关闭流程")

        # PR3 review hardening（major reliability）：此前这里还无条件 drain
        # terminal_write_pending 并同步调用 _retry_terminal_write_pending
        # 做"最后一次补偿"，现已删除——论证冗余且危险：
        #
        # 冗余：能进入 terminal_write_pending 的 task_id，唯一登记点是
        # llm_ops.process_llm_queue 提交失败分支里"写 FAILED 终态本身也
        # 抛异常"的那一刻（见 register_terminal_write_pending 调用处）。
        # update_task_status 内部的 UPDATE 经由 _get_cursor 事务包裹，抛异常
        # 即触发 conn.rollback()，不会有部分提交——所以这批 task_id 对应的
        # DB 行此刻必然仍停在 queued/processing/calibrating 非终态。而上面
        # 几行刚执行完的 drain_non_terminal_tasks_on_shutdown 正是"枚举全部
        # 非终态任务、在预算内逐个 CAS 写 failed"，天然已经覆盖这批 id，
        # 不需要再单独处理一遍。
        #
        # 危险：这里调用的是 cache_manager.update_task_status 直调，不经过
        # _fail_non_terminal_tasks，因此不接受 deadline、也不会收紧
        # SQLite busy_timeout——而上面 drain_non_terminal_tasks_on_shutdown
        # 结束时的 finally 恰好把 busy_timeout 复位回默认的 5000ms（见
        # CacheManager._fail_non_terminal_tasks）。一旦此刻真的撞上锁竞争，
        # 单个任务就可能额外阻塞近 5s，多个任务串行累加，直接击穿
        # WORKER_STOP_TIMEOUT_SECONDS 预算，破坏"整条 aclose()/close() 在
        # 单一预算内返回"的不变式（见
        # tests/unit/test_runtime_lifecycle.py::
        # test_aclose_bounded_despite_held_sqlite_lock_and_pending_terminal_write）。
        #
        # 删除后 terminal_write_pending 不再是"进程即将退出前才清空一次"，
        # 而是保持它原本的主补偿通道：_periodic_maintenance 每轮维护里的
        # drain + _retry_terminal_write_pending（正常运行期、无预算约束，
        # 见 app.py）——集合仍有消费者，不会变成只进不出的泄漏。关闭前那一刻
        # 若集合里还挂着 id，DB 行已经被上面的 drain_non_terminal_tasks_on_
        # shutdown 处理过（写成 failed）或因预算耗尽仍留在非终态；后者与其它
        # 未被清算的非终态任务一样，交给下次启动的孤儿恢复兜底
        # （CacheManager.recover_orphaned_tasks，语义已存在，非本次改动
        # 新增）。集合本身留下的过期 id 随进程退出一并释放，无需专门清理。


def _retry_terminal_write_pending(cache_manager, task_ids: set[str], logger) -> set[str]:
    """对 terminal_write_pending 快照里的 task_id 逐个重试 CAS 写 FAILED
    终态（K1 桶 b 的有界补偿，CI review 第 3 轮 major）。

    唯一调用方是 app.py::_periodic_maintenance：每轮维护 drain 一次
    RuntimeContext.terminal_write_pending，调用本函数重试，仍失败的
    id 重新登记回去，留给下一轮——正常运行期、无预算约束，是这个集合的
    主补偿通道。

    RuntimeContext._drain_non_terminal_tasks_on_shutdown 此前也调用过
    本函数（关闭前最后尝试一次），PR3 review hardening 已删除该调用：
    进入 terminal_write_pending 的 task_id 对应的 DB 行必然仍是非终态，
    已被同一次关闭清算前段的 drain_non_terminal_tasks_on_shutdown（枚举
    全部非终态任务、有界预算内逐个 CAS 写 failed）覆盖处理；而这里的
    update_task_status 直调不经过有界预算、也不收紧 SQLite busy_timeout，
    关闭路径单独再跑一遍纯属冗余且可能顶着默认 ~5s busy_timeout 拖慢
    aclose()/close()（详见 _drain_non_terminal_tasks_on_shutdown 内联
    注释）。

    error_message 是通用文案，不还原触发这次补偿的原始异常文本——
    RuntimeContext.terminal_write_pending 按设计只登记 task_id（"极小"
    的补偿登记，见其字段注释），不携带原始错误详情；真正的原始异常已经
    在 process_llm_queue 的提交失败分支里以 ERROR 级别记过日志一次，这
    里的职责只是"确保它最终落成终态"，不是重现原始错误文案。

    Args:
        cache_manager: 拥有 update_task_status 的缓存管理器实例。
        task_ids: 待重试的 task_id 集合（调用方一次 drain 的快照）。
        logger: 调用方的 logger 实例（周期维护与关闭路径各自持有不同的
            logger，不在这里写死成模块级单例）。

    Returns:
        set: 仍未成功确认终态的 task_id 子集。写入抛异常的才计入这里；
        CAS 返回 False（任务已被其它路径先一步写成终态，成功或失败都
        算"已确认"）不算需要重试的失败。
    """
    if not task_ids:
        return set()
    still_pending = set()
    for task_id in task_ids:
        try:
            cache_manager.update_task_status(
                task_id, TaskStatus.FAILED,
                error_message="LLM 任务提交失败，终态写入曾经失败，已由运行期维护补偿写入",
            )
        except Exception:
            logger.exception(
                f"终态写入补偿重试仍然失败，保留到下一次触达此路径时重试: {task_id}"
            )
            still_pending.add(task_id)
    return still_pending


_runtime_var: contextvars.ContextVar[RuntimeContext | None] = contextvars.ContextVar(
    "video_transcript_runtime", default=None
)
def bind_runtime(runtime: RuntimeContext):
    return _runtime_var.set(runtime)


def unbind_runtime(token) -> None:
    _runtime_var.reset(token)


def get_runtime() -> RuntimeContext:
    runtime = _runtime_var.get()
    if runtime is None:
        raise RuntimeError("RuntimeContext is not active; use the FastAPI lifespan")
    return runtime


_T = TypeVar("_T")


def run_with_runtime(
    runtime: RuntimeContext, function: Callable[..., _T], *args, **kwargs
) -> _T:
    """Run one worker entry with its owning application runtime bound."""
    token = _runtime_var.set(runtime)
    try:
        return function(*args, **kwargs)
    finally:
        _runtime_var.reset(token)


class _LazyResource:
    """Import-safe compatibility proxy for existing route/service globals.

    Deliberately does NOT subclass collections.abc.Mapping: Mapping injects
    mixin methods (get/keys/items/values/__contains__/__eq__/__ne__) directly
    onto this class, which take priority over __getattr__ in normal attribute
    lookup. Since this same proxy wraps non-dict resources too (queue.Queue,
    ThreadPoolExecutor, CacheManager, ...), a wrapped resource with its own
    same-named method -- e.g. queue.Queue.get() -- would silently resolve to
    Mapping.get(self, key, default=None) instead, breaking with a confusing
    TypeError the moment that method is called with the wrapped resource's
    own signature. Plain attribute/item access below already delegates
    everything (including .get()/.keys() for the dict-shaped `config` proxy)
    through __getattr__/__getitem__ to the real object, so no Mapping mixin
    is needed to keep dict-like proxies (e.g. `config = lazy_resource(get_config)`)
    working.
    """

    def __init__(self, accessor: Callable[[], Any]):
        object.__setattr__(self, "_accessor", accessor)

    def _value(self):
        return object.__getattribute__(self, "_accessor")()

    def __getattr__(self, name):
        # unittest.mock probes these attributes while patching. They describe
        # functions, not the proxied runtime object, and must not activate it.
        if name.startswith("__") or name == "_is_coroutine":
            raise AttributeError(name)
        return getattr(self._value(), name)

    def __setattr__(self, name, value):
        setattr(self._value(), name, value)

    def __delattr__(self, name):
        delattr(self._value(), name)

    def __getitem__(self, key):
        return self._value()[key]

    def __iter__(self):
        return iter(self._value())

    def __len__(self):
        return len(self._value())

    def __call__(self, *args, **kwargs):
        return self._value()(*args, **kwargs)


def lazy_resource(accessor: Callable[[], Any]):
    return _LazyResource(accessor)


def get_logger():
    runtime = _runtime_var.get()
    return runtime.logger if runtime else setup_logger("api_server", bootstrap=True)


def get_config():
    runtime = _runtime_var.get()
    return runtime.config if runtime else load_config()


def get_user_manager():
    runtime = _runtime_var.get()
    if runtime:
        return runtime.user_manager
    from ..utils.accounts import get_user_manager as legacy_get

    return legacy_get(fallback_config=get_config())


def get_audit_logger():
    runtime = _runtime_var.get()
    if runtime:
        return runtime.audit_logger
    from ..utils.logging import get_audit_logger as legacy_get

    return legacy_get()


def get_usage_recorder():
    runtime = _runtime_var.get()
    if runtime:
        return runtime.usage_recorder
    from ..utils.logging.usage_recorder import get_usage_recorder as legacy_get

    return legacy_get()


def get_cache_manager():
    runtime = _runtime_var.get()
    if runtime:
        return runtime.cache_manager
    global _legacy_cache_manager
    if _legacy_cache_manager is None:
        from ..cache import CacheManager

        _legacy_cache_manager = CacheManager(get_config()["storage"]["cache_dir"])
    return _legacy_cache_manager


def get_temp_manager():
    runtime = _runtime_var.get()
    if runtime:
        return runtime.temp_manager
    from ..utils.tempfile_manager import get_shared_temp_manager

    return get_shared_temp_manager()


def get_workspace_dir() -> str:
    runtime = _runtime_var.get()
    return runtime.workspace_dir if runtime else get_config()["storage"]["workspace_dir"]


def get_llm_coordinator():
    runtime = _runtime_var.get()
    if runtime:
        return runtime.llm_coordinator
    global _legacy_llm_coordinator
    if _legacy_llm_coordinator is None:
        from ..llm import LLMCoordinator

        config = get_config()
        _legacy_llm_coordinator = LLMCoordinator(
            config_dict=config, cache_dir=config["storage"]["cache_dir"]
        )
    return _legacy_llm_coordinator


def get_task_queue() -> asyncio.Queue:
    runtime = _runtime_var.get()
    if runtime:
        return runtime.task_queue
    global _legacy_task_queue
    if _legacy_task_queue is None:
        _legacy_task_queue = asyncio.Queue(get_config()["concurrent"]["queue_size"])
    return _legacy_task_queue


def get_executor() -> concurrent.futures.ThreadPoolExecutor:
    runtime = _runtime_var.get()
    if runtime:
        return runtime.executor
    global _legacy_executor
    if _legacy_executor is None:
        _legacy_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=get_config()["concurrent"]["max_workers"]
        )
    return _legacy_executor


def get_llm_queue() -> queue.Queue:
    runtime = _runtime_var.get()
    if runtime:
        return runtime.llm_queue
    global _legacy_llm_queue
    if _legacy_llm_queue is None:
        _legacy_llm_queue = queue.Queue(maxsize=LLM_QUEUE_MAXSIZE)
    return _legacy_llm_queue


# 未绑定 runtime 时 get_inflight_registry() 兜底实例的容量：不代表任何
# 真实生产语义（生产容量见 RuntimeContext.__init__），只需要大到不会被
# 正常测试/脚本用例意外撞到上限。
_FALLBACK_INFLIGHT_CAPACITY = 1000


def get_inflight_registry() -> _InflightTaskRegistry:
    """返回当前 RuntimeContext 的在途任务登记表；未绑定 runtime 时（测试
    直接调用路由函数、脚本绕开 create_app() 的 lifespan）每次返回一个全新
    的空登记表，不缓存为模块级单例——与 get_task_queue/get_llm_queue/
    get_executor 的"未绑定时懒建并缓存单例"约定刻意不同：那三者的身份
    （同一个 Queue/Executor 实例）需要跨调用保持一致，生产者/消费者才能
    真正协作；登记表在没有 runtime 的分支下不服务于任何真实的跨调用协作
    场景（没有 runtime 就没有真正在跑的后台 worker 去消费队列，"受理中+
    执行中"这个概念本就不成立）。若缓存成单例，会让互不相干的测试用例
    共享同一份登记状态——一个用例只验证"入队成功"、不模拟消费者跑完
    （因此永远不会触发 release）就会悄悄占用一个名额，拖累之后没有显式
    接管这个依赖的测试。每次返回全新空实例从根上避免这类跨用例状态泄漏；
    真实生产路径永远经由 create_app() 的 lifespan 绑定 runtime，走上面
    的 if 分支，返回进程内唯一、真正参与背压闭环的那一份。

    容量不取自 get_config()：未绑定 runtime 的分支本就不追求真实的生产
    容量语义（见上），刻意不读配置文件，避免这个纯粹为测试/脚本兜底的
    路径意外依赖一个在测试进程里未必存在、或未必是预期内容的配置文件。
    """
    runtime = _runtime_var.get()
    if runtime:
        return runtime.inflight_registry
    return _InflightTaskRegistry(
        {
            "transcription": _FALLBACK_INFLIGHT_CAPACITY,
            "llm": _FALLBACK_INFLIGHT_CAPACITY,
        }
    )


def get_llm_executor() -> concurrent.futures.ThreadPoolExecutor:
    runtime = _runtime_var.get()
    if runtime:
        return runtime.llm_executor
    global _legacy_llm_executor
    if _legacy_llm_executor is None:
        _legacy_llm_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=get_config()["concurrent"]["llm_max_workers"]
        )
    return _legacy_llm_executor


_legacy_cache_manager = None
_legacy_llm_coordinator = None
_legacy_task_queue = None
_legacy_executor = None
_legacy_llm_queue = None
_legacy_llm_executor = None


_task_locks: Dict[str, threading.Lock] = {}
_task_lock_refcounts: Dict[str, int] = {}
_task_locks_guard = threading.Lock()


@contextmanager
def task_lock(task_id: str | None):
    key = task_id or "default"
    with _task_locks_guard:
        lock = _task_locks.setdefault(key, threading.Lock())
        _task_lock_refcounts[key] = _task_lock_refcounts.get(key, 0) + 1
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _task_locks_guard:
            remaining = _task_lock_refcounts[key] - 1
            if remaining == 0:
                _task_lock_refcounts.pop(key, None)
                _task_locks.pop(key, None)
            else:
                _task_lock_refcounts[key] = remaining


def get_template_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "web" / "templates"


def get_templates() -> Jinja2Templates:
    return Jinja2Templates(directory=str(get_template_dir()))


def get_static_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "web" / "static"
