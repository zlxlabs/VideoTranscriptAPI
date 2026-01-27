#!/bin/bash
###############################################################################
# 核心模块迁移回滚脚本
#
# 用途：如果迁移失败，将模块移回 utils/ 并还原导入路径
#
# 使用方法：
#   bash scripts/rollback_migration.sh
#
# 注意：请在项目根目录下执行此脚本
###############################################################################

set -e

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
echo "Module Migration Rollback Script"
echo "========================================================================"
echo "This script will:"
echo "  1. Move modules back to utils/"
echo "  2. Restore old import paths"
echo "  3. Validate rollback"
echo "========================================================================"
echo ""

# 询问用户确认
read -p "Do you want to proceed with rollback? (y/n): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    log_warning "Rollback cancelled by user"
    exit 0
fi

echo ""
log_info "Step 1/3: Moving modules back to utils/..."

cd src/video_transcript_api

# 检查模块是否在顶层
if [ ! -d "llm" ] && [ ! -d "cache" ] && [ ! -d "risk_control" ]; then
    log_error "No modules found at top-level. Already rolled back?"
    cd ../..
    exit 1
fi

# 确保 utils 目录存在
mkdir -p utils

# 移回原位置
if [ -d "llm" ]; then
    mv llm utils/llm
    log_success "Moved llm back to utils/"
fi

if [ -d "cache" ]; then
    mv cache utils/cache
    log_success "Moved cache back to utils/"
fi

if [ -d "risk_control" ]; then
    mv risk_control utils/risk_control
    log_success "Moved risk_control back to utils/"
fi

cd ../..

echo ""
log_info "Step 2/3: Restoring import paths..."

# 运行回滚脚本
uv run python scripts/rollback_imports.py --all

log_success "Import paths restored"

echo ""
log_info "Step 3/3: Validating rollback..."

# 检查是否还有新的导入路径（不包含 utils.）
log_info "Checking for residual new-style imports..."

# 简单的验证：确认关键文件已经恢复
if grep -q "from.*utils\.llm" src/video_transcript_api/api/context.py; then
    log_success "Verified: Old import paths restored"
else
    log_warning "Warning: Some import paths may not be fully restored"
fi

echo ""
echo "========================================================================"
echo "Rollback Completed"
echo "========================================================================"
echo ""
echo "Next steps:"
echo "  1. Run tests to verify: uv run pytest tests/"
echo "  2. Check git status: git status"
echo "  3. If satisfied, commit the rollback:"
echo "     git add -A"
echo "     git commit -m 'revert: rollback module migration'"
echo ""
echo "========================================================================"
