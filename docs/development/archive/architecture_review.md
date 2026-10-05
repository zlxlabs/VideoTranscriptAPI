# LLM 校对流程架构评审报告

> 评审时间: 2026-01-27
> 评审范围: `src/video_transcript_api/utils/llm/` 模块
> 评审角度: 工程最佳实践、代码可维护性、性能优化

---

## 一、当前架构概览

### 1.1 模块组成

```
utils/llm/
├── llm_enhanced.py          (2100+ 行) - 主处理器，场景路由
├── structured_calibrator.py  (500+ 行) - 结构化校对处理
├── llm_segmented.py          (600+ 行) - 分段处理
├── text_segmentation.py      (300+ 行) - 文本分段逻辑
├── speaker_mapping.py        (200+ 行) - 说话人映射推断
├── llm.py                    (800+ 行) - LLM API 调用封装
├── prompts.py                (400+ 行) - 提示词模板
└── schemas/                  - JSON Schema 定义
    ├── calibration.py
    ├── validation.py
    └── speaker_mapping.py

总计: ~5700 行代码
```

### 1.2 处理流程图

```
EnhancedLLMProcessor.process_llm_task()
    │
    ├─ _select_models()  # 风险检测 + 模型选择
    │
    └─ 场景路由（4个分支）:
        │
        ├─ _process_with_structured_output()     # 场景1: 完整说话人数据
        │   └─ process_llm_task_with_structure()
        │       ├─ 说话人推断
        │       ├─ StructuredCalibrator.calibrate_structured_dialogs()
        │       └─ 总结生成
        │
        ├─ _process_json_segmented()             # 场景2: JSON 分段
        │   ├─ extract_speaker_mapping_from_json()
        │   ├─ SegmentedLLMProcessor.calibrate_text_segmented()
        │   └─ 总结生成（并发）
        │
        ├─ _process_txt_segmented()              # 场景3: TXT 分段
        │   ├─ SegmentedLLMProcessor.calibrate_text_segmented()
        │   └─ 总结生成（并发）
        │
        └─ _process_original_logic()             # 场景4: 短文本
            ├─ 校对（并发）
            └─ 总结（并发）
```

---

## 二、设计优点

### 2.1 ✅ 职责分离清晰

**优点**:
- `EnhancedLLMProcessor`: 场景判断和路由
- `StructuredCalibrator`: 专注结构化处理
- `SegmentedLLMProcessor`: 专注分段逻辑
- `TextSegmentationProcessor`: 文本分段策略

**评价**: 符合单一职责原则，易于测试和维护。

### 2.2 ✅ KV Cache 优化设计

**优点**:
- System Prompt 静态内容（可缓存）
- User Prompt 动态内容（末尾追加）
- 最大化 LLM 提供商的 KV Cache 命中率

**评价**: 对成本和延迟优化非常有效。

### 2.3 ✅ 并发处理策略

**优点**:
- 校对和总结可并发执行
- 分段校对支持多线程（默认 10 workers）
- 结构化处理支持分块并发（默认 3 workers）

**评价**: 充分利用 I/O 并发特性，缩短整体处理时间。

### 2.4 ✅ 质量保证机制

**优点**:
- 长度检查（min_calibrate_ratio >= 0.80）
- 结构化校对质量验证（overall_score >= 8.0）
- 失败降级策略（回退到原文）

**评价**: 保证输出质量的底线。

### 2.5 ✅ 风险控制集成

**优点**:
- 元数据风险检测（标题、作者、描述）
- 自动切换高安全性模型
- 统一的风险检测结果共享

**评价**: 符合合规要求，减少 API 风险。

---

## 三、设计缺陷

### 3.1 ❌ 代码重复严重

**问题**:

四个处理方法中存在大量重复逻辑：

| 重复内容 | 出现位置 | 重复次数 |
|---------|---------|---------|
| 总结生成逻辑 | `_process_with_structured_output`, `_process_json_segmented`, `_process_txt_segmented`, `_process_original_logic` | 4次 |
| 并发调用模式 | `_process_txt_segmented`, `_process_json_segmented`, `_process_original_logic` | 3次 |
| 长度检查 | `_process_txt_segmented`, `_process_json_segmented`, `_process_original_logic` | 3次 |
| 异常处理 | 所有方法 | 4次 |
| 结果字典构建 | 所有方法 | 4次 |

**代码示例**（重复的总结生成逻辑）:

