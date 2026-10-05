# 核心模块提升迁移方案

## 📋 迁移概述

### 目标
将三个核心业务模块从 `utils/` 提升到一级目录：
- `utils/llm/` → `llm/`
- `utils/cache/` → `cache/`
- `utils/risk_control/` → `risk_control/`

### 原因
1. **业务重要性**：这三个模块是核心业务逻辑，不应归类为"工具"
2. **架构清晰**：与 `api/`, `downloaders/`, `transcriber/` 平级更合理
3. **可维护性**：明确的模块分层有助于新开发者理解项目结构

---

## 📊 影响范围分析

### 模块规模
| 模块 | 文件数 | 主要功能 |
|------|--------|---------|
| `llm/` | 27 | 文本校对、总结、说话人推断、质量验证 |
| `cache/` | 3 | 数据缓存管理、缓存分析 |
| `risk_control/` | 3 | 敏感词管理、文本脱敏 |

### 受影响文件统计
#### 源代码文件
- `llm`: 37 个文件引用
- `cache`: 12 个文件引用
- `risk_control`: 6 个文件引用

#### 测试文件
- `tests/llm/`: 10+ 个测试文件
- `tests/cache/`: 3 个测试文件
- `tests/features/`: 涉及风控和 LLM 的集成测试

#### 文档文件
- `docs/development/llm/`: 多个 LLM 相关文档
- `docs/development/risk_control.md`

---

## 🛠️ 迁移步骤

### 阶段 1：准备工作（预估 30 分钟）

#### 1.1 创建迁移分支
```bash
git checkout -b refactor/promote-core-modules
```

#### 1.2 备份当前状态
```bash
git add -A
git commit -m "chore: snapshot before module migration"
```

#### 1.3 运行完整测试套件（建立基线）
```bash
uv run pytest tests/ -v --tb=short > migration_baseline_test.log 2>&1
```

---

### 阶段 2：执行迁移（预估 20 分钟）

#### 2.1 移动目录
```bash
# 在项目根目录执行
cd src/video_transcript_api

# 移动三个模块
mv utils/llm ./llm
mv utils/cache ./cache
mv utils/risk_control ./risk_control

# 验证移动成功
ls -la llm/ cache/ risk_control/
```

#### 2.2 更新包内导入路径

**自动化脚本**（见下节 `scripts/migrate_imports.py`）：
- 批量替换 `from ...utils.llm` → `from ...llm`
- 批量替换 `from ..utils.cache` → `from ..cache`
- 批量替换 `from ...utils.risk_control` → `from ...risk_control`
- 处理相对导入的层级调整

```bash
uv run python scripts/migrate_imports.py
```

#### 2.3 手动验证关键文件

需要特别检查的文件：
- `src/video_transcript_api/api/context.py` - 全局依赖注入
- `src/video_transcript_api/api/services/transcription.py` - 业务流程
- `src/video_transcript_api/api/app.py` - 应用初始化

---

### 阶段 3：更新测试和文档（预估 15 分钟）

#### 3.1 更新测试文件导入
```bash
# 测试文件中的导入路径
uv run python scripts/migrate_imports.py --target-dir tests
```

#### 3.2 更新脚本文件
```bash
uv run python scripts/migrate_imports.py --target-dir scripts
```

#### 3.3 更新文档引用（手动）
- `README.md` 中的目录结构图
- `docs/development/` 中的架构文档

---

### 阶段 4：验证测试（预估 30 分钟）

#### 4.1 运行单元测试
```bash
uv run pytest tests/unit/ -v
```

#### 4.2 运行 LLM 测试
```bash
uv run pytest tests/llm/ -v
```

#### 4.3 运行缓存测试
```bash
uv run pytest tests/cache/ -v
```

#### 4.4 运行集成测试
```bash
uv run pytest tests/integration/ -v
```

#### 4.5 运行完整测试套件
```bash
uv run pytest tests/ -v --tb=short > migration_after_test.log 2>&1
```

#### 4.6 对比测试结果
```bash
diff migration_baseline_test.log migration_after_test.log
```

---

