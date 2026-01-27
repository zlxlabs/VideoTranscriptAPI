# LLM 校对配置参数分析

> 分析时间: 2026-01-27
> 目的: 检查当前配置的合理性，确定哪些参数应该统一，哪些应该拆分

---

## 一、当前配置对比

### 1.1 无说话人文本配置（`llm.segmentation`）

```jsonc
"segmentation": {
    "enable_threshold": 20000,      // 触发分段的文本长度阈值
    "segment_size": 8000,           // 每段的目标大小
    "max_segment_size": 12000,      // 每段的最大大小
    "concurrent_workers": 10,       // 并发处理的段落数
    "min_segment_ratio": 0.8        // 分段处理后长度/原长度的最小比例
}
```

**触发条件**: `文本长度 >= 20000` 且 `use_speaker_recognition=false`

**适用场景**:
- CapsWriter 转录的长文本
- YouTube 字幕
- SRT/VTT 字幕

---

### 1.2 有说话人文本配置（`llm.structured_calibration`）

```jsonc
"structured_calibration": {
    "min_chunk_length": 800,        // 单个校对块的最小长度
    "max_chunk_length": 3000,       // 单个校对块的最大长度
    "preferred_chunk_length": 2000, // 首选块长度
    "max_calibration_retries": 2,   // 单块校对失败的最大重试次数
    "calibration_concurrent_limit": 10,  // 并发校对的块数
    "quality_threshold": {
        "overall_score": 8.0,       // 校对质量整体分数阈值
        "minimum_single_score": 7.0 // 单项最低分
    },
    "enable_validation": true,      // 是否对校对结果进行质量验证
    "fallback_to_original": true,   // 校对失败时是否回退到原文
    "validator_model": "deepseek-chat",          // 质量验证使用的模型
    "validator_reasoning_effort": null,
    "risk_validator_model": "gpt-4.1-mini",      // 风险内容校验模型
    "risk_validator_reasoning_effort": null
}
```

**触发条件**: `use_speaker_recognition=true` 且有转录数据

**适用场景**:
- FunASR 转录结果（带 spk_0, spk_1 标识）

---

## 二、关键差异分析

### 2.1 分段长度阈值对比

| 参数 | 无说话人文本 | 有说话人文本 | 差异 |
|-----|------------|------------|------|
| **触发分段阈值** | 20000 字符 | 无明确阈值（总是分段） | ⚠️ 不一致 |
| **目标段长度** | 8000 字符 | 2000 字符 | ⚠️ **相差 4 倍** |
| **最大段长度** | 12000 字符 | 3000 字符 | ⚠️ **相差 4 倍** |
| **最小段长度** | 无 | 800 字符 | - |

**问题识别**:
1. ❌ **长度阈值不统一**: 无说话人 8000 vs 有说话人 2000，差异巨大
2. ❌ **触发分段逻辑不同**: 无说话人有阈值，有说话人总是分段
3. ⚠️ **可能导致问题**: 相同长度的文本，按有/无说话人会分成不同数量的段

---

### 2.2 并发处理对比

| 参数 | 无说话人文本 | 有说话人文本 | 是否统一 |
|-----|------------|------------|---------|
| **并发线程数** | 10 | 10 | ✅ 一致 |
| **参数名** | `concurrent_workers` | `calibration_concurrent_limit` | ⚠️ 命名不一致 |

**问题识别**:
1. ✅ 数值统一（都是 10）
2. ⚠️ 参数名不一致（语义相同但命名不同）

---

### 2.3 质量保证对比

| 功能 | 无说话人文本 | 有说话人文本 |
|-----|------------|------------|
| **长度检查** | ✅ `min_segment_ratio: 0.8` | ❌ **不使用** |
| **LLM 打分验证** | ❌ **不使用** | ✅ `enable_validation: true`<br>LLM 打分（overall_score >= 8.0） |
| **验证方式** | **仅长度检查** | **仅 LLM 打分验证** |
| **失败重试** | ❌ 无 | ✅ `max_calibration_retries: 2` |
| **失败降级** | ✅ 隐式（长度不足回退） | ✅ `fallback_to_original: true` |