```python
# _process_txt_segmented() 中 (第 593-607 行)
def run_summary():
    nonlocal summary_result, summary_error
    try:
        speaker_count = self._detect_speaker_count(...)
        summary_result = self._get_or_generate_summary(...)
    except Exception as exc:
        summary_error = exc

# _process_json_segmented() 中 (第 717-731 行)
def run_summary():
    nonlocal summary_result, summary_error
    try:
        speaker_count = self._detect_speaker_count(...)
        summary_result = self._get_or_generate_summary(...)
    except Exception as exc:
        summary_error = exc

# _process_original_logic() 中 (第 911-925 行)
def run_summary():
    nonlocal summary_result, summary_error
    try:
        speaker_count = self._detect_speaker_count(...)
        summary_result = self._get_or_generate_summary(...)
    except Exception as exc:
        summary_error = exc
```

**影响**:
- 代码维护成本高（修改需要同步 4 个位置）
- 容易引入不一致的 bug
- 违反 DRY 原则

**严重性**: ⚠️ 高

---

### 3.2 ❌ 场景判断逻辑复杂

**问题**:

`process_llm_task()` 方法中的场景判断嵌套过深：

```python
def process_llm_task(self, llm_task):
    # 360 行: 场景1
    if use_speaker_recognition and transcription_data and platform and media_id:
        result = self._process_with_structured_output(...)

    # 373 行: 场景2
    elif use_speaker_recognition and transcription_data:
        if need_segmentation:
            result = self._process_json_segmented(...)
        else:
            result = self._process_original_logic(...)

    # 397 行: 场景3
    else:
        if need_segmentation:
            result = self._process_txt_segmented(...)
        else:
            result = self._process_original_logic(...)
```

**问题点**:
1. 条件判断复杂（最多 4 个条件组合）
2. `_process_original_logic()` 在两个分支都被调用（重复）
3. 新增场景需要修改多个条件
4. 难以理解各场景的优先级

**改进建议**: 使用策略模式或责任链模式

**严重性**: ⚠️ 中

---

### 3.3 ❌ 配置初始化冗余

**问题**:

`EnhancedLLMProcessor`, `StructuredCalibrator`, `SegmentedLLMProcessor` 三个类都初始化相同的配置项：

```python
# EnhancedLLMProcessor.__init__() (第 60-71 行)
self.api_key = self.llm_config["api_key"]
self.base_url = self.llm_config["base_url"]
self.calibrate_model = self.llm_config["calibrate_model"]
self.summary_model = self.llm_config["summary_model"]
self.max_retries = self.llm_config["max_retries"]
self.retry_delay = self.llm_config["retry_delay"]

# StructuredCalibrator.__init__() (第 44-57 行)
self.api_key = self.llm_config['api_key']
self.base_url = self.llm_config['base_url']
self.calibrate_model = self.llm_config['calibrate_model']
self.max_retries = self.llm_config['max_retries']
self.retry_delay = self.llm_config['retry_delay']

# SegmentedLLMProcessor.__init__() (第 50-59 行)
self.api_key = self.llm_config['api_key']
self.base_url = self.llm_config['base_url']
self.calibrate_model = self.llm_config['calibrate_model']
self.summary_model = self.llm_config['summary_model']
self.max_retries = self.llm_config['max_retries']
self.retry_delay = self.llm_config['retry_delay']
```

**影响**:
- 违反 DRY 原则
- 配置修改需要同步多处
- 内存占用冗余

**改进建议**: 引入 `LLMConfig` 配置类，统一管理

**严重性**: ⚠️ 中

---

### 3.4 ❌ 异常处理不一致

**问题**:

不同方法的异常处理策略不统一：

| 方法 | 异常处理策略 | 返回值 |
|------|------------|--------|
| `_process_txt_segmented()` | 捕获异常，设置 `calibrated_text = "【LLM call failed】..."` | 包含错误信息的字典 |
| `_process_json_segmented()` | 捕获异常，设置 `calibrated_text = "【LLM call failed】..."` | 包含错误信息的字典 |
| `_process_original_logic()` | 捕获异常，设置 `calibrated_text = "【LLM call failed】..."` | 包含错误信息的字典 |
| `StructuredCalibrator.calibrate_structured_dialogs()` | 抛出异常，由外层捕获 | 无 |
| `call_llm_api()` | 抛出 `LLMCallError` | 无 |

