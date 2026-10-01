"""Printf-vs-loguru scan.

判据：日志方法、消息是字符串常量、含 printf 且另有格式参数的调用，只按该调用点的接收者来源分三态。
证明 loguru：setup_logger 链、from loguru import logger、赋给变量或 self 后的直接调用；属性名含 logger（如 self.logger）且赋值不冲突时也算。
证明标准库：调用点之前、同一作用域内把名字绑到 logging.getLogger（含 import logging as 别名与 .getLogger() 链式），或 logging 模块函数（lg.info）。
同一名字在调用点之前已有两种来源 → 来源不明并失败；第二次绑定之前只有一种来源的，分别判定。禁止按「文件里出现过 getLogger」整文件放行。
%% 是转义，不当占位符。logger.log(level, message, ...) 的消息在第二个实参。
仍会漏（已知边界，不做跨模块数据流）：参数传入或函数返回的 logger；从属性/容器取出后再赋给普通名字；消息来自变量、拼接或 f-string；getattr、描述符、猴子补丁；导入别名与赋值别名的复杂传播；函数局部遮蔽与分支赋值。%10s 与 %(name)s 也不识别。
"""
import ast
from pathlib import Path

_LOG = {"trace", "debug", "info", "success", "warning", "warn", "error", "exception", "critical", "fatal", "log"}
_SITES = ["video_transcript_api/llm/__init__.py:57", "video_transcript_api/llm/llm.py:802", "video_transcript_api/llm/llm.py:808"]


def _printf(text):
    """True when text has a printf conversion. %% is a literal percent."""
    index = 0
    while (found := text.find("%", index)) >= 0:
        if found + 1 < len(text) and text[found + 1] == "%":
            index = found + 2
            continue
        if found + 1 < len(text) and text[found + 1] in "sdfdr":
            return True
        index = found + 1
    return False


def _candidate(call):
    """Log call whose message slot is a constant printf string and which passes format args."""
    slot = 1 if call.func.attr == "log" else 0
    if len(call.args) < (3 if slot else 2):
        return False
    arg = call.args[slot]
    return isinstance(arg, ast.Constant) and isinstance(arg.value, str) and _printf(arg.value)


def classify_printf_calls(tree):
    """Return (lineno, kind) per candidate. kind is loguru, stdlib, or unknown."""
    found, scopes = [], [{"t": "module", "n": {}, "a": {}}]

    def resolve(name, lineno):
        skip_class = scopes[-1]["t"] != "class"
        for scope in reversed(scopes):
            if skip_class and scope["t"] == "class":
                continue
            hits = [kind for line, kind in scope["n"].get(name, []) if line < lineno]
            if hits:
                return hits[0] if len(set(hits)) == 1 else "unknown"
        return None

    def recv(node, lineno):
        if isinstance(node, ast.Call):
            func, name = node.func, getattr(node.func, "id", None) or getattr(node.func, "attr", "")
            owner = func.value if isinstance(func, ast.Attribute) else None
            if name == "setup_logger":
                return "loguru"
            if name == "getLogger" and isinstance(owner, ast.Name) and resolve(owner.id, lineno) == "logging_module":
                return "stdlib"
            if name == "getLogger" and isinstance(func, ast.Name) and resolve(name, lineno) == "getlogger_func":
                return "stdlib"
            if isinstance(func, ast.Attribute) and name in {"bind", "opt", "patch"}:
                return recv(func.value, lineno)
            return "unknown"
        if isinstance(node, ast.Name):
            kind = resolve(node.id, lineno)
            return "stdlib" if kind == "logging_module" else kind if kind in {"loguru", "stdlib", "unknown"} else "unknown"
        if isinstance(node, ast.Attribute) and "logger" in node.attr:
            if isinstance(node.value, ast.Name) and resolve(node.value.id, lineno) == "loguru_module":
                return "loguru"
            if isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"}:
                for scope in reversed(scopes):
                    if scope["t"] == "class":
                        hits = [kind for _, kind in scope["a"].get(node.attr, [])]
                        return "loguru" if not hits else hits[0] if len(set(hits)) == 1 else "unknown"
            return "loguru"
        return "unknown"

    def bound(expr, lineno):
        if isinstance(expr, ast.Call):
            return recv(expr, lineno)
        if isinstance(expr, ast.Name):
            kind = resolve(expr.id, lineno)
            return kind if kind in {"loguru", "stdlib"} else "unknown"
        return "unknown"

    def add(scope, bucket, key, lineno, kind):
        scope[bucket].setdefault(key, []).append((lineno, kind))

    def bind_import(node):
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            if isinstance(node, ast.Import):
                root = alias.name.split(".")[0]
                if root in {"logging", "loguru"}:
                    add(scopes[-1], "n", local, node.lineno, "logging_module" if root == "logging" else "loguru_module")
                continue
            if (node.module, alias.name) == ("loguru", "logger"):
                add(scopes[-1], "n", local, node.lineno, "loguru")
            elif (node.module, alias.name) == ("logging", "getLogger"):
                add(scopes[-1], "n", local, node.lineno, "getlogger_func")

    def walk_calls(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(node, ast.Lambda):
            walk_calls(node.body)
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _LOG and _candidate(node):
            kind = recv(node.func.value, node.lineno)
            found.append((node.lineno, kind if kind in {"loguru", "stdlib", "unknown"} else "unknown"))
        for child in ast.iter_child_nodes(node):
            walk_calls(child)

    def walk(stmts):
        for stmt in stmts:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                bind_import(stmt)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                walk_calls(stmt.value)
                kind = bound(stmt.value, stmt.lineno)
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        add(scopes[-1], "n", target.id, stmt.lineno, kind)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for node in stmt.decorator_list:
                    walk_calls(node)
                if not isinstance(stmt, ast.ClassDef):
                    walk_calls(stmt.args)
                scopes.append({"t": "class" if isinstance(stmt, ast.ClassDef) else "func", "n": {}, "a": {}})
                if isinstance(stmt, ast.ClassDef):
                    for sub in ast.walk(stmt):
                        sub_targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target] if isinstance(sub, ast.AnnAssign) else []
                        if getattr(sub, "value", None) is None:
                            continue
                        for target in sub_targets:
                            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id in {"self", "cls"}:
                                add(scopes[-1], "a", target.attr, sub.lineno, bound(sub.value, sub.lineno))
                walk(stmt.body)
                scopes.pop()
            else:
                for _, value in ast.iter_fields(stmt):
                    for item in value if isinstance(value, list) else [value]:
                        if isinstance(item, ast.stmt):
                            walk([item])
                        elif isinstance(item, ast.ExceptHandler):
                            walk(item.body)
                        elif type(item).__name__ == "match_case":
                            walk(item.body)
                        elif isinstance(item, ast.AST):
                            walk_calls(item)

    walk(tree.body)
    return found