**重要说明**:
- **无说话人文本**: 仅使用**长度检查**
  - 参数：`min_segment_ratio: 0.8`
  - 校对后长度 >= 原文长度 × 0.8 才通过
  - 不通过则回退到原文
  - **不使用 LLM 打分验证**

- **有说话人文本**: 仅使用 **LLM 打分验证**
  - 参数：`enable_validation: true`
  - 调用 LLM 对校对结果打分（格式正确性、内容保真度、文本质量、说话人一致性等）
  - overall_score >= 8.0 且所有单项 >= 7.0 才通过
  - 需要额外的 LLM API 调用（成本增加）
  - **不使用长度检查**

**问题识别**:
1. ✅ **质量验证方式完全不同**: 这是合理的设计
   - 无说话人文本简单，长度检查足够
   - 有说话人文本复杂（对话结构、说话人映射），需要 LLM 打分
2. ⚠️ **参数不可统一**: `min_segment_ratio` 仅适用于无说话人文本
3. ⚠️ **配置结构不对称**: 质量验证参数分散在两个配置节

---

### 2.4 模型配置对比

| 参数 | 无说话人文本 | 有说话人文本 |
|-----|------------|------------|
| **校对模型** | 全局 `calibrate_model` | 全局 `calibrate_model` |
| **总结模型** | 全局 `summary_model` | 全局 `summary_model` |
| **验证模型** | ❌ 无 | `validator_model` |
| **风险校对模型** | 全局 `risk_calibrate_model` | 全局 `risk_calibrate_model` |
| **风险验证模型** | ❌ 无 | `risk_validator_model` |

**问题识别**:
1. ✅ 校对和总结模型统一（都使用全局配置）
2. ⚠️ 验证模型仅有说话人文本有（但无说话人文本也需要验证）

---

## 三、问题总结

### 3.1 严重问题

#### ❌ 问题 1：分段长度阈值差异巨大

**现状**:
- 无说话人：`segment_size: 8000`, `max_segment_size: 12000`
- 有说话人：`preferred_chunk_length: 2000`, `max_chunk_length: 3000`

**问题**:
- 相同长度的文本，分段数量差异 4 倍
- 可能导致 LLM 处理效果不一致
- 配置不直观，难以理解为何差异如此大

**示例**:
```
10000 字符的文本：
- 无说话人：分成 1-2 段（8000 + 2000）
- 有说话人：分成 5 段（2000 × 5）
```

**影响**:
- LLM 上下文窗口利用不充分（有说话人文本段太小）
- 有说话人文本的 API 调用次数可能过多（成本增加）
- 处理时间可能不一致

---

#### ❌ 问题 2：触发分段逻辑不统一

**现状**:
- 无说话人：`enable_threshold: 20000`（文本 >= 20000 才分段）
- 有说话人：**无阈值**（总是分段）

**问题**:
- 短文本（如 5000 字符）：
  - 无说话人：不分段，直接处理
  - 有说话人：分成 2-3 个 chunk 处理
- 逻辑不一致，难以预测行为

**影响**:
- 短文本的有说话人处理可能不必要地分段
- 增加复杂度和 API 调用

---

### 3.2 次要问题

#### ⚠️ 问题 3：参数命名不一致

**现状**:
- 并发数：`concurrent_workers` vs `calibration_concurrent_limit`
- 长度比例：`min_segment_ratio` vs 隐式检查

**问题**:
- 语义相同但命名不同
- 增加理解成本

---

#### ✅ 说明 4：质量保证机制的合理差异

**现状**:
- **无说话人文本**: 仅长度检查（`min_segment_ratio: 0.8`）
- **有说话人文本**: 仅 LLM 打分验证（**不使用长度检查**）

**分析**:
- ✅ **这是合理的设计差异**，原因：
  1. 有说话人文本更复杂（对话结构、说话人映射、时间信息），需要 LLM 全面评估
  2. 结构化输出可能合理改变文本长度，因此不适用长度检查
  3. LLM 打分验证需要额外的 API 调用（成本增加），仅用于复杂场景
  4. 无说话人文本相对简单，长度检查已足够

