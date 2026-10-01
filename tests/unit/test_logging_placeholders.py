import ast
import re
from pathlib import Path


PRINTF_PLACEHOLDER = re.compile(r"%[sdfdr]")


def test_loguru_calls_do_not_use_printf_placeholders():
    assert PRINTF_PLACEHOLDER.search("%s %d %f %r"), "scanner must detect printf placeholders"

    source_root = Path(__file__).resolve().parents[2] / "src"
    violations = []
    for source_path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function = node.func
            # Check every direct logger method; loguru methods are not limited to levels.
            if not (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "logger"
            ):
                continue
            format_string = node.args[0]
            if (
                isinstance(format_string, ast.Constant)
                and isinstance(format_string.value, str)
                and PRINTF_PLACEHOLDER.search(format_string.value)
            ):
                relative_path = source_path.relative_to(source_root.parent)
                violations.append(f"{relative_path}:{node.lineno}: {format_string.value}")

    assert not violations, "printf placeholders in loguru calls:\n" + "\n".join(violations)
