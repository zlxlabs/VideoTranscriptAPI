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

当前白名单有 3 项，仍按调用点放行，不按变量名放行。这 3 项只依赖两处绑定。
两条来源契约用语法树核对这两处绑定；核对通过，它们才是已验证的标准库
``logging`` 绑定例外：

- ``llm/__init__.py``：``import logging``，再 ``_logger = logging.getLogger(...)``
- ``llm/llm.py``：函数内 ``import logging as _logging``，再
  ``_log = _logging.getLogger(...)``

每条契约核对四件事：导入的模块名就是 ``logging``；目标变量被赋值为该别名的
``getLogger(...)``；获准调用的接收者名字就是这个变量，并且解析到这一处赋值；
该变量在所属作用域里只有这一个绑定。嵌套函数里的 ``global`` / ``nonlocal``
赋值算进被赋值的那个作用域。作用域里再出现第二个绑定，契约就失败，所以绑定和
调用之间不可能还有另一次赋值。

strftime、``%`` 运算符，以及其它不是日志方法的调用，不进候选，也不进白名单。

三条约束互相不能替代：

- 实际放行集合必须等于白名单。多一条、少一条、改一条消息，都会红。
- 下方反例夹具在识别器变窄时会红。
- 获准调用改由 loguru 绑定（``from loguru import logger as _log``）会红。
  只改绑定、不改消息时，上面的指纹白名单本身仍然是绿的。

下面这些本门禁做不到，也不在这里修：

- 动态拼接的消息、f-string
- 先把日志方法赋给别的名字再调用（``emit = logger.error`` 然后 ``emit(...)``）
- 包装函数返回 logger 之后，调用点仍然写成 ``.error(...)`` 的，抓得到；
  抓不到的是上面那种「方法别名」
- printf 的宽度、旗标、映射键（``%10s``、``%(name)s``）。本仓当前没有这种写法
- 跨模块数据流。来源契约只核上述两个文件里的两处绑定，不跟着 import 进别的模块，
  也不会因为一条来源链就自动放行其它调用
- 来源契约看不见的重绑定：``exec`` / ``eval``、``globals()`` / ``locals()`` 下标赋值、
  把 ``logging.getLogger`` 这个属性换成别的函数。这些都不留下变量绑定节点

