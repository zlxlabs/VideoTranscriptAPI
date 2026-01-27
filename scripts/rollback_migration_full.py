#!/usr/bin/env python3
"""
核心模块迁移回滚脚本（跨平台版本）

用途：如果迁移失败，将模块移回 utils/ 并还原导入路径

使用方法：
    python scripts/rollback_migration_full.py
"""

import shutil
import subprocess
import sys
from pathlib import Path


def log_info(msg: str):
    print(f"INFO: {msg}")


def log_success(msg: str):
    print(f"OK: {msg}")


def log_warning(msg: str):
    print(f"WARNING: {msg}")


def log_error(msg: str):
    print(f"ERROR: {msg}")


def main():
    # 检查是否在项目根目录
    project_root = Path.cwd()
    if not (project_root / "pyproject.toml").exists():
        log_error("Please run this script from the project root directory")
        sys.exit(1)

    print("=" * 70)
    print("Module Migration Rollback Script (Cross-Platform)")
    print("=" * 70)
    print("This script will:")
    print("  1. Move modules back to utils/")
    print("  2. Restore old import paths")
    print("  3. Validate rollback")
    print("=" * 70)
    print()

    # 询问用户确认
    response = input("Do you want to proceed with rollback? (y/n): ").strip().lower()
    if response not in ['y', 'yes']:
        log_warning("Rollback cancelled by user")
        sys.exit(0)

    print()
    log_info("Step 1/3: Moving modules back to utils/...")

    src_dir = project_root / "src" / "video_transcript_api"
    utils_dir = src_dir / "utils"

    # 确保 utils 目录存在
    utils_dir.mkdir(exist_ok=True)

    modules = ["llm", "cache", "risk_control"]

    # 检查模块是否在顶层
    top_level_modules = [m for m in modules if (src_dir / m).exists()]

    if not top_level_modules:
        log_error("No modules found at top-level. Already rolled back?")
        sys.exit(1)

    # 移回原位置
    for module in top_level_modules:
        src_path = src_dir / module
        dest_path = utils_dir / module

        if dest_path.exists():
            log_warning(f"Destination 'utils/{module}' already exists, removing it first")
            shutil.rmtree(dest_path)

        shutil.move(str(src_path), str(dest_path))
        log_success(f"Moved {module} back to utils/")

    print()
    log_info("Step 2/3: Restoring import paths...")

    result = subprocess.run(
        ["uv", "run", "python", "scripts/rollback_imports.py", "--all"],
        capture_output=False,
    )

    if result.returncode != 0:
        log_error("Failed to restore import paths")
        sys.exit(1)

    log_success("Import paths restored")

    print()
    log_info("Step 3/3: Validating rollback...")

    # 简单验证：检查关键文件
    context_file = src_dir / "api" / "context.py"
    if context_file.exists():
        content = context_file.read_text(encoding="utf-8")
        if "from ..llm" in content or "from ...llm" in content:
            log_success("Verified: Old import paths restored in context.py")
        else:
            log_warning("Warning: Some import paths may not be fully restored")

    print()
    print("=" * 70)
    print("Rollback Completed")
    print("=" * 70)
    print()
    print("Next steps:")
    print("  1. Run tests to verify: uv run pytest tests/")
    print("  2. Check git status: git status")
    print("  3. If satisfied, commit the rollback:")
    print("     git add -A")
    print("     git commit -m 'revert: rollback module migration'")
    print()
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        log_warning("Rollback interrupted by user")
        sys.exit(1)
    except Exception as e:
        print()
        log_error(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
