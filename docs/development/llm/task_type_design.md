# task_type 参数传递设计

> **文档版本**: v1.0
> **设计时间**: 2026-01-27
> **目标**: 为新架构的所有 LLM 调用添加 `task_type` 参数

---

## 一、背景

### 1.1 为什么需要 task_type？

`task_type` 参数用于：
1. **日志追踪**：标识每个 LLM 调用的业务类型
2. **性能监控**：统计不同任务类型的耗时和成功率
3. **KV Cache 优化**：相同 `task_type` 的请求可以共享缓存

### 1.2 现有使用情况

旧架构中已经在使用以下 `task_type` 值：

| task_type | 使用场景 | 位置 |
|-----------|----------|------|
| `"calibrate"` | 基础校对 | `llm.py` |
| `"calibrate_segment"` | 分段校对（纯文本） | `llm_segmented.py`, `plain_text_processor.py` |
| `"calibrate_chunk"` | 分块校对（有说话人） | `structured_calibrator.py`, `speaker_aware_processor.py` |
| `"summary"` | 基础总结 | `llm_enhanced.py`, `llm_segmented.py` |
| `"segment_summary"` | 分段总结 | `llm_segmented.py` |
| `"final_summary"` | 最终总结 | `llm_segmented.py` |
| `"key_info"` | 关键信息提取 | `key_info_extractor.py` |
| `"speaker_inference"` | 说话人推断 | `llm_enhanced.py`, `speaker_inferencer.py` |
| `"speaker_mapping"` | 说话人映射 | `text_segmentation.py` |
| `"validate"` / `"quality_validation"` | 质量验证 | `structured_calibrator.py`, `quality_validator.py` |

---

## 二、LLMClient 支持情况

### 2.1 当前实现（已完成 ✅）

```python
# core/llm_client.py

class LLMClient:
    def call(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: Optional[Dict] = None,
        reasoning_effort: Optional[str] = None,
        task_type: str = "unknown",  # ← 已支持
    ) -> LLMResponse:
        """调用 LLM API（带智能重试）"""
        # ...
        result = self._actual_call(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=response_schema,
            reasoning_effort=reasoning_effort,
            task_type=task_type,  # ← 已传递
        )
        # ...

    def _actual_call(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: Optional[Dict],
        reasoning_effort: Optional[str],
        task_type: str,  # ← 已接受
    ) -> LLMResponse:
        """实际的 API 调用（不包含重试逻辑）"""
        result = call_llm_api(
            model=model,
            prompt=user_prompt,
            api_key=self.api_key,
            base_url=self.base_url,
            response_schema=response_schema,
            system_prompt=system_prompt,
            max_retries=0,
            retry_delay=0,
            reasoning_effort=reasoning_effort,
            task_type=task_type,  # ← 已传递
        )
        # ...
```

**结论**：✅ `LLMClient` 本身已经完整支持 `task_type` 参数。

---

## 三、新架构需要添加的位置

### 3.1 SummaryProcessor

#### 修改前（当前设计）：

```python
# processors/summary_processor.py

class SummaryProcessor:
    def process(
        self,
        text: str,
        title: str,
        author: str = "",
        description: str = "",
        speaker_count: int = 0,
        transcription_data: Optional[Dict] = None,
        selected_models: Optional[Dict] = None,
    ) -> Optional[str]:
        """生成文本总结"""
        # ...
        response = self.llm_client.call(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            reasoning_effort=reasoning_effort,
            # ❌ 缺少 task_type
        )
```

#### 修改后（需要添加）：

```python
# processors/summary_processor.py

class SummaryProcessor:
    def process(
        self,
        text: str,
        title: str,
        author: str = "",
        description: str = "",
        speaker_count: int = 0,
        transcription_data: Optional[Dict] = None,
        selected_models: Optional[Dict] = None,
    ) -> Optional[str]:
        """生成文本总结"""
        # ...

        # 调用 LLM
        response = self.llm_client.call(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            reasoning_effort=reasoning_effort,
            task_type="summary",  # ✅ 添加 task_type
        )

        # ...
```

### 3.2 PlainTextProcessor

#### 当前实现（已添加 ✅）：

```python
# processors/plain_text_processor.py (第 172 行)

response = self.llm_client.call(
    model=model,
    system_prompt=CALIBRATE_SYSTEM_PROMPT,
    user_prompt=user_prompt,
    reasoning_effort=reasoning_effort,
    task_type="calibrate_segment",  # ✅ 已添加
)
```

**结论**：✅ `PlainTextProcessor` 已经正确添加了 `task_type`。

### 3.3 SpeakerAwareProcessor

#### 当前实现（已添加 ✅）：