``git ls-files`` 无法启动、退出码非 0、或一个已跟踪 Python 文件都没有时，
测试直接失败，不退回到更小的目录。
"""
import ast
import hashlib
import os
import subprocess
import symtable
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
class BindingContract:
    """One stdlib logging assignment and the allow-list calls that must use it."""

    path: str
    binding_name: str
    logging_alias: str
    allows: tuple[Allow, ...]


# Two bindings cover all three ALLOW entries. Calls stay listed above, one by one.
BINDING_CONTRACTS = (
    BindingContract(
        "src/video_transcript_api/llm/__init__.py",
        "_logger",
        "logging",
        (ALLOW[0],),
    ),
    BindingContract(
        "src/video_transcript_api/llm/llm.py",
        "_log",
        "_logging",
        (ALLOW[1], ALLOW[2]),
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


class _Scope:
    """One lexical scope used only to place bindings of the contract names."""

    def __init__(self, kind: str, parent: "_Scope | None", name: str, lineno: int) -> None:
        self.kind = kind
        self.parent = parent
        self.name = name
        self.lineno = lineno
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()
        self.local_stores: dict[str, list[tuple[ast.AST, "_Scope"]]] = {}
        self.global_stores: dict[str, list[tuple[ast.AST, "_Scope"]]] = {}
        self.nonlocal_stores: dict[str, list[tuple[ast.AST, "_Scope"]]] = {}
        self.nonlocal_incoming: dict[str, list[tuple[ast.AST, "_Scope"]]] = {}
        self.star_imports: list[ast.ImportFrom] = []


# type aliases are a 3.12 statement. The test interpreter is 3.11.
_TYPE_ALIAS = getattr(ast, "TypeAlias", None)


def _alias_bound_name(alias: ast.alias) -> str:
    """Name bound by one import alias. ``import pkg.mod`` binds ``pkg``."""
    if alias.asname:
        return alias.asname
    return alias.name.split(".", 1)[0]


def _binding_node_ids(tree: ast.AST, names: set[str]) -> set[int]:
    """Ids of every syntactic binding of ``names``. The walker must see each one."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.id in names:
            found.add(id(node))
        elif isinstance(node, ast.alias) and _alias_bound_name(node) in names:
            found.add(id(node))
        elif isinstance(node, ast.arg) and node.arg in names:
            found.add(id(node))
        elif isinstance(node, ast.ExceptHandler) and node.name in names:
            found.add(id(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            found.add(id(node))
        elif isinstance(node, ast.MatchAs) and node.name in names:
            found.add(id(node))
        elif isinstance(node, ast.MatchStar) and node.name in names:
            found.add(id(node))
        elif isinstance(node, ast.MatchMapping) and node.rest in names:
            found.add(id(node))
        elif (
            _TYPE_ALIAS is not None
            and isinstance(node, _TYPE_ALIAS)
            and isinstance(node.name, ast.Name)
            and node.name.id in names
        ):
            found.add(id(node.name))
    return found


def _direct_decls(body: list[ast.stmt]) -> tuple[set[str], set[str]]:
    """``global`` / ``nonlocal`` names in this block. Nested function and class bodies are separate."""
    globals_: set[str] = set()
    nonlocals_: set[str] = set()
    stack = list(body)
    while stack:
        stmt = stack.pop()
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, ast.Global):
            globals_.update(stmt.names)
        elif isinstance(stmt, ast.Nonlocal):
            nonlocals_.update(stmt.names)
        elif isinstance(stmt, ast.Try):
            stack.extend(stmt.body)
            stack.extend(stmt.orelse)
            stack.extend(stmt.finalbody)
            for handler in stmt.handlers:
                stack.extend(handler.body)
        elif isinstance(stmt, ast.Match):
            for case in stmt.cases:
                stack.extend(case.body)
        elif isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            stack.extend(stmt.body)
            stack.extend(stmt.orelse)
    return globals_, nonlocals_


class _BindingWalk:
    """Place each contract-name binding in the scope that actually owns it.

    Comprehension targets stay in the comprehension: in Python 3.12 they do not
    rebind the enclosing name. A walrus inside a comprehension does.
    ``global`` stores belong to the module. ``nonlocal`` stores belong to the
    enclosing function that already binds the name. Class bodies do not enclose
    methods or comprehensions.
    """

    def __init__(self, contract: BindingContract) -> None:
        self.contract = contract
        self.tracked = {contract.binding_name, contract.logging_alias}
        self.seen: set[int] = set()
        self.module = _Scope("module", None, "<module>", 0)
        self.scopes = [self.module]
        self.scope = self.module
        self.calls: list[tuple[int, ast.Call, _Scope]] = []

    def final_stores(self, scope: _Scope, name: str) -> list[tuple[ast.AST, _Scope]]:
        """Bindings of ``name`` that write this scope, after global/nonlocal are moved."""
        found = [*scope.local_stores.get(name, ()), *scope.nonlocal_incoming.get(name, ())]
        if scope.kind == "module":
            for other in self.scopes:
                found.extend(other.global_stores.get(name, ()))
        return found

    def walk_module(self, tree: ast.Module) -> None:
        """Walk the whole module so a later ``global`` store cannot hide."""
        for stmt in tree.body:
            self.walk_stmt(stmt)

    def _record(self, scope: _Scope, name: str, marker: ast.AST) -> None:
        entry = (marker, scope)
        if scope.kind != "module" and name in scope.global_names:
            scope.global_stores.setdefault(name, []).append(entry)
        elif name in scope.nonlocal_names:
            scope.nonlocal_stores.setdefault(name, []).append(entry)
        else:
            scope.local_stores.setdefault(name, []).append(entry)

    def _bind_target(self, target: ast.AST, scope: _Scope, marker: ast.AST) -> None:
        if isinstance(target, ast.Name):
            if target.id in self.tracked:
                self._record(scope, target.id, marker)
                self.seen.add(id(target))
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._bind_target(elt, scope, marker)
            return
        if isinstance(target, ast.Starred):
            self._bind_target(target.value, scope, marker)

    def _bind_alias(self, node: ast.Import | ast.ImportFrom, scope: _Scope) -> None:
        for alias in node.names:
            if isinstance(node, ast.ImportFrom) and alias.name == "*":
                scope.star_imports.append(node)
                continue
            bound = _alias_bound_name(alias)
            if bound in self.tracked:
                self._record(scope, bound, node)
                self.seen.add(id(alias))

    def _bind_pattern(self, pattern: ast.AST, scope: _Scope) -> None:
        if isinstance(pattern, ast.MatchAs):
            if pattern.pattern is not None:
                self._bind_pattern(pattern.pattern, scope)
            if pattern.name in self.tracked:
                self._record(scope, pattern.name, pattern)
                self.seen.add(id(pattern))
        elif isinstance(pattern, ast.MatchStar):
            if pattern.name in self.tracked:
                self._record(scope, pattern.name, pattern)
                self.seen.add(id(pattern))
        elif isinstance(pattern, ast.MatchMapping):
            for key, subpattern in zip(pattern.keys, pattern.patterns):
                self.walk_expr(key)
                self._bind_pattern(subpattern, scope)
            if pattern.rest in self.tracked:
                self._record(scope, pattern.rest, pattern)
                self.seen.add(id(pattern))
        elif isinstance(pattern, ast.MatchClass):
            for subpattern in pattern.patterns:
                self._bind_pattern(subpattern, scope)
            for subpattern in pattern.kwd_patterns:
                self._bind_pattern(subpattern, scope)
        elif isinstance(pattern, (ast.MatchOr, ast.MatchSequence)):
            for subpattern in pattern.patterns:
                self._bind_pattern(subpattern, scope)

    def _open_scope(self, kind: str, name: str, lineno: int, parent: _Scope) -> _Scope:
        scope = _Scope(kind, parent, name, lineno)
        self.scopes.append(scope)
        return scope

    def _walk_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, parent: _Scope) -> None:
        if not isinstance(node, ast.Lambda):
            for decorator in node.decorator_list:
                self.walk_expr(decorator)
            if node.returns is not None:
                self.walk_expr(node.returns)
        self._walk_argument_exprs(node.args)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in self.tracked:
            self._record(parent, node.name, node)
            self.seen.add(id(node))
        lookup_parent = parent.parent if parent.kind == "class" else parent
        label = "<lambda>" if isinstance(node, ast.Lambda) else node.name
        scope = self._open_scope("function", label, node.lineno, lookup_parent)
        if not isinstance(node, ast.Lambda):
            scope.global_names, scope.nonlocal_names = _direct_decls(node.body)
        self._bind_arguments(node.args, scope)
        previous = self.scope
        self.scope = scope
        if isinstance(node, ast.Lambda):
            self.walk_expr(node.body)
        else:
            for stmt in node.body:
                self.walk_stmt(stmt)
        self.scope = previous

    def _walk_argument_exprs(self, args: ast.arguments) -> None:
        """Defaults and annotations run in the enclosing scope, not the new function."""
        for arg in [*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg]:
            if arg is not None and arg.annotation is not None:
                self.walk_expr(arg.annotation)
        for default in [*args.defaults, *args.kw_defaults]:
            if default is not None:
                self.walk_expr(default)

    def _bind_arguments(self, args: ast.arguments, scope: _Scope) -> None:
        for arg in [*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg]:
            if arg is not None and arg.arg in self.tracked:
                self._record(scope, arg.arg, arg)
                self.seen.add(id(arg))

    def _walk_class(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.walk_expr(decorator)
        for base in node.bases:
            self.walk_expr(base)
        for keyword in node.keywords:
            self.walk_expr(keyword.value)
        if node.name in self.tracked:
            self._record(self.scope, node.name, node)
            self.seen.add(id(node))
        scope = self._open_scope("class", node.name, node.lineno, self.scope)
        previous = self.scope
        self.scope = scope
        for stmt in node.body:
            self.walk_stmt(stmt)
        self.scope = previous

    def _walk_comp(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> None:
        # A class body does not enclose a comprehension. The leftmost iterable does not either.
        parent = self.scope.parent if self.scope.kind == "class" else self.scope
        comp = self._open_scope("comprehension", "<comp>", node.lineno, parent)
        generators = node.generators
        previous = self.scope
        self.scope = parent
        self.walk_expr(generators[0].iter)
        self.scope = comp
        for index, generator in enumerate(generators):
            self._bind_target(generator.target, comp, generator.target)
            if index > 0:
                self.walk_expr(generator.iter)
            for test in generator.ifs:
                self.walk_expr(test)
        if isinstance(node, ast.DictComp):
            self.walk_expr(node.key)
            self.walk_expr(node.value)
        else:
            self.walk_expr(node.elt)
        self.scope = previous

    def walk_stmt(self, stmt: ast.stmt) -> None:
        """Walk one statement. An unknown statement fails the contract instead of being skipped."""
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._walk_function(stmt, self.scope)
        elif isinstance(stmt, ast.ClassDef):
            self._walk_class(stmt)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                self._bind_target(target, self.scope, stmt)
            self.walk_expr(stmt.value)
        elif isinstance(stmt, ast.AnnAssign):
            self._bind_target(stmt.target, self.scope, stmt)
            self.walk_expr(stmt.annotation)
            if stmt.value is not None:
                self.walk_expr(stmt.value)
        elif isinstance(stmt, ast.AugAssign):
            self._bind_target(stmt.target, self.scope, stmt)
            self.walk_expr(stmt.value)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            self._bind_alias(stmt, self.scope)
        elif isinstance(stmt, ast.For):
            self._bind_target(stmt.target, self.scope, stmt)
            self.walk_expr(stmt.iter)
            for sub in stmt.body:
                self.walk_stmt(sub)
            for sub in stmt.orelse:
                self.walk_stmt(sub)
        elif isinstance(stmt, ast.AsyncFor):
            self._bind_target(stmt.target, self.scope, stmt)
            self.walk_expr(stmt.iter)
            for sub in stmt.body:
                self.walk_stmt(sub)
            for sub in stmt.orelse:
                self.walk_stmt(sub)
        elif isinstance(stmt, ast.While):
            self.walk_expr(stmt.test)
            for sub in stmt.body:
                self.walk_stmt(sub)
            for sub in stmt.orelse:
                self.walk_stmt(sub)
        elif isinstance(stmt, ast.If):
            self.walk_expr(stmt.test)
            for sub in stmt.body:
                self.walk_stmt(sub)
            for sub in stmt.orelse:
                self.walk_stmt(sub)
        elif isinstance(stmt, ast.With):
            for item in stmt.items:
                self.walk_expr(item.context_expr)
                if item.optional_vars is not None:
                    self._bind_target(item.optional_vars, self.scope, stmt)
            for sub in stmt.body:
                self.walk_stmt(sub)
        elif isinstance(stmt, ast.AsyncWith):
            for item in stmt.items:
                self.walk_expr(item.context_expr)
                if item.optional_vars is not None:
                    self._bind_target(item.optional_vars, self.scope, stmt)
            for sub in stmt.body:
                self.walk_stmt(sub)
        elif isinstance(stmt, ast.Try):
            for sub in stmt.body:
                self.walk_stmt(sub)
            for handler in stmt.handlers:
                if handler.type is not None:
                    self.walk_expr(handler.type)
                if handler.name in self.tracked:
                    self._record(self.scope, handler.name, handler)
                    self.seen.add(id(handler))
                for sub in handler.body:
                    self.walk_stmt(sub)
            for sub in stmt.orelse:
                self.walk_stmt(sub)
            for sub in stmt.finalbody:
                self.walk_stmt(sub)
        elif isinstance(stmt, ast.Match):
            self.walk_expr(stmt.subject)
            for case in stmt.cases:
                self._bind_pattern(case.pattern, self.scope)
                if case.guard is not None:
                    self.walk_expr(case.guard)
                for sub in case.body:
                    self.walk_stmt(sub)
        elif isinstance(stmt, ast.Delete):
            self._bind_deleted(stmt.targets)
        elif isinstance(stmt, ast.Return):
            if stmt.value is not None:
                self.walk_expr(stmt.value)
        elif isinstance(stmt, ast.Raise):
            if stmt.exc is not None:
                self.walk_expr(stmt.exc)
            if stmt.cause is not None:
                self.walk_expr(stmt.cause)
        elif isinstance(stmt, ast.Assert):
            self.walk_expr(stmt.test)
            if stmt.msg is not None:
                self.walk_expr(stmt.msg)
        elif isinstance(stmt, ast.Expr):
            self.walk_expr(stmt.value)
        elif _TYPE_ALIAS is not None and isinstance(stmt, _TYPE_ALIAS):
            if isinstance(stmt.name, ast.Name) and stmt.name.id in self.tracked:
                self._record(self.scope, stmt.name.id, stmt)
                self.seen.add(id(stmt.name))
            self.walk_expr(stmt.value)
        elif isinstance(stmt, (ast.Global, ast.Nonlocal, ast.Pass, ast.Break, ast.Continue)):
            return
        else:
            raise AssertionError(
                f"unhandled statement {type(stmt).__name__} at line {getattr(stmt, 'lineno', 0)}"
            )

    def _bind_deleted(self, targets: list[ast.AST]) -> None:
        for target in targets:
            if isinstance(target, ast.Name):
                if target.id in self.tracked:
                    self._record(self.scope, target.id, target)
                    self.seen.add(id(target))
            elif isinstance(target, (ast.Tuple, ast.List)):
                self._bind_deleted(list(target.elts))
            elif isinstance(target, ast.Starred):
                self._bind_deleted([target.value])

    def walk_expr(self, node: ast.AST | None) -> None:
        """Walk an expression. Stores inside it are walrus, lambda, or comprehension only."""
        if node is None or isinstance(node, ast.Constant):
            return
        if isinstance(node, ast.Name):
            return
        if isinstance(node, ast.NamedExpr):
            owner = self.scope
            while owner.kind == "comprehension":
                owner = owner.parent
            if owner is None or owner.kind == "class":
                raise AssertionError(f"assignment expression has no function scope at line {node.lineno}")
            self._bind_target(node.target, owner, node)
            self.walk_expr(node.value)
            return
        if isinstance(node, ast.Lambda):
            self._walk_function(node, self.scope)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            self._walk_comp(node)
            return
        if isinstance(node, ast.Call):
            self._note_call(node)
            self.walk_expr(node.func)
            for arg in node.args:
                self.walk_expr(arg)
            for keyword in node.keywords:
                self.walk_expr(keyword.value)
            return
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                self.walk_expr(key)
                self.walk_expr(value)
            return
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for elt in node.elts:
                self.walk_expr(elt)
            return
        for child in ast.iter_child_nodes(node):
            self.walk_expr(child)

    def _note_call(self, node: ast.Call) -> None:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        method = func.attr
        for index, allow in enumerate(self.contract.allows):
            if allow.method != method:
                continue
            slot = 1 if method == "log" else 0
            if len(node.args) <= slot:
                continue
            message = node.args[slot]
            if isinstance(message, ast.Constant) and message.value == allow.message:
                self.calls.append((index, node, self.scope))

    def attach_nonlocals(self) -> None:
        """Move ``nonlocal`` stores onto the enclosing function that binds the name."""
        for scope in self.scopes:
            for name, stores in scope.nonlocal_stores.items():
                target = self._nonlocal_target(scope, name)
                target.nonlocal_incoming.setdefault(name, []).extend(stores)

    def _nonlocal_target(self, scope: _Scope, name: str) -> _Scope:
        current = scope.parent
        while current is not None:
            if current.kind in {"class", "comprehension"}:
                current = current.parent
                continue
            if current.kind == "module":
                break
            if name in current.nonlocal_names or name in current.global_names:
                current = current.parent
                continue
            if current.local_stores.get(name):
                return current
            current = current.parent
        raise AssertionError(f"nonlocal {name} does not reach a function binding")


def _is_getlogger(marker: ast.AST, binding_name: str, logging_alias: str) -> bool:
    """True when ``marker`` is ``binding_name = logging_alias.getLogger(...)``."""
    if isinstance(marker, ast.Assign):
        if len(marker.targets) != 1 or not isinstance(marker.targets[0], ast.Name):
            return False
        if marker.targets[0].id != binding_name:
            return False
        value: ast.AST | None = marker.value
    elif isinstance(marker, ast.AnnAssign):
        if marker.value is None or not isinstance(marker.target, ast.Name):
            return False
        if marker.target.id != binding_name:
            return False
        value = marker.value
    elif isinstance(marker, ast.NamedExpr):
        if not isinstance(marker.target, ast.Name) or marker.target.id != binding_name:
            return False
        value = marker.value
    else:
        return False
    func = value.func if isinstance(value, ast.Call) else None
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "getLogger"
        and isinstance(func.value, ast.Name)
        and func.value.id == logging_alias
    )


def _is_stdlib_logging_import(marker: ast.AST, logging_alias: str) -> bool:
    """True for ``import logging`` or ``import logging as logging_alias``."""
    if not isinstance(marker, ast.Import):
        return False
    return any(alias.name == "logging" and _alias_bound_name(alias) == logging_alias for alias in marker.names)


def _star_on_path(start: _Scope, stop: _Scope) -> ast.ImportFrom | None:
    """Star import between a use and its binding. Its names are invisible to the AST."""
    current: _Scope | None = start
    while current is not None:
        if current.star_imports:
            return current.star_imports[0]
        if current is stop:
            return None
        current = current.parent
    return None


def _resolve(scope: _Scope, name: str, module: _Scope, walk: _BindingWalk) -> _Scope | None:
    """Scope whose binding ``name`` refers to, starting at ``scope``. Class scopes are not parents."""
    current: _Scope | None = scope
    while current is not None:
        if current.kind != "module" and name in current.global_names:
            current = module
            continue
        if name in current.nonlocal_names:
            current = walk._nonlocal_target(current, name)
            continue
        if walk.final_stores(current, name):
            return current
        if current.kind == "module":
            return None
        current = current.parent
    return None


def _find_function_table(root: symtable.SymbolTable, scope: _Scope) -> symtable.Function:
    """Symbol table of one walked function. Line and name must both match."""
    stack = [root]
    while stack:
        table = stack.pop()
        if table.get_type() == "function" and table.get_name() == scope.name and table.get_lineno() == scope.lineno:
            return table
        stack.extend(table.get_children())
    raise AssertionError(f"symtable has no function {scope.name} at line {scope.lineno}")


def _symtable_agrees(table: symtable.SymbolTable, name: str, owner: _Scope, call_scope: _Scope) -> None:
    """Fail when the compiler classifies the name differently from the walker."""
    try:
        symbol = table.lookup(name)
    except KeyError as exc:
        raise AssertionError(f"symtable has no symbol {name} in {call_scope.name}") from exc
    if owner.kind == "module" and call_scope is not owner:
        if symbol.is_local() or symbol.is_assigned() or not symbol.is_global():
            raise AssertionError(f"symtable disagrees: {name} in {call_scope.name} is not a global load")
        return
    if owner is call_scope:
        if not symbol.is_local() or not symbol.is_assigned() or symbol.is_global():
            raise AssertionError(f"symtable disagrees: {name} in {call_scope.name} is not a local binding")
        return
    raise AssertionError(f"symtable check does not recognize this scope shape for {name}")


def assert_stdlib_binding(source: str, contract: BindingContract) -> None:
    """Require ``contract`` calls to use one unrebound ``logging.getLogger`` assignment.

    The check is syntactic. It does not execute the module. A second binding of the
    name in the same scope is a rebinding, including one that appears after the call.
    """
    tree = ast.parse(source)
    walk = _BindingWalk(contract)
    walk.walk_module(tree)
    expected = _binding_node_ids(tree, walk.tracked)
    if walk.seen != expected:
        missing = len(expected - walk.seen)
        extra = len(walk.seen - expected)
        raise AssertionError(f"binding walk missed {missing} stores and invented {extra} for {contract.path}")
    walk.attach_nonlocals()

    found: list[tuple[_Scope, tuple[ast.AST, _Scope]]] = []
    for scope in walk.scopes:
        for entry in walk.final_stores(scope, contract.binding_name):
            if _is_getlogger(entry[0], contract.binding_name, contract.logging_alias):
                found.append((scope, entry))
    if not found:
        raise AssertionError(
            f"stdlib logging binding missing: {contract.path} does not assign "
            f"{contract.binding_name} = {contract.logging_alias}.getLogger(...)"
        )
    if len(found) != 1:
        raise AssertionError(
            f"stdlib logging binding rebound: {contract.path} has {len(found)} "
            f"{contract.binding_name} = {contract.logging_alias}.getLogger(...) assignments"
        )
    owner, (marker, syntactic) = found[0]
    owner_stores = walk.final_stores(owner, contract.binding_name)
    if len(owner_stores) != 1:
        raise AssertionError(
            f"stdlib logging binding rebound: {contract.path} binds {contract.binding_name} "
            f"{len(owner_stores)} times in the same scope"
        )
    alias_scope = _resolve(syntactic, contract.logging_alias, walk.module, walk)
    if alias_scope is None:
        raise AssertionError(f"stdlib logging import missing: {contract.path} does not bind {contract.logging_alias}")
    alias_stores = walk.final_stores(alias_scope, contract.logging_alias)
    if len(alias_stores) != 1 or not _is_stdlib_logging_import(alias_stores[0][0], contract.logging_alias):
        raise AssertionError(
            f"stdlib logging import missing: {contract.path} does not bind "
            f"{contract.logging_alias} with import logging"
        )
    import_line = alias_stores[0][0].lineno
    assign_line = marker.lineno
    if not import_line < assign_line:
        raise AssertionError(f"stdlib logging import does not precede getLogger assignment in {contract.path}")
    star = _star_on_path(syntactic, alias_scope)
    if star is not None:
        raise AssertionError(f"star import hides the logging alias at line {star.lineno}")

    tables = symtable.symtable(source, contract.path, "exec")
    for index, allow in enumerate(contract.allows):
        matches = [item for item in walk.calls if item[0] == index]
        if len(matches) != 1:
            raise AssertionError(f"allow-listed call count for {allow.method} is {len(matches)} in {contract.path}")
        _index, call, call_scope = matches[0]
        receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
        if not isinstance(receiver, ast.Name) or receiver.id != contract.binding_name:
            raise AssertionError(
                f"allow-listed call does not use {contract.binding_name}: {contract.path}:{call.lineno}"
            )
        if call_scope.kind != "function":
            raise AssertionError(f"allow-listed call is not directly inside a function: {contract.path}:{call.lineno}")
        resolved = _resolve(call_scope, contract.binding_name, walk.module, walk)
        if resolved is not owner:
            raise AssertionError(
                f"allow-listed call does not resolve to the logging binding: {contract.path}:{call.lineno}"
            )
        if not assign_line < call.lineno:
            raise AssertionError(f"allow-listed call is not after the logging binding: {contract.path}:{call.lineno}")
        star = _star_on_path(call_scope, owner)
        if star is not None:
            raise AssertionError(f"star import hides {contract.binding_name} at line {star.lineno}")
        _symtable_agrees(_find_function_table(tables, call_scope), contract.binding_name, owner, call_scope)


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
    assert tuple(item for contract in BINDING_CONTRACTS for item in contract.allows) == ALLOW
    result = scan_paths(_REPO, rels, ALLOW)
    assert (
        result.violations == ()
        and result.missing == ()
        and result.duplicated == ()
        and len(result.matched) == 3
    ), result
    for contract in BINDING_CONTRACTS:
        assert contract.path in rels
        assert_stdlib_binding((_REPO / contract.path).read_text(encoding="utf-8"), contract)


def test_loguru_alias_does_not_satisfy_the_stdlib_binding_contract():
    """A whitelisted message bound with ``from loguru import logger as _log`` fails the source contract."""
    info, warning = ALLOW[1], ALLOW[2]
    source = (
        "from loguru import logger as _log\n"
        "def log_llm_config_summary(task, exc):\n"
        f"    _log.info({info.message!r}, task, 'm', 'p', 'mode', 'src')\n"
        f"    _log.warning({warning.message!r}, task, exc)\n"
    )
    # The fingerprint allow list stays green: same path, method, and message bytes.
    scanned = scan_texts({info.path: source}, (info, warning))
    assert scanned.violations == () and scanned.missing == () and scanned.duplicated == ()
    contract = BINDING_CONTRACTS[1]
    with pytest.raises(AssertionError, match="stdlib logging binding missing"):
        assert_stdlib_binding(source, contract)


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