**建议**:
- ✅ 保持现状（两者使用完全不同的验证方式）
- ⚠️ 可以为无说话人文本提供**可选的** LLM 打分验证（默认关闭，高质量要求时使用）

---

## 四、优化建议

### 4.1 方案 A：完全统一（激进）

**思路**: 两类文本使用完全相同的分段阈值

**配置调整**:
```jsonc
{
  "llm": {
    // 统一的分段配置（两类文本共用）
    "calibration_common": {
      "enable_threshold": 10000,      // 统一：触发分段的阈值
      "segment_size": 4000,           // 统一：目标段长度
      "max_segment_size": 6000,       // 统一：最大段长度
      "min_segment_length": 500,      // 统一：最小段长度
      "concurrent_workers": 10,       // 统一：并发数
      "min_quality_ratio": 0.8        // 统一：质量检查比例
    },

    // 无说话人文本特有配置（可选）
    "plain_text_calibration": {
      // 如果需要特殊配置，在这里覆盖
    },

    // 有说话人文本特有配置
    "speaker_aware_calibration": {
      "max_calibration_retries": 2,
      "enable_validation": true,
      "quality_threshold": {
        "overall_score": 8.0,
        "minimum_single_score": 7.0
      },
      "validator_model": "deepseek-chat",
      "validator_reasoning_effort": null,
      "risk_validator_model": "gpt-4.1-mini",
      "risk_validator_reasoning_effort": null
    }
  }
}
```

**优点**:
- ✅ 配置统一，易于理解
- ✅ 行为一致，易于预测
- ✅ 减少配置项，降低复杂度

**缺点**:
- ⚠️ 可能不适合所有场景（两类文本特性不同）
- ⚠️ 需要仔细调整阈值以兼顾两者

---

### 4.2 方案 B：部分统一（推荐）

**思路**: 统一核心参数，保留必要差异

**配置调整**:
```jsonc
{
  "llm": {
    // ============================================================
    // 通用配置（两类文本共用）
    // ============================================================
    "calibration": {
      "enable_threshold": 10000,      // 统一：触发分段的阈值
      "concurrent_workers": 10,       // 统一：并发数
      "max_retries": 2,               // 统一：重试次数
      "fallback_to_original": true    // 统一：失败降级
    },

    // ============================================================
    // 无说话人文本配置
    // ============================================================
    "plain_text_segmentation": {
      "segment_size": 5000,           // 独立：纯文本段更长（无对话结构限制）
      "max_segment_size": 8000,

      // 质量验证：长度检查（仅无说话人文本使用）
      "min_quality_ratio": 0.8        // 校对后长度 >= 原文 × 0.8 才通过
    },

    // ============================================================
    // 有说话人文本配置
    // ============================================================
    "speaker_aware_chunking": {
      "min_chunk_length": 800,        // 独立：对话需要更精细的分块
      "max_chunk_length": 3000,
      "preferred_chunk_length": 2000,

      // 质量验证：LLM 打分验证（仅有说话人文本使用）
      "enable_validation": true,      // 是否启用 LLM 打分验证
      "quality_threshold": {
        "overall_score": 8.0,         // 整体分数阈值
        "minimum_single_score": 7.0   // 单项最低分
      },
      "validator_model": "deepseek-chat",
      "validator_reasoning_effort": null,
      "risk_validator_model": "gpt-4.1-mini",
      "risk_validator_reasoning_effort": null
    }
  }
}
```

**统一的参数（`calibration` 配置节）**:
- ✅ `enable_threshold`（触发分段的阈值）
- ✅ `concurrent_workers`（并发数）
- ✅ `max_retries`（重试次数）
- ✅ `fallback_to_original`（失败降级）

**无法统一的参数（质量验证方式不同）**:
- ⚠️ `segment_size` / `chunk_length`（分段大小，文本特性不同）
- ⚠️ `min_quality_ratio`（长度检查，**仅无说话人使用**）
- ⚠️ `enable_validation` + `validator_model`（LLM 打分验证，**仅有说话人使用**）

