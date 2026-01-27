#!/usr/bin/env python3
"""
模块迁移验证脚本

用途：检查迁移后是否还有遗漏的旧导入路径

使用方法：
    python scripts/validate_migration.py
"""

import re
from pathlib import Path
from typing import List, Tuple


MODULES_TO_CHECK = ["llm", "cache", "risk_control"]


def find_python_files(directory: Path) -> List[Path]:
    """递归查找目录下所有 .py 文件"""
    return list(directory.rglob("*.py"))


def is_top_level_core_module(file_path: Path, modules: List[str]) -> bool:
    """判断文件是否位于迁移后的顶层模块中。"""
    try:
        parts = file_path.parts
        idx = parts.index("video_transcript_api")
    except ValueError:
        return False
    if idx + 1 >= len(parts):
        return False
    return parts[idx + 1] in modules


def check_old_imports(file_path: Path, modules: List[str]) -> List[Tuple[int, str]]:
    """
    检查文件中是否还有旧的导入路径

    Args:
        file_path: 文件路径
        modules: 需要检查的模块列表

    Returns:
        [(行号, 行内容), ...]
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        return []

    issues = []

    for i, line in enumerate(lines, start=1):
        for module in modules:
            # 检查是否包含旧的 utils 路径（已迁移到顶级）
            pattern = rf"utils\.{module}"
            if re.search(pattern, line):
                issues.append((i, line.rstrip()))

        # 检查迁移后顶层模块内是否仍在使用旧的相对 logging 导入
        if is_top_level_core_module(file_path, modules):
            if "utils.logging" not in line:
                pattern_logging = r"(from\s+)(\.+)(logging)(\s+import\s+)"
                if re.search(pattern_logging, line):
                    issues.append((i, line.rstrip()))

    return issues


def main():
    project_root = Path(__file__).parent.parent

    # 检查的目录
    target_dirs = [
        project_root / "src",
        project_root / "tests",
        project_root / "scripts",
    ]

    print("=" * 70)
    print("Module Migration Validation")
    print("=" * 70)
    print(
        "Checking for old import paths: "
        + ", ".join(f"utils.{m}" for m in MODULES_TO_CHECK)
    )
    print("=" * 70)
    print()

    total_issues = 0
    files_with_issues = 0

    for target_dir in target_dirs:
        if not target_dir.exists():
            continue

        print(f"Scanning directory: {target_dir.relative_to(project_root)}")
        python_files = find_python_files(target_dir)

        for file_path in python_files:
            issues = check_old_imports(file_path, MODULES_TO_CHECK)

            if issues:
                files_with_issues += 1
                total_issues += len(issues)

                rel_path = file_path.relative_to(project_root)
                print(f"\nERROR: {rel_path}")
                for line_num, line_content in issues:
                    print(f"   Line {line_num}: {line_content}")

        print()

    print("=" * 70)
    print("Validation Summary")
    print("=" * 70)

    if total_issues == 0:
        print("OK: No old import paths found. Migration appears successful.")
    else:
        print(
            f"ERROR: Found {total_issues} old import path(s) in "
            f"{files_with_issues} file(s)"
        )
        print("\nPlease review and fix these manually, or re-run the migration script.")

    print("=" * 70)

    return 0 if total_issues == 0 else 1


if __name__ == "__main__":
    exit(main())
