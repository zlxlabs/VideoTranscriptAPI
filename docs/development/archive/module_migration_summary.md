# 核心模块迁移方案 - 总结

## 📦 交付物清单

为了完成 `llm/`, `cache/`, `risk_control/` 三个模块从 `utils/` 提升到一级目录的迁移，我已经创建了以下文件：

---

## 📄 文档文件

### 1. **详细迁移方案** (`docs/development/module_migration_plan.md`)

**用途**：完整的迁移方案文档，包括：
- 迁移概述和原因
- 影响范围分析
- 详细的 7 个阶段执行步骤
- 回滚方案
- 验收标准
- 风险点和缓解措施
- 预计时间线

**适用人群**：需要了解迁移全貌的项目负责人

---

### 2. **快速开始指南** (`docs/development/module_migration_quickstart.md`)

**用途**：精简的执行指南，包括：
- 快速一键执行方式
- 手动逐步执行方式
- 回滚方案
- 验收清单
- 常见问题解答

**适用人群**：执行迁移的开发者

---

### 3. **本文档** (`docs/development/module_migration_summary.md`)

**用途**：所有交付物的索引和说明

---

## 🛠️ 自动化脚本

### 4. **导入路径迁移脚本** (`scripts/migrate_imports.py`)

**用途**：批量更新 Python 文件中的导入语句

**功能**：
- 替换 `from ...utils.llm` → `from ...llm`
- 替换 `from ..utils.cache` → `from ..cache`
- 替换 `from ...utils.risk_control` → `from ...risk_control`
- 支持指定目录迁移（`--target-dir`）
- 支持全部目录迁移（`--all`）
- 支持预览模式（`--dry-run`）

**使用方法**：
```bash
# 预览更改
python scripts/migrate_imports.py --all --dry-run

# 执行迁移
python scripts/migrate_imports.py --all
```

---

### 5. **导入路径回滚脚本** (`scripts/rollback_imports.py`)

**用途**：反向操作，将导入路径还原回 `utils/` 下

**功能**：
- 替换 `from ...llm` → `from ...utils.llm`
- 替换 `from ..cache` → `from ..utils.cache`
- 替换 `from ...risk_control` → `from ...utils.risk_control`
- 支持预览模式（`--dry-run`）

**使用方法**：
```bash
# 执行回滚
python scripts/rollback_imports.py --all
```

---

### 6. **迁移验证脚本** (`scripts/validate_migration.py`)

**用途**：检查迁移后是否还有遗漏的旧导入路径

**功能**：
- 扫描所有 Python 文件
- 查找 `utils.llm`, `utils.cache`, `utils.risk_control` 等旧路径
- 报告遗漏的文件和行号
- 返回非零退出码（如果发现问题）

**使用方法**：
```bash
python scripts/validate_migration.py
```

---

### 7. **一键迁移脚本（Shell 版本）** (`scripts/execute_migration.sh`)

**用途**：自动化执行完整迁移流程

**适用平台**：Linux / macOS / Git Bash on Windows

**功能**：
1. 创建迁移分支
2. 备份当前状态
3. 运行基线测试
4. 移动模块目录
5. 更新导入路径
6. 验证迁移
7. 运行完整测试

**使用方法**：
```bash
bash scripts/execute_migration.sh
```

---

### 8. **一键迁移脚本（Python 版本）** (`scripts/execute_migration.py`)

**用途**：跨平台版本的一键迁移脚本

**适用平台**：Windows / Linux / macOS（推荐）

**功能**：与 Shell 版本相同，但使用纯 Python 实现

**使用方法**：
```bash
python scripts/execute_migration.py
```

---

### 9. **一键回滚脚本（Shell 版本）** (`scripts/rollback_migration.sh`)

**用途**：快速回滚迁移

**适用平台**：Linux / macOS / Git Bash on Windows

**功能**：
1. 移动模块回 `utils/`
2. 还原导入路径
3. 验证回滚结果

**使用方法**：
```bash
bash scripts/rollback_migration.sh
```

---

### 10. **一键回滚脚本（Python 版本）** (`scripts/rollback_migration_full.py`)

**用途**：跨平台版本的一键回滚脚本

**适用平台**：Windows / Linux / macOS（推荐）

**功能**：与 Shell 版本相同

**使用方法**：
```bash
python scripts/rollback_migration_full.py
```

---

## 🚀 推荐执行流程

### Windows 用户（推荐）

```bash
# 1. 预览导入路径更改
python scripts/migrate_imports.py --all --dry-run

# 2. 执行完整迁移
python scripts/execute_migration.py

# 3. 如果失败，回滚
python scripts/rollback_migration_full.py
```

