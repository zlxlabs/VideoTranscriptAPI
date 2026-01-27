# Coordinator 集成总结功能设计

## 一、改造目标

在 `LLMCoordinator.process()` 中集成总结功能，实现完整的"校对 + 总结"流程。

## 二、改造前后对比

### 2.1 改造前（当前）

```python
class LLMCoordinator:
    def process(self, content, title, ...):
        # 1. 路由到 PlainTextProcessor 或 SpeakerAwareProcessor
        if isinstance(content, str):
            return self.plain_text_processor.process(...)
        else:
            return self.speaker_aware_processor.process(...)

        # 返回格式：
        # {
        #     "calibrated_text": "...",
        #     "key_info": {...},
        #     "stats": {...}
        # }
```

### 2.2 改造后（目标）

```python
class LLMCoordinator:
    def __init__(self, ...):
        # 新增：总结处理器
        self.summary_processor = SummaryProcessor(
            llm_client=self.llm_client,
            config=self.config,
        )

    def process(self, content, title, ...):
        # 1. 路由到校对处理器
        calibration_result = self._route_to_processor(content, ...)

        # 2. 生成总结（基于校对后的文本）
        summary_text = self._generate_summary_if_needed(
            calibration_result, title, ...
        )

        # 3. 合并返回
        return {
            "calibrated_text": calibration_result["calibrated_text"],
            "summary_text": summary_text,  # ← 新增
            "key_info": calibration_result.get("key_info"),
            "stats": calibration_result.get("stats"),
            ...
        }
```

## 三、详细设计

### 3.1 新增初始化逻辑

```python
def __init__(self, config_dict: dict, cache_dir: str):
    """初始化协调器"""

    # 原有逻辑...
    self.config = LLMConfig.from_dict(config_dict)
    self.llm_client = LLMClient(...)
    self.plain_text_processor = PlainTextProcessor(...)
    self.speaker_aware_processor = SpeakerAwareProcessor(...)

    # 新增：总结处理器
    self.summary_processor = SummaryProcessor(
        llm_client=self.llm_client,
        config=self.config,
    )

    logger.info("LLM Coordinator initialized with summary support")
```

### 3.2 修改 process() 方法

```python
def process(
    self,
    content: Union[str, List[Dict]],
    title: str,
    author: str = "",
    description: str = "",
    platform: str = "",
    media_id: str = "",
    has_risk: bool = False,
) -> Dict:
    """处理文本（校对 + 总结）

    Returns:
        {
            "calibrated_text": str,        # 校对后的文本
            "summary_text": Optional[str], # 总结文本（可能为 None）
            "key_info": dict,              # 关键信息
            "stats": dict,                 # 统计信息
            "structured_data": dict,       # 结构化数据（仅有说话人）
        }
    """
    # 选择模型
    selected_models = self.config.select_models_for_task(has_risk)

    # ========== 步骤 1: 校对处理 ==========
    logger.info(f"Step 1: Calibration for '{title}'")

    calibration_result = self._route_to_processor(
        content=content,
        title=title,
        author=author,
        description=description,
        platform=platform,
        media_id=media_id,
        selected_models=selected_models,
    )

    # 提取校对文本和说话人信息
    calibrated_text = calibration_result.get("calibrated_text", "")
    speaker_count = self._extract_speaker_count(content, calibration_result)

    # ========== 步骤 2: 总结生成 ==========
    logger.info(f"Step 2: Summary generation for '{title}'")

    summary_text = self._generate_summary_if_needed(
        text=calibrated_text,
        title=title,
        author=author,
        description=description,
        speaker_count=speaker_count,
        selected_models=selected_models,
    )

    # ========== 步骤 3: 合并结果 ==========
    return {
        "calibrated_text": calibrated_text,
        "summary_text": summary_text,
        "key_info": calibration_result.get("key_info"),
        "stats": {
            **calibration_result.get("stats", {}),
            "summary_length": len(summary_text) if summary_text else 0,
        },
        "structured_data": calibration_result.get("structured_data"),
    }
```

