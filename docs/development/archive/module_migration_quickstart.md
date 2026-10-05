# 核心模块迁移 - 快速开始指南

## 🎯 目标

将三个核心模块提升到一级目录：
- `utils/llm/` → `llm/`
- `utils/cache/` → `cache/`
- `utils/risk_control/` → `risk_control/`

---

## ⚡ 快速执行（推荐）

### 方式一：一键自动化脚本

#### Windows / macOS / Linux（推荐）

```bash
# 在项目根目录执行（跨平台 Python 版本）
python scripts/execute_migration.py
```

#### Linux / macOS（Shell 版本）

```bash
# 在项目根目录执行
bash scripts/execute_migration.sh
```

脚本会自动完成所有步骤，包括：
- ✅ 创建迁移分支
- ✅ 备份当前状态
- ✅ 运行基线测试
- ✅ 移动模块
- ✅ 更新导入路径
- ✅ 验证迁移
- ✅ 运行完整测试

**预计时间：10-15 分钟**（取决于测试套件速度）

---

## 📋 手动执行（逐步控制）

如果你想逐步控制迁移过程：

### 步骤 1：准备工作

```bash
# 创建迁移分支
git checkout -b refactor/promote-core-modules

# 备份当前状态
git add -A
git commit -m "chore: snapshot before module migration"

# 运行基线测试（可选）
uv run pytest tests/ -v > baseline_test.log 2>&1
```

### 步骤 2：预览更改

```bash
# 预览导入路径更改（不实际修改）
uv run python scripts/migrate_imports.py --all --dry-run
```

### 步骤 3：移动模块

```bash
cd src/video_transcript_api

# 移动三个模块
mv utils/llm ./llm
mv utils/cache ./cache
mv utils/risk_control ./risk_control

# 返回项目根目录
cd ../..
```

### 步骤 4：更新导入路径

```bash
# 更新所有目录的导入路径
uv run python scripts/migrate_imports.py --all
```

### 步骤 5：验证迁移

```bash
# 检查是否有遗漏的旧路径
uv run python scripts/validate_migration.py
```

### 步骤 6：运行测试

```bash
# 运行各类测试
uv run pytest tests/unit/ -v
uv run pytest tests/llm/ -v
uv run pytest tests/cache/ -v
uv run pytest tests/integration/ -v
```

### 步骤 7：手动验证

```bash
# 启动服务
uv run python main.py --start

# 在另一个终端测试关键功能
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/video.mp4"}'
```

### 步骤 8：提交更改

```bash
git add -A
git commit -m "refactor: promote llm, cache, risk_control to top-level modules

- Move utils/llm → llm/
- Move utils/cache → cache/
- Move utils/risk_control → risk_control/
- Update all import paths across codebase
- Update tests and documentation

BREAKING CHANGE: Module paths changed for core business modules
"

git push origin refactor/promote-core-modules
```

---

## 🔄 如果需要回滚

### 方式一：一键回滚脚本

#### Windows / macOS / Linux（推荐）

```bash
# 跨平台 Python 版本
python scripts/rollback_migration_full.py
```

#### Linux / macOS（Shell 版本）

```bash
bash scripts/rollback_migration.sh
```

### 方式二：Git 回滚

```bash
# 回到迁移前的状态
git reset --hard HEAD~1

# 或者回到备份提交
git log --oneline  # 找到 "snapshot before module migration" 的 SHA
git reset --hard <commit-sha>
```

### 方式三：手动回滚

```bash
# 移回原位置
cd src/video_transcript_api
mv llm utils/llm
mv cache utils/cache
mv risk_control utils/risk_control
cd ../..

# 还原导入路径
uv run python scripts/rollback_imports.py --all
```

---

## ✅ 验收清单

迁移完成后，确认以下项目：

- [ ] 所有测试套件通过
- [ ] 服务正常启动，无导入错误
- [ ] 手动测试关键功能（提交任务、查看结果）
- [ ] 日志输出正常
- [ ] `validate_migration.py` 无告警
- [ ] README.md 中的目录结构已更新
- [ ] 架构文档已更新

---

## 📁 迁移后的目录结构

```
src/video_transcript_api/
├── api/                 # API 服务层
├── downloaders/         # 内容下载器
├── transcriber/         # 语音识别
├── llm/                 # ⬆️ 文本智能处理（新位置）
├── cache/               # ⬆️ 缓存管理（新位置）
├── risk_control/        # ⬆️ 风控系统（新位置）
└── utils/               # 通用工具（保留其他模块）
    ├── accounts/
    ├── logging/
    ├── notifications/
    ├── rendering/
    └── ...
```

---

## 🆘 常见问题

### Q: 迁移脚本报错 "encoding issue"
**A:** 某些文件可能有编码问题，手动检查这些文件，通常可以安全忽略。

### Q: 测试失败了怎么办？
**A:** 先查看具体错误信息。如果是导入错误，运行 `validate_migration.py` 找出遗漏的路径。

### Q: 验证脚本发现旧路径怎么办？
**A:** 手动修改这些文件，或重新运行 `migrate_imports.py --all`。

### Q: 需要更新哪些文档？
**A:** 主要是 `README.md` 的"项目结构"章节和 `docs/development/` 下的架构文档。

---

## 📞 需要帮助？

如果迁移过程中遇到问题：

1. 查看详细迁移方案：[module_migration_plan.md](./module_migration_plan.md)
2. 查看 Git 提交历史：`git log --oneline`
3. 提交 Issue 寻求帮助

---

## 📚 相关文档

- [详细迁移方案](./module_migration_plan.md)
- [LLM 架构文档](./llm/refactoring_completed.md)
- [项目架构总览](../../README.md#架构概览)
