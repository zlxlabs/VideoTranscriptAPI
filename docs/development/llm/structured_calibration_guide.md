# 结构化 JSON 文本校对流程设计文档

> **文档版本**: v1.0
> **创建时间**: 2026-01-27
> **适用范围**: 重构后的 LLM 校对系统
> **核心目标**: 保留时间戳和对话结构，实现高质量的结构化文本校对

---

## 一、背景与目标

### 1.1 为什么需要结构化校对？

**普通文本校对 vs 结构化文本校对**：

| 维度 | 普通文本（TXT） | 结构化文本（JSON） |
|------|----------------|-------------------|
| **输入格式** | 纯文本字符串 | 带时间戳的对话列表 |
| **输出格式** | 纯文本字符串 | 带时间戳的对话列表 + 纯文本 |
| **核心难点** | 分段边界 | 保留时间戳、说话人、对话结构 |
| **应用场景** | YouTube 字幕、CapsWriter 转录 | FunASR 说话人识别转录 |

### 1.2 结构化校对的核心要求

✅ **必须保留的信息**：
- `start_time`：对话开始时间（格式：`HH:MM:SS`）
- `end_time`：对话结束时间
- `duration`：对话持续时长（秒）
- `speaker`：说话人标识（可以是映射后的真实姓名）
- `original_text`：原始文本（用于对比）

✅ **必须满足的约束**：
- 对话数量保持不变（`len(input) == len(output)`）
- 对话顺序保持不变
- 时间戳完全一致（不信任 LLM 生成的时间戳）
- 只校对 `text` 字段

---

## 二、新旧方案对比

### 2.1 旧方案：`llm_segmented.py` 的 `_calibrate_json_segmented()`

**流程**：
```
1. 生成说话人映射
   ↓
2. 应用映射并分段（TextSegmentationProcessor）
   ↓
3. 并发校对每个段落
   ↓
4. 合并段落为纯文本
```

**问题**：
- ❌ **丢失结构化信息**：最终只返回纯文本，不保留时间戳
- ❌ **无法用于后续处理**：无法生成 `llm_processed.json`
- ❌ **分段策略不合理**：将对话转为文本后按字符数分段，破坏对话边界

**代码片段**（`llm_segmented.py` 第 270-350 行）：
```python
# 应用说话人映射并分段
segments = self.segmentation_processor.segment_json_content(file_path, speaker_mapping)

# 校对每个段落（转为纯文本）
segment_text = self._json_segment_to_text(segment_data)
calibrated_text = call_llm_api(...)  # 返回纯文本

# 合并为纯文本
final_result = self.segmentation_processor.merge_json_segments(calibrated_segments)
```

---

### 2.2 旧方案：`structured_calibrator.py`（推荐参考）

**流程**：
```
1. 智能分块（按对话长度，保持对话完整性）
   ↓
2. 并发校对每个 chunk（LLM 返回结构化 JSON）
   ↓
3. 合并校对结果与原始对话（保留时间戳）
   ↓
4. 质量验证（每个 chunk 独立验证）
   ↓
5. 返回完整的结构化对话列表 + 纯文本
```

**优点**：
- ✅ **保留完整结构**：输出包含时间戳、说话人、原始文本
- ✅ **质量可控**：每个 chunk 独立验证，失败降级
- ✅ **可用于后续处理**：可生成 `llm_processed.json`

**核心逻辑**（`structured_calibrator.py` 第 394-407 行）：
```python
for i, dialog_data in enumerate(calibrated_data_list):
    # 找到对应的原始对话
    original_dialog = self._find_matching_original_dialog(dialog_data, chunk, i)

    # 合并校对结果与原始对话
    calibrated_dialog = {
        'start_time': dialog_data.get('start_time', original_dialog.get('start_time')),
        'end_time': original_dialog.get('end_time'),      # ✅ 从原始对话获取
        'duration': original_dialog.get('duration'),       # ✅ 从原始对话获取
        'speaker': dialog_data.get('speaker'),
        'text': dialog_data.get('text'),
        'original_text': original_dialog.get('text')       # ✅ 保留原始文本
    }
```

