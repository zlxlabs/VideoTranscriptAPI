#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pytest 全局配置文件

用于管理测试环境的全局资源，包括企业微信通知器的单例实例。
"""

import os
import sys
import pytest

# 添加src目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))

# ---------------------------------------------------------------------------
# 在导入 video_transcript_api 之前，为缺失 config/config.jsonc 的场景兜底。
#
# video_transcript_api 在“导入时”就会立即加载配置：
# utils/logging/__init__.py -> audit_logger.py 在模块级别调用
# setup_logger() -> load_config()，且 load_config() 对文件缺失没有任何兜底
# 分支。config.jsonc 已被 .gitignore 排除（存放真实密钥），因此全新 checkout
# 或 CI runner 上通常没有这个文件，导致仅仅 `import video_transcript_api`
# 就会在任何测试收集之前直接崩溃。
#
# 这里不写任何文件到磁盘（不做 cp config.example.jsonc -> config.jsonc 那种
# 手工操作的自动化版本），而是把 logger.py 作为独立模块预先执行一次，用
# config.example.jsonc（仅占位符，无真实密钥）的解析结果直接灌进它的
# `_config_cache` 全局变量，再注册进 sys.modules。这样后续包内的
# `from .logger import ...` 会复用这个已经"预热"过缓存的模块对象，
# load_config() 命中缓存分支，不再触碰磁盘上的 config.jsonc。
# 若本机确实存在真实 config.jsonc（本地开发场景），则完全不介入，走原有逻辑。
#
# 注意：部分测试文件用 `from src.video_transcript_api...` 而不是
# `from video_transcript_api...` 导入（两种写法在 sys.path 上都能解析到，
# 但 Python 会把它们当成两个不同的模块身份分别执行一遍 __init__ 链）。
# 因此要把 `video_transcript_api.*` 和 `src.video_transcript_api.*` 两套
# 模块身份都预热一遍，否则后一种写法仍会绕开缓存、重新触发磁盘读取。
# ---------------------------------------------------------------------------
def _seed_config_cache_for_missing_config_jsonc() -> None:
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _real_config = os.path.join(_project_root, "config", "config.jsonc")
    if os.path.exists(_real_config):
        return  # 本地已有真实配置，交给生产代码原有逻辑加载

    _example_config = os.path.join(_project_root, "config", "config.example.jsonc")
    _logger_py = os.path.join(
        _project_root, "src", "video_transcript_api", "utils", "logging", "logger.py"
    )
    if not os.path.exists(_example_config) or not os.path.exists(_logger_py):
        return  # 缺少必要文件时不介入，保持原有报错方式，避免掩盖真实问题

    import importlib.util

    try:
        import commentjson as _config_json  # 与生产代码一致：优先支持 JSONC 注释
    except ImportError:
        import json as _config_json

    with open(_example_config, "r", encoding="utf-8") as f:
        _placeholder_config = _config_json.load(f)

    for _module_prefix in ("video_transcript_api", "src.video_transcript_api"):
        _module_name = f"{_module_prefix}.utils.logging.logger"
        _spec = importlib.util.spec_from_file_location(_module_name, _logger_py)
        _logger_module = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_logger_module)
        _logger_module._config_cache = _placeholder_config
        sys.modules[_module_name] = _logger_module


_seed_config_cache_for_missing_config_jsonc()

from video_transcript_api.utils.notifications import (
    init_all_notifiers,
    shutdown_all_notifiers,
)


@pytest.fixture(scope="session", autouse=True)
def setup_global_notifiers():
    """
    Initialize all notification subsystems (WeCom + Feishu + Router)
    once per test session.
    """
    init_all_notifiers()
    yield
    shutdown_all_notifiers()