**质量验证说明**:
- **无说话人文本**: 仅使用**长度检查**（`min_quality_ratio: 0.8`）
  - 校对后长度 >= 原文长度 × 0.8 才通过
  - 不通过则回退到原文
  - **不使用 LLM 打分验证**（成本和必要性考虑）

- **有说话人文本**: 仅使用 **LLM 打分验证**（`enable_validation: true`）
  - 调用 LLM 对校对结果打分（格式、内容、质量、说话人一致性等）
  - overall_score >= 8.0 且所有单项 >= 7.0 才通过
  - 不通过可以重试或回退
  - **不使用长度检查**（因为结构化输出可能改变长度）
  - **需要额外 API 调用**（成本增加）

**优点**:
- ✅ 核心逻辑统一（触发阈值、并发、质量比例）
- ✅ 保留必要差异（分段大小根据文本特性调整）
- ✅ 平衡了一致性和灵活性

**缺点**:
- ⚠️ 仍需维护两套分段参数

---

### 4.3 方案 C：保留现状 + 调整阈值（保守）

**思路**: 保持当前结构，仅调整明显不合理的阈值

**配置调整**:
```jsonc
{
  "llm": {
    // 无说话人文本配置
    "segmentation": {
      "enable_threshold": 10000,      // 调整：从 20000 降到 10000
      "segment_size": 5000,           // 调整：从 8000 降到 5000
      "max_segment_size": 8000,       // 调整：从 12000 降到 8000
      "concurrent_workers": 10,
      "min_segment_ratio": 0.8
    },

    // 有说话人文本配置
    "structured_calibration": {
      "enable_threshold": 10000,      // 新增：统一触发阈值
      "min_chunk_length": 800,
      "max_chunk_length": 3000,
      "preferred_chunk_length": 2000,
      "max_calibration_retries": 2,
      "calibration_concurrent_limit": 10,
      "quality_threshold": {
        "overall_score": 8.0,
        "minimum_single_score": 7.0
      },
      "enable_validation": true,
      "fallback_to_original": true,
      "validator_model": "deepseek-chat",
      "validator_reasoning_effort": null,
      "risk_validator_model": "gpt-4.1-mini",
      "risk_validator_reasoning_effort": null
    }
  }
}
```

**调整内容**:
1. **统一触发阈值**: 两者都是 10000（从 20000 降低，添加有说话人的阈值）
2. **缩小无说话人段长度**: 从 8000 降到 5000（缩小与有说话人的差距）
3. **保留其他配置**: 其他参数不变

**优点**:
- ✅ 改动最小，风险最低
- ✅ 解决了触发阈值不一致的问题
- ✅ 缩小了分段长度的差异

**缺点**:
- ⚠️ 未统一参数命名
- ⚠️ 仍有 2-3 倍的分段长度差异

---

## 五、推荐方案

### 🎯 **推荐：方案 B（部分统一）**

**理由**:

1. **平衡一致性和灵活性**:
   - 核心逻辑统一（触发阈值、并发数）
   - 保留必要差异（分段大小根据文本特性）

2. **符合两类文本的特性**:
   - **无说话人文本**：纯文本，可以用更大的段（5000-8000 字符）
   - **有说话人文本**：对话结构，需要更精细的分块（2000-3000 字符）

3. **降低配置复杂度**:
   - 提取公共参数到 `calibration`
   - 特定参数放在各自的子配置

4. **易于理解和维护**:
   - 配置结构清晰（通用 + 特定）
   - 参数命名统一

---

## 六、具体调整建议

### 6.1 新的配置结构