---

### 2.3 新方案：重构后的 `SpeakerAwareProcessor`

**当前状态**：
- ✅ 已实现关键信息提取（`KeyInfoExtractor`）
- ✅ 已实现说话人推断（`SpeakerInferencer`）
- ✅ 已实现智能分块（`DialogSegmenter`）
- ✅ 已实现并发校对
- ❌ **缺少合并逻辑**（需要补充 `_merge_calibrated_with_original()`）

---

## 三、完整的结构化校对流程

### 3.1 流程图

```
┌─────────────────────────────────────────────────────────────┐
│           SpeakerAwareProcessor.process()                   │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ 输入：dialogs = [                                            │
│   {start_time, end_time, duration, speaker, text},          │
│   ...                                                        │
│ ]                                                            │
└───────────────────────┬─────────────────────────────────────┘
                        │
        ┌───────────────┼───────────────┐
        │               │               │
┌───────▼────────┐ ┌───▼────────┐ ┌───▼──────────┐
│ 步骤 1.1       │ │ 步骤 1.2   │ │ 步骤 1.3     │
│ 提取关键信息   │ │ 说话人推断 │ │ 应用映射     │
│ (KeyInfo)      │ │ (Mapping)  │ │ Speaker1→张三│
└───────┬────────┘ └───┬────────┘ └───┬──────────┘
        │              │              │
        └──────────────┼──────────────┘
                       │
                       ▼
        ┌──────────────────────────┐
        │ 步骤 2: 智能分块         │
        │ DialogSegmenter.segment()│
        │                          │
        │ chunks = [               │
        │   [{dialog1}, {dialog2}],│
        │   [{dialog3}],           │
        │   ...                    │
        │ ]                        │
        └──────────┬───────────────┘
                   │
                   ▼
        ┌──────────────────────────┐
        │ 步骤 3: 并发校对         │
        │ ThreadPoolExecutor       │
        │                          │
        │ 对每个 chunk:            │
        │  1. 格式化为 prompt      │
        │  2. 调用 LLM（结构化输出）│
        │  3. 解析 JSON 结果       │
        └──────────┬───────────────┘
                   │
                   ▼
        ┌──────────────────────────┐
        │ 步骤 4: 合并逻辑 ✅ 核心 │
        │ _merge_calibrated_with_  │
        │ original()               │
        │                          │
        │ 校对结果 + 原始对话      │
        │     ↓                    │
        │ 完整的结构化对话         │
        │ (保留时间戳)             │
        └──────────┬───────────────┘
                   │
                   ▼
        ┌──────────────────────────┐
        │ 步骤 5: 质量验证（可选） │
        │ QualityValidator         │
        │                          │
        │ 每个 chunk 独立验证      │
        │ 失败 → 降级到原文        │
        └──────────┬───────────────┘
                   │
                   ▼
        ┌──────────────────────────┐
        │ 步骤 6: 合并所有 chunks  │
        │                          │
        │ calibrated_dialogs = []  │
        │ for chunk in chunks:     │
        │   calibrated_dialogs     │
        │     .extend(chunk)       │
        └──────────┬───────────────┘
                   │
                   ▼
        ┌──────────────────────────┐
        │ 输出：                   │
        │ {                        │
        │   calibrated_text: str,  │
        │   structured_data: {     │
        │     dialogs: [...],      │
        │     speaker_mapping: {}  │
        │   },                     │
        │   key_info: {...},       │
        │   stats: {...}           │
        │ }                        │
        └──────────────────────────┘
```

---

### 3.2 关键步骤详解

#### 步骤 1：前置处理（关键信息提取 + 说话人推断）

**目的**：为后续校对提供上下文信息