**问题点**:
1. 有些方法吞掉异常，有些抛出异常
2. 错误信息格式不统一（`【LLM call failed】` vs 抛出异常）
3. 调用方无法区分"处理失败"和"部分成功"

**改进建议**: 统一异常处理策略，引入自定义异常类型

**严重性**: ⚠️ 中

---

### 3.5 ❌ 临时文件管理混乱

**问题**:

`_process_txt_segmented()` 和 `_process_json_segmented()` 创建临时文件，但管理方式不一致：

```python
# _process_txt_segmented() (第 540-545 行)
temp_file_path = os.path.join(cache_dir, "temp_transcript.txt")
with open(temp_file_path, "w", encoding="utf-8") as f:
    f.write(transcript)
# 没有显式删除临时文件

# _process_json_segmented() (第 674-679 行)
temp_file_path = os.path.join(cache_dir, "temp_transcription.json")
with open(temp_file_path, "w", encoding="utf-8") as f:
    json.dump(transcription_data, f, ensure_ascii=False, indent=2)
# 没有显式删除临时文件
```

**问题点**:
1. 临时文件可能残留（未清理）
2. 没有使用 `tempfile` 模块（不跨平台）
3. 异常发生时文件可能未关闭

**改进建议**: 使用 `tempfile.NamedTemporaryFile` 或确保异常时也清理

**严重性**: ⚠️ 低

---

### 3.6 ❌ 方法职责过重

**问题**:

`EnhancedLLMProcessor` 类过于庞大（2100+ 行），包含过多职责：

| 职责类别 | 方法数量 | 示例方法 |
|---------|---------|---------|
| 场景路由 | 1 | `process_llm_task()` |
| 具体处理 | 4 | `_process_with_structured_output()`, `_process_txt_segmented()`, ... |
| 说话人推断 | 3 | `_infer_speakers_from_funasr()`, `_process_speaker_mapping_result()`, ... |
| 总结生成 | 2 | `_get_or_generate_summary()`, `_generate_summary()` |
| 格式化输出 | 3 | `_format_transcript_for_display()`, `_build_json_summary_text()`, ... |
| 工具方法 | 10+ | `_ensure_min_length()`, `_detect_speaker_count()`, ... |

**违反原则**:
- 单一职责原则（SRP）
- 开闭原则（OCP）

**影响**:
- 类难以理解和测试
- 修改某个功能可能影响其他功能
- 难以扩展新的处理场景

**严重性**: ⚠️ 高

---

### 3.7 ❌ 并发原语不统一

**问题**:

同时使用 `threading.Thread` 和 `ThreadPoolExecutor`：

```python
# _process_original_logic() 使用 threading.Thread (第 937-949 行)
t1 = threading.Thread(target=run_calibrate)
t1.start()
if original_length >= min_summary_threshold:
    t2 = threading.Thread(target=run_summary)
    t2.start()
    t1.join()
    t2.join()

# SegmentedLLMProcessor 使用 ThreadPoolExecutor (第 210-218 行)
with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
    futures = [executor.submit(calibrate_segment, i, segment)
               for i, segment in enumerate(segments)]
    for future in concurrent.futures.as_completed(futures):
        future.result()

# StructuredCalibrator 使用 ThreadPoolExecutor (第 314-320 行)
with ThreadPoolExecutor(max_workers=max_workers) as executor:
    futures = [executor.submit(process_single_chunk, i, chunk)
               for i, chunk in enumerate(chunks)]
```

**问题点**:
1. `threading.Thread` 更底层，需要手动管理生命周期
2. `ThreadPoolExecutor` 更高级，自动管理线程池
3. 混用增加理解成本

**改进建议**: 统一使用 `ThreadPoolExecutor`

**严重性**: ⚠️ 低

---

## 四、性能问题

### 4.1 ⚠️ 说话人推断的冗余调用

**问题**:

在 `_process_with_structured_output()` 中，说话人推断逻辑被调用两次：

```python
# 第 1 次：在 process_llm_task_with_structure() 中 (第 1479-1505 行)
speaker_mapping_result = self._infer_speakers_from_funasr(
    speakers, video_metadata, selected_calibrate_model, selected_calibrate_effort
)

# 第 2 次：在 SegmentedLLMProcessor.extract_speaker_mapping_from_json() 中
# (llm_segmented.py 第 397-428 行)
# 如果走 JSON 分段流程，会重复推断
```

**影响**:
- 额外的 LLM API 调用成本
- 延长处理时间

