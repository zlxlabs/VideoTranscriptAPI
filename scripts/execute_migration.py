#!/usr/bin/env python3
"""
核心模块提升一键迁移脚本（跨平台版本）

用途：自动执行 llm, cache, risk_control 模块从 utils/ 到一级目录的迁移

使用方法：
    python scripts/execute_migration.py
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import List


def log_info(msg: str):
    print(f"INFO: {msg}")


def log_success(msg: str):
    print(f"OK: {msg}")


def log_warning(msg: str):
    print(f"WARNING: {msg}")


def log_error(msg: str):
    print(f"ERROR: {msg}")


def run_command(cmd: List[str], description: str, check: bool = True) -> bool:
    """
    执行命令并处理错误

    Args:
        cmd: 命令列表
        description: 命令描述
        check: 是否检查返回码

    Returns:
        成功返回 True，失败返回 False
    """
    try:
        subprocess.run(cmd, check=check, capture_output=False)
        return True
    except subprocess.CalledProcessError as e:
        if check:
            log_error(f"{description} failed with exit code {e.returncode}")
            return False
        else:
            log_warning(f"{description} completed with warnings")
            return True


def main():
    # 检查是否在项目根目录
    project_root = Path.cwd()
    if not (project_root / "pyproject.toml").exists():
        log_error("Please run this script from the project root directory")
        sys.exit(1)

    print("=" * 70)
    print("Core Module Migration Script (Cross-Platform)")
    print("=" * 70)
    print("This script will:")
    print("  1. Create a migration branch")
    print("  2. Create a backup commit")
    print("  3. Run baseline tests")
    print("  4. Move modules (llm, cache, risk_control)")
    print("  5. Update import paths")
    print("  6. Validate migration")
    print("  7. Run post-migration tests")
    print("=" * 70)
    print()

    # 询问用户确认
    response = input("Do you want to proceed? (y/n): ").strip().lower()
    if response not in ['y', 'yes']:
        log_warning("Migration cancelled by user")
        sys.exit(0)

    print()
    log_info("Step 1/7: Creating migration branch...")

    # 尝试创建分支
    result = subprocess.run(
        ["git", "checkout", "-b", "refactor/promote-core-modules"],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        if "already exists" in result.stderr:
            log_warning("Branch already exists, switching to it")
            run_command(
                ["git", "checkout", "refactor/promote-core-modules"],
                "Switch to branch",
            )
        else:
            log_error("Failed to create branch")
            sys.exit(1)

    log_success("Branch ready")

    print()
    log_info("Step 2/7: Creating backup commit...")
    run_command(["git", "add", "-A"], "Stage all files")
    run_command(
        ["git", "commit", "-m", "chore: snapshot before module migration", "--allow-empty"],
        "Create backup commit",
    )
    log_success("Backup commit created")

    print()
    log_info("Step 3/7: Running baseline tests...")
    log_warning("This may take a few minutes...")

    baseline_log = project_root / "migration_baseline_test.log"
    with open(baseline_log, "w", encoding="utf-8") as f:
        result = subprocess.run(
            ["uv", "run", "pytest", "tests/", "-v", "--tb=short"],
            stdout=f,
            stderr=subprocess.STDOUT,
        )

    if result.returncode != 0:
        log_warning("Some tests failed in baseline (see migration_baseline_test.log)")
        log_warning("Continuing anyway...")

    log_success("Baseline tests completed")

    print()
    log_info("Step 4/7: Moving modules to top-level...")

    src_dir = project_root / "src" / "video_transcript_api"
    utils_dir = src_dir / "utils"

    # 检查模块是否存在
    modules = ["llm", "cache", "risk_control"]
    for module in modules:
        if not (utils_dir / module).exists():
            log_error(f"Module '{module}' not found in utils/")
            sys.exit(1)

    # 移动模块
    for module in modules:
        src_path = utils_dir / module
        dest_path = src_dir / module

        if dest_path.exists():
            log_warning(f"Destination '{module}' already exists, removing it first")
            shutil.rmtree(dest_path)

        shutil.move(str(src_path), str(dest_path))
        log_success(f"Moved {module}")

    # 验证移动结果
    all_exist = all((src_dir / module).exists() for module in modules)
    if all_exist:
        log_success("Verified: All modules in place")
    else:
        log_error("Verification failed: Some modules missing")
        sys.exit(1)

    print()
    log_info("Step 5/7: Updating import paths...")

    if not run_command(
        ["uv", "run", "python", "scripts/migrate_imports.py", "--all"],
        "Update import paths",
    ):
        sys.exit(1)

    log_success("Import paths updated")

    print()
    log_info("Step 6/7: Validating migration...")

    result = subprocess.run(
        ["uv", "run", "python", "scripts/validate_migration.py"],
        capture_output=False,
    )

    if result.returncode != 0:
        log_error("Validation found issues. Please review and fix manually.")
        sys.exit(1)

    log_success("Validation passed")

    print()
    log_info("Step 7/7: Running post-migration tests...")
    log_warning("This may take a few minutes...")

    test_suites = [
        ("Unit tests", ["tests/unit/"]),
        ("LLM tests", ["tests/llm/"]),
        ("Cache tests", ["tests/cache/"]),
        ("Integration tests", ["tests/integration/"]),
    ]

    for suite_name, suite_path in test_suites:
        log_info(f"Running {suite_name}...")
        if not run_command(
            ["uv", "run", "pytest"] + suite_path + ["-v"],
            suite_name,
        ):
            log_error(f"{suite_name} failed")
            sys.exit(1)

    log_success("All tests passed")

    print()
    print("=" * 70)
    print("Migration Completed Successfully.")
    print("=" * 70)
    print()
    print("Next steps:")
    print("  1. Review the changes: git diff HEAD~1")
    print("  2. Update documentation (README.md, architecture docs)")
    print("  3. Test the service manually: uv run python main.py --start")
    print("  4. Commit the changes:")
    print("     git add -A")
    print("     git commit -m 'refactor: promote llm, cache, risk_control to top-level modules'")
    print("  5. Push to remote: git push origin refactor/promote-core-modules")
    print()
    print("Cleanup:")
    print("  - Baseline test log: migration_baseline_test.log")
    print()
    print("If you need to rollback:")
    print("  - Git rollback: git reset --hard HEAD~2")
    print("  - Or run: python scripts/rollback_migration_full.py")
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        log_warning("Migration interrupted by user")
        sys.exit(1)
    except Exception as e:
        print()
        log_error(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