```python
# 步骤 1.1: 提取关键信息
key_info = self.key_info_extractor.extract(
    title=title,
    author=author,
    description=description,
    platform=platform,
    media_id=media_id,
)
# 输出示例：
# KeyInfo(
#     names=["贝壳", "热带木"],
#     places=["湖南", "临湘"],
#     technical_terms=["碎尸案", "安检"],
#     ...
# )

# 步骤 1.2: 说话人推断
speakers = ["Speaker1", "Speaker2"]
speaker_mapping = self.speaker_inferencer.infer(
    speakers=speakers,
    dialogs=dialogs,
    title=title,
    key_info=key_info,
    platform=platform,
    media_id=media_id,
)
# 输出示例：
# {
#     "Speaker1": "贝壳",
#     "Speaker2": "热带木"
# }
```

---

#### 步骤 2：智能分块

**目的**：将长对话列表分成多个 chunk，便于并发处理

```python
chunks = self.segmenter.segment(dialogs)

# 输入示例：
# dialogs = [
#   {"speaker": "Speaker1", "text": "这是一段很长的对话...", "start_time": "00:00:03", ...},  # 1500 字符
#   {"speaker": "Speaker2", "text": "回复...", "start_time": "00:00:23", ...},  # 200 字符
#   {"speaker": "Speaker1", "text": "继续...", "start_time": "00:00:30", ...},  # 800 字符
#   ...
# ]

# 输出示例（按长度分块）：
# chunks = [
#   [dialog1, dialog2],  # 总长度 1700 字符
#   [dialog3],           # 总长度 800 字符
#   ...
# ]
```

**分块策略**（参考 `DialogSegmenter`）：
- 单个对话 > `max_chunk_length`（1500）→ 拆分该对话（⚠️ 时间戳共享）
- 累积长度 > `max_chunk_length` → 结束当前 chunk
- 累积长度 >= `preferred_chunk_length`（800）→ 结束当前 chunk
- 最后一个 chunk < `min_chunk_length`（300）→ 合并到前一个

---

#### 步骤 3：并发校对（结构化输出）

**目的**：调用 LLM 校对每个 chunk，返回结构化 JSON

```python
def calibrate_single_chunk(index: int, chunk: List[Dict]):
    # 3.1 格式化为 prompt
    chunk_text = self._format_chunk_for_prompt(chunk, speaker_mapping)
    # 输出示例：
    # """
    # [贝壳]: 手法非常的干净利落，切口也十分的整齐...
    # [热带木]: 大家好，我是小贝。
    # """

    # 3.2 构建 user prompt（包含关键信息）
    user_prompt = build_structured_calibrate_user_prompt(
        dialogs_text=chunk_text,
        video_title=title,
        description=description,
        key_info=key_info.format_for_prompt(),  # ✅ 新增：传入关键信息
        dialog_count=len(chunk),  # ✅ 明确告知对话数量
        min_ratio=self.config.min_calibrate_ratio,
    )

    # 3.3 调用 LLM（结构化输出）
    response = self.llm_client.call(
        model=model,
        system_prompt=STRUCTURED_CALIBRATE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        response_schema=CALIBRATION_RESULT_SCHEMA,  # ✅ 强制结构化输出
        reasoning_effort=reasoning_effort,
        task_type="calibrate_chunk",
    )

    # 3.4 解析结构化输出
    calibrated_dialogs = response.structured_output.get("calibrated_dialogs", [])
    # 输出示例：
    # [
    #   {"start_time": "00:00:03", "speaker": "Speaker1", "text": "手法非常干净利落..."},
    #   {"start_time": "00:00:23", "speaker": "Speaker2", "text": "大家好，我是小贝。"}
    # ]
```

**Response Schema**（`schemas/calibration.py`）：
```python
CALIBRATION_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "calibrated_dialogs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "string"},
                    "speaker": {"type": "string"},
                    "text": {"type": "string"}
                },
                "required": ["start_time", "speaker", "text"]
            }
        }
    },
    "required": ["calibrated_dialogs"]
}
```