---

### Linux / macOS 用户

#### 方式一：使用 Python 脚本（推荐）

```bash
# 同 Windows 流程
python scripts/execute_migration.py
```

#### 方式二：使用 Shell 脚本

```bash
# 执行迁移
bash scripts/execute_migration.sh

# 如果失败，回滚
bash scripts/rollback_migration.sh
```

---

## 📋 迁移检查清单

在执行迁移前，请确认：

- [ ] 已阅读 `module_migration_plan.md` 了解全貌
- [ ] 已阅读 `module_migration_quickstart.md` 了解步骤
- [ ] 当前工作目录是项目根目录
- [ ] Git 仓库状态干净（无未提交更改）
- [ ] 已安装 `uv` 包管理器
- [ ] 基线测试可以正常运行

执行迁移后，请确认：

- [ ] 运行 `validate_migration.py` 无告警
- [ ] 所有测试套件通过
- [ ] 服务可以正常启动
- [ ] 手动测试关键功能正常
- [ ] README.md 已更新
- [ ] 架构文档已更新

---

## 🗂️ 文件路径速查

| 文件 | 路径 | 用途 |
|------|------|------|
| 详细方案 | `docs/development/module_migration_plan.md` | 完整迁移方案 |
| 快速指南 | `docs/development/module_migration_quickstart.md` | 执行指南 |
| 总结文档 | `docs/development/module_migration_summary.md` | 本文档 |
| 导入迁移 | `scripts/migrate_imports.py` | 更新导入路径 |
| 导入回滚 | `scripts/rollback_imports.py` | 还原导入路径 |
| 迁移验证 | `scripts/validate_migration.py` | 检查遗漏 |
| 一键迁移（Shell） | `scripts/execute_migration.sh` | 自动化迁移 |
| 一键迁移（Python） | `scripts/execute_migration.py` | 跨平台迁移 |
| 一键回滚（Shell） | `scripts/rollback_migration.sh` | 自动化回滚 |
| 一键回滚（Python） | `scripts/rollback_migration_full.py` | 跨平台回滚 |

---

## 💡 最佳实践建议

1. **首次执行建议使用 `--dry-run` 模式**
   ```bash
   python scripts/migrate_imports.py --all --dry-run
   ```

2. **保留基线测试日志**
   - 用于对比迁移前后的测试结果
   - 文件：`migration_baseline_test.log`

3. **分阶段验证**
   - 移动模块后，先验证文件系统
   - 更新导入后，运行验证脚本
   - 最后运行完整测试套件

4. **手动验证关键文件**
   - `src/video_transcript_api/api/context.py`
   - `src/video_transcript_api/api/services/transcription.py`
   - `src/video_transcript_api/api/app.py`

5. **提交前做最后检查**
   ```bash
   # 检查 Git 状态
   git status

   # 查看具体更改
   git diff

   # 确认迁移完整
   python scripts/validate_migration.py
   ```

---

## 🆘 问题排查

### 问题 1：导入脚本报告 "encoding issue"

**原因**：某些文件可能使用了特殊编码

**解决方案**：
- 这些文件通常可以安全忽略
- 如果是关键文件，手动用 UTF-8 重新保存

---

### 问题 2：测试失败

**原因**：可能有遗漏的导入路径

**解决方案**：
```bash
# 运行验证脚本
python scripts/validate_migration.py

# 手动检查失败的测试文件
# 重新运行迁移脚本
python scripts/migrate_imports.py --all
```

---

### 问题 3：验证脚本发现旧路径

**原因**：某些特殊格式的导入可能未被识别

**解决方案**：
- 手动修改这些文件
- 或调整 `migrate_imports.py` 中的正则表达式模式

---

### 问题 4：服务启动报 ImportError

**原因**：某些动态导入或配置文件未更新

**解决方案**：
- 检查错误堆栈中的文件路径
- 手动更新相关文件
- 搜索整个项目：`grep -r "utils\.llm" .`

---

## 📞 获取帮助

如果遇到未在此文档覆盖的问题：

1. 查看详细的 `module_migration_plan.md` 文档
2. 检查 Git 提交历史和日志
3. 运行验证脚本获取详细错误信息
4. 如有必要，使用 Git 回滚并重新尝试

---

## ✅ 完成标志

迁移成功的标准：

- ✅ 所有脚本执行无错误
- ✅ `validate_migration.py` 无告警
- ✅ 测试套件全部通过
- ✅ 服务正常启动和运行
- ✅ 文档已更新
- ✅ Git 提交已推送

恭喜！模块迁移完成！🎉
