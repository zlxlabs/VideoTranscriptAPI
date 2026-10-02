from __future__ import annotations

import runpy
from pathlib import Path

from coverage import Coverage


REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
CANARY_SOURCE = """\
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


def _write_canary(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    canary = tmp_path / "src" / "video_transcript_api" / "coverage_canary.py"
    canary.parent.mkdir(parents=True)
    canary.write_text(CANARY_SOURCE, encoding="utf-8")
    lines = CANARY_SOURCE.splitlines()
    except_line = lines.index("    except Exception:")
    line_numbers = {
        "type_checking": next(
            number
            for number, line in enumerate(lines, start=1)
            if line == "if TYPE_CHECKING:"
        ),
        "f_definition": next(
            number
            for number, line in enumerate(lines, start=1)
            if line == "def f(password):"
        ),
        "except_pass": next(
            number
            for number, line in enumerate(lines, start=1)
            if line == "        pass" and number > except_line + 1
        ),
    }
    return canary, line_numbers


def _analyze_canary(
    canary: Path,
    *,
    config_file: Path = PYPROJECT,
) -> tuple[int, ...]:
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


def test_real_pyproject_exclusions_are_precise_and_reversible(tmp_path, monkeypatch):
    canary, line_numbers = _write_canary(tmp_path)
    monkeypatch.chdir(tmp_path)

    configured_coverage = Coverage(config_file=str(PYPROJECT))
    assert configured_coverage.get_option("run:omit") in (None, [])
    exclude_lines = configured_coverage.get_option("report:exclude_lines")
    assert "pass" not in exclude_lines
    assert "pragma: no cover" not in exclude_lines
    assert any("TYPE_CHECKING" in line for line in exclude_lines)
    assert configured_coverage.get_option("report:exclude_also") == [
        "if __name__ == .__main__.",
    ]

    excluded = _analyze_canary(canary)
    assert line_numbers["f_definition"] not in excluded
    assert line_numbers["except_pass"] not in excluded
    assert line_numbers["type_checking"] in excluded

    real_config = PYPROJECT.read_text(encoding="utf-8")
    original_rule = 'exclude_also = [\n    "if __name__ == .__main__.",\n]'
    reverse_config = tmp_path / "pyproject.reverse.toml"
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
    reverse_excluded = _analyze_canary(canary, config_file=reverse_config)
    assert (
        line_numbers["f_definition"] in reverse_excluded
        or line_numbers["except_pass"] in reverse_excluded
    )