```python
# processors/speaker_aware_processor.py (第 217 行)

response = self.llm_client.call(
    model=model,
    system_prompt=CALIBRATE_SYSTEM_PROMPT_WITH_SPEAKER,
    user_prompt=user_prompt,
    response_schema=CALIBRATION_RESULT_SCHEMA,
    reasoning_effort=reasoning_effort,
    task_type="calibrate_chunk",  # ✅ 已添加
)
```

**结论**：✅ `SpeakerAwareProcessor` 已经正确添加了 `task_type`。

### 3.4 KeyInfoExtractor

#### 当前实现（已添加 ✅）：

```python
# core/key_info_extractor.py (第 138 行)

result = self.llm_client.call(
    model=self.model,
    system_prompt=system_prompt,
    user_prompt=user_prompt,
    response_schema=KEY_INFO_SCHEMA,
    reasoning_effort=self.reasoning_effort,
    task_type="key_info",  # ✅ 已添加
)
```

**结论**：✅ `KeyInfoExtractor` 已经正确添加了 `task_type`。

### 3.5 SpeakerInferencer

#### 当前实现（已添加 ✅）：

```python
# core/speaker_inferencer.py (第 109 行)

result = self.llm_client.call(
    model=self.model,
    system_prompt=system_prompt,
    user_prompt=user_prompt,
    response_schema=SPEAKER_MAPPING_SCHEMA,
    reasoning_effort=self.reasoning_effort,
    task_type="speaker_inference",  # ✅ 已添加
)
```

**结论**：✅ `SpeakerInferencer` 已经正确添加了 `task_type`。

### 3.6 QualityValidator

#### 当前实现（已添加 ✅）：

```python
# core/quality_validator.py (第 121 行)

result = self.llm_client.call(
    model=self.model,
    system_prompt=system_prompt,
    user_prompt=user_prompt,
    response_schema=VALIDATION_RESULT_SCHEMA,
    reasoning_effort=self.reasoning_effort,
    task_type="quality_validation",  # ✅ 已添加
)
```

**结论**：✅ `QualityValidator` 已经正确添加了 `task_type`。

---

## 四、总结功能 task_type 设计

### 4.1 task_type 命名规则

根据现有命名规则：
- **基础任务**：使用简单名称（如 `"summary"`）
- **分段任务**：添加 `_segment` 后缀（如 `"calibrate_segment"`）
- **分块任务**：添加 `_chunk` 后缀（如 `"calibrate_chunk"`）
- **最终任务**：添加 `final_` 前缀（如 `"final_summary"`）

### 4.2 总结功能使用的 task_type

| 场景 | task_type | 说明 |
|------|-----------|------|
| **基础总结** | `"summary"` | 新架构默认使用此值 |
| **分段总结** | `"segment_summary"` | 未来如果实现分段总结 |
| **最终总结** | `"final_summary"` | 未来如果实现多级总结 |

**当前实现**：只需要使用 `"summary"`。

---

## 五、实施 Checklist

### 5.1 已完成（无需修改）

- [x] `LLMClient.call()` - 已支持 `task_type` 参数
- [x] `LLMClient._actual_call()` - 已传递 `task_type` 参数
- [x] `PlainTextProcessor` - 已添加 `task_type="calibrate_segment"`
- [x] `SpeakerAwareProcessor` - 已添加 `task_type="calibrate_chunk"`
- [x] `KeyInfoExtractor` - 已添加 `task_type="key_info"`
- [x] `SpeakerInferencer` - 已添加 `task_type="speaker_inference"`
- [x] `QualityValidator` - 已添加 `task_type="quality_validation"`

### 5.2 需要添加（仅 SummaryProcessor）

- [ ] `SummaryProcessor.process()` - 需要添加 `task_type="summary"`

---

## 六、实施细节

### 6.1 SummaryProcessor 修改

#### 位置

`src/video_transcript_api/utils/llm/processors/summary_processor.py`

#### 修改内容

在 `process()` 方法中，调用 `self.llm_client.call()` 时添加 `task_type="summary"` 参数：

```python
def process(
    self,
    text: str,
    title: str,
    author: str = "",
    description: str = "",
    speaker_count: int = 0,
    transcription_data: Optional[Dict] = None,
    selected_models: Optional[Dict] = None,
) -> Optional[str]:
    """生成文本总结"""

    # 步骤 1-4: 长度检查、选择模型、选择 Prompt、构建 User Prompt
    # ... (保持不变)

    # 步骤 5: 调用 LLM
    response = self.llm_client.call(
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        reasoning_effort=reasoning_effort,
        task_type="summary",  # ← 添加这一行
    )

    # 步骤 6: 验证结果
    # ... (保持不变)
```

#### 完整修改示例

```python
# 步骤 5: 调用 LLM
logger.debug(f"Calling LLM for summary generation (task_type=summary)")

response = self.llm_client.call(
    model=model,
    system_prompt=system_prompt,
    user_prompt=user_prompt,
    reasoning_effort=reasoning_effort,
    task_type="summary",  # ← 新增：标识为总结任务
)

summary_text = response.text
```