**改进建议**: 缓存说话人推断结果，避免重复调用

**严重性**: ⚠️ 中

---

### 4.2 ⚠️ 分段策略可能次优

**问题**:

当前分段策略是固定长度切分（segment_size=2000）：

```python
# text_segmentation.py (第 55-87 行)
def segment_text(self, text: str, is_capswriter_format: bool = False) -> List[str]:
    # 固定按 segment_size 分段
    segments.append(current_segment)
```

**问题点**:
1. 不考虑语义边界（可能在句子中间切断）
2. 不考虑说话人切换点（多说话人场景）
3. 固定长度可能不适合所有模型（不同模型上下文窗口不同）

**改进建议**: 引入智能分段策略（基于语义、说话人边界）

**严重性**: ⚠️ 中

---

### 4.3 ⚠️ 缓存未充分利用

**问题**:

当前缓存策略：
- 缓存整个任务的最终结果（cache.db + 文件系统）
- **未缓存中间结果**（如说话人推断、分段校对）

**潜在改进**:
1. 缓存说话人推断结果（按 platform + media_id）
2. 缓存单个分段的校对结果（按 segment hash）
3. 支持部分结果复用

**收益**:
- 重新处理同一视频时可复用中间结果
- 处理失败时可从断点继续

**严重性**: ⚠️ 低（优化方向）

---

## 五、可维护性问题

### 5.1 ❌ 测试覆盖率不足

**问题**:

从 `tests/` 目录结构来看：

```
tests/
├── llm/                    # LLM 功能测试
│   ├── test_llm_enhanced.py
│   ├── test_structured_calibrator.py
│   └── test_segmented_llm.py
├── integration/            # 集成测试
└── unit/                   # 单元测试
```

**缺失的测试**:
1. 场景路由逻辑的单元测试（4 种场景覆盖）
2. 异常情况的边界测试（API 失败、部分失败）
3. 并发安全性测试
4. 性能基准测试

**改进建议**: 补充测试用例，目标覆盖率 >= 80%

**严重性**: ⚠️ 中

---

### 5.2 ❌ 日志信息不完整

**问题**:

关键信息缺失：

```python
# 缺少说话人推断的日志
speaker_mapping_result = self._infer_speakers_from_funasr(...)
# 没有记录推断结果的详细信息（映射了多少个说话人、置信度等）

# 缺少质量验证的日志
validation_result = self._validate_calibration(...)
# 没有记录验证评分的详细信息
```

**影响**:
- 问题排查困难
- 无法追踪处理质量

**改进建议**: 添加结构化日志（包含关键指标）

**严重性**: ⚠️ 低

---

## 六、优化建议

### 6.1 优先级 1：消除代码重复

**目标**: 将代码重复率从 ~40% 降低到 < 10%

**方案 1: 提取公共处理方法**

```python
class EnhancedLLMProcessor:
    def _execute_calibration_and_summary(
        self,
        calibrate_func: Callable[[], str],
        llm_task: Dict[str, Any],
        selected_summary_model: str,
        selected_summary_effort: str,
    ) -> Dict[str, Any]:
        """
        统一的校对和总结执行逻辑

        Args:
            calibrate_func: 校对函数（返回校对后的文本）
            llm_task: 任务信息
            selected_summary_model: 总结模型
            selected_summary_effort: 总结 reasoning_effort

        Returns:
            包含校对文本和总结的字典
        """
        # 统一的并发执行逻辑
        calibrated_text = None
        summary_result = None
        calibrate_error = None
        summary_error = None

        def run_calibrate():
            nonlocal calibrated_text, calibrate_error
            try:
                calibrated_text = calibrate_func()
            except Exception as exc:
                calibrate_error = exc

        def run_summary():
            nonlocal summary_result, summary_error
            try:
                speaker_count = self._detect_speaker_count(...)
                summary_result = self._get_or_generate_summary(...)
            except Exception as exc:
                summary_error = exc

        # 并发执行
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(run_calibrate),
                executor.submit(run_summary)
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        # 统一的错误处理和结果构建
        if calibrate_error:
            calibrated_text = f"【LLM call failed】{calibrate_error}"

        if summary_error:
            logger.warning(f"总结生成失败: {summary_error}")

        # 统一的长度检查
        calibrated_text = self._ensure_min_length(
            llm_task["transcript"], calibrated_text, llm_task["task_id"]
        )

        return {
            "校对文本": calibrated_text,
            "内容总结": summary_result or "",
            "stats": self._build_stats(llm_task["transcript"], calibrated_text, summary_result)
        }
```

