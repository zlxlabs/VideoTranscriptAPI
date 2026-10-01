import ast
import re
from pathlib import Path


PRINTF_PLACEHOLDER = re.compile(r"%[sdfdr]")


def test_loguru_calls_do_not_use_printf_placeholders():
    assert PRINTF_PLACEHOLDER.search("%s %d %f %r")
    source_root = Path(__file__).resolve().parents[2] / "src"
    violations = [
        f"{source_path.relative_to(source_root.parent)}:{node.lineno}: {node.args[0].value}"
        for source_path in sorted(source_root.rglob("*.py"))
        for node in ast.walk(ast.parse(source_path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and node.args and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
        and PRINTF_PLACEHOLDER.search(node.args[0].value)
    ]
    assert not violations, violations
