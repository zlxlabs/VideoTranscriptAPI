"""Printf 占位符门禁。

本测试实际检查的范围，只有下面这一条：

``git ls-files -z -- *.py`` 列出的每个已跟踪文件里，同时满足这四条的调用算候选，
查到就报，不问接收者是不是 loguru：

- 被调用的属性名是日志方法（trace、debug、info、success、warning、warn、
  error、exception、critical、fatal、log）
- 消息槽是字符串常量。普通方法看第一个位置参数；``log`` 的消息在第二个位置参数
- 该常量含 printf 转换符：百分号后面紧挨着转换字母。``%%`` 是字面百分号。
  转换字母是 a A d i o u x X e E f F g G c C r s
- 消息槽后面还有至少一个位置参数

唯一的放行入口是下面的 ``ALLOW``。每一项是「仓库相对路径 + 指纹 + 理由」。
指纹是 ``sha256(方法名 + 换行 + 消息字面量)`` 的十六进制。白名单没有目录、
接收者名字或模式通配。只改消息字面量，指纹就对不上：该调用变成违规，
旧的白名单项同时变成落空项。

当前白名单有 3 项，都是 llm 包里标准库 ``logging.Logger`` 的 printf 调用。
标准库本来就用 printf 填参数，所以按调用点放行，不按变量名放行。

strftime、``%`` 运算符，以及其它不是日志方法的调用，不进候选，也不进白名单。

两条约束互相不能替代：

- 实际放行集合必须等于白名单。多一条、少一条、改一条消息，都会红。
- 下方反例夹具在识别器变窄时会红。

下面这些本门禁做不到，也不在这里修：

- 动态拼接的消息、f-string
- 先把日志方法赋给别的名字再调用（``emit = logger.error`` 然后 ``emit(...)``）
- 包装函数返回 logger 之后，调用点仍然写成 ``.error(...)`` 的，抓得到；
  抓不到的是上面那种「方法别名」
- printf 的宽度、旗标、映射键（``%10s``、``%(name)s``）。本仓当前没有这种写法
- 跨模块数据流。标准库调用不会因为来源链自动放行

``git ls-files`` 无法启动、退出码非 0、或一个已跟踪 Python 文件都没有时，
测试直接失败，不退回到更小的目录。
"""
import ast
import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

_LOG_METHODS = frozenset({
    "trace", "debug", "info", "success", "warning", "warn",
    "error", "exception", "critical", "fatal", "log",
})
# a is a CPython printf type. A and C are not, but a log call that passes
# them with format args is still printf-style. Width and mapping are out of scope.
_PRINTF_TYPES = frozenset("diouxXeEfFgGcrsaAC")
_REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Allow:
    """One exact call that may keep a printf message."""

    path: str
    method: str
    message: str
    reason: str


# Exact calls, not names. Editing the message string breaks the fingerprint.
ALLOW = (
    Allow(
        "src/video_transcript_api/llm/__init__.py",
        "warning",
        "Unknown reasoning_effort %r, treating as None (provider default). "
        "Valid values: %s",
        "stdlib logging.Logger; printf is that library's formatter",
    ),
    Allow(
        "src/video_transcript_api/llm/llm.py",
        "info",
        "[LLM] %s: %s (%s) | thinking=%s(%s)",
        "stdlib logging.Logger; printf is that library's formatter",
    ),
    Allow(
        "src/video_transcript_api/llm/llm.py",
        "warning",
        "[LLM] Failed to describe task %r: %s",
        "stdlib logging.Logger; printf is that library's formatter",
    ),
)


@dataclass(frozen=True)
class ScanResult:
    """Violations are unlisted candidates. missing/duplicated are allow-list drift."""

    violations: tuple[str, ...]
    matched: tuple[str, ...]
    missing: tuple[str, ...]
    duplicated: tuple[str, ...]


def call_fingerprint(method: str, message: str) -> str:
    """Stable id of one log call. Changing the message changes the digest."""
    payload = f"{method}\n{message}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _has_printf(text: str) -> bool:
    """True when % is immediately followed by a conversion letter. %% is literal."""
    index = 0
    while (found := text.find("%", index)) >= 0:
        if found + 1 < len(text) and text[found + 1] == "%":
            index = found + 2
            continue
        if found + 1 < len(text) and text[found + 1] in _PRINTF_TYPES:
            return True
        index = found + 1
    return False