**注意**：
- ❌ **不要求 LLM 生成 `end_time` 和 `duration`**（避免错误）
- ✅ **后续通过合并逻辑从原始对话补充**

---

#### 步骤 4：合并逻辑（核心）✅

**目的**：将 LLM 返回的部分字段与原始对话合并，保留完整时间戳

```python
def _merge_calibrated_with_original(
    self,
    calibrated_dialogs: List[Dict],
    original_chunk: List[Dict]
) -> List[Dict]:
    """
    将校对后的对话与原始对话合并，保留完整的时间戳信息

    核心策略：
    - 时间戳（start_time, end_time, duration）：强制使用原始值
    - 说话人（speaker）：使用校对后的值（可能已应用映射）
    - 文本（text）：使用校对后的值
    - 原始文本（original_text）：备份原始值

    Args:
        calibrated_dialogs: LLM 返回的校对后对话（只包含 start_time, speaker, text）
        original_chunk: 原始对话列表（包含完整字段）

    Returns:
        合并后的对话列表（包含完整时间戳信息）
        如果数量不匹配，返回原始对话（降级策略）
    """
    # 1. 严格验证数量一致性
    if len(calibrated_dialogs) != len(original_chunk):
        logger.warning(
            f"Dialog count mismatch detected: "
            f"calibrated={len(calibrated_dialogs)}, original={len(original_chunk)}. "
            f"Falling back to original dialogs."
        )
        return original_chunk

    # 2. 逐个合并（按索引一对一映射）
    merged_dialogs = []
    for i, calibrated in enumerate(calibrated_dialogs):
        original = original_chunk[i]

        # 构建合并后的对话
        merged = {
            # ✅ 时间戳信息：从原始对话获取（不信任 LLM）
            'start_time': original.get('start_time', '00:00:00'),
            'end_time': original.get('end_time', '00:00:00'),
            'duration': original.get('duration', 0),

            # ✅ 说话人：从校对结果获取（LLM 可能已应用映射）
            'speaker': calibrated.get('speaker', original.get('speaker', 'unknown')),

            # ✅ 文本：从校对结果获取
            'text': calibrated.get('text', original.get('text', '')),

            # ✅ 原始文本：保留备份
            'original_text': original.get('text', ''),
        }

        merged_dialogs.append(merged)

    logger.debug(f"Successfully merged {len(merged_dialogs)} dialogs with original timestamps")
    return merged_dialogs
```

**合并前后对比**：

```python
# LLM 返回（部分字段）
calibrated_dialogs = [
    {"start_time": "00:00:03", "speaker": "Speaker1", "text": "手法非常干净利落..."}
]

# 原始对话（完整字段）
original_chunk = [
    {
        "start_time": "00:00:03",
        "end_time": "00:00:23",
        "duration": 20.05,
        "speaker": "Speaker1",
        "text": "手法非常的干净利落..."
    }
]

# 合并后（完整字段 + 校对文本）
merged = [
    {
        "start_time": "00:00:03",        # ← 从原始对话
        "end_time": "00:00:23",          # ← 从原始对话
        "duration": 20.05,               # ← 从原始对话
        "speaker": "Speaker1",           # ← 从校对结果（可能已映射）
        "text": "手法非常干净利落...",   # ← 从校对结果
        "original_text": "手法非常的干净利落..."  # ← 备份原始文本
    }
]
```

---

#### 步骤 5：质量验证（可选）

**目的**：确保校对质量，失败则降级到原文

```python
if self.config.enable_validation:
    validation_result = self.quality_validator.validate_by_score(
        original=chunk,
        calibrated=merged_dialogs,  # ✅ 使用合并后的对话
        video_metadata={"title": title, "description": description},
        selected_models=selected_models,
    )

    if not validation_result["passed"]:
        logger.warning(f"Chunk {index + 1} validation failed, falling back to original")
        return chunk  # 降级到原文
```

**验证指标**：
- `overall_score >= 8.0`：总体质量评分
- 单项得分 >= `7.0`：各维度（准确性、流畅性、一致性）

