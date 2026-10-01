import ast
import re
from pathlib import Path


PRINTF_PLACEHOLDER = re.compile(r"%[sdfdr]")


def test_loguru_calls_do_not_use_printf_placeholders():
    assert PRINTF_PLACEHOLDER.search("%s %d %f %r")
    source_root = Path(__file__).resolve().parents[2] / "src"
    # Match a Name or Attribute receiver when its final identifier contains "logger".
    # For a Call receiver, match only when a Name or Attribute on its func side
    # contains "logger"; this includes setup_logger(...).info and skips
    # datetime.now().strftime(...).
    # Known boundaries: only string-constant first arguments are scanned, so
    # dynamic/binary-formatted strings and ast.JoinedStr f-strings are skipped.
    # Names bound from logging.getLogger are skipped because stdlib logging uses
    # printf formatting. Factories/aliases with no "logger" in the func chain
    # can still leak past this rule (as can dynamic or f-string arguments).
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
            if isinstance(receiver, ast.Name):
                receiver_names = {receiver.id}
            elif isinstance(receiver, ast.Attribute):
                receiver_names = {receiver.attr}
            elif isinstance(receiver, ast.Call):
                receiver_names = {
                    child.id if isinstance(child, ast.Name) else child.attr
                    for child in ast.walk(receiver.func)
                    if isinstance(child, (ast.Name, ast.Attribute))
                }
            else:
                continue
            if not any("logger" in name for name in receiver_names) or (
                isinstance(receiver, ast.Name) and receiver.id in stdlib_logger_names
            ):
                continue
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                if PRINTF_PLACEHOLDER.search(first_arg.value):
                    violations.append(f"{relative_path}:{node.lineno}")
    assert not violations, violations