def _allow_label(item: Allow) -> str:
    return f"{item.path} {item.method} {call_fingerprint(item.method, item.message)}"


def _git_env() -> dict[str, str]:
    """Subprocess env. The cwd argument is the work tree, not a parent GIT_DIR."""
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def tracked_python_files(repo: Path) -> tuple[str, ...]:
    """Tracked *.py paths from git ls-files. Failure and empty output both raise."""
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            cwd=repo,
            env=_git_env(),
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise AssertionError(f"git ls-files could not be started: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[:300]
        raise AssertionError(f"git ls-files failed with exit {completed.returncode}: {detail}")
    if not completed.stdout:
        raise AssertionError("git ls-files returned no tracked Python files")
    rels = tuple(part.decode("utf-8") for part in completed.stdout.split(b"\0") if part)
    if not rels:
        raise AssertionError("git ls-files returned no tracked Python files")
    return rels


def _candidates(tree: ast.AST):
    """Yield (lineno, method, message) for log calls with a constant printf message and format args."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        if method not in _LOG_METHODS:
            continue
        slot = 1 if method == "log" else 0
        if len(node.args) < slot + 2:
            continue
        message = node.args[slot]
        if not isinstance(message, ast.Constant) or not isinstance(message.value, str):
            continue
        if not _has_printf(message.value):
            continue
        yield node.lineno, method, message.value


def scan_texts(files: dict[str, str], allows: tuple[Allow, ...]) -> ScanResult:
    """Scan source text. Each allow entry must match exactly one candidate."""
    allowed: dict[str, Allow] = {}
    for item in allows:
        label = _allow_label(item)
        if label in allowed:
            raise AssertionError(f"duplicate allow entry: {item.path} {item.method}")
        allowed[label] = item
    seen = {label: 0 for label in allowed}
    violations: list[str] = []
    for path in sorted(files):
        for lineno, method, message in _candidates(ast.parse(files[path])):
            label = f"{path} {method} {call_fingerprint(method, message)}"
            if label in seen:
                seen[label] += 1
            else:
                violations.append(f"{path}:{lineno}")
    return ScanResult(
        violations=tuple(violations),
        matched=tuple(label for label, count in seen.items() if count == 1),
        missing=tuple(label for label, count in seen.items() if count == 0),
        duplicated=tuple(label for label, count in seen.items() if count > 1),
    )


def scan_paths(repo: Path, rels: tuple[str, ...], allows: tuple[Allow, ...]) -> ScanResult:
    """Read repo-relative files and scan them. A bad encoding or syntax error propagates."""
    files = {rel: (repo / rel).read_text(encoding="utf-8") for rel in rels}
    return scan_texts(files, allows)


def _init_tracked(repo: Path, rel: str, source: str) -> None:
    """Create a tiny git repo with one tracked file. Does not touch the parent repo."""
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    env = _git_env()
    for args in (["git", "init"], ["git", "add", "-A"]):
        completed = subprocess.run(args, cwd=repo, env=env, capture_output=True, check=False)
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()[:300]
            raise AssertionError(f"{args[0]} {' '.join(args[1:])} failed: {detail}")


def test_fingerprint_changes_when_the_message_changes():
    """The digest is the allow key. Same bytes match; a different message does not."""
    original = call_fingerprint("warning", "value %s")
    assert original == call_fingerprint("warning", "value %s")
    assert original != call_fingerprint("warning", "value %d")
    assert len(original) == 64


def test_tracked_files_match_allow_list_exactly():
    """Unlisted printf log calls fail. Allow entries that match nothing fail too."""
    rels = tracked_python_files(_REPO)
    assert "skill/scripts/videotranscript.py" in rels
    assert "main.py" in rels
    assert any(rel.startswith("tests/") for rel in rels)
    assert any(rel.startswith("scripts/") for rel in rels)
    assert len(ALLOW) == 3
    assert all("*" not in item.path and not item.path.endswith("/") for item in ALLOW)
    result = scan_paths(_REPO, rels, ALLOW)
    assert (
        result.violations == ()
        and result.missing == ()
        and result.duplicated == ()
        and len(result.matched) == 3
    ), result


def test_edited_allow_message_is_a_violation():
    """Changing a whitelisted message in place drops the fingerprint and fails both ways."""
    item = ALLOW[0]
    source = (
        "def f(value, labels):\n"
        "    _logger.warning(\n"
        f"        {item.message!r},\n"
        "        value,\n"
        "        labels,\n"
        "    )\n"
    )
    intact = scan_texts({item.path: source}, (item,))
    assert intact.violations == () and intact.missing == () and len(intact.matched) == 1
    edited = source.replace(item.message, item.message.replace("%s", "%d"))
    broken = scan_texts({item.path: edited}, (item,))
    assert broken.violations == (f"{item.path}:2",), broken
    assert broken.missing and broken.matched == ()


def test_extra_call_and_stale_allow_entry_are_drift():
    """An unlisted call and an allow entry with no call are separate failures."""
    item = ALLOW[1]
    extra = (
        "def f(task):\n"
        f"    _log.info({item.message!r}, task, 'm', 'p', 'mode', 'src')\n"
        "    _log.info('unlisted %s', task)\n"
    )
    extra_result = scan_texts({item.path: extra}, (item,))
    assert extra_result.violations == (f"{item.path}:3",), extra_result
    assert extra_result.missing == () and len(extra_result.matched) == 1

    stale_item = ALLOW[2]
    stale = "def f(task):\n    _log.warning('different %s', task)\n"
    stale_result = scan_texts({stale_item.path: stale}, (stale_item,))
    assert stale_result.violations == (f"{stale_item.path}:2",), stale_result
    assert stale_result.matched == () and stale_result.missing


def test_recognizer_fixtures():
    """Each case records the shape that must stay visible. Notes say what it proves."""
    cases = [
        (
            "self.logger.error is a violation; the receiver name is irrelevant",
            "class C:\n    def m(self, pending):\n        self.logger.error('REGRESSION %s', pending)\n",
            ["mod.py:3"],
        ),
        (
            "setup_logger(...).info is a violation; the receiver may be a call",
            "def f(y):\n    setup_logger('n').info('x %s', y)\n",
            ["mod.py:2"],
        ),
        (
            "a chained attribute receiver is still a violation",
            "def f(request, pending):\n    request.app.state.runtime.logger.error('x %s', pending)\n",
            ["mod.py:2"],
        ),
        (
            "binding a factory to a variable does not hide the later log call",
            "def f(y):\n    logger = setup_logger('n')\n    logger.info('x %s', y)\n",
            ["mod.py:3"],
        ),
        (
            "logger.log reads the message from the second positional arg",
            "def f(level, y):\n    logger.log(level, 'x %s', y)\n",
            ["mod.py:2"],
        ),
        (
            "printf text in the log() level slot is not the message",
            "def f(y):\n    logger.log('x %s', y)\n",
            [],
        ),
        (
            "logger.opt(...).error is a violation; opt does not hide the method",
            "def f(y):\n    logger.opt(depth=1).error('x %s', y)\n",
            ["mod.py:2"],
        ),
        (
            "rebinding the same name does not allow either call",
            "import logging\n"
            "logger = logging.getLogger('a')\n"
            "logger.info('early %s', y)\n"
            "logger = setup_logger('b')\n"
            "logger.info('late %s', y)\n",
            ["mod.py:3", "mod.py:5"],
        ),
        (
            "a wrapper that is still called as a log method is a violation",
            "def get_logger():\n    return logger\ndef f(y):\n    get_logger().error('x %s', y)\n",
            ["mod.py:4"],
        ),
        (
            "time.strftime is date formatting, not a log call and not an allow entry",
            "import time\ntime.strftime('%Y-%m-%dT%H:%M:%S')\n",
            [],
        ),
        (
            "instance strftime is date formatting, not a log call",
            "def f(d):\n    return d.strftime('%y%m%d-%H%M%S')\n",
            [],
        ),
        (
            "datetime.now().strftime is date formatting, not a log call",
            "import datetime\ndatetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')\n",
            [],
        ),
        (
            "the percent operator is not a log call",
            "def f(v):\n    text = 'x %s' % v\n",
            [],
        ),
        (
            "a printf message with no format argument is not a candidate",
            "def f():\n    logger.error('literal %s')\n",
            [],
        ),
        (
            "escaped %% is not a conversion",
            "def f(v):\n    logger.info('progress 100%%s done', v)\n",
            [],
        ),
        (
            "escaped %% does not hide a later conversion",
            "def f(v):\n    logger.info('100%% %s', v)\n",
            ["mod.py:2"],
        ),
        # Known gaps. These assertions lock today's boundary; they are not a promise to catch more.
        (
            "known gap: a width specifier is not recognized",
            "def f(y):\n    logger.error('v %10s', y)\n",
            [],
        ),
        (
            "known gap: a mapping key is not recognized",
            "def f(y):\n    logger.error('v %(name)s', y)\n",
            [],
        ),
        (
            "known gap: an f-string message is not a constant",
            "def f(y):\n    logger.error(f'v %s {y}', y)\n",
            [],
        ),
        (
            "known gap: a log method bound to another name is not an attribute call",
            "def f(y):\n    emit = logger.error\n    emit('v %s', y)\n",
            [],
        ),
    ]
    assert _PRINTF_TYPES == set("diouxXeEfFgGcrsaAC")
    for letter in sorted(_PRINTF_TYPES):
        source = f"def f(y):\n    logger.error('v %{letter}', y)\n"
        got = scan_texts({"mod.py": source}, ()).violations
        assert got == ("mod.py:2",), (letter, got)
    for note, source, expected in cases:
        got = scan_texts({"mod.py": source}, ()).violations
        assert got == tuple(expected), (note, got)


def test_new_directory_is_covered(tmp_path):
    """A tracked file under a brand-new directory is scanned. No directory list is consulted."""
    rel = "brand_new_pkg/probe.py"
    _init_tracked(tmp_path, rel, "def f(v):\n    self.logger.error('REGRESSION %s', v)\n")
    rels = tracked_python_files(tmp_path)
    assert rels == (rel,)
    result = scan_paths(tmp_path, rels, ())
    assert result.violations == ("brand_new_pkg/probe.py:2",), result
    assert result.matched == ()


def test_tests_directory_probe_is_caught(tmp_path):
    """tests/ is not excluded by purpose. A printf log call there is a violation."""
    rel = "tests/manual/probe.py"
    _init_tracked(tmp_path, rel, "def f(v):\n    logger.warning('probe %s', v)\n")
    rels = tracked_python_files(tmp_path)
    assert rels == (rel,)
    result = scan_paths(tmp_path, rels, ())
    assert result.violations == ("tests/manual/probe.py:2",), result


def test_git_ls_files_failure_is_loud(tmp_path):
    """A directory that is not a git repo fails the scan. No narrower fallback."""
    with pytest.raises(AssertionError, match="git ls-files failed with exit"):
        tracked_python_files(tmp_path)


def test_git_ls_files_empty_is_loud(tmp_path):
    """An empty listing is a failure, not an empty success."""
    completed = subprocess.run(
        ["git", "init"],
        cwd=tmp_path,
        env=_git_env(),
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    with pytest.raises(AssertionError, match="no tracked Python files"):
        tracked_python_files(tmp_path)


def test_git_missing_from_path_is_loud(tmp_path, monkeypatch):
    """This test's PATH change is undone by monkeypatch and does not affect other tests."""
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(AssertionError, match="git ls-files could not be started"):
        tracked_python_files(_REPO)


def test_shutdown_unsafe_count_is_rendered_by_loguru():
    """The unsafe-shutdown template renders its count. The aclose path is covered in test_runtime_lifecycle."""
    source = (_REPO / "src/video_transcript_api/api/context.py").read_text(encoding="utf-8")
    template = next(
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and "未能在关闭预算内响应取消" in node.args[0].value
    )
    from loguru._logger import Core, Logger

    # Isolated Logger: the process-wide logger also writes stderr, and this template is not ASCII.
    probe = Logger(
        core=Core(),
        exception=None,
        depth=0,
        record=False,
        lazy=False,
        colors=False,
        raw=False,
        capture=True,
        patchers=[],
        extra={},
    )
    captured = []
    sink_id = probe.add(lambda message: captured.append(message), format="{message}")
    try:
        probe.error(template, 4)
    finally:
        probe.remove(sink_id)
    assert captured and "4" in captured[0] and "%d" not in captured[0]
