#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

uv run --frozen pytest -q tests/unit/test_coverage_exclude_rules.py

uv run --frozen python - "$repo_root" <<'PY'
import os
import runpy
import sys
import tempfile
from pathlib import Path

from coverage import Coverage


repo_root = Path(sys.argv[1]).resolve()
pyproject = repo_root / "pyproject.toml"
source = """\
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from x import Y

def f(password):
    return password

def g():
    try:
        pass
    except Exception:
        pass

f("covered")
g()
"""
lines = source.splitlines()
except_line = lines.index("    except Exception:")
line_numbers = {
    "type_checking": lines.index("if TYPE_CHECKING:") + 1,
    "f_definition": lines.index("def f(password):") + 1,
    "except_pass": next(
        index + 1
        for index, line in enumerate(lines)
        if line == "        pass" and index > except_line
    ),
}


def analyze(canary: Path, *, config_file: Path = pyproject) -> tuple[int, ...]:
    coverage = Coverage(
        config_file=str(config_file),
        data_file=str(canary.parent / ".coverage-canary"),
    )

    coverage.start()
    try:
        runpy.run_path(str(canary), run_name="__main__")
    finally:
        coverage.stop()
    coverage.save()
    return tuple(coverage.analysis2(str(canary))[2])


with tempfile.TemporaryDirectory(prefix="coverage-exclude-") as temp_dir:
    temp_root = Path(temp_dir)
    canary = temp_root / "src" / "video_transcript_api" / "coverage_canary.py"
    canary.parent.mkdir(parents=True)
    canary.write_text(source, encoding="utf-8")

    os.chdir(temp_root)
    normal_excluded = analyze(canary)
    real_config = pyproject.read_text(encoding="utf-8")
    original_rule = 'exclude_also = [\n    "if __name__ == .__main__.",\n]'
    reverse_config = temp_root / "pyproject.reverse.toml"
    reverse_config.write_text(
        real_config.replace(
            original_rule,
            'exclude_also = [\n'
            '    "if __name__ == .__main__.",\n'
            '    "pass",\n'
            ']',
        ),
        encoding="utf-8",
    )
    assert reverse_config.read_text(encoding="utf-8") != real_config
    reverse_excluded = analyze(canary, config_file=reverse_config)

    assert line_numbers["f_definition"] not in normal_excluded
    assert line_numbers["except_pass"] not in normal_excluded
    assert line_numbers["type_checking"] in normal_excluded
    assert (
        line_numbers["f_definition"] in reverse_excluded
        or line_numbers["except_pass"] in reverse_excluded
    )

print(f"canary normal excluded_lines={list(normal_excluded)}")
print(f"canary reverse excluded_lines={list(reverse_excluded)}")
print("canary assertions: PASS")

os.chdir(repo_root)
configured = Coverage(config_file=str(pyproject))
assert configured.get_option("run:omit") in (None, [])
exclude_lines = configured.get_option("report:exclude_lines")
assert "pass" not in exclude_lines
assert "pragma: no cover" not in exclude_lines
assert any("TYPE_CHECKING" in line for line in exclude_lines)
assert configured.get_option("report:exclude_also") == [
    "if __name__ == .__main__.",
]
for relative_path in (
    "src/video_transcript_api/api/services/task_dedup.py",
    "src/video_transcript_api/api/services/view_token_resolver.py",
):
    excluded = configured.analysis2(str(repo_root / relative_path))[2]
    print(f"{relative_path}: excluded_lines={excluded}")
    assert excluded == (
        [7, 8]
        if relative_path.endswith("task_dedup.py")
        else [9, 10]
    )
print("configuration assertions: PASS")
PY
