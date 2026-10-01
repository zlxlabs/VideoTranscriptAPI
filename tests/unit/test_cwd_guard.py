#!/usr/bin/env python3
"""反向检验：证明 tests/conftest.py 里的 _guard_cwd 守卫真的有约束力。

守卫本身在 pytest 主进程里生效，无法在同一个进程里"造一个泄漏的测试"来自证
（造了就会把本文件的门禁跑红）。所以这里在**子进程**里跑 pytest，测试对象
必须放在仓库 tests/ 目录下（这样它才会向上继承真正的 tests/conftest.py 守卫，
而不是一份复制品），跑完立刻删除整个探针目录。

两组对照：
  - 绿色对照：探针测试不碰 cwd -> 子进程退出码 0
  - 红色对照：探针测试 os.chdir 且不还原 -> 子进程退出码 != 0，且输出里含
    守卫的报错文案（说明是守卫判红的，不是别的巧合）
只有红色对照真的红，才说明装的是守卫而不是"悄悄清理"。
"""

import os
import shutil
import subprocess
import sys

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)

# 守卫（tests/conftest.py::_guard_cwd）的报错特征串
_GUARD_MESSAGE = "泄漏了进程级 cwd"

_CLEAN_PROBE = '''\
import os


def test_probe_does_not_touch_cwd():
    assert os.getcwd() is not None
'''

_LEAKY_PROBE = '''\
import os
import tempfile


def test_probe_leaks_cwd():
    # 故意把进程级 cwd 改到别处且不还原：模拟 issue #113 里
    # tests/unit/test_timezone.py 曾经的 os.chdir(tests/unit)。
    os.chdir(tempfile.mkdtemp(prefix="cwd_guard_probe_"))
    assert True
'''


def _run_probe(tmp_dir_name: str, probe_source: str):
    """在 tests/ 下落一个探针测试文件，用子进程跑 pytest，返回 (returncode, output)。"""
    probe_dir = os.path.join(_TESTS_DIR, tmp_dir_name)
    probe_file = os.path.join(probe_dir, "test_probe.py")
    try:
        os.makedirs(probe_dir, exist_ok=True)
        with open(probe_file, "w", encoding="utf-8") as fh:
            fh.write(probe_source)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", probe_file],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
        )
        return proc.returncode, proc.stdout + proc.stderr
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def test_cwd_guard_passes_when_cwd_unchanged():
    """对照组：不泄漏 cwd 的测试必须仍然全绿（守卫不能误伤/恒红）。"""
    returncode, output = _run_probe("cwd_guard_probe_clean", _CLEAN_PROBE)
    assert returncode == 0, f"对照组应全绿，实际退出码 {returncode}:\n{output}"


def test_cwd_guard_fails_when_cwd_leaked():
    """反向检验：泄漏 cwd 的测试必须被守卫判红，且报错指向具体测试与新 cwd。"""
    returncode, output = _run_probe("cwd_guard_probe_leaky", _LEAKY_PROBE)
    assert returncode != 0, f"泄漏 cwd 竟通过了门禁，守卫没有约束力：\n{output}"
    assert _GUARD_MESSAGE in output, f"失败信息里没有守卫的报错文案：\n{output}"
    # 报错要指明是哪个测试
    assert "test_probe.py::test_probe_leaks_cwd" in output, \
        f"守卫报错未指明泄漏的测试：\n{output}"


def test_timezone_test_does_not_leak_cwd():
    """时区测试自身前后 cwd 一致（issue #115 的直接验收点）。

    断言方式不是"跑完看看没红"——那只能证明守卫没触发，证明不了 cwd 相同。
    这里让子进程里的探针先记下起始 cwd，再调用时区测试函数，最后比较两者。
    """
    probe = '''\
import os

import sys

sys.path.insert(0, os.path.join(%r, "tests", "unit"))
sys.path.insert(0, os.path.join(%r, "src"))

import test_timezone

_EXPECTED = os.getcwd()

test_timezone.test_timezone_functionality()
test_timezone.test_edge_cases()

assert os.getcwd() == _EXPECTED, f"cwd 被改了: {_EXPECTED} -> {os.getcwd()}"


def test_probe_ran():
    # 探针文件需要至少一个测试函数，否则 pytest 以 "no tests ran"(退出码 5) 结束；
    # 上面的断言在 import 阶段执行，失败即为收集错误。
    assert True
''' % (_REPO_ROOT, _REPO_ROOT)
    returncode, output = _run_probe("cwd_guard_probe_timezone", probe)
    assert returncode == 0, f"时区测试不应改变 cwd，实际退出码 {returncode}:\n{output}"


def test_timezone_test_restores_configured_timezone():
    """时区测试把配置改完后必须恢复原值（不是硬编码 "UTC+8"），异常路径也恢复。"""
    probe = '''\
import os
import sys

sys.path.insert(0, os.path.join(%r, "tests", "unit"))
sys.path.insert(0, os.path.join(%r, "src"))

import test_timezone
from video_transcript_api.utils.timeutil import timezone_helper

# 用一个与 "UTC+8" 不同的原值，这样"硬编码恢复"必然被发现
config = timezone_helper.load_config()
config["web"]["timezone"] = "UTC-7"
test_timezone.test_timezone_functionality()
assert config["web"]["timezone"] == "UTC-7", \\
    f"未恢复原值，而是: {config['web']['timezone']}"

# 异常路径：让中间断言失败，finally 仍要恢复原值
config["web"]["timezone"] = "UTC-3"
real_hours = test_timezone.tz_to_hours
test_timezone.tz_to_hours = lambda tz: -999  # 触发 offset_hours 断言失败
try:
    try:
        test_timezone.test_timezone_functionality()
    except AssertionError:
        pass
    else:
        raise SystemExit("预期断言失败，但没有失败")
finally:
    test_timezone.tz_to_hours = real_hours
assert config["web"]["timezone"] == "UTC-3", \\
    f"异常路径未恢复原值，而是: {config['web']['timezone']}"


def test_probe_ran():
    assert True
''' % (_REPO_ROOT, _REPO_ROOT)
    returncode, output = _run_probe("cwd_guard_probe_tzrestore", probe)
    assert returncode == 0, f"配置恢复校验失败，退出码 {returncode}:\n{output}"