---

#### 步骤 6：合并所有 chunks

**目的**：将所有校对后的 chunks 合并为完整的对话列表

```python
calibrated_dialogs = []
for chunk in calibrated_chunks:
    calibrated_dialogs.extend(chunk)

# 同时生成纯文本（用于显示）
calibrated_text = self._build_text_from_dialogs(calibrated_dialogs)
```

---

### 3.3 最终输出格式

```python
{
    "calibrated_text": "[贝壳]: 手法非常干净利落...\n[热带木]: 大家好，我是小贝。",
    "structured_data": {
        "dialogs": [
            {
                "start_time": "00:00:03",
                "end_time": "00:00:23",
                "duration": 20.05,
                "speaker": "贝壳",  # ← 已应用映射
                "text": "手法非常干净利落，切口也十分整齐...",
                "original_text": "手法非常的干净利落，切口也十分的整齐..."
            },
            {
                "start_time": "00:00:23",
                "end_time": "00:00:25",
                "duration": 1.48,
                "speaker": "热带木",
                "text": "大家好，我是小贝。",
                "original_text": "大家好，我是小贝。"
            }
        ],
        "speaker_mapping": {
            "Speaker1": "贝壳",
            "Speaker2": "热带木"
        }
    },
    "key_info": {
        "names": ["贝壳", "热带木"],
        "places": ["湖南", "临湘"],
        ...
    },
    "stats": {
        "original_length": 120,
        "calibrated_length": 115,
        "dialog_count": 126,
        "chunk_count": 8
    }
}
```

---

## 四、与旧方案的关键差异

| 维度 | 旧方案 `llm_segmented.py` | 旧方案 `structured_calibrator.py` | 新方案（重构后） |
|------|--------------------------|----------------------------------|-----------------|
| **输出格式** | ❌ 纯文本 | ✅ 结构化 JSON + 纯文本 | ✅ 结构化 JSON + 纯文本 |
| **时间戳保留** | ❌ 无 | ✅ 完整保留 | ✅ 完整保留 |
| **关键信息提取** | ❌ 无 | ❌ 无 | ✅ 有 |
| **说话人推断** | ⚠️ 独立模块 | ⚠️ 外部传入 | ✅ 内置 |
| **质量验证** | ❌ 无 | ✅ 整体验证 | ✅ 每段独立验证 |
| **降级策略** | ❌ 无 | ✅ 有 | ✅ 有 |

---

## 五、实施清单

### 5.1 必须完成的修复

| 序号 | 任务 | 文件 | 优先级 | 状态 |
|------|------|------|--------|------|
| 1 | 新增 `_merge_calibrated_with_original()` 方法 | `speaker_aware_processor.py` | 🔴 高 | ⏳ 待实现 |
| 2 | 修改 `calibrate_single_chunk()` 调用合并逻辑 | `speaker_aware_processor.py` | 🔴 高 | ⏳ 待实现 |
| 3 | 修复 `DialogSegmenter` 的 3 个 bug | `dialog_segmenter.py` | 🔴 高 | ⏳ 待修复 |
| 4 | 优化 `CALIBRATION_RESULT_SCHEMA` | `schemas/calibration.py` | 🟡 中 | ⏳ 待优化 |
| 5 | 强化 Prompt 约束 | `prompts/prompts.py` | 🟡 中 | ⏳ 待强化 |

### 5.2 DialogSegmenter 的 3 个必修 Bug

#### Bug 1: 缺少空 chunk 检查（第 62 行）
```python
# ❌ 当前代码
if current_length + dialog_length > self.max_chunk_length:
    chunks.append(current_chunk)  # 没有检查是否为空

# ✅ 修复后
if current_length + dialog_length > self.max_chunk_length:
    if current_chunk:  # 添加检查
        chunks.append(current_chunk)
```

