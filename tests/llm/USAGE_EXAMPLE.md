# 质量验证打分测试 - 使用示例

## 快速开始

### 1. 运行测试

在项目根目录执行：

```bash
# Windows
python tests\llm\test_quality_validation_scoring.py

# Linux/Mac
python tests/llm/test_quality_validation_scoring.py
```

### 2. 查看输出

测试会输出三部分内容：

#### 第一部分：处理进度

```
Starting quality validation scoring test...

[1/5] Loading config...
[2/5] Creating LLM config...
  Validation enabled: True
  Validator model: deepseek-chat
  Overall score threshold: 8.0
  Minimum single score: 7.0

[3/5] Initializing components...
[4/5] Loading test data...
  Loaded test data: 30 dialogs from 30 segments

[5/5] Processing with quality validation...
  (开始处理，会调用 LLM 进行校对和打分)

Processing completed:
  Original length: 1234
  Calibrated length: 1198
  Dialog count: 25
  Chunk count: 3
```

#### 第二部分：详细打分结果

```
================================================================================
Quality Validation Scoring Report
================================================================================

--- Individual Chunk Scores ---

Chunk 0:
  Overall Score: 8.50
  Passed: True
  Dimension Scores:
    - format_correctness: 9.00
    - content_fidelity: 8.50
    - text_quality: 8.00
    - speaker_consistency: 9.00
    - time_consistency: 9.00
  Issues:
    - 个别标点符号可以优化
    - 有一处口语化表达可以更流畅
  Recommendation: 整体质量良好，建议采纳校对结果

Chunk 1:
  Overall Score: 7.20
  Passed: False
  Dimension Scores:
    - format_correctness: 9.00
    - content_fidelity: 6.50
    - text_quality: 7.50
    - speaker_consistency: 9.00
    - time_consistency: 9.00
  Issues:
    - 内容保真度不足，部分句子被过度简化
    - 删除了一些口语化表达，改变了说话风格
  Recommendation: 建议回退到原文，重新校对

Chunk 2:
  Overall Score: 8.80
  Passed: True
  Dimension Scores:
    - format_correctness: 9.00
    - content_fidelity: 9.00
    - text_quality: 8.50
    - speaker_consistency: 9.00
    - time_consistency: 9.00
  Issues: []
  Recommendation: 校对质量优秀
```

#### 第三部分：统计分析

```
--------------------------------------------------------------------------------
--- Statistical Summary ---

Total Chunks: 3
Passed: 2 (66.7%)
Failed: 1 (33.3%)

Overall Score Statistics:
  Mean: 8.17
  Median: 8.50
  Min: 7.20
  Max: 8.80
  Stdev: 0.82

Dimension Score Statistics:

  format_correctness:
    Mean: 9.00
    Median: 9.00
    Min: 9.00
    Max: 9.00
    Stdev: 0.00

  content_fidelity:
    Mean: 8.00
    Median: 8.50
    Min: 6.50
    Max: 9.00
    Stdev: 1.29

  text_quality:
    Mean: 8.00
    Median: 8.00
    Min: 7.50
    Max: 8.50
    Stdev: 0.50

  speaker_consistency:
    Mean: 9.00
    Median: 9.00
    Min: 9.00
    Max: 9.00
    Stdev: 0.00

  time_consistency:
    Mean: 9.00
    Median: 9.00
    Min: 9.00
    Max: 9.00
    Stdev: 0.00

--------------------------------------------------------------------------------
--- Threshold Analysis ---

Current thresholds:
  overall_score_threshold: 8.0
  minimum_single_score: 7.0

Pass rate with different overall_score thresholds:
  6.0: 3/3 (100.0%)
  7.0: 3/3 (100.0%)
  7.5: 2/3 (66.7%)
  8.0: 2/3 (66.7%) (current)
  8.5: 1/3 (33.3%)
  9.0: 0/3 (0.0%)

Pass rate with different minimum_single_score thresholds:
  5.0: 3/3 (100.0%)
  6.0: 3/3 (100.0%)
  6.5: 2/3 (66.7%)
  7.0: 2/3 (66.7%) (current)
  7.5: 2/3 (66.7%)
  8.0: 2/3 (66.7%)

================================================================================
```

### 3. 查看结果文件

测试完成后，结果会保存到：

```
tests/llm/validation_scoring_results.json
```

可以使用任何 JSON 查看器查看详细数据。

## 如何解读结果

### 1. 判断当前阈值是否合理

查看 "Threshold Analysis" 部分：

- **通过率太低**（< 50%）：说明阈值设置过严，建议降低
- **通过率太高**（> 90%）：说明阈值设置过松，无法有效过滤低质量结果
- **理想范围**：60%-80% 的通过率，既能过滤低质量结果，又不会过度拒绝

### 2. 识别问题维度

查看 "Dimension Score Statistics"：

- **format_correctness 低**：说明 LLM 没有正确输出 JSON 格式
- **content_fidelity 低**：说明校对改变了原文意思，需要优化校对 Prompt
- **text_quality 低**：说明校对没有有效改善文本质量
- **speaker_consistency 低**：说明校对错误地修改了说话人
- **time_consistency 低**：说明校对错误地修改了时间戳

### 3. 优化方向

根据测试结果：

#### 如果 content_fidelity 普遍较低
说明校对 Prompt 需要加强"不改变原意"的约束：
- 检查 `STRUCTURED_CALIBRATE_SYSTEM_PROMPT`
- 增加示例说明什么是"改变原意"
- 强调"最小改动原则"

#### 如果 text_quality 普遍较低
说明校对效果不佳：
- 增加具体的校对规则
- 提供更多错误类型示例
- 考虑使用更强的模型

#### 如果验证器给出不合理判断
查看 "Individual Chunk Scores" 中的 Issues 和 Recommendation：
- 如果问题不成立，说明验证 Prompt 需要优化
- 检查 `VALIDATION_SYSTEM_PROMPT`
- 明确各维度的评分标准

## 高级用法

### 测试不同数据量

修改 `main()` 函数中的参数：

```python
dialogs, metadata = load_test_data(num_segments=50)  # 测试50个片段
```

### 测试不同模型

修改 `config/config.jsonc`：

```jsonc
"structured_calibration": {
  "validator_model": "gpt-4.1-mini",  // 改用其他模型
  // ...
}
```

### 测试不同阈值

修改 `main()` 函数中的配置：

```python
llm_config.overall_score_threshold = 7.5
llm_config.minimum_single_score = 6.5
```

## 注意事项

1. **API 成本**：每个 chunk 会调用一次验证 API，成本取决于模型和数据量
2. **运行时间**：串行执行模式下，30个片段约需 5-10 分钟
3. **中文日志乱码**：Windows 控制台可能显示乱码，不影响功能
4. **结果可重现性**：相同配置下结果应该基本一致（LLM 输出有随机性）

## 疑难解答

### 导入错误

```
ModuleNotFoundError: No module named 'src.video_transcript_api'
```

**解决**：确保在项目根目录运行脚本，或检查 Python 路径。

### API 调用失败

```
LLM API call failed: Connection timeout
```

**解决**：
1. 检查 `config/config.jsonc` 中的 API 配置
2. 确认网络连接正常
3. 检查 API Key 是否有效

### 打分数据为空

```
No scores collected
```

**解决**：
1. 确认 `structured_calibration.quality_validation.enabled = True`
2. 检查是否有报错信息
3. 查看日志文件 `data/logs/app.log`
