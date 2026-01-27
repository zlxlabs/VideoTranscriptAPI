# ✅ 模块迁移方案已就绪

## 📦 已创建的文件

### 📚 文档（3 个）
1. `docs/development/module_migration_plan.md` - 详细迁移方案
2. `docs/development/module_migration_quickstart.md` - 快速执行指南
3. `docs/development/module_migration_summary.md` - 方案总结和索引

### 🛠️ 自动化脚本（7 个）
4. `scripts/migrate_imports.py` - 导入路径迁移
5. `scripts/rollback_imports.py` - 导入路径回滚
6. `scripts/validate_migration.py` - 迁移验证
7. `scripts/execute_migration.py` - 一键迁移（跨平台 ⭐）
8. `scripts/rollback_migration_full.py` - 一键回滚（跨平台 ⭐）
9. `scripts/execute_migration.sh` - 一键迁移（Linux/macOS）
10. `scripts/rollback_migration.sh` - 一键回滚（Linux/macOS）

---

## 🚀 快速开始（Windows）

在项目根目录执行以下命令：

### 1. 预览更改（推荐先执行）
```bash
python scripts/migrate_imports.py --all --dry-run
```

### 2. 执行迁移
```bash
python scripts/execute_migration.py
```

### 3. 如果需要回滚
```bash
python scripts/rollback_migration_full.py
```

---

## 📋 迁移将会做什么

1. ✅ 创建迁移分支 `refactor/promote-core-modules`
2. ✅ 创建备份提交
3. ✅ 运行基线测试（保存日志）
4. ✅ 移动三个模块：
   - `utils/llm/` → `llm/`
   - `utils/cache/` → `cache/`
   - `utils/risk_control/` → `risk_control/`
5. ✅ 更新所有 Python 文件中的导入路径
6. ✅ 验证迁移（检查遗漏）
7. ✅ 运行完整测试套件

**预计时间：10-15 分钟**

---

## ⚠️ 迁移前检查清单

请确认以下条件：

- [ ] 当前在项目根目录
- [ ] Git 工作区干净（`git status` 无未提交更改）
- [ ] 已安装 `uv` 包管理器
- [ ] 测试可以正常运行（`uv run pytest tests/unit/ -v`）
- [ ] 已阅读 `docs/development/module_migration_quickstart.md`

---

## 🎯 推荐执行顺序

### 第一步：了解方案
```bash
# 阅读快速指南（5 分钟）
cat docs/development/module_migration_quickstart.md
```

### 第二步：预览更改
```bash
# 查看哪些文件会被修改
python scripts/migrate_imports.py --all --dry-run
```

### 第三步：执行迁移
```bash
# 自动化执行完整流程
python scripts/execute_migration.py
```

### 第四步：验证结果
```bash
# 启动服务测试
uv run python main.py --start

# 在另一个终端手动测试 API
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/video.mp4"}'
```

### 第五步：提交更改
```bash
git add -A
git commit -m "refactor: promote llm, cache, risk_control to top-level modules"
git push origin refactor/promote-core-modules
```

---

## 🔄 如果迁移失败

### 方式 1：一键回滚（推荐）
```bash
python scripts/rollback_migration_full.py
```

### 方式 2：Git 回滚
```bash
# 回到迁移前的状态
git reset --hard HEAD~2
```

---

## 📚 详细文档

- **快速指南**：`docs/development/module_migration_quickstart.md`
- **详细方案**：`docs/development/module_migration_plan.md`
- **文件索引**：`docs/development/module_migration_summary.md`

---

## 🆘 常见问题

**Q: 我应该先运行哪个命令？**
A: 先运行 `python scripts/migrate_imports.py --all --dry-run` 预览更改。

**Q: 迁移会影响现有功能吗？**
A: 不会，只是改变模块的目录位置和导入路径，功能逻辑不变。

**Q: 迁移失败了怎么办？**
A: 运行 `python scripts/rollback_migration_full.py` 一键回滚。

**Q: 需要更新文档吗？**
A: 是的，迁移成功后需要手动更新 README.md 中的目录结构部分。

**Q: Windows 用户可以用 .sh 脚本吗？**
A: 可以在 Git Bash 中使用，但推荐使用 Python 版本（.py）更稳定。

---

## ✨ 准备好了吗？

所有工具都已就绪，随时可以开始迁移！

**推荐命令：**
```bash
python scripts/execute_migration.py
```

祝迁移顺利！🚀