**使用方式**:

```python
def _process_txt_segmented(self, llm_task, ...):
    # 定义校对逻辑
    def calibrate():
        return self.segmented_llm_processor.calibrate_text_segmented(...)

    # 复用统一的执行逻辑
    return self._execute_calibration_and_summary(
        calibrate_func=calibrate,
        llm_task=llm_task,
        selected_summary_model=selected_summary_model,
        selected_summary_effort=selected_summary_effort
    )

def _process_json_segmented(self, llm_task, ...):
    # 定义校对逻辑
    def calibrate():
        # 提取说话人映射
        speaker_mapping = seg_processor.extract_speaker_mapping_from_json(...)
        # 分段校对
        return self.segmented_llm_processor.calibrate_text_segmented(...)

    # 复用统一的执行逻辑
    return self._execute_calibration_and_summary(
        calibrate_func=calibrate,
        llm_task=llm_task,
        selected_summary_model=selected_summary_model,
        selected_summary_effort=selected_summary_effort
    )
```

**收益**:
- 减少 ~500 行重复代码
- 修改逻辑只需在一处修改
- 减少 bug 风险

---

### 6.2 优先级 2：重构场景路由

**目标**: 降低场景判断的复杂度，提高可扩展性

**方案 2: 策略模式 + 责任链**

```python
from abc import ABC, abstractmethod
from typing import Optional

class CalibrationStrategy(ABC):
    """校对策略基类"""

    @abstractmethod
    def can_handle(self, llm_task: Dict[str, Any]) -> bool:
        """判断是否可以处理该任务"""
        pass

    @abstractmethod
    def process(
        self,
        llm_task: Dict[str, Any],
        selected_models: Dict[str, str]
    ) -> Dict[str, Any]:
        """执行处理"""
        pass

    @property
    @abstractmethod
    def priority(self) -> int:
        """优先级（数字越小优先级越高）"""
        pass


class StructuredCalibrationStrategy(CalibrationStrategy):
    """结构化校对策略（最高优先级）"""

    def can_handle(self, llm_task: Dict[str, Any]) -> bool:
        return (
            llm_task.get("use_speaker_recognition", False)
            and llm_task.get("transcription_data") is not None
            and llm_task.get("platform")
            and llm_task.get("media_id")
        )

    def process(self, llm_task, selected_models):
        return self._processor._process_with_structured_output(
            llm_task, **selected_models
        )

    @property
    def priority(self) -> int:
        return 1


class JsonSegmentedStrategy(CalibrationStrategy):
    """JSON 分段策略"""

    def can_handle(self, llm_task: Dict[str, Any]) -> bool:
        if not (llm_task.get("use_speaker_recognition") and llm_task.get("transcription_data")):
            return False

        text_length = len(llm_task["transcript"])
        return text_length > self._processor.segmentation_processor.enable_threshold

    def process(self, llm_task, selected_models):
        return self._processor._process_json_segmented(
            llm_task, **selected_models
        )

    @property
    def priority(self) -> int:
        return 2


class TxtSegmentedStrategy(CalibrationStrategy):
    """TXT 分段策略"""

    def can_handle(self, llm_task: Dict[str, Any]) -> bool:
        text_length = len(llm_task["transcript"])
        return text_length > self._processor.segmentation_processor.enable_threshold

    def process(self, llm_task, selected_models):
        return self._processor._process_txt_segmented(
            llm_task, **selected_models
        )

    @property
    def priority(self) -> int:
        return 3


class DefaultCalibrationStrategy(CalibrationStrategy):
    """默认策略（最低优先级，兜底）"""

    def can_handle(self, llm_task: Dict[str, Any]) -> bool:
        return True  # 总是可以处理

    def process(self, llm_task, selected_models):
        return self._processor._process_original_logic(
            llm_task, **selected_models
        )

    @property
    def priority(self) -> int:
        return 999


class EnhancedLLMProcessor:
    def __init__(self, config: Dict[str, Any]):
        # ... 原有初始化 ...

        # 注册策略（按优先级排序）
        self.strategies = [
            StructuredCalibrationStrategy(self),
            JsonSegmentedStrategy(self),
            TxtSegmentedStrategy(self),
            DefaultCalibrationStrategy(self),
        ]
        self.strategies.sort(key=lambda s: s.priority)

    def process_llm_task(self, llm_task: Dict[str, Any]) -> Dict[str, Any]:
        """处理LLM任务（使用策略模式）"""
        task_id = llm_task["task_id"]
        logger.info(f"开始处理LLM任务: {task_id}")

        # 选择模型
        selected_models = self._select_models(
            task_id, llm_task["video_title"],
            llm_task["author"], llm_task.get("description", "")
        )

        # 选择策略
        for strategy in self.strategies:
            if strategy.can_handle(llm_task):
                logger.info(f"使用策略: {strategy.__class__.__name__}")
                result = strategy.process(llm_task, selected_models)
                result["models_used"] = selected_models
                return result

        # 理论上不会到这里（DefaultStrategy 总是 can_handle）
        raise RuntimeError("No strategy can handle this task")
```

