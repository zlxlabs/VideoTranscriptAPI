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
# 在导入 video_transcript_api 之前，为默认测试套件强制注入脱敏占位配置。
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
#
# 重要：默认测试套件永远注入占位配置，不再检查磁盘上是否存在真实
# config.jsonc。原先"本机已有真实配置就跳过预热"的分支已删除——那样会让
# 默认套件的行为随开发机是否有真实配置而漂移（覆盖不同代码分支、结果不可
# 复现），且真实凭据路径下个别测试打印的 API key 前缀等信息存在通过
# `pytest -s` 或失败日志泄露的风险。真正需要读取真实配置的场景，只保留给
# `tests/manual/` 下显式手动运行的测试（见下方 _tests_manual_env_enabled）。
#
# 注意：部分测试文件用 `from src.video_transcript_api...` 而不是
# `from video_transcript_api...` 导入（两种写法在 sys.path 上都能解析到，
# 但 Python 会把它们当成两个不同的模块身份分别执行一遍 __init__ 链）。
# 因此要把 `video_transcript_api.*` 和 `src.video_transcript_api.*` 两套
# 模块身份都预热一遍，否则后一种写法仍会绕开缓存、重新触发磁盘读取。
# ---------------------------------------------------------------------------
def _seed_config_cache_for_missing_config_jsonc() -> None:
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


# ---------------------------------------------------------------------------
# tests/manual/ 下的用例是"显式手动运行、依赖真实配置"的测试，预热逻辑不能
# 介入它们：介入会导致这些测试用占位 API key 真的发起网络请求，而不是按
# 生产代码原有逻辑在缺配置时干净地报错/跳过。
#
# 判断依据为什么用环境变量，而不是解析 sys.argv：
# 早期实现试图从命令行参数猜测"本次调用是不是显式针对 tests/manual"（区分
# 选项标志和位置参数、解析相对/绝对路径），先后被 codex review 抓出两轮真实
# 边角案例（字符串子串误判、相对路径漏判、`--ignore=tests/manual` 与
# `--ignore tests/manual` 空格分隔等价写法处理不一致——pytest 有一长串会
# 消耗下一个 argv 元素作为值的选项，如 `--ignore`、`--ignore-glob`、
# `--deselect`、`-k`、`-m`、`--confcutdir`、`-p`、`-c`、`-o` 等）。要在不
# 依赖 pytest 自己的参数解析器的前提下（pytest_configure 等 hook 时机太晚，
# 来不及在 conftest.py 顶层 import 之前生效，已用探针验证过）手工正确处理
# 全部情况，本质是在重新实现一个不完整、会持续冒出新边角案例的 argparse——
# 不值得继续投入。
#
# 因此改为显式环境变量门禁：手动运行 tests/manual 下测试的人一定知道自己在
# 做什么，让其显式设置 `VTAPI_TESTS_MANUAL=1`（已在下方各手动测试文件的
# 运行示例中体现）即可，不存在任何猜测和边角案例——命令行里出现多少次
# "tests/manual" 字样、以什么形式出现，都不影响判断结果。
# ---------------------------------------------------------------------------
def _tests_manual_env_enabled() -> bool:
    """判断环境变量 VTAPI_TESTS_MANUAL 是否被显式设置为真值。

    宽容大小写和常见写法（"1"/"true"/"True"/"yes"），未设置或设置为其他
    值一律视为假，走默认套件的占位配置预热路径。
    """
    return os.environ.get("VTAPI_TESTS_MANUAL", "").strip() in ("1", "true", "True", "yes")


if not _tests_manual_env_enabled():
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