### 3.3 新增辅助方法

```python
def _route_to_processor(
    self,
    content: Union[str, List[Dict]],
    title: str,
    author: str,
    description: str,
    platform: str,
    media_id: str,
    selected_models: Dict,
) -> Dict:
    """路由到对应的校对处理器"""

    if isinstance(content, str):
        logger.info("Routing to PlainTextProcessor")
        return self.plain_text_processor.process(
            text=content,
            title=title,
            author=author,
            description=description,
            platform=platform,
            media_id=media_id,
            selected_models=selected_models,
        )
    elif isinstance(content, list):
        logger.info("Routing to SpeakerAwareProcessor")
        return self.speaker_aware_processor.process(
            dialogs=content,
            title=title,
            author=author,
            description=description,
            platform=platform,
            media_id=media_id,
            selected_models=selected_models,
        )
    else:
        raise ValueError(f"Unsupported content type: {type(content)}")


def _extract_speaker_count(
    self,
    content: Union[str, List[Dict]],
    calibration_result: Dict,
) -> int:
    """提取说话人数量"""

    # 纯文本 → 0 或 1
    if isinstance(content, str):
        return 0

    # 有说话人 → 从结果中提取
    structured_data = calibration_result.get("structured_data", {})
    speaker_mapping = structured_data.get("speaker_mapping", {})
    return len(speaker_mapping)


def _generate_summary_if_needed(
    self,
    text: str,
    title: str,
    author: str,
    description: str,
    speaker_count: int,
    selected_models: Dict,
) -> Optional[str]:
    """生成总结（如果需要）"""

    # 检查长度阈值
    if len(text) < self.config.min_summary_threshold:
        logger.info(
            f"Text too short for summary: {len(text)} < {self.config.min_summary_threshold}"
        )
        return None

    # 调用总结处理器
    logger.info(f"Generating summary for text (length: {len(text)})")

    try:
        summary = self.summary_processor.process(
            text=text,
            title=title,
            author=author,
            description=description,
            speaker_count=speaker_count,
            selected_models=selected_models,
        )

        if summary:
            logger.info(f"Summary generated successfully (length: {len(summary)})")
        else:
            logger.warning("Summary generation returned None")

        return summary

    except Exception as e:
        logger.error(f"Summary generation failed: {e}")
        return None
```

## 四、返回格式变化

### 4.1 新增字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `summary_text` | `Optional[str]` | 总结文本（新增） |
| `stats.summary_length` | `int` | 总结长度（新增） |

### 4.2 完整返回示例

**无说话人文本**：
```python
{
    "calibrated_text": "特朗普最近表示...",
    "summary_text": "## 概述\n本视频讨论了...",  # ← 新增
    "key_info": {
        "names": ["特朗普", "委内瑞拉"],
        "places": ["美国", "委内瑞拉"],
        ...
    },
    "stats": {
        "original_length": 5000,
        "calibrated_length": 4800,
        "segment_count": 3,
        "summary_length": 800,  # ← 新增
    },
    "structured_data": None,
}
```

**有说话人文本**：
```python
{
    "calibrated_text": "[张三]: 你好...\n[李四]: 你好...",
    "summary_text": "## 概述\n本次对话中...",  # ← 新增
    "key_info": {...},
    "stats": {
        "original_length": 5000,
        "calibrated_length": 4800,
        "dialog_count": 50,
        "chunk_count": 5,
        "summary_length": 600,  # ← 新增
    },
    "structured_data": {
        "dialogs": [...],
        "speaker_mapping": {...},
    },
}
```

## 五、与 transcription.py 的集成

### 5.1 调用方式不变

```python
# transcription.py
coordinator_result = llm_coordinator.process(
    content=content,
    title=video_title,
    author=llm_task.get("author", ""),
    description=llm_task.get("description", ""),
    platform=platform or "",
    media_id=media_id or "",
    has_risk=False,
)
```