**收益**:
- 场景判断逻辑清晰（每个策略独立）
- 易于扩展新场景（添加新策略类）
- 优先级显式声明
- 符合开闭原则

---

### 6.3 优先级 3：统一配置管理

**目标**: 消除配置初始化冗余

**方案 3: 配置类 + 依赖注入**

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class LLMConfig:
    """LLM 配置类"""
    # API 配置
    api_key: str
    base_url: str

    # 模型配置
    calibrate_model: str
    calibrate_reasoning_effort: Optional[str]
    summary_model: str
    summary_reasoning_effort: Optional[str]

    # 风险模型配置
    risk_calibrate_model: Optional[str] = None
    risk_calibrate_reasoning_effort: Optional[str] = None
    risk_summary_model: Optional[str] = None
    risk_summary_reasoning_effort: Optional[str] = None

    # 重试配置
    max_retries: int = 3
    retry_delay: int = 5

    # 质量配置
    min_calibrate_ratio: float = 0.80
    min_summary_threshold: int = 500

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "LLMConfig":
        """从配置字典创建配置对象"""
        llm_config = config_dict.get("llm", {})

        return cls(
            api_key=llm_config["api_key"],
            base_url=llm_config["base_url"],
            calibrate_model=llm_config["calibrate_model"],
            calibrate_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("calibrate_reasoning_effort")
            ),
            summary_model=llm_config["summary_model"],
            summary_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("summary_reasoning_effort")
            ),
            risk_calibrate_model=llm_config.get("risk_calibrate_model"),
            risk_calibrate_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("risk_calibrate_reasoning_effort")
            ),
            risk_summary_model=llm_config.get("risk_summary_model"),
            risk_summary_reasoning_effort=normalize_reasoning_effort(
                llm_config.get("risk_summary_reasoning_effort")
            ),
            max_retries=llm_config.get("max_retries", 3),
            retry_delay=llm_config.get("retry_delay", 5),
            min_calibrate_ratio=llm_config.get("min_calibrate_ratio", 0.80),
            min_summary_threshold=llm_config.get("min_summary_threshold", 500),
        )


class EnhancedLLMProcessor:
    def __init__(self, config: Dict[str, Any]):
        # 统一配置管理
        self.llm_config = LLMConfig.from_dict(config)

        # 初始化子处理器（注入配置）
        self.segmented_llm_processor = SegmentedLLMProcessor(self.llm_config)
        self.structured_calibrator = StructuredCalibrator(self.llm_config)


class StructuredCalibrator:
    def __init__(self, llm_config: LLMConfig):
        self.config = llm_config
        # 直接使用配置对象，无需重复初始化


class SegmentedLLMProcessor:
    def __init__(self, llm_config: LLMConfig):
        self.config = llm_config
        # 直接使用配置对象，无需重复初始化
```

**收益**:
- 配置验证集中在一处
- 类型提示提供 IDE 自动补全
- 减少重复代码
- 易于序列化和测试

---

### 6.4 优先级 4：统一异常处理

**目标**: 统一异常处理策略，提高错误可追溯性

**方案 4: 自定义异常类型 + 统一处理器**

```python
class CalibrationError(Exception):
    """校对错误基类"""
    pass

class CalibrationAPIError(CalibrationError):
    """LLM API 调用失败"""
    def __init__(self, message: str, model: str, original_error: Exception):
        super().__init__(message)
        self.model = model
        self.original_error = original_error

class CalibrationValidationError(CalibrationError):
    """校对结果验证失败"""
    def __init__(self, message: str, score: float, threshold: float):
        super().__init__(message)
        self.score = score
        self.threshold = threshold

