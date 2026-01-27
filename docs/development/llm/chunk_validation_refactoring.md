# 分段质量验证逻辑重构

> **日期**: 2026-01-27
> **版本**: v1.1
> **状态**: 已完成

---

## 问题描述

### 原逻辑（v1.0）

```
步骤1: 提取关键信息
步骤2: 说话人推断
步骤3: 分段校对（并发处理）
步骤4: 合并所有分段
步骤5: 整体打分验证
   - 验证通过：返回所有校对结果
   - 验证失败：全部降级到原文 ❌
```

**问题**：
- 即使只有 1 个分段质量差，也会导致整体降级
- "一颗老鼠屎坏了一锅汤"

---

## 新逻辑（v1.1）

```
步骤1: 提取关键信息
步骤2: 说话人推断
步骤3: 分段校对 + 独立验证（可选）
   - 每个分段独立校对
   - 每个分段独立打分（enable_validation=true 时）
     - 验证通过：保留校对结果
     - 验证失败：该分段降级到原文 ✅
步骤4: 合并结果
   - 合并所有分段（成功+降级的混合）
   - 不再进行整体验证
```

**优势**：
- 分段间互不影响，质量差的分段独立降级
- 质量好的分段保留校对结果
- 更细粒度的质量控制

---

## 配置变更

### `enable_validation` 字段行为变化

| 版本 | 行为 | 默认值 |
|------|------|-------|
| v1.0 | 控制"合并后的整体打分验证" | `true` |
| v1.1 | 控制"分段时的独立打分验证" | `false` |

### 配置文件更新

**位置**: `config/config.example.jsonc` → `llm.structured_calibration.enable_validation`

```jsonc
{
  "llm": {
    "structured_calibration": {
      // 质量验证开关
      // - true: 对每个分段独立打分验证，不通过的分段降级到原文
      // - false: 不进行质量验证，直接使用校对结果
      // 注意：已移除"合并后整体验证"，现在只在分段时独立验证
      "enable_validation": false
    }
  }
}
```

### 推荐设置

| 场景 | 推荐值 | 说明 |
|------|-------|------|
| **性能优先** | `false` | 无验证开销，速度最快 |
| **质量优先** | `true` | 每个分段独立打分，不合格降级 |
| **生产环境** | `false` | 避免不必要的 LLM 调用成本 |

---

## 代码修改

### 1. `SpeakerAwareProcessor.process()`

**修改前**：
```python
# 步骤3: 分段校对
calibrated_chunks = self._calibrate_chunks(...)

# 步骤4: 合并结果
calibrated_dialogs = [...]

# 步骤5: 整体验证
if self.config.enable_validation:
    validation_result = self.quality_validator.validate_by_score(...)
    if not validation_result["passed"]:
        calibrated_dialogs = dialogs  # 全部降级 ❌
```

**修改后**：
```python
# 步骤3: 分段校对（每段独立验证）
calibrated_chunks = self._calibrate_chunks(
    original_chunks=chunks,  # 传入原始chunk用于验证失败时降级
    ...
)

# 步骤4: 合并结果（不再整体验证）
calibrated_dialogs = [...]
calibrated_text = self._build_text_from_dialogs(calibrated_dialogs)
```

### 2. `SpeakerAwareProcessor._calibrate_chunks()`

**新增逻辑**：
```python
def calibrate_single_chunk(index: int, chunk: List[Dict]):
    # 校对
    response = self.llm_client.call(...)
    calibrated_dialogs = response.structured_output.get("calibrated_dialogs", [])

    # 分段质量验证（可选）
    if self.config.enable_validation:
        validation_result = self.quality_validator.validate_by_score(
            original=chunk,  # 只验证当前chunk
            calibrated=calibrated_dialogs,
            ...
        )

        if not validation_result["passed"]:
            logger.warning(f"Chunk {index + 1} validation failed, falling back")
            calibrated_chunks[index] = chunk  # 该分段降级 ✅
            return

    calibrated_chunks[index] = calibrated_dialogs
```

### 3. `LLMConfig`

```python
@dataclass
class LLMConfig:
    # 结构化校对配置
    enable_validation: bool = False  # 是否启用分段质量验证（每个chunk独立打分）
```

---

## 测试验证

### 测试脚本

**位置**: `tests/llm/test_chunk_validation.py`

**测试场景**：
1. `enable_validation=false`：不进行验证，直接返回校对结果
2. `enable_validation=true`：每个chunk独立验证，失败降级

**运行方式**：
```bash
uv run python tests/llm/test_chunk_validation.py
```

---

## 日志输出示例

### enable_validation=false

```
[INFO] Calibrating chunk 1/5, dialog count: 31, length: 2003
[INFO] Chunk 1 calibration completed
[INFO] Calibrating chunk 2/5, dialog count: 25, length: 2005
[INFO] Chunk 2 calibration completed
...
[INFO] Speaker-aware text processing completed: original length 12834, calibrated length 13125
```

### enable_validation=true

```
[INFO] Calibrating chunk 1/5, dialog count: 31, length: 2003
[INFO] Validating chunk 1/5
[INFO] Chunk 1 validation passed (score: 8.5)
[INFO] Chunk 1 calibration completed

[INFO] Calibrating chunk 2/5, dialog count: 25, length: 2005
[INFO] Validating chunk 2/5
[WARNING] Chunk 2 validation failed (score: 3.0), falling back to original
[INFO] Chunk 2 calibration completed

...
[INFO] Speaker-aware text processing completed: original length 12834, calibrated length 11923
```

---

## 兼容性说明

### 向后兼容

- ✅ 配置文件字段名未变化（`enable_validation`）
- ✅ 字段类型未变化（`bool`）
- ⚠️ **默认值变化**：`true` → `false`

### 迁移指南

**如果您希望保持原有行为（启用验证）**：

1. 打开 `config/config.jsonc`
2. 找到 `llm.structured_calibration.enable_validation`
3. 将值改为 `true`

**注意**：即使设置为 `true`，验证逻辑也与 v1.0 不同：
- v1.0：整体验证，全部降级
- v1.1：分段验证，独立降级

---

## 相关文档

- [LLM 重构方案](refactoring_plan.md)
- [LLM 工程指南](engineering_guide.md)

---

## 总结

| 项目 | 说明 |
|------|------|
| **修改内容** | 分段质量验证逻辑从"整体验证"改为"分段独立验证" |
| **配置变更** | `enable_validation` 默认值从 `true` 改为 `false` |
| **优势** | 分段间互不影响，更细粒度的质量控制 |
| **兼容性** | 字段名未变，但行为变化，需注意迁移 |
| **测试** | 提供测试脚本验证新逻辑 |

---

**修改完成时间**: 2026-01-27