#### Bug 2: 长度计算低效（第 78 行）
```python
# ❌ 当前代码
if chunks and len("".join(d.get("text", "") for d in current_chunk)) < self.min_chunk_length:

# ✅ 修复后
if chunks and current_length < self.min_chunk_length:
```

#### Bug 3: 缺少空 chunk 过滤（返回前）
```python
# ✅ 在返回前添加
chunks = [chunk for chunk in chunks if chunk]
```

### 5.3 测试验证

```python
# 测试脚本示例
def test_structured_calibration():
    # 准备测试数据
    dialogs = [
        {"start_time": "00:00:03", "end_time": "00:00:23", "duration": 20.05, "speaker": "Speaker1", "text": "手法非常的干净利落..."},
        {"start_time": "00:00:23", "end_time": "00:00:25", "duration": 1.48, "speaker": "Speaker2", "text": "大家好，我是小贝。"}
    ]

    # 调用处理器
    result = processor.process(
        dialogs=dialogs,
        title="测试视频",
        platform="xiaoyuzhou",
        media_id="test123"
    )

    # 验证输出
    assert "structured_data" in result
    assert "dialogs" in result["structured_data"]
    assert len(result["structured_data"]["dialogs"]) == len(dialogs)

    # 验证时间戳保留
    for i, dialog in enumerate(result["structured_data"]["dialogs"]):
        assert dialog["start_time"] == dialogs[i]["start_time"]
        assert dialog["end_time"] == dialogs[i]["end_time"]
        assert dialog["duration"] == dialogs[i]["duration"]
        assert "original_text" in dialog
```

---

## 六、常见问题

### Q1: 为什么不让 LLM 生成 `end_time` 和 `duration`？

**A**: LLM 无法准确推断时间戳，生成的值可能不准确。通过强制从原始对话获取，确保数据一致性。

### Q2: 如果 LLM 返回的对话数量不匹配怎么办？

**A**: `_merge_calibrated_with_original()` 会检测数量不匹配，直接返回原始对话（降级策略）。

### Q3: 拆分超长对话会破坏时间戳吗？

**A**: 是的，这是已知限制。拆分后的子对话会共享相同的时间戳。建议在日志中记录警告，或禁止拆分（降级到原文）。

### Q4: 新方案与旧的 `llm_segmented.py` 如何共存？

**A**:
- `llm_segmented.py` 用于**纯文本校对**（无结构化要求）
- `SpeakerAwareProcessor` 用于**结构化校对**（保留时间戳）
- 通过协调器（`LLMCoordinator`）根据输入类型自动选择

### Q5: 质量验证的 `enable_validation` 配置项是什么？

**A**:
- `enable_validation=false`（默认）：不进行质量验证，直接使用校对结果（性能优先）
- `enable_validation=true`：每个分段独立打分验证，不合格则降级到原文（质量优先）

**注意**: 这与重构方案 v1.0 中的描述不同。v1.0 中该字段控制"合并后的整体打分验证"，v1.1 改为"分段时的独立打分验证"。

---

## 七、总结

**重构后的结构化校对流程**核心优势：
1. ✅ **完整保留时间戳**：`start_time`, `end_time`, `duration` 完全一致
2. ✅ **结构化输出**：可生成 `llm_processed.json`
3. ✅ **质量可控**：每段独立验证，失败降级
4. ✅ **集成新功能**：关键信息提取、说话人推断
5. ✅ **并发高效**：多线程并发处理

**下一步行动**：
1. 补充 `_merge_calibrated_with_original()` 方法
2. 修复 `DialogSegmenter` 的 3 个 bug
3. 运行测试验证输出格式
4. 逐步废弃旧的 `llm_segmented._calibrate_json_segmented()`

**相关文档**：
- [LLM 重构方案](./refactoring_plan.md)
- [LLM 工程指南](./engineering_guide.md)
- [LLM 重构完成报告](./refactoring_completed.md)

---

**文档维护**：
- 如有修改，请更新文档版本号
- 实施完成后，请更新实施清单中的状态
- 如发现新问题，请补充到"常见问题"章节
