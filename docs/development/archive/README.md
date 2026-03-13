# 文档归档

本目录存放已完成的文档，供历史参考。

## 归档说明

### LLM 模块重构（2026-01-27）

**归档原因**：LLM 引擎已完成模块化重构，旧架构和过渡文档已移除。

**保留文档**：
- `architecture_review.md` - 旧架构评审报告
- `refactoring_plan.md` - 重构方案设计
- `refactoring_completed.md` - 重构完成报告
- `summary_feature_design.md` - 总结功能设计

### 架构优化（2026-01-27）

**归档原因**：业务流程重构已完成，过渡文档已移除。

**保留文档**：
- `architecture_optimization_plan.md` - 架构优化方案
- `architecture_optimization_phase1.md` - 阶段一完成报告
- `architecture_optimization_phase2.md` - 阶段二完成报告

### 模块迁移（2026-01-27）

**归档原因**：核心模块已从 `utils/` 提升到一级目录，迁移文档已移除。

**保留文档**：
- `module_migration_plan.md` - 模块迁移方案
- `module_migration_quickstart.md` - 快速开始指南
- `module_migration_summary.md` - 迁移总结

## 查看归档文档

所有归档文档都可以通过 Git 历史查看。如需恢复，可使用：

```bash
git show <commit_hash>:docs/development/<file_path>
```

## 文档维护原则

1. **删除已完成的过渡文档**：迁移、切换、阶段完成等文档在完成实施后应移除
2. **保留设计方案文档**：重要的架构设计、功能设计方案应保留
3. **归档而非删除**：对于仍有参考价值的文档，移至归档目录
4. **保持文档时效性**：定期清理过时文档，确保文档库的清晰和有效
