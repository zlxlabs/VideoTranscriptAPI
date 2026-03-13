# 统一质量验证模块设计方案

> **版本**: v1.0
> **日期**: 2026-01-28
> **状态**: 设计阶段（待实现）

---

## 📋 目录

- [概述](#概述)
- [核心目标](#核心目标)
- [架构设计](#架构设计)
- [配置方案](#配置方案)
- [实现细节](#实现细节)
- [迁移指南](#迁移指南)
- [测试计划](#测试计划)

---

## 概述

### 背景

当前系统中存在两套质量验证逻辑：
1. **纯文本分段校对**（`PlainTextProcessor`）：仅使用长度检查
2. **对话流校对**（`SpeakerAwareProcessor`）：使用 LLM 打分，但 LLM 自己计算加权平均（不可靠）

### 问题

1. **LLM 计算加权平均不准确**：数学计算不是 LLM 的强项
2. **对话流用长度判断不合理**：说话人标识错误无法通过长度发现
3. **逻辑重复**：两套独立的验证逻辑，维护成本高
4. **缺乏灵活性**：无法根据场景独立开关质量验证

### 解决方案

设计一个**统一的质量验证模块**：
- ✅ 支持纯文本和对话流两种输入
- ✅ 本地计算加权平均分（准确）
- ✅ 对话流增加结构一致性检查
- ✅ 独立开关控制两种场景

---

## 核心目标

### 设计原则

**"输入格式是表象，评估本质是相同的"**

无论是纯文本还是对话流，评估维度都是：
1. **准确性（40%）**：核心信息是否保留
2. **完整性（30%）**：删减是否合理
3. **流畅度（20%）**：语言是否通顺
4. **格式规范（10%）**：标点段落是否正确

### 关键约束

#### 纯文本（PlainTextProcessor）
- ✅ 可以使用长度比例作为预筛选
- ✅ 长度删减合理（直播场景）
- ✅ 默认**开启**质量验证

#### 对话流（SpeakerAwareProcessor）
- ❌ 不能改变对话条数
- ❌ 不能改变 speaker 字段
- ✅ 只校对 text 字段
- ✅ 结构一致性检查（本地算法）
- ✅ 默认**关闭**质量验证（因为已有其他方法判断说话人信息）

---

## 架构设计

### 整体流程

```
┌─────────────────────────────────────────────────────────────┐
│                     Processor 层                            │
│  PlainTextProcessor         SpeakerAwareProcessor           │
│         ↓                            ↓                      │
│  LLM 校对完成                   LLM 校对完成                 │
└─────────────────────────────────────────────────────────────┘
                         ↓
        ┌────────────────────────────────┐
        │  是否启用质量验证？              │
        │  segmentation.quality_validation.enabled  │
        │  structured_calibration.quality_validation.enabled  │
        └────────────────────────────────┘
                         ↓
                    ┌────┴────┐
                 Yes │        │ No
                    ↓          ↓
    ┌───────────────────┐  直接返回校对结果
    │ UnifiedQualityValidator │
    └───────────────────┘
             ↓
    ┌────────────────────────────────────┐
    │ 1. 输入标准化 (ValidationInput)   │
    │    - 纯文本: str → str             │
    │    - 对话流: List[Dict] → List[Dict] │
    └────────────────────────────────────┘
             ↓
    ┌────────────────────────────────────┐
    │ 2. 对话流特殊处理                   │
    │    结构一致性检查（本地算法）        │
    │    - 条数检查                       │
    │    - 说话人检查                     │
    │    - 顺序对应检查                   │
    │    失败 → 直接返回失败              │
    └────────────────────────────────────┘
             ↓ 通过
    ┌────────────────────────────────────┐
    │ 3. Prompt 构建 (PromptBuilder)     │
    │    - 纯文本: 关注删减合理性         │
    │    - 对话流: 关注 text 质量         │
    └────────────────────────────────────┘
             ↓
    ┌────────────────────────────────────┐
    │ 4. LLM 调用（获取单项评分）         │
    │    返回: {scores: {...}, issues: [...]} │
    └────────────────────────────────────┘
             ↓
    ┌────────────────────────────────────┐
    │ 5. 本地计算 (ScoreCalculator)      │
    │    - 加权平均: overall_score        │
    │    - 阈值判断: passed (bool)        │
    └────────────────────────────────────┘
             ↓
    ┌────────────────────────────────────┐
    │ 6. 返回质量报告                     │
    │    {scores, overall_score, passed, ...} │
    └────────────────────────────────────┘
```

### 核心组件

| 组件 | 职责 | 文件位置 |
|------|------|---------|
| **UnifiedQualityValidator** | 统一验证入口 | `llm/validators/unified_quality_validator.py` |
| **ValidationInput** | 输入标准化 | 同上 |
| **PromptBuilder** | 根据类型构建 Prompt | 同上 |
| **ScoreCalculator** | 本地计算加权平均 | 同上 |
| **结构一致性检查** | 对话流专用检查 | 同上 |

---

## 配置方案

### 完整配置示例

```jsonc
// config/config.jsonc
{
    "llm": {
        // ... 其他 LLM 配置 ...

        // ============================================================
        // 统一质量验证配置（新增）
        // ============================================================
        "quality_validation": {
            // 评分维度权重（总和为 1.0）
            "score_weights": {
                "accuracy": 0.40,      // 准确性权重 40%
                "completeness": 0.30,  // 完整性权重 30%
                "fluency": 0.20,       // 流畅度权重 20%
                "format": 0.10         // 格式规范权重 10%
            },

            // 质量阈值
            "quality_threshold": {
                "overall_score": 8.0,           // 整体分数阈值（0-10）
                "minimum_single_score": 7.0     // 单项最低分阈值（0-10）
            }
        },

        // ============================================================
        // 分段处理配置（纯文本）
        // ============================================================
        "segmentation": {
            "enable_threshold": 20000,
            "segment_size": 8000,
            "max_segment_size": 12000,
            "concurrent_workers": 10,

            // 质量验证配置（用于纯文本分段）
            "quality_validation": {
                // ===== 开关：默认开启 =====
                "enabled": true,

                // 三个区间的阈值（仅用于纯文本）
                "pass_ratio": 0.7,          // >= 0.7 绿灯区，直接通过
                "force_retry_ratio": 0.5,   // < 0.5 红灯区，直接重试
                // 0.5 ~ 0.7 黄灯区，触发质量验证

                // 失败策略
                // - "best_quality": 接受质量分最高的结果（推荐）
                // - "formatted_original": 回退到格式化原文
                // - "second_attempt": 无条件接受第二次结果
                "fallback_strategy": "best_quality"
            }
        },

        // ============================================================
        // 结构化校准配置（对话流）
        // ============================================================
        "structured_calibration": {
            "min_chunk_length": 800,
            "max_chunk_length": 3000,
            "preferred_chunk_length": 2000,
            "max_calibration_retries": 2,
            "calibration_concurrent_limit": 10,

            // 质量验证配置（用于对话流）
            "quality_validation": {
                // ===== 开关：默认关闭 =====
                // 因为对话流已有说话人推断和结构检查，通常不需要额外验证
                "enabled": false,

                // 对话流不使用长度比例筛选，直接验证
                // 验证前会进行结构一致性检查（本地算法）

                // 失败策略
                "fallback_strategy": "best_quality"
            }
        }
    }
}
```

### 配置说明

#### 1. 统一配置（llm.quality_validation）

**位置**：`llm.quality_validation`

**说明**：所有质量验证共享的基础配置。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `score_weights` | object | 见上 | 评分维度权重（总和为 1.0） |
| `score_weights.accuracy` | float | 0.40 | 准确性权重（对话流：含说话人标识） |
| `score_weights.completeness` | float | 0.30 | 完整性权重 |
| `score_weights.fluency` | float | 0.20 | 流畅度权重 |
| `score_weights.format` | float | 0.10 | 格式规范权重 |
| `quality_threshold` | object | - | 质量阈值 |
| `quality_threshold.overall_score` | float | 8.0 | 整体分数阈值（0-10） |
| `quality_threshold.minimum_single_score` | float | 7.0 | 单项最低分阈值（0-10） |

#### 2. 纯文本场景（segmentation.quality_validation）

**位置**：`llm.segmentation.quality_validation`

**默认状态**：**开启**（`enabled: true`）

**使用场景**：
- CapsWriter 转录的长文本
- YouTube 直接获取的字幕
- 无说话人识别的纯文本

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | bool | true | 是否启用质量验证 |
| `pass_ratio` | float | 0.7 | 绿灯区阈值：>= 0.7 直接通过，不验证 |
| `force_retry_ratio` | float | 0.5 | 红灯区阈值：< 0.5 直接重试，不验证 |
| `fallback_strategy` | string | "best_quality" | 失败策略（见下文） |

**三区间逻辑**：

```
长度比例 >= 0.7  → 绿灯区：直接通过 ✅
0.5 <= 长度比例 < 0.7  → 黄灯区：触发质量验证 🔍
长度比例 < 0.5  → 红灯区：直接重试 ❌
```

#### 3. 对话流场景（structured_calibration.quality_validation）

**位置**：`llm.structured_calibration.quality_validation`

**默认状态**：**关闭**（`enabled: false`）

**原因**：对话流已有以下保障机制，通常不需要额外验证：
- 说话人推断（SpeakerInferencer）
- 结构一致性检查（本地算法）
- Prompt 约束（要求保持对话结构）

**使用场景**（开启时）：
- 需要额外质量保障
- 调试校对效果
- 特殊场景验证

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | bool | false | 是否启用质量验证 |
| `fallback_strategy` | string | "best_quality" | 失败策略（见下文） |

**注意**：对话流不使用长度比例筛选，直接进入质量验证。

#### 4. 失败策略（fallback_strategy）

| 策略 | 说明 | 推荐场景 |
|------|------|---------|
| `best_quality` | 接受质量分最高的结果（即使不达标） | **推荐**，避免回退到原文 |
| `formatted_original` | 回退到格式化原文 | 对质量要求极高 |
| `second_attempt` | 无条件接受第二次结果 | 快速通过，不计较质量 |

---

## 实现细节

### 文件结构

```
src/video_transcript_api/llm/
└── validators/
    └── unified_quality_validator.py    # 新增：统一质量验证器（约 600 行）
        ├── ValidationInput              # 输入标准化
        ├── PromptBuilder               # Prompt 构建
        ├── ScoreCalculator             # 评分计算
        ├── VALIDATION_SCHEMA           # JSON Schema
        └── UnifiedQualityValidator     # 主类
```

### 核心类设计

#### 1. ValidationInput（输入标准化）

```python
@dataclass
class ValidationInput:
    """验证输入的标准化数据类"""

    content_type: str  # "text" 或 "dialog"
    original: Union[str, List[Dict]]
    calibrated: Union[str, List[Dict]]
    length_info: Dict[str, Any]

    @classmethod
    def from_inputs(cls, original, calibrated) -> "ValidationInput":
        """自动识别类型并标准化"""
        pass
```

**长度信息**：

- **纯文本**：
  ```python
  {
      "original_length": 1000,
      "calibrated_length": 750,
      "ratio": 0.75
  }
  ```

- **对话流**：
  ```python
  {
      "original_count": 10,        # 对话条数
      "calibrated_count": 10,
      "count_ratio": 1.0,
      "original_length": 1000,     # 总字符数
      "calibrated_length": 800,
      "ratio": 0.80
  }
  ```

#### 2. PromptBuilder（Prompt 构建）

**统一 System Prompt**：

```
你是一位专业的文本质量评估专家。
评估维度：
1. 准确性（40%）：核心信息是否保留
2. 完整性（30%）：删减是否合理
3. 流畅度（20%）：语句是否通顺
4. 格式规范（10%）：段落划分是否合理

重点：对于直播、闲聊类内容，合理删减不应扣分。
```

**User Prompt 差异**：

| 类型 | 输入展示 | 重点提示 |
|------|---------|---------|
| 纯文本 | 文本片段（前 2000 字符） | 关注删减合理性、长度比例 |
| 对话流 | JSON 结构（采样 50 条） | 关注 text 质量、说话人信息已验证 |

**对话流采样策略**：
- 总数 ≤ 50：全部展示
- 总数 > 50：采样头部 40% + 中部 30% + 尾部 30%

#### 3. 对话流结构一致性检查

**检查项**：

```python
def _check_dialog_structure(original: List[Dict], calibrated: List[Dict]) -> Dict:
    """检查对话结构一致性

    Returns:
        {
            "passed": bool,
            "issues": List[str],
            "count_match": bool,
            "speaker_mismatches": List[Dict[index, original_speaker, calibrated_speaker]]
        }
    """
```

**约束条件**：

1. **条数检查**：`len(original) == len(calibrated)`
2. **说话人检查**：`original[i].speaker == calibrated[i].speaker` （逐条）
3. **顺序对应**：索引必须一一对应

**失败处理**：
- 结构检查失败 → 直接返回失败（不调用 LLM）
- 返回 `overall_score: 0`，`passed: false`

#### 4. ScoreCalculator（本地计算）

```python
class ScoreCalculator:
    def calculate_overall_score(self, scores: Dict[str, float]) -> float:
        """本地计算加权平均"""
        overall = sum(scores[dim] * weight for dim, weight in self.weights.items())
        return round(overall, 2)

    def check_passed(self, overall_score: float, scores: Dict[str, float]) -> bool:
        """检查是否通过"""
        if overall_score < self.thresholds["overall_score"]:
            return False
        if any(score < self.thresholds["minimum_single_score"] for score in scores.values()):
            return False
        return True
```

#### 5. JSON Schema

```python
VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "properties": {
                "accuracy": {"type": "number", "minimum": 0, "maximum": 10},
                "completeness": {"type": "number", "minimum": 0, "maximum": 10},
                "fluency": {"type": "number", "minimum": 0, "maximum": 10},
                "format": {"type": "number", "minimum": 0, "maximum": 10}
            },
            "required": ["accuracy", "completeness", "fluency", "format"]
        },
        "issues": {"type": "array", "items": {"type": "string"}},
        "deleted_content_analysis": {"type": "string"},
        "recommendation": {"type": "string"}
    },
    "required": ["scores"]
}
```

**注意**：Schema 中**不包含** `overall_score` 和 `pass` 字段（由本地计算）。

### 说话人信息时序

| 阶段 | speaker 字段 | 示例 |
|------|-------------|------|
| **转录原始输出** | spk_0, spk_1, spk_2 | `spk_0` |
| **说话人推断后** | 真实姓名（如果推断成功） | `张三` |
| **校对时（Prompt）** | 真实姓名 | `张三` |
| **质量验证时** | 真实姓名 | `张三` |

**关键**：质量验证看到的是**真实姓名**，而非 spk_0。

---

## 迁移指南

> ✅ **补充说明（修订）**：本节已纳入实现差异与潜在问题的修复措施，避免出现“改了配置却不生效”“Schema 不兼容”等隐性故障。

### 前置条件

- 备份现有配置文件
- 确保测试环境可用

### 步骤 1：更新配置文件

#### 1.1 新增统一质量验证配置

在 `llm` 下新增：

```jsonc
"llm": {
    // ... 其他配置 ...

    "quality_validation": {
        "score_weights": {
            "accuracy": 0.40,
            "completeness": 0.30,
            "fluency": 0.20,
            "format": 0.10
        },
        "quality_threshold": {
            "overall_score": 8.0,
            "minimum_single_score": 7.0
        }
    }
}
```

#### 1.2 修改 segmentation 配置

新增 `quality_validation` 子配置：

```jsonc
"segmentation": {
    // ... 原有配置保持不变 ...

    "quality_validation": {
        "enabled": true,
        "pass_ratio": 0.7,
        "force_retry_ratio": 0.5,
        "fallback_strategy": "best_quality"
    }
}
```

#### 1.3 修改 structured_calibration 配置

**删除**以下废弃字段：
- `quality_threshold` → 移到 `llm.quality_validation`
- `enable_validation` → 改为 `quality_validation.enabled`
- `fallback_to_original` → 改为 `quality_validation.fallback_strategy`
- `validator_model` → 自动使用校对模型
- `validator_reasoning_effort` → 自动继承
- `risk_validator_model` → 自动处理
- `risk_validator_reasoning_effort` → 自动处理

**新增**：

```jsonc
"structured_calibration": {
    // ... 原有配置保持不变 ...

    "quality_validation": {
        "enabled": false,  // 默认关闭
        "fallback_strategy": "best_quality"
    }
}
```

---

### 步骤 1.4 重要：同步更新配置解析逻辑（否则新配置不生效）

> **问题**：当前配置解析仍读取旧路径  
> `structured_calibration.quality_threshold` / `structured_calibration.enable_validation`  
> 这会导致新的 `llm.quality_validation` / `segmentation.quality_validation` / `structured_calibration.quality_validation` **不生效**。

**修复要求**：

- 读取统一质量配置：
  - `llm.quality_validation.score_weights`
  - `llm.quality_validation.quality_threshold`
- 读取纯文本开关与阈值：
  - `llm.segmentation.quality_validation.enabled`
  - `llm.segmentation.quality_validation.pass_ratio`
  - `llm.segmentation.quality_validation.force_retry_ratio`
  - `llm.segmentation.quality_validation.fallback_strategy`
- 读取对话流开关与策略：
  - `llm.structured_calibration.quality_validation.enabled`
  - `llm.structured_calibration.quality_validation.fallback_strategy`

**兼容策略（建议）**：
- 若新字段缺失，则回退到旧字段（向后兼容）
- 新旧字段同时存在时，以新字段为准

---

### 步骤 1.5 风险模型选择生效（必须修复）

> **问题**：当前验证函数接收 `selected_models` 但未使用。  
> 即使风险模型选择开启，也不会切换验证模型。

**修复要求**：
- 验证时优先使用 `selected_models["validator_model"]`
- 若 `selected_models` 为空，再回退到默认模型

### 步骤 2：配置对比表

| 旧配置 | 新配置 | 迁移说明 |
|--------|--------|---------|
| `structured_calibration.quality_threshold` | `llm.quality_validation.quality_threshold` | 移到统一配置 |
| `structured_calibration.enable_validation` | `structured_calibration.quality_validation.enabled` | 改名 + 默认改为 false |
| `structured_calibration.fallback_to_original: true` | `structured_calibration.quality_validation.fallback_strategy: "formatted_original"` | 改为枚举值 |
| `structured_calibration.fallback_to_original: false` | `structured_calibration.quality_validation.fallback_strategy: "best_quality"` | 改为枚举值 |
| `structured_calibration.validator_model` | （删除） | 自动使用校对模型 |
| `structured_calibration.validator_reasoning_effort` | （删除） | 自动继承 |
| `structured_calibration.risk_validator_model` | （删除） | 自动处理 |
| `structured_calibration.risk_validator_reasoning_effort` | （删除） | 自动处理 |

### 步骤 3：代码更新

#### 3.1 PlainTextProcessor

**旧代码**：
```python
# 只有长度检查
if calibrated_length < min_length:
    formatted_segment = self._format_plain_text(segment)
    calibrated_segments[index] = formatted_segment
```

**新代码**：
```python
# 三区间 + 质量验证
if calibrated_length >= min_length:  # 绿灯区
    calibrated_segments[index] = calibrated_text
elif calibrated_length < force_retry_threshold:  # 红灯区
    # 重试逻辑...
else:  # 黄灯区
    if config.quality_validation.enabled:
        quality_result = validator.validate(segment, calibrated_text)
        if quality_result["passed"]:
            calibrated_segments[index] = calibrated_text
        else:
            # 重试或回退...
```

#### 3.2 SpeakerAwareProcessor

**旧代码**：
```python
# 直接使用 QualityValidator.validate_by_score()
if enable_validation:
    result = quality_validator.validate_by_score(original, calibrated)
    if result["passed"]:
        return calibrated
```

**新代码**：
```python
# 使用 UnifiedQualityValidator.validate()
if config.quality_validation.enabled:
    quality_result = validator.validate(original, calibrated, context)
    if quality_result["passed"]:
        return calibrated
    else:
        # 失败策略处理...
```

---

### 步骤 3.3 补充：结构一致性检查应“强制执行”（即使关闭质量验证）

> **问题**：如果质量验证默认关闭，当前流程只依赖 LLM 输出，可能出现说话人被改写但未被发现。  
> 对话结构一致性是“硬约束”，不应由开关控制。

**修复建议**：
- 在合并 LLM 结果后，始终执行本地结构一致性检查：
  - 条数一致
  - speaker 一致
  - 顺序一致
- 若失败：直接降级到原始 chunk

---

### 步骤 3.4 补充：评分维度与 Schema 必须同步

> **问题**：当前验证 Schema 使用 `format_correctness/content_fidelity/...`  
> 新方案使用 `accuracy/completeness/fluency/format`，并改为本地计算 `overall_score`。

**修复要求**：
- 替换 Prompt 中的评分维度描述
- 替换 Schema 字段（去掉 `overall_score` 与 `pass`）
- 本地计算 `overall_score` 与 `passed`
- 若需兼容旧输出，可增加解析兼容分支（可选）

---

### 步骤 3.5 补充：长度阈值统一策略（避免冲突）

> **问题**：现有 `min_calibrate_ratio` 与新方案 `pass_ratio/force_retry_ratio` 可能冲突。

**修复建议**：
1. **统一主策略**：  
   - 纯文本分段使用 `pass_ratio/force_retry_ratio` 作为主阈值  
   - `min_calibrate_ratio` 仅用于 prompt 提示与日志，不作为硬拒绝
2. 或者：  
   - 明确优先级：`force_retry_ratio <= min_calibrate_ratio <= pass_ratio`

---

### 步骤 3.6 补充：Prompt/Schema 位置

建议新增以下文件以避免与现有验证逻辑混淆：

```
src/video_transcript_api/llm/validators/unified_quality_validator.py
src/video_transcript_api/llm/prompts/unified_validation_prompts.py
src/video_transcript_api/llm/prompts/schemas/unified_validation.py
```

并在 `llm/prompts/__init__.py` 中显式导出，以便统一调用。

### 步骤 4：测试验证

#### 4.1 单元测试

```bash
# 测试统一验证器
pytest tests/llm/test_unified_quality_validator.py -v

# 测试纯文本处理器
pytest tests/llm/test_plain_text_processor.py -v

# 测试对话流处理器
pytest tests/llm/test_speaker_aware_processor.py -v
```

#### 4.2 集成测试

```bash
# 完整流程测试
pytest tests/integration/test_quality_validation_flow.py -v
```

#### 4.3 手动验证

1. **纯文本场景**：
   - 提交一个 YouTube 字幕转录
   - 检查日志：是否触发质量验证
   - 验证结果：quality_result 是否包含 overall_score

2. **对话流场景**：
   - 提交一个 FunASR 带说话人的转录
   - 检查日志：是否跳过质量验证（默认关闭）
   - 手动开启后：是否有结构检查日志

#### 4.4 现有测试需要同步更新（否则全部失败）

> **受影响测试**（现有字段不兼容）：
- `tests/llm/test_quality_validation_scoring.py`
- `tests/llm/test_chunk_validation.py`

**修复要点**：
1. 更新评分字段名（旧：format_correctness → 新：format 等）
2. 由本地计算 `overall_score` / `passed`，测试断言需更新
3. 若保留兼容解析逻辑，增加覆盖测试（新旧 Schema 都可通过）

### 步骤 5：回滚方案

如果迁移出现问题，回滚步骤：

1. 恢复配置文件备份
2. 切换到迁移前的 git commit
3. 重启服务

---

## 测试计划

### 单元测试

#### 1. ValidationInput 测试

```python
def test_validation_input_text():
    """测试纯文本输入标准化"""
    original = "这是原始文本"
    calibrated = "这是校对后的文本"

    vi = ValidationInput.from_inputs(original, calibrated)

    assert vi.content_type == "text"
    assert vi.length_info["ratio"] == len(calibrated) / len(original)

def test_validation_input_dialog():
    """测试对话流输入标准化"""
    original = [
        {"speaker": "张三", "text": "你好"},
        {"speaker": "李四", "text": "你好"}
    ]
    calibrated = [
        {"speaker": "张三", "text": "您好"},
        {"speaker": "李四", "text": "您好"}
    ]

    vi = ValidationInput.from_inputs(original, calibrated)

    assert vi.content_type == "dialog"
    assert vi.length_info["original_count"] == 2
    assert vi.length_info["calibrated_count"] == 2
```

#### 2. 结构检查测试

```python
def test_dialog_structure_check_pass():
    """测试对话流结构检查通过"""
    validator = UnifiedQualityValidator(...)

    original = [{"speaker": "A", "text": "hi"}, {"speaker": "B", "text": "hi"}]
    calibrated = [{"speaker": "A", "text": "hello"}, {"speaker": "B", "text": "hello"}]

    result = validator._check_dialog_structure(original, calibrated)

    assert result["passed"] == True
    assert result["count_match"] == True
    assert len(result["speaker_mismatches"]) == 0

def test_dialog_structure_check_fail_count():
    """测试对话流条数不匹配"""
    original = [{"speaker": "A", "text": "hi"}]
    calibrated = [{"speaker": "A", "text": "hello"}, {"speaker": "B", "text": "hi"}]

    result = validator._check_dialog_structure(original, calibrated)

    assert result["passed"] == False
    assert result["count_match"] == False

def test_dialog_structure_check_fail_speaker():
    """测试说话人不匹配"""
    original = [{"speaker": "A", "text": "hi"}]
    calibrated = [{"speaker": "B", "text": "hello"}]  # 说话人错误

    result = validator._check_dialog_structure(original, calibrated)

    assert result["passed"] == False
    assert len(result["speaker_mismatches"]) == 1
```

#### 3. ScoreCalculator 测试

```python
def test_calculate_overall_score():
    """测试加权平均计算"""
    calculator = ScoreCalculator(
        weights={"accuracy": 0.4, "completeness": 0.3, "fluency": 0.2, "format": 0.1},
        thresholds={"overall_score": 8.0, "minimum_single_score": 7.0}
    )

    scores = {"accuracy": 9.0, "completeness": 8.5, "fluency": 9.5, "format": 8.0}
    overall = calculator.calculate_overall_score(scores)

    expected = 9.0 * 0.4 + 8.5 * 0.3 + 9.5 * 0.2 + 8.0 * 0.1
    assert overall == round(expected, 2)

def test_check_passed():
    """测试阈值判断"""
    calculator = ScoreCalculator(...)

    # 通过
    assert calculator.check_passed(8.5, {"accuracy": 8, "completeness": 8, ...}) == True

    # 整体分不足
    assert calculator.check_passed(7.5, {"accuracy": 8, ...}) == False

    # 单项分不足
    assert calculator.check_passed(8.5, {"accuracy": 6.5, ...}) == False
```

### 集成测试

#### 1. 纯文本完整流程

```python
def test_plain_text_quality_validation_flow():
    """测试纯文本质量验证完整流程"""
    processor = PlainTextProcessor(...)

    # 模拟：长度在黄灯区（0.5~0.7），触发验证，验证通过
    result = processor.process(
        text="...",  # 模拟文本
        title="测试视频",
        ...
    )

    # 验证日志
    assert "Quality validation started" in logs
    assert "Quality validation completed: overall=8.5, passed=True" in logs
```

#### 2. 对话流完整流程

```python
def test_dialog_quality_validation_disabled():
    """测试对话流默认关闭质量验证"""
    processor = SpeakerAwareProcessor(...)

    result = processor.process(
        dialogs=[...],
        title="测试视频",
        ...
    )

    # 验证：未触发质量验证
    assert "Quality validation" not in logs

def test_dialog_quality_validation_structure_fail():
    """测试对话流结构检查失败"""
    # 手动开启质量验证
    config.structured_calibration.quality_validation.enabled = True

    # 模拟 LLM 返回了错误的结构（条数不匹配）
    ...

    result = processor.process(...)

    # 验证：结构检查失败，返回失败结果
    assert result["quality_check"]["passed"] == False
    assert "Dialog count mismatch" in result["quality_check"]["issues"]
```

---

## 附录

### A. 完整配置示例

见 `config/config.example.jsonc`（已包含所有新增配置）

### B. API 文档

#### UnifiedQualityValidator.validate()

```python
def validate(
    self,
    original: Union[str, List[Dict]],
    calibrated: Union[str, List[Dict]],
    context: Optional[Dict] = None,
) -> Dict:
    """统一验证入口

    Args:
        original: 原始内容
            - 纯文本: str
            - 对话流: List[Dict[speaker, text, start_time, ...]]
        calibrated: 校对后内容（格式同 original）
        context: 上下文信息
            - title: str
            - description: str
            - author: str

    Returns:
        {
            "scores": {
                "accuracy": float (0-10),
                "completeness": float (0-10),
                "fluency": float (0-10),
                "format": float (0-10)
            },
            "overall_score": float (0-10),  # 本地计算
            "passed": bool,                  # 本地判断
            "issues": List[str],
            "deleted_content_analysis": str,
            "recommendation": str,
            "length_info": Dict,
            "structure_check": Dict (仅对话流)
        }
    """
```

### C. 参考资料

- [LLM 工程指南](./engineering_guide.md)
- [质量验证打分机制](../../guides/quality_scoring.md)
- [配置文件说明](../../../config/README.md)

---

**文档结束**