```jsonc
{
  "llm": {
    // ... 其他配置 ...

    // ============================================================
    // 校对通用配置（两类文本共用）
    // ============================================================
    "calibration": {
      "enable_threshold": 10000,      // 触发分段的文本长度阈值（字符数）
      "concurrent_workers": 10,       // 并发处理的段落/块数
      "max_retries": 2,               // 校对失败的最大重试次数
      "fallback_to_original": true    // 校对失败时是否回退到原文
    },

    // ============================================================
    // 无说话人文本分段配置
    // ============================================================
    "plain_text_segmentation": {
      "segment_size": 5000,           // 每段的目标大小（纯文本可以更大）
      "max_segment_size": 8000,       // 每段的最大大小

      // 质量验证：长度检查（仅无说话人文本使用）
      "min_quality_ratio": 0.8        // 校对后长度 >= 原文长度 × 0.8 才通过
                                      // 不使用 LLM 打分验证（成本和必要性考虑）
    },

    // ============================================================
    // 有说话人文本分块配置
    // ============================================================
    "speaker_aware_chunking": {
      "min_chunk_length": 800,        // 单个校对块的最小长度
      "max_chunk_length": 3000,       // 单个校对块的最大长度
      "preferred_chunk_length": 2000, // 首选块长度（对话结构需要精细分块）

      // LLM 打分验证配置（仅有说话人文本使用）
      // 注意：有说话人文本仅使用 LLM 打分验证，不使用长度检查
      //   - 原因：结构化输出可能合理改变文本长度
      //   - LLM 会评估格式正确性、内容保真度、说话人一致性等
      "enable_validation": true,      // 是否启用 LLM 打分验证
      "quality_threshold": {
        "overall_score": 8.0,         // 校对质量整体分数阈值（0-10）
        "minimum_single_score": 7.0   // 单项最低分
      },
      "validator_model": "deepseek-chat",
      "validator_reasoning_effort": null,
      "risk_validator_model": "gpt-4.1-mini",
      "risk_validator_reasoning_effort": null
    }
  }
}
```

---

### 6.2 迁移路径

#### 步骤 1：向后兼容（保留旧配置支持）

```python
# LLMConfig.from_dict() 中添加兼容逻辑

# 优先使用新配置，如果不存在则回退到旧配置
calibration = llm_config.get("calibration", {})
plain_text_seg = llm_config.get("plain_text_segmentation", {})
speaker_aware_chunk = llm_config.get("speaker_aware_chunking", {})

# 兼容旧配置（segmentation）
if not calibration and "segmentation" in llm_config:
    old_seg = llm_config["segmentation"]
    calibration = {
        "enable_threshold": old_seg.get("enable_threshold", 10000),
        "concurrent_workers": old_seg.get("concurrent_workers", 10),
        "min_quality_ratio": old_seg.get("min_segment_ratio", 0.8),
        "max_retries": 2,
        "fallback_to_original": True
    }
    plain_text_seg = {
        "segment_size": old_seg.get("segment_size", 5000),
        "max_segment_size": old_seg.get("max_segment_size", 8000)
    }

# 兼容旧配置（structured_calibration）
if not speaker_aware_chunk and "structured_calibration" in llm_config:
    old_struct = llm_config["structured_calibration"]
    speaker_aware_chunk = old_struct  # 直接复用
```

#### 步骤 2：更新文档

在配置文件示例中添加新配置说明，标记旧配置为 `deprecated`。

#### 步骤 3：逐步迁移

- 用户可以继续使用旧配置（向后兼容）
- 新用户使用新配置
- 3-6 个月后移除旧配置支持

---

## 七、参数对照表

### 7.1 统一参数（`calibration` 配置节）

| 参数名（新） | 无说话人（旧） | 有说话人（旧） | 建议值 | 说明 |
|------------|--------------|--------------|-------|------|
| `enable_threshold` | `segmentation.enable_threshold` | 无 | 10000 | 触发分段的阈值 |
| `concurrent_workers` | `segmentation.concurrent_workers` | `structured_calibration.calibration_concurrent_limit` | 10 | 并发数 |
| `max_retries` | 无 | `structured_calibration.max_calibration_retries` | 2 | 重试次数 |
| `fallback_to_original` | 隐式 | `structured_calibration.fallback_to_original` | true | 失败降级 |

**重要说明**:
- 以上参数是两类文本**共用**的通用配置
- 质量验证方式**不统一**，放在各自的配置节中：
  - 无说话人文本：使用长度检查（`plain_text_segmentation.min_quality_ratio: 0.8`）
  - 有说话人文本：使用 LLM 打分验证（`speaker_aware_chunking.enable_validation: true`）

---

### 7.2 保留差异参数