### 5.2 结果适配

```python
# 适配返回格式
calibrated_text = coordinator_result.get("calibrated_text", "")
summary_text = coordinator_result.get("summary_text")  # ← 新增

# 判断是否跳过总结
should_skip_summary = summary_text is None

result_dict = {
    "校对文本": calibrated_text,
    "内容总结": summary_text,  # ← 从新架构获取
    "skip_summary": should_skip_summary,
    "stats": coordinator_result.get("stats", {}),
    "models_used": {},
    "calibrate_success": True,
    "summary_success": summary_text is not None,  # ← 根据是否有总结判断
}
```

## 六、性能影响分析

### 6.1 时间开销

**旧架构（并行）**：
```
开始 ─┬─ 校对 (20s) ────┐
      └─ 总结 (15s) ──┐  │
                       └──┴─ 完成 (20s)
```

**新架构（串行）**：
```
开始 ── 校对 (20s) ── 总结 (15s) ── 完成 (35s)
```

**差异**：多 15 秒（约 75% 增加）

### 6.2 优化策略（可选）

如果性能是关键考虑，可以实现**混合策略**：

```python
def _generate_summary_if_needed(self, ...):
    # 判断是否并行
    use_parallel = len(text) > 5000  # 长文本并行

    if use_parallel:
        # 并行：基于原始文本生成总结（速度快）
        summary = self._generate_summary_parallel(original_text, ...)
    else:
        # 串行：基于校对文本生成总结（质量好）
        summary = self.summary_processor.process(text, ...)

    return summary
```

**但我建议先实现串行版本**，原因：
1. 简单清晰
2. 质量优先
3. 15 秒延迟可接受
4. 未来可以优化

## 七、测试计划

### 7.1 单元测试

```python
def test_coordinator_with_summary_short_text():
    """测试短文本跳过总结"""
    result = coordinator.process(
        content="这是一段很短的文本。",  # < 500 字
        title="测试",
    )
    assert result["summary_text"] is None


def test_coordinator_with_summary_long_text():
    """测试长文本生成总结"""
    result = coordinator.process(
        content="这是一段很长的文本..." * 100,  # > 500 字
        title="测试",
    )
    assert result["summary_text"] is not None
    assert len(result["summary_text"]) > 0
```

### 7.2 集成测试

使用真实的 BV1JkzaBpETo 视频测试：
1. 校对文本正确
2. 总结文本正确
3. 返回格式符合预期
4. transcription.py 集成正常

## 八、实施步骤

1. ✅ **设计阶段**（当前）
   - 设计 SummaryProcessor
   - 设计 Coordinator 改造方案

2. **实现阶段**（预计 3-4 小时）
   - [ ] 实现 SummaryProcessor（1-2h）
   - [ ] 改造 Coordinator（1h）
   - [ ] 修改 transcription.py 适配（30min）
   - [ ] 编写单元测试（30min）

3. **测试阶段**（预计 1-2 小时）
   - [ ] 运行单元测试
   - [ ] 运行集成测试（BV1JkzaBpETo）
   - [ ] 验证完整流程

4. **优化阶段**（可选）
   - [ ] 性能优化（并行策略）
   - [ ] 缓存优化
   - [ ] 监控和日志

## 九、风险和缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 总结生成失败 | 用户看不到总结 | 返回 None，graceful degradation |
| 性能下降 | 用户等待时间变长 | 实现并行策略（可选） |
| 兼容性问题 | 旧代码无法使用 | 保持返回格式兼容 |
| LLM API 超时 | 流程卡住 | 使用 LLMClient 的重试和超时机制 |

## 十、总结

这个改造方案：
- ✅ 完全恢复旧架构的总结功能
- ✅ 保持新架构的模块化设计
- ✅ 提供更好的质量（基于校对文本总结）
- ✅ 易于测试和维护
- ✅ 为未来扩展留有空间

**唯一的代价**：时间增加约 15 秒（可通过并行策略优化）
