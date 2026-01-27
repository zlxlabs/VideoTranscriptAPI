#!/usr/bin/env python3
"""
模块导入路径迁移脚本

用途：将 llm, cache, risk_control 从 utils/ 提升到一级目录后，
     批量更新所有 Python 文件中的导入语句。

使用方法：
    # 预览模式（不实际修改）
    python scripts/migrate_imports.py --dry-run

    # 迁移源代码
    python scripts/migrate_imports.py --target-dir src

    # 迁移测试代码
    python scripts/migrate_imports.py --target-dir tests

    # 迁移所有代码
    python scripts/migrate_imports.py --all
"""

import argparse
import re
from pathlib import Path
from typing import List, Tuple


# 需要迁移的模块列表
MODULES_TO_MIGRATE = ["llm", "cache", "risk_control"]


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


def find_python_files(directory: Path) -> List[Path]:
    """递归查找目录下所有 .py 文件"""
    return list(directory.rglob("*.py"))


def migrate_import_line(
    line: str, modules: List[str], file_path: Path | None = None
) -> Tuple[str, bool]:
    """
    迁移单行导入语句

    Args:
        line: 原始行
        modules: 需要迁移的模块列表

    Returns:
        (新行, 是否修改)
    """
    modified = False

    for module in modules:
        # 模式 1: from ...utils.llm import ...
        pattern1 = rf"(from\s+)(\.+)(utils\.{module})"
        if re.search(pattern1, line):
            # 移除 utils. 部分
            line = re.sub(pattern1, rf"\1\2{module}", line)
            modified = True

        # 模式 2: from video_transcript_api.utils.llm import ...
        pattern2 = rf"(from\s+video_transcript_api\.)(utils\.{module})"
        if re.search(pattern2, line):
            line = re.sub(pattern2, rf"\1{module}", line)
            modified = True

        # 模式 3: import video_transcript_api.utils.llm
        pattern3 = rf"(import\s+video_transcript_api\.)(utils\.{module})"
        if re.search(pattern3, line):
            line = re.sub(pattern3, rf"\1{module}", line)
            modified = True

        # 模式 4: from src.video_transcript_api.utils.llm import ...
        pattern4 = rf"(from\s+src\.video_transcript_api\.)(utils\.{module})"
        if re.search(pattern4, line):
            line = re.sub(pattern4, rf"\1{module}", line)
            modified = True

        # 模式 5: import src.video_transcript_api.utils.llm
        pattern5 = rf"(import\s+src\.video_transcript_api\.)(utils\.{module})"
        if re.search(pattern5, line):
            line = re.sub(pattern5, rf"\1{module}", line)
            modified = True

    if file_path and is_top_level_core_module(file_path, modules):
        if "utils.logging" not in line:
            pattern_logging = r"(from\s+)(\.+)(logging)(\s+import\s+)"
            if re.search(pattern_logging, line):
                line = re.sub(pattern_logging, r"\1\2utils.logging\4", line)
                modified = True

    return line, modified


def migrate_file(file_path: Path, dry_run: bool = False) -> Tuple[int, List[str]]:
    """
    迁移单个文件的导入语句

    Args:
        file_path: 文件路径
        dry_run: 是否仅预览不实际修改

    Returns:
        (修改行数, 修改内容列表)
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        print(f"WARNING: Skipping {file_path} (encoding issue)")
        return 0, []

    new_lines = []
    changes = []
    total_modified = 0

    for i, line in enumerate(lines, start=1):
        new_line, modified = migrate_import_line(
            line, MODULES_TO_MIGRATE, file_path=file_path
        )
        new_lines.append(new_line)

        if modified:
            total_modified += 1
            changes.append(f"  Line {i}:")
            changes.append(f"    - {line.rstrip()}")
            changes.append(f"    + {new_line.rstrip()}")

    if total_modified > 0 and not dry_run:
        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

    return total_modified, changes


def main():
    parser = argparse.ArgumentParser(
        description="Migrate import paths for promoted modules"
    )
    parser.add_argument(
        "--target-dir",
        type=str,
        default="src",
        help="Target directory to migrate (default: src)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Migrate all directories (src, tests, scripts)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files",
    )

    args = parser.parse_args()

    # 确定要处理的目录
    project_root = Path(__file__).parent.parent

    if args.all:
        target_dirs = [
            project_root / "src",
            project_root / "tests",
            project_root / "scripts",
        ]
    else:
        target_dirs = [project_root / args.target_dir]

    # 验证目录存在
    for target_dir in target_dirs:
        if not target_dir.exists():
            print(f"ERROR: Directory not found: {target_dir}")
            return

    print("=" * 70)
    print("Module Import Migration Script")
    print("=" * 70)
    print(
        f"Mode: {'DRY RUN (preview only)' if args.dry_run else 'LIVE (will modify files)'}"
    )
    print(f"Modules to migrate: {', '.join(MODULES_TO_MIGRATE)}")
    print(f"Target directories: {[str(d.relative_to(project_root)) for d in target_dirs]}")
    print("=" * 70)
    print()

    total_files_checked = 0
    total_files_modified = 0
    total_lines_modified = 0

    for target_dir in target_dirs:
        print(f"Processing directory: {target_dir.relative_to(project_root)}")
        python_files = find_python_files(target_dir)

        for file_path in python_files:
            total_files_checked += 1
            modified_count, changes = migrate_file(file_path, dry_run=args.dry_run)

            if modified_count > 0:
                total_files_modified += 1
                total_lines_modified += modified_count

                rel_path = file_path.relative_to(project_root)
                print(f"\nChanged: {rel_path} ({modified_count} lines)")
                for change in changes:
                    print(change)

        print()

    print("=" * 70)
    print("Migration Summary")
    print("=" * 70)
    print(f"Total files checked: {total_files_checked}")
    print(f"Files modified: {total_files_modified}")
    print(f"Lines modified: {total_lines_modified}")

    if args.dry_run:
        print("\nWARNING: This was a DRY RUN. No files were actually modified.")
        print("   Run without --dry-run to apply changes.")
    else:
        print("\nOK: Migration completed successfully.")

    print("=" * 70)


if __name__ == "__main__":
    main()