class CalibrationLengthError(CalibrationError):
    """校对结果长度不足"""
    def __init__(self, message: str, actual_ratio: float, min_ratio: float):
        super().__init__(message)
        self.actual_ratio = actual_ratio
        self.min_ratio = min_ratio


class ErrorHandler:
    """统一错误处理器"""

    @staticmethod
    def handle_calibration_error(
        error: Exception,
        task_id: str,
        fallback_text: str,
        context: str = ""
    ) -> str:
        """
        统一处理校对错误

        Args:
            error: 异常对象
            task_id: 任务 ID
            fallback_text: 降级文本（原文）
            context: 上下文信息

        Returns:
            处理后的文本（可能是错误信息或降级文本）
        """
        if isinstance(error, CalibrationAPIError):
            logger.error(
                f"LLM API 调用失败 [{context}]: {task_id}, "
                f"模型: {error.model}, 错误: {error.original_error}"
            )
            return f"【LLM API 调用失败】模型: {error.model}, 错误: {error.original_error}"

        elif isinstance(error, CalibrationValidationError):
            logger.warning(
                f"校对结果验证失败 [{context}]: {task_id}, "
                f"评分: {error.score}, 阈值: {error.threshold}, 使用原文"
            )
            return fallback_text

        elif isinstance(error, CalibrationLengthError):
            logger.warning(
                f"校对结果长度不足 [{context}]: {task_id}, "
                f"比例: {error.actual_ratio*100:.2f}%, "
                f"最低: {error.min_ratio*100:.2f}%, 使用原文"
            )
            return fallback_text

        else:
            logger.error(
                f"未知错误 [{context}]: {task_id}, {type(error).__name__}: {error}"
            )
            return f"【处理失败】{type(error).__name__}: {error}"


# 使用示例
def _process_txt_segmented(self, llm_task, ...):
    try:
        calibrated_text = self.segmented_llm_processor.calibrate_text_segmented(...)
    except Exception as exc:
        calibrated_text = ErrorHandler.handle_calibration_error(
            error=exc,
            task_id=llm_task["task_id"],
            fallback_text=llm_task["transcript"],
            context="txt_segmented"
        )
