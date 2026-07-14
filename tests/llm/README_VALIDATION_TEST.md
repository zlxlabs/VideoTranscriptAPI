# 质量验证打分测试

## 概述

此测试用于评估分段校对后的质量打分功能，帮助确定合理的阈值设置和优化 Prompt。

## 测试文件

- `test_quality_validation_scoring.py` - 主测试脚本
- `../manual/run_validation_scoring_test.py` - 便捷运行脚本（位于 `tests/manual/`，依赖真实 LLM API，需显式手动运行）
- `validation_scoring_results.json` - 测试结果输出文件

## 运行测试

### 基础运行

```bash
# 在项目根目录执行
python tests/llm/test_quality_validation_scoring.py
```

### 使用便捷脚本

```bash
# 默认测试（30个片段）
python tests/manual/run_validation_scoring_test.py

# 指定片段数量
python tests/manual/run_validation_scoring_test.py --segments 50

# 使用串行执行（默认）
python tests/manual/run_validation_scoring_test.py --serial
```

## 测试配置

测试会自动从 `config/config.jsonc` 读取配置，并进行以下调整：

1. **强制启用质量验证**：`structured_calibration.quality_validation.enabled = True`
2. **串行执行**：`calibration_concurrent_limit = 1`（确保打分结果顺序正确）
3. **使用配置中的验证模型**：默认为 `deepseek-v4-flash`

## 输出内容

### 1. 个别 Chunk 打分详情

```
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
    - Minor punctuation improvements needed
  Recommendation: Quality is good, minor improvements suggested
```

### 2. 统计摘要

- **总体统计**：总chunk数、通过数、失败数、通过率
- **Overall Score 统计**：平均值、中位数、最小值、最大值、标准差
- **各维度分数统计**：每个维度的统计数据

### 3. 阈值分析

模拟不同阈值下的通过率，帮助确定合理的边界值：

```
Pass rate with different overall_score thresholds:
  6.0: 10/10 (100.0%)
  7.0: 9/10 (90.0%)
  7.5: 8/10 (80.0%)
  8.0: 6/10 (60.0%) (current)
  8.5: 4/10 (40.0%)
  9.0: 2/10 (20.0%)
```

## 结果文件

测试结果会保存到 `tests/llm/validation_scoring_results.json`，包含：

```json
{
  "test_metadata": {
    "num_segments": 30,
    "platform": "xiaoyuzhou",
    "media_id": "69788224cbeabe94f34495af",
    "validator_model": "deepseek-v4-flash",
    "overall_score_threshold": 8.0,
    "minimum_single_score": 7.0
  },
  "scores": [
    {
      "chunk_index": 0,
      "overall_score": 8.5,
      "passed": true,
      "scores": {
        "format_correctness": 9.0,
        "content_fidelity": 8.5,
        "text_quality": 8.0,
        "speaker_consistency": 9.0,
        "time_consistency": 9.0
      },
      "issues": ["..."],
      "recommendation": "..."
    }
  ],
  "statistics": {
    "total_chunks": 10,
    "passed_count": 8,
    "failed_count": 2,
    "pass_rate": 0.8,
    "overall_score_stats": {...},
    "dimension_stats": {...}
  }
}
```

## 评估要点

### 1. 分数分布分析

查看 overall_score 的分布情况：
- **均值**：代表平均质量水平
- **标准差**：反映分数稳定性
- **最小值/最大值**：了解分数范围

### 2. 各维度表现

重点关注哪些维度得分较低：
- `format_correctness`（格式正确性）：通常应该接近满分
- `content_fidelity`（内容保真度）：最重要的维度
- `text_quality`（文本质量）：反映校对效果
- `speaker_consistency`（说话人一致性）：不应该变化
- `time_consistency`（时间一致性）：不应该变化

### 3. 边界值设定建议

根据测试结果：

- **过于严格**（通过率 < 50%）：阈值太高，大量有效校对被拒绝
- **合理范围**（通过率 60%-80%）：能够过滤低质量结果，同时保留大部分有效校对
- **过于宽松**（通过率 > 90%）：阈值太低，无法有效识别质量问题

### 4. Prompt 优化方向

查看 `issues` 和 `recommendation` 字段：
- 如果经常出现某类问题，说明校对 Prompt 需要加强该方面的指令
- 如果验证器给出不合理的判断，说明验证 Prompt 需要调整

## 常见问题

### Q: 为什么要串行执行？

A: 串行执行确保打分结果的收集顺序与 chunk 索引一致，便于分析。虽然速度较慢，但测试数据量不大（默认30个片段），影响不大。

### Q: 如何测试不同的验证模型？

A: 修改 `config/config.jsonc` 中的 `llm.structured_calibration.validator_model` 配置。

### Q: 可以测试更多数据吗？

A: 可以，通过 `--segments` 参数指定更多片段数量，但注意会增加 API 调用成本和时间。

## 后续优化建议

基于测试结果，可以考虑：

1. **调整阈值**：修改 `config.jsonc` 中的 `quality_threshold.overall_score` 和 `minimum_single_score`
2. **优化校对 Prompt**：根据低分维度调整 `STRUCTURED_CALIBRATE_SYSTEM_PROMPT`
3. **优化验证 Prompt**：根据不合理判断调整 `VALIDATION_SYSTEM_PROMPT`
4. **测试不同模型**：对比不同模型的打分特征
