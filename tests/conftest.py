#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pytest 全局配置文件

用于管理测试环境的全局资源，包括企业微信通知器的单例实例。
"""

import ipaddress
import os
import socket
import sys
from pathlib import Path

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
# `tests/manual/` 下显式手动运行的测试（见 tests/manual/conftest.py）。
#
# 预热已与 VTAPI_TESTS_MANUAL 完全解耦（issue #116）：该开关只控制
# tests/manual 的收集，不参与这里的调用决策。函数名 `_seed_config_cache_for_
# missing_config_jsonc` 是历史遗留，行为以上面这段注释为准。
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
#
# 该开关只认 "1"（见 _tests_manual_env_enabled），且只控制 tests/manual 的
# 收集与否，绝不影响占位配置预热——预热由 config.jsonc 是否缺失决定。
# ---------------------------------------------------------------------------
def _tests_manual_env_enabled() -> bool:
    """VTAPI_TESTS_MANUAL 开关的唯一判定定义（tests/manual/conftest.py 复用）。

    只认 "1"。手动测试会发真实企业微信 webhook、连真实网络、用真实凭据，
    危险操作的开关应当保守：只认最明确的那一种拼写，true/yes 一律不生效。
    """
    return os.environ.get("VTAPI_TESTS_MANUAL", "").strip() == "1"


# 无条件预热。原先这里挂过一个与预热毫无关系的环境变量
# （`if not _tests_manual_env_enabled()`），后果是设成 VTAPI_TESTS_MANUAL=true
# 会被判为"手动模式开启"从而跳过预热：手动测试一个没跑，反而把主门禁弄红。
# 一个开关的失败后果应该是"少跑一些"，不该是"主门禁红"。也从未改成
# "config.jsonc 缺失才注入"——那会让开发机（有真实配置）与 CI（无配置）走不同
# 代码分支，实测导致本分支在有真实 config.jsonc 的环境下 5 failed。
_seed_config_cache_for_missing_config_jsonc()


def _is_manual_test_item(item) -> bool:
    """Return whether a pytest item belongs to the explicitly manual suite."""
    item_path = getattr(item, "path", getattr(item, "fspath", None))
    if item_path is None:
        return False

    try:
        candidate = Path(os.fspath(item_path)).resolve()
    except TypeError:
        candidate = Path(str(item_path)).resolve()

    manual_dir = Path(__file__).resolve().parent / "manual"
    try:
        candidate.relative_to(manual_dir)
    except ValueError:
        return False
    return True


def _is_loopback(host) -> bool:
    """Return whether a socket destination is an allowed loopback target."""
    if isinstance(host, tuple) and host:
        host = host[0]
    host = str(host)
    if host in ("localhost", "", "0.0.0.0", "::"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NetworkGuard:
    """Record and block non-loopback socket connections for one test."""

    def __init__(self):
        self.blocked = []
        self.allowed = []
        self.expected_blocks = 0

    def expect_blocks(self, count: int) -> None:
        """Register the number of deliberate blocked attempts in the test."""
        self.expected_blocks += count

    def connected_to(self, host: str) -> bool:
        """Return whether ``connect`` reached the real socket implementation."""
        return f"connect:{host}" in self.allowed


@pytest.fixture(autouse=True)
def no_outbound_network(request, monkeypatch):
    """Block non-loopback AF_INET/AF_INET6 ``connect`` and ``connect_ex``.

    Only Internet sockets are in scope: Unix-domain sockets are local IPC
    and pass through to the real implementation unrecorded. DNS resolution
    and the explicitly opted-in ``tests/manual/`` suite also remain outside
    this guard. Tests that exercise blocking must call
    ``guard.expect_blocks(n)`` so deliberate attempts stay explicit.
    """
    if _is_manual_test_item(request.node):
        yield None
        return

    guard = _NetworkGuard()
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    terminal_reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if terminal_reporter is not None and not getattr(
        request.config, "_outbound_guard_announced", False
    ):
        request.config._outbound_guard_announced = True
        terminal_reporter.write_line(
            "outbound network guard installed: "
            "socket.connect/socket.connect_ex block non-loopback "
            "AF_INET/AF_INET6 targets; DNS, loopback, and Unix-domain "
            "sockets are allowed"
        )

    def fake_connect(self, address, *args, **kwargs):
        if self.family not in (socket.AF_INET, socket.AF_INET6):
            return real_connect(self, address, *args, **kwargs)
        host = address[0] if isinstance(address, tuple) and address else address
        if not _is_loopback(host):
            guard.blocked.append(f"connect:{host}")
            raise AssertionError(f"outbound connect blocked by test guard: {host}")
        guard.allowed.append(f"connect:{host}")
        return real_connect(self, address, *args, **kwargs)

    def fake_connect_ex(self, address, *args, **kwargs):
        if self.family not in (socket.AF_INET, socket.AF_INET6):
            return real_connect_ex(self, address, *args, **kwargs)
        host = address[0] if isinstance(address, tuple) and address else address
        if not _is_loopback(host):
            guard.blocked.append(f"connect_ex:{host}")
            raise AssertionError(f"outbound connect_ex blocked by test guard: {host}")
        guard.allowed.append(f"connect_ex:{host}")
        return real_connect_ex(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", fake_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", fake_connect_ex)
    try:
        yield guard
    finally:
        assert len(guard.blocked) == guard.expected_blocks, (
            f"unexpected outbound attempts: {guard.blocked} "
            f"(expected {guard.expected_blocks})"
        )

from video_transcript_api.utils.notifications import (
    init_all_notifiers,
    shutdown_all_notifiers,
)
from video_transcript_api.llm.core import usage_context


@pytest.fixture(autouse=True)
def _guard_cwd(request):
    """哨兵：禁止任何测试改变进程级 cwd。

    为什么需要（issue #113 / #115）：`os.chdir()` 改的是进程全局状态，不随
    测试结束回滚。历史上 tests/unit/test_timezone.py 里的 `os.chdir(tests/unit)`
    就把整个 pytest 进程的工作目录永久改了，导致此后所有按相对路径解析数据
    目录的代码（例如 ./data/logs/debug）把产物写进 tests/unit/ —— 全量门禁
    每次运行都在 tests/ 下凭空产出新文件。

    这里做两件事，缺一不可：
      1. 报警：cwd 变了就让该测试失败，报错里写明是哪个测试、改成了什么。
         只做清理的守卫会让下一个引入泄漏的人以为一切正常。
      2. 清理：无论成败都把 cwd 还原，否则一次泄漏会污染其后所有测试，
         让排查变得几乎不可能。
    """
    before = os.getcwd()
    yield
    after = os.getcwd()
    if after != before:
        os.chdir(before)
        pytest.fail(
            f"测试 {request.node.nodeid} 泄漏了进程级 cwd：{before!r} -> {after!r}。"
            " 测试内不要调用 os.chdir()；确需切换请用 monkeypatch.chdir()，"
            " 它会在测试结束后自动还原。"
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


@pytest.fixture(autouse=True)
def _reset_usage_context():
    """
    Reset video_transcript_api.llm.core.usage_context's contextvar state
    before and after every test.

    Why this is needed: usage_context.bind_task_id() is a one-shot bind by
    design (no paired reset -- see its docstring), which is correct in
    production because ThreadPoolExecutor worker threads re-call it at every
    task entry point, overwriting any leftover value. Several integration
    tests (test_llm_stage_terminal_state.py, test_layered_cache.py,
    test_llm_ops_status_backfill.py) call the production entry point
    llm_ops._handle_llm_task() directly and synchronously in the pytest main
    thread rather than through a real worker thread. That leaves a real
    task_id sitting in the contextvar after those tests finish, which then
    leaks into whichever test pytest happens to run next in the same
    process/thread (e.g. test_usage_context_propagation.py expecting a
    pristine 'unknown' default) -- a collection-order-dependent flake, not a
    genuine test failure. Resetting here removes the dependency on test
    ordering entirely.
    """
    usage_context.reset_context_for_testing()
    yield
    usage_context.reset_context_for_testing()