| 参数名 | 无说话人文本 | 有说话人文本 | 差异原因 |
|-------|------------|------------|---------|
| **分段大小** | `segment_size: 5000`<br>`max_segment_size: 8000` | `min_chunk_length: 800`<br>`max_chunk_length: 3000`<br>`preferred_chunk_length: 2000` | 对话结构需要更精细的分块 |
| **质量验证方式** | **长度检查**<br>`min_quality_ratio: 0.8` | **LLM 打分验证**<br>`enable_validation: true`<br>`quality_threshold: {...}` | 有说话人文本更复杂（对话结构、说话人映射），需要 LLM 全面评估；<br>结构化输出可能改变长度，不适用长度检查 |

---

## 八、常见问题

### Q1: 为什么有说话人文本的分段更小？

**A**: 因为对话结构的特殊性：
- **对话边界**: 需要保持说话人的完整发言
- **上下文**: 对话之间有逻辑关系，不能随意切断
- **语义完整**: 一个话题的讨论可能需要多轮对话

如果分段太大（如 8000 字符），可能包含多个话题，LLM 难以保持对话的连贯性。

---

### Q2: 为什么无说话人文本可以用更大的分段？

**A**: 因为纯文本的特性：
- **连续性**: 没有对话边界的限制
- **语义密度**: 通常是连贯的叙述或论述
- **灵活性**: 可以在句子边界切分，影响较小

更大的分段可以让 LLM 看到更多上下文，提高校对质量。

---

### Q3: 是否应该完全统一分段大小？

**A**: 不建议。原因：
1. 两类文本的特性不同（对话 vs 叙述）
2. 完全统一可能损失各自的优化空间
3. **部分统一**（核心逻辑统一 + 保留必要差异）是更好的选择

---

### Q4: 如何确定最佳的分段大小？

**A**: 建议通过实验确定：
1. 准备测试集（包含不同长度的文本）
2. 测试不同分段大小的效果（质量、成本、时间）
3. 找到平衡点

**经验值**:
- 无说话人：4000-6000 字符
- 有说话人：2000-3000 字符

---

## 九、总结

### 9.1 主要问题

1. ❌ **分段长度差异巨大**（8000 vs 2000，相差 4 倍）
2. ❌ **触发分段逻辑不统一**（无说话人有阈值，有说话人总是分段）
3. ⚠️ **参数命名不一致**（concurrent_workers vs calibration_concurrent_limit）
4. ✅ **质量验证方式完全不同**（无说话人仅长度检查，有说话人仅 LLM 打分验证）
   - 这是合理的设计差异，不是问题

### 9.2 推荐调整

**采用方案 B（部分统一）**:

**统一参数**:
- ✅ `enable_threshold: 10000`（触发阈值）
- ✅ `concurrent_workers: 10`（并发数）
- ✅ `max_retries: 2`（重试次数）
- ✅ `fallback_to_original: true`（失败降级）

**保留差异**:
- ⚠️ 分段大小：
  - 无说话人：`segment_size: 5000`, `max: 8000`
  - 有说话人：`preferred: 2000`, `max: 3000`
- ⚠️ 质量验证方式：
  - 无说话人：长度检查（`min_quality_ratio: 0.8`）
  - 有说话人：LLM 打分验证（`enable_validation: true`）

**新增配置结构**:
```
llm.calibration (通用)
llm.plain_text_segmentation (无说话人)
llm.speaker_aware_chunking (有说话人)
```

### 9.3 预期收益

- ✅ 配置更清晰、易于理解
- ✅ 行为更一致、易于预测
- ✅ 保留必要的灵活性
- ✅ 向后兼容，平滑迁移

---

**分析人**: Claude Sonnet 4.5
**分析时间**: 2026-01-27
**文档版本**: v1.1

**修订记录**:
- v1.0 (2026-01-27): 初始版本，完成配置参数对比分析
- v1.1 (2026-01-27): 修正质量验证方式描述
  - 明确无说话人文本仅使用长度检查
  - 明确有说话人文本仅使用 LLM 打分验证（不使用长度检查）
  - 调整配置结构和统一参数列表