### 阶段 5：手动功能验证（预估 20 分钟）

#### 5.1 启动服务
```bash
uv run python main.py --start
```

#### 5.2 验证关键流程
1. **提交任务**：`POST /api/transcribe`
2. **查询任务**：`GET /api/task/{task_id}`
3. **查看结果**：`GET /view/{view_token}`
4. **缓存命中**：重复提交相同视频，验证缓存逻辑
5. **风控系统**：提交包含敏感词的内容，验证脱敏功能

#### 5.3 检查日志输出
- 确认模块路径在日志中正确显示
- 确认无 ImportError 或 ModuleNotFoundError

---

### 阶段 6：清理和提交（预估 10 分钟）

#### 6.1 删除临时文件
```bash
rm migration_baseline_test.log migration_after_test.log
```

#### 6.2 提交更改
```bash
git add -A
git commit -m "refactor: promote llm, cache, risk_control to top-level modules

- Move utils/llm → llm/
- Move utils/cache → cache/
- Move utils/risk_control → risk_control/
- Update all import paths across codebase
- Update tests and documentation references

BREAKING CHANGE: Module paths changed for core business modules
"
```

#### 6.3 推送到远程
```bash
git push origin refactor/promote-core-modules
```

---

## 🤖 自动化脚本

见 `scripts/migrate_imports.py`（下一步创建）

---

## 🔄 回滚方案

### 如果迁移失败

#### 方案 A：Git 回滚（推荐）
```bash
# 回到迁移前的提交
git reset --hard HEAD~1

# 或者使用快照提交的 SHA
git reset --hard <snapshot-commit-sha>
```

#### 方案 B：手动还原
```bash
cd src/video_transcript_api

# 移回原位置
mv llm utils/llm
mv cache utils/cache
mv risk_control utils/risk_control

# 还原导入路径（运行反向脚本）
uv run python scripts/rollback_imports.py
```

---

## ✅ 验收标准

迁移成功的标准：

1. ✅ 所有测试套件通过（与基线一致）
2. ✅ 服务正常启动，无模块导入错误
3. ✅ 关键功能流程验证通过
4. ✅ 日志输出正常，无异常堆栈
5. ✅ 文档和 README 已更新
6. ✅ 代码格式和类型检查通过

---

## ⚠️ 风险点和缓解措施

### 风险 1：遗漏的导入路径
**缓解措施**：
- 使用自动化脚本全局替换
- 手动检查关键文件
- 运行完整测试套件

### 风险 2：相对导入层级错误
**缓解措施**：
- 脚本中精确处理 `..` 和 `...` 层级
- 按模块内部文件逐一验证

### 风险 3：测试文件路径不一致
**缓解措施**：
- 单独运行 tests 目录的导入替换
- 逐个测试套件验证

### 风险 4：文档和配置文件未更新
**缓解措施**：
- 迁移后手动检查文档目录
- 搜索配置文件中的硬编码路径

---

## 📅 预计时间线

| 阶段 | 预估时间 | 累计时间 |
|------|---------|---------|
| 阶段 1：准备 | 30 分钟 | 30 分钟 |
| 阶段 2：迁移 | 20 分钟 | 50 分钟 |
| 阶段 3：更新 | 15 分钟 | 1 小时 5 分钟 |
| 阶段 4：验证 | 30 分钟 | 1 小时 35 分钟 |
| 阶段 5：手动测试 | 20 分钟 | 1 小时 55 分钟 |
| 阶段 6：提交 | 10 分钟 | **2 小时 5 分钟** |

---

## 📝 后续工作

迁移完成后的优化任务：

1. **更新架构文档**：在 `docs/development/architecture.md` 中更新模块图
2. **重新生成 API 文档**：如果使用 Sphinx，重新构建文档网站
3. **CI/CD 更新**：检查持续集成脚本中是否有硬编码路径
4. **更新 README**：调整"项目结构"和"核心模块"章节

---

## 🔗 相关文档

- [LLM 架构设计](./llm/refactoring_completed.md)
- [缓存系统设计](../guides/cache_system.md)（待创建）
- [风控模块设计](./risk_control.md)
