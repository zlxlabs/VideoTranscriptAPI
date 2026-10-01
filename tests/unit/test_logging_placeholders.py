import ast
import re
from pathlib import Path


PRINTF_PLACEHOLDER = re.compile(r"%[sdfdr]")


def test_loguru_calls_do_not_use_printf_placeholders():
    assert PRINTF_PLACEHOLDER.search("%s %d %f %r")
    source_root = Path(__file__).resolve().parents[2] / "src"
    # Known boundaries, not omissions: only constant strings are scanned.
    # Dynamic logger.info("x %s" % v) calls and ast.JoinedStr f-strings are out of scope.
    # Direct logging.getLogger names use stdlib printf formatting.
    # Call-shaped receivers such as setup_logger(...).info are outside this scan.
    violations = []
    for source_path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        relative_path = source_path.relative_to(source_root)
        stdlib_logger_names = {
            target.id for binding in ast.walk(tree)
            if isinstance(binding, ast.Assign) and isinstance(binding.value, ast.Call)
            and ast.unparse(binding.value.func) == "logging.getLogger"
            for target in binding.targets if isinstance(target, ast.Name)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            receiver = node.func.value
            receiver_name = getattr(receiver, "id", getattr(receiver, "attr", ""))
            if "logger" not in receiver_name or (
                isinstance(receiver, ast.Name) and receiver_name in stdlib_logger_names
            ):
                continue
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                if PRINTF_PLACEHOLDER.search(first_arg.value):
                    violations.append(f"{relative_path}:{node.lineno}")
    assert not violations, violations