def _scan_src():
    root = Path(__file__).resolve().parents[2] / "src"
    buckets = {"loguru": [], "stdlib": [], "unknown": []}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        for lineno, kind in classify_printf_calls(ast.parse(path.read_text(encoding="utf-8"))):
            buckets[kind].append(f"{relative}:{lineno}")
    return buckets


def test_loguru_calls_do_not_use_printf_placeholders():
    buckets = _scan_src()
    assert buckets["loguru"] == [], buckets["loguru"]
    assert buckets["unknown"] == [], buckets["unknown"]
    assert buckets["stdlib"] == _SITES


def test_placeholder_scan_synthetic_sources():
    # Each note is what that inline module proves.
    cases = [
        ("stdlib alias lg.info is allowed", "import logging as lg\nlg.info('x %s', y)\n", ["stdlib"]),
        ("logging.getLogger().info chain is allowed", "import logging\nlogging.getLogger(__name__).info('x %s', y)\n", ["stdlib"]),
        ("import logging as _logging then getLogger is allowed", "import logging as _logging\n_log = _logging.getLogger('n')\n_log.warning('x %r: %s', a, b)\n", ["stdlib"]),
        ("self.logger is a loguru violation", "class C:\n    def m(self, y):\n        self.logger.info('x %s', y)\n", ["loguru"]),
        ("self.logger assigned from getLogger is stdlib even if the call is earlier", "import logging\nclass C:\n    def m(self, y):\n        self.logger.info('x %s', y)\n    def __init__(self):\n        self.logger = logging.getLogger('a')\n", ["stdlib"]),
        ("setup_logger().info is a loguru violation", "def f(y):\n    setup_logger('n').info('x %s', y)\n", ["loguru"]),
        ("a name bound to setup_logger is a loguru violation", "logger = setup_logger('n')\nlogger.info('x %s', y)\n", ["loguru"]),
        ("logger.opt(...).error stays tied to that logger", "from loguru import logger\nlogger.opt(depth=1).error('x %s', y)\n", ["loguru"]),
        ("logger.log reads the message from the second arg", "from loguru import logger\nlogger.log('INFO', 'x %s', y)\n", ["loguru"]),
        ("rebind getLogger then setup_logger is unknown", "import logging\nlogger = logging.getLogger('a')\nlogger = setup_logger('b')\nlogger.info('x %s', y)\n", ["unknown"]),
        ("rebind setup_logger then getLogger is unknown", "import logging\nlogger = setup_logger('b')\nlogger = logging.getLogger('a')\nlogger.info('x %s', y)\n", ["unknown"]),
        ("a call before the second binding stays stdlib", "import logging\nlogger = logging.getLogger('a')\nlogger.info('early %s', y)\nlogger = setup_logger('b')\nlogger.info('late %s', y)\n", ["stdlib", "unknown"]),
        ("escaped %% is not a placeholder", "from loguru import logger\nlogger.info('progress 100%%s done', 1)\n", []),
        ("escaped %% does not hide a real %s", "from loguru import logger\nlogger.info('100%% %s', 1)\n", ["loguru"]),
    ]
    for note, source, kinds in cases:
        got = [kind for _, kind in classify_printf_calls(ast.parse(source))]
        assert got == kinds, (note, got)


def test_shutdown_unsafe_count_is_rendered_by_loguru():
    """The real unsafe-shutdown template must show its count in a loguru sink."""
    source = (Path(__file__).resolve().parents[2] / "src/video_transcript_api/api/context.py").read_text(encoding="utf-8")
    template = next(n.args[0].value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str) and "未能在关闭预算内响应取消" in n.args[0].value)
    from loguru._logger import Core, Logger
    # Isolated Logger: the process-wide logger also writes stderr, and this template is not ASCII.
    probe = Logger(core=Core(), exception=None, depth=0, record=False, lazy=False, colors=False, raw=False, capture=True, patchers=[], extra={})
    captured, sink_id = [], probe.add(lambda message: captured.append(message), format="{message}")
    try:
        probe.error(template, 4)
    finally:
        probe.remove(sink_id)
    assert captured and "4" in captured[0] and "%d" not in captured[0]
