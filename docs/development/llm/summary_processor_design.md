# SummaryProcessor 设计文档

## 一、职责

生成视频文本的内容总结

## 二、接口设计

### 2.1 初始化

```python
class SummaryProcessor:
    def __init__(
        self,
        llm_client: LLMClient,
        config: LLMConfig,
    ):
        """
        初始化总结处理器

        Args:
            llm_client: LLM 客户端
            config: LLM 配置
        """
```

### 2.2 核心方法

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
    """
    生成文本总结

    Args:
        text: 待总结的文本（通常是校对后的文本）
        title: 视频标题
        author: 作者/频道
        description: 视频描述
        speaker_count: 说话人数量（用于选择 prompt）
        transcription_data: 原始转录数据（可选，用于辅助分析）
        selected_models: 选定的模型（可选）

    Returns:
        总结文本，如果文本过短则返回 None
    """
```

## 三、实现逻辑

### 3.1 流程

```
┌──────────────────────────────────┐
│  SummaryProcessor.process()     │
├──────────────────────────────────┤
│                                  │
│  1. 长度检查                     │
│     if len(text) < min_threshold:│
│         return None              │
│                                  │
│  2. 选择 System Prompt          │
│     if speaker_count > 1:        │
│         → 多说话人 prompt        │
│     else:                        │
│         → 单说话人 prompt        │
│                                  │
│  3. 构建 User Prompt            │
│     - 视频元数据（标题、作者等） │
│     - 待总结文本                 │
│     - 转录引擎信息（可选）       │
│                                  │
│  4. 调用 LLM                    │
│     - 使用 summary_model        │
│     - 应用 reasoning_effort     │
│                                  │
│  5. 验证结果                    │
│     - 检查是否为空               │
│     - 检查长度是否合理           │
│                                  │
│  6. 返回总结                    │
│                                  │
└──────────────────────────────────┘
```

### 3.2 Prompt 选择策略

**单说话人 Prompt**（speaker_count <= 1）:
- 强调论点提取
- 关注逻辑结构
- 总结主要观点

**多说话人 Prompt**（speaker_count > 1）:
- 强调对话动态
- 关注观点碰撞
- 区分不同说话人立场

### 3.3 错误处理

```python
try:
    summary = llm_client.call(...)

    # 验证
    if not summary or len(summary) < 50:
        logger.warning("Summary too short or empty")
        return None

    return summary

except FatalError as e:
    logger.error(f"Fatal error in summary: {e}")
    return None

except RetryableError as e:
    logger.error(f"Summary failed after retries: {e}")
    return None
```

## 四、配置项

从 `LLMConfig` 读取：

```python
# 模型配置
summary_model: str                          # 总结模型
summary_reasoning_effort: Optional[str]     # reasoning effort

# 风险模型配置
risk_summary_model: Optional[str]           # 风险总结模型
risk_summary_reasoning_effort: Optional[str]

# 长度阈值
min_summary_threshold: int = 500            # 最小总结阈值（字符数）
```

## 五、与旧架构对比

| 维度 | 旧架构 | 新架构 |
|------|--------|--------|
| **模块位置** | EnhancedLLMProcessor 内部方法 | 独立的 SummaryProcessor |
| **输入来源** | 原始文本（与校对并行） | 校对后文本（串行） |
| **职责** | 混合（校对+总结在一个类） | 单一（只负责总结） |
| **可复用性** | 低（耦合在大类中） | 高（独立模块） |
| **测试性** | 难（需要完整环境） | 易（可单独测试） |

## 六、使用示例

```python
# 初始化
summary_processor = SummaryProcessor(
    llm_client=llm_client,
    config=config,
)

# 生成总结
summary = summary_processor.process(
    text=calibrated_text,
    title="特朗普盯上委内瑞拉石油？",
    author="差评",
    description="",
    speaker_count=0,  # 单说话人
    selected_models=selected_models,
)

if summary:
    print(f"总结生成成功，长度: {len(summary)}")
else:
    print("文本过短，跳过总结")
```

## 七、测试要点

1. **长度阈值测试**
   - 输入 400 字文本 → 应返回 None
   - 输入 600 字文本 → 应返回总结

2. **Prompt 选择测试**
   - speaker_count=0 → 使用单说话人 prompt
   - speaker_count=2 → 使用多说话人 prompt

3. **模型选择测试**
   - 无风险 → 使用 summary_model
   - 有风险 → 使用 risk_summary_model

4. **错误处理测试**
   - LLM API 失败 → 返回 None
   - 返回空内容 → 返回 None
   - 返回过短内容 → 返回 None

## 八、性能考虑

1. **无缓存**：总结每次都基于最新的校对文本生成，不缓存
2. **并发控制**：总结和校对串行执行，避免同时占用过多资源
3. **超时设置**：继承 LLMClient 的超时和重试机制

## 九、未来优化方向

1. **增量总结**：超长文本分段总结后合并
2. **多级总结**：先生成详细总结，再生成精简版
3. **个性化**：根据用户偏好调整总结风格
4. **缓存策略**：缓存基于特定文本的总结结果