```

**收益**:
- 错误处理逻辑统一
- 错误信息结构化（便于监控和告警）
- 降级策略清晰
- 易于测试

---

## 七、合并优化方案

### 7.1 建议的模块合并

#### 方案 A：激进合并（不推荐）

将 `llm_enhanced.py`, `llm_segmented.py`, `structured_calibrator.py` 合并为一个文件。

**优点**:
- 减少文件数量

**缺点**:
- 单个文件过大（可能 3000+ 行）
- 违反单一职责原则
- 难以测试和维护

**结论**: ❌ 不推荐

---

#### 方案 B：渐进合并（推荐）

**第一步：合并配置管理**

```
utils/llm/
├── config.py              # 新增：统一配置管理
│   └── LLMConfig
├── llm_enhanced.py        # 修改：使用 LLMConfig
├── structured_calibrator.py  # 修改：使用 LLMConfig
├── llm_segmented.py       # 修改：使用 LLMConfig
└── ...
```

**第二步：提取公共逻辑**

```
utils/llm/
├── config.py
├── common.py              # 新增：公共处理逻辑
│   ├── ErrorHandler
│   ├── ConcurrentExecutor
│   └── ResultBuilder
├── llm_enhanced.py        # 修改：使用 common 模块
├── structured_calibrator.py
├── llm_segmented.py
└── ...
```

**第三步：重构场景路由**

```
utils/llm/
├── config.py
├── common.py
├── strategies/            # 新增：策略模式实现
│   ├── __init__.py
│   ├── base.py           # CalibrationStrategy 基类
│   ├── structured.py     # StructuredCalibrationStrategy
│   ├── json_segmented.py # JsonSegmentedStrategy
│   ├── txt_segmented.py  # TxtSegmentedStrategy
│   └── default.py        # DefaultCalibrationStrategy
├── llm_enhanced.py        # 修改：使用策略模式
├── structured_calibrator.py
├── llm_segmented.py
└── ...
```

**第四步：拆分大类**

```
utils/llm/
├── config.py
├── common.py
├── strategies/
├── processors/            # 重构：拆分处理逻辑
│   ├── __init__.py
│   ├── coordinator.py    # EnhancedLLMProcessor（仅负责协调）
│   ├── structured.py     # StructuredCalibrator
│   ├── segmented.py      # SegmentedLLMProcessor
│   └── speaker.py        # 说话人推断逻辑
└── ...
```

**收益**:
- 逐步重构，风险可控
- 每步都可单独测试
- 保持模块边界清晰

---

### 7.2 重构路线图

| 阶段 | 目标 | 预估工作量 | 风险 |
|-----|------|----------|------|
| **阶段 1** | 提取公共处理方法（消除重复代码） | 3-5 天 | 低 |
| **阶段 2** | 统一配置管理（引入 LLMConfig） | 2-3 天 | 低 |
| **阶段 3** | 统一异常处理（ErrorHandler） | 2-3 天 | 低 |
| **阶段 4** | 重构场景路由（策略模式） | 5-7 天 | 中 |
| **阶段 5** | 拆分大类（processors 目录） | 3-5 天 | 中 |
| **阶段 6** | 性能优化（缓存、分段策略） | 5-7 天 | 低 |

**总计**: 20-30 天

**建议**: 优先执行阶段 1-3（高收益、低风险）

---

## 八、总结与建议

### 8.1 当前设计评分

| 维度 | 评分 (1-10) | 说明 |
|-----|-----------|------|
| **功能完整性** | 9 | 功能齐全，支持多种场景 |
| **代码复用性** | 4 | 大量重复代码，违反 DRY 原则 |
| **可扩展性** | 5 | 场景路由复杂，扩展困难 |
| **可维护性** | 5 | 类过大，职责过重 |
| **性能** | 7 | 并发处理良好，但有优化空间 |
| **测试性** | 6 | 有测试，但覆盖不全 |
| **文档化** | 7 | Docstring 较完整，但缺乏架构文档 |

**综合评分**: 6.1/10

---

### 8.2 核心问题

1. **代码重复严重**（重复率 ~40%）
2. **场景判断复杂**（条件嵌套深）
3. **配置初始化冗余**（3 个类重复相同初始化）
4. **异常处理不一致**（吞掉 vs 抛出）

---

### 8.3 优化建议优先级

| 优先级 | 优化项 | 收益 | 风险 | 建议执行 |
|-------|-------|------|------|---------|
| **P0** | 消除代码重复 | 高（减少维护成本） | 低 | ✅ 立即执行 |
| **P0** | 统一配置管理 | 中（减少错误） | 低 | ✅ 立即执行 |
| **P1** | 统一异常处理 | 中（提高可追溯性） | 低 | ✅ 近期执行 |
| **P1** | 重构场景路由 | 高（提高可扩展性） | 中 | ⚠️ 充分测试后执行 |
| **P2** | 性能优化（缓存） | 中（降低成本） | 低 | ⏳ 后续执行 |
| **P3** | 拆分大类 | 中（提高可维护性） | 中 | ⏳ 后续执行 |

---

### 8.4 合并建议

**不建议**：将多个文件合并为一个大文件

**建议**：
1. 提取公共逻辑到独立模块（`common.py`, `config.py`）
2. 使用策略模式解耦场景路由
3. 保持现有模块边界（`structured_calibrator`, `llm_segmented` 继续独立）

**原因**：
- 当前模块职责基本清晰，合并会破坏边界
- 独立模块易于测试和替换
- 未来可能需要支持插件化（如新增转录引擎）

---

### 8.5 行动计划

**短期（1-2 周）**:
1. 提取 `_execute_calibration_and_summary()` 公共方法
2. 引入 `LLMConfig` 配置类
3. 添加单元测试覆盖场景路由逻辑

**中期（1-2 个月）**:
1. 实现策略模式重构场景路由
2. 统一异常处理机制
3. 补充集成测试和性能基准测试

**长期（3-6 个月）**:
1. 性能优化（缓存中间结果）
2. 智能分段策略
3. 支持插件化架构

---

## 九、参考资料

- [SOLID 原则](https://en.wikipedia.org/wiki/SOLID)
- [设计模式：可复用面向对象软件的基础](https://book.douban.com/subject/1052241/)
- [重构：改善既有代码的设计](https://book.douban.com/subject/4262627/)
- [Python 并发编程实战](https://realpython.com/python-concurrency/)

---

**评审人**: Claude Sonnet 4.5
**评审日期**: 2026-01-27
**文档版本**: v1.0