### 6.2 验证方法

实施后，可以通过以下方式验证：

1. **日志检查**：
   ```
   grep "task_type=summary" logs/*.log
   ```
   应该能看到总结相关的日志。

2. **代码审查**：
   检查 `SummaryProcessor.process()` 方法中是否正确传递了 `task_type`。

3. **集成测试**：
   运行总结功能的测试，确保没有报错。

---

## 七、最佳实践

### 7.1 task_type 命名建议

1. **使用小写和下划线**：如 `"calibrate_segment"` 而不是 `"CalibrateSegment"`
2. **描述性强**：让人一眼看出任务类型
3. **保持一致**：相同功能使用相同的 `task_type`
4. **避免过于具体**：不要包含临时变量（如任务 ID、时间戳等）

**好的例子**：
- ✅ `"summary"`
- ✅ `"calibrate_segment"`
- ✅ `"speaker_inference"`

**不好的例子**：
- ❌ `"summary_20260127"`（包含时间）
- ❌ `"task_12345"`（包含临时 ID）
- ❌ `"llm_call"`（过于宽泛）

### 7.2 未来扩展

如果未来需要添加新的任务类型：

1. **检查是否已存在**：查看上表，避免重复定义
2. **遵循命名规则**：保持与现有命名风格一致
3. **更新文档**：在本文档中添加新的 `task_type`
4. **全局搜索**：确保所有调用点都正确使用

---

## 八、参考

### 8.1 call_llm_api() 签名

```python
def call_llm_api(
    model: str,
    prompt: str,
    api_key: str,
    base_url: str,
    max_retries: int = 2,
    retry_delay: int = 5,
    reasoning_effort: Optional[str] = None,
    task_type: str = "unknown",  # ← 默认值为 "unknown"
    *,
    response_schema: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    system_prompt: str = "You are a helpful assistant.",
) -> Union[str, StructuredResult]:
```

### 8.2 LLMClient.call() 签名

```python
def call(
    self,
    model: str,
    system_prompt: str,
    user_prompt: str,
    response_schema: Optional[Dict] = None,
    reasoning_effort: Optional[str] = None,
    task_type: str = "unknown",  # ← 默认值为 "unknown"
) -> LLMResponse:
```

---

## 九、总结

### 9.1 当前状态

| 组件 | 状态 | task_type | 备注 |
|------|------|-----------|------|
| `LLMClient` | ✅ 已支持 | - | 完整支持参数传递 |
| `PlainTextProcessor` | ✅ 已添加 | `"calibrate_segment"` | 无需修改 |
| `SpeakerAwareProcessor` | ✅ 已添加 | `"calibrate_chunk"` | 无需修改 |
| `KeyInfoExtractor` | ✅ 已添加 | `"key_info"` | 无需修改 |
| `SpeakerInferencer` | ✅ 已添加 | `"speaker_inference"` | 无需修改 |
| `QualityValidator` | ✅ 已添加 | `"quality_validation"` | 无需修改 |
| `SummaryProcessor` | ❌ 待添加 | `"summary"` | **需要修改** |

### 9.2 需要的改动

**唯一需要修改的位置**：`SummaryProcessor.process()` 方法

**修改内容**：在调用 `self.llm_client.call()` 时添加一行：
```python
task_type="summary",
```

**预计时间**：< 1 分钟

### 9.3 优先级

- **优先级**：中（不影响功能，但影响日志和监控）
- **建议**：在实现 `SummaryProcessor` 时一并添加
- **风险**：低（只是添加一个参数，不会破坏现有功能）

---

## 十、快速参考

### 10.1 所有 task_type 值

```python
# 校对相关
"calibrate"          # 基础校对
"calibrate_segment"  # 分段校对（纯文本）
"calibrate_chunk"    # 分块校对（有说话人）

# 总结相关
"summary"            # 基础总结 ← 新架构使用
"segment_summary"    # 分段总结（未来可能使用）
"final_summary"      # 最终总结（未来可能使用）

# 辅助功能
"key_info"           # 关键信息提取
"speaker_inference"  # 说话人推断
"speaker_mapping"    # 说话人映射
"quality_validation" # 质量验证
```

### 10.2 代码模板

```python
# 在任何需要调用 LLM 的地方，使用此模板：

response = self.llm_client.call(
    model=model,
    system_prompt=system_prompt,
    user_prompt=user_prompt,
    response_schema=response_schema,  # 可选
    reasoning_effort=reasoning_effort,  # 可选
    task_type="your_task_type_here",  # ← 必须添加
)
```

---

**完成标志**：当所有 `LLMClient.call()` 调用都传递了有意义的 `task_type` 参数时，此文档的目标即达成。
