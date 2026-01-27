#!/bin/bash
###############################################################################
# 核心模块提升一键迁移脚本
#
# 用途：自动执行 llm, cache, risk_control 模块从 utils/ 到一级目录的迁移
#
# 使用方法：
#   bash scripts/execute_migration.sh
#
# 注意：请在项目根目录下执行此脚本
###############################################################################

set -e  # 遇到错误立即退出

# Log helpers (ASCII only)
log_info() {
    echo "INFO: $1"
}

log_success() {
    echo "OK: $1"
}

log_warning() {
    echo "WARNING: $1"
}

log_error() {
    echo "ERROR: $1"
}

# 检查是否在项目根目录
if [ ! -f "pyproject.toml" ]; then
    log_error "Please run this script from the project root directory"
    exit 1
fi

echo "========================================================================"
echo "Core Module Migration Script"
echo "========================================================================"
echo "This script will:"
echo "  1. Create a migration branch"
echo "  2. Create a backup commit"
echo "  3. Run baseline tests"
echo "  4. Move modules (llm, cache, risk_control)"
echo "  5. Update import paths"
echo "  6. Validate migration"
echo "  7. Run post-migration tests"
echo "========================================================================"
echo ""

# 询问用户确认
read -p "Do you want to proceed? (y/n): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    log_warning "Migration cancelled by user"
    exit 0
fi

echo ""
log_info "Step 1/7: Creating migration branch..."
git checkout -b refactor/promote-core-modules || {
    log_warning "Branch already exists, switching to it"
    git checkout refactor/promote-core-modules
}
log_success "Branch ready"

echo ""
log_info "Step 2/7: Creating backup commit..."
git add -A
git commit -m "chore: snapshot before module migration" --allow-empty
log_success "Backup commit created"

echo ""
log_info "Step 3/7: Running baseline tests..."
log_warning "This may take a few minutes..."
uv run pytest tests/ -v --tb=short > migration_baseline_test.log 2>&1 || {
    log_warning "Some tests failed in baseline (see migration_baseline_test.log)"
    log_warning "Continuing anyway..."
}
log_success "Baseline tests completed"

echo ""
log_info "Step 4/7: Moving modules to top-level..."

# 进入 src/video_transcript_api 目录
cd src/video_transcript_api

# 检查模块是否存在
if [ ! -d "utils/llm" ] || [ ! -d "utils/cache" ] || [ ! -d "utils/risk_control" ]; then
    log_error "One or more modules not found in utils/"
    cd ../..
    exit 1
fi

# 移动模块
mv utils/llm ./llm
mv utils/cache ./cache
mv utils/risk_control ./risk_control

log_success "Modules moved successfully"

# 验证移动结果
if [ -d "llm" ] && [ -d "cache" ] && [ -d "risk_control" ]; then
    log_success "Verified: All modules in place"
else
    log_error "Verification failed: Some modules missing"
    cd ../..
    exit 1
fi

# 回到项目根目录
cd ../..

echo ""
log_info "Step 5/7: Updating import paths..."

# 运行迁移脚本（所有目录）
uv run python scripts/migrate_imports.py --all

log_success "Import paths updated"

echo ""
log_info "Step 6/7: Validating migration..."

# 运行验证脚本
uv run python scripts/validate_migration.py || {
    log_error "Validation found issues. Please review and fix manually."
    exit 1
}

log_success "Validation passed"

echo ""
log_info "Step 7/7: Running post-migration tests..."
log_warning "This may take a few minutes..."

# 运行单元测试
log_info "Running unit tests..."
uv run pytest tests/unit/ -v || {
    log_error "Unit tests failed"
    exit 1
}

# 运行 LLM 测试
log_info "Running LLM tests..."
uv run pytest tests/llm/ -v || {
    log_error "LLM tests failed"
    exit 1
}

# 运行缓存测试
log_info "Running cache tests..."
uv run pytest tests/cache/ -v || {
    log_error "Cache tests failed"
    exit 1
}

# 运行集成测试
log_info "Running integration tests..."
uv run pytest tests/integration/ -v || {
    log_error "Integration tests failed"
    exit 1
}

log_success "All tests passed"

echo ""
echo "========================================================================"
echo "Migration Completed Successfully."
echo "========================================================================"
echo ""
echo "Next steps:"
echo "  1. Review the changes: git diff HEAD~1"
echo "  2. Update documentation (README.md, architecture docs)"
echo "  3. Test the service manually: uv run python main.py --start"
echo "  4. Commit the changes:"
echo "     git add -A"
echo "     git commit -m 'refactor: promote llm, cache, risk_control to top-level modules'"
echo "  5. Push to remote: git push origin refactor/promote-core-modules"
echo ""
echo "Cleanup:"
echo "  - Baseline test log: migration_baseline_test.log"
echo ""
echo "If you need to rollback:"
echo "  - Git rollback: git reset --hard HEAD~2"
echo "  - Or run: bash scripts/rollback_migration.sh"
echo "========================================================================"
